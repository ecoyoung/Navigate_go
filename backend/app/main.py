from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import (
    COOKIE_NAME,
    SESSION_TTL,
    create_session,
    create_user,
    current_user,
    hash_password,
    normalize_email,
    session_from_token,
    validate_password,
    verify_password,
)
from .channel_adapters import (
    ChannelConfigurationError,
    canonicalize_parser_config,
    crawl_source,
    validate_channel_config,
)
from .config import settings
from .content_processing import PROCESSOR_NAME, PROCESSOR_VERSION
from .content_quality import is_reader_eligible, quality_tier
from .contracts import count_words
from .crawl_scheduler import SourceHealth, create_due_runs, due_sources, source_health_map
from .daily_report import (
    available_report_dates,
    available_topic_report_dates,
    collect_daily_report,
    collect_topic_daily_report,
    render_daily_report,
)
from .database import SessionLocal, database_ready, get_db
from .entity_reviews import decide_entity_candidate
from .firecrawl import (
    SCRAPE_BATCH_MAX,
    FirecrawlClient,
    FirecrawlError,
    cached_search,
    search_results,
)
from .llm_editorial import DeepSeekClient
from .models import (
    AuthSession,
    ContentDomainAssignment,
    ContentItem,
    ContentProcessingResult,
    ContentValueScore,
    ContentValueScoreRun,
    CrawlRun,
    Domain,
    Entity,
    EntityAlias,
    EntityCandidateReview,
    EntityMention,
    EntityProcessingResult,
    Event,
    EventMember,
    InterestTopic,
    LLMProcessingResult,
    PageSnapshot,
    RawItem,
    Source,
    TopicMatch,
    TopicRun,
    TopicSourceCandidate,
    User,
    UserSubscription,
)
from .normalization import normalize_url
from .run_coverage import resolve_run_coverage
from .schemas import (
    AdminUserCreate,
    AdminUserUpdate,
    AuthResponse,
    ChangePasswordRequest,
    ContentItemRead,
    ContentValueScoreRead,
    CrawlAccepted,
    CrawlRunRead,
    DailyReportHistoryItem,
    DomainRead,
    EntityAliasRead,
    EntityCandidateDecision,
    EntityCandidateReviewRead,
    EntityDetailRead,
    EntityMentionRead,
    EntityRead,
    EventDetailRead,
    EventMemberRead,
    EventRead,
    LoginRequest,
    PageSnapshotRead,
    RawItemRead,
    RegisterRequest,
    SourceCreate,
    SourceRead,
    SourceUpdate,
    SubscriptionRead,
    SubscriptionUpdate,
    TopicCreate,
    TopicDiscoverRequest,
    TopicDiscoverResponse,
    TopicFeedItem,
    TopicPreview,
    TopicRead,
    TopicSourceCandidateRead,
    TopicUpdate,
    UserRead,
)
from .secrets import MissingSecretError, require_secret
from .topic_discovery import (
    attach_discovered_match,
    content_needs_discovery_enrichment,
    enrich_discovered_content_from_web,
    enrichment_retry_due,
    existing_content_for_url,
    ingest_discovered_metadata,
    ingest_discovered_page,
)
from .topic_intelligence import process_topic_contents
from .topic_matching import (
    COMPILER_NAME,
    COMPILER_VERSION,
    compile_topic_intent,
    refresh_topic_matches,
    suggested_topic_name,
)
from .topic_search_plan import (
    build_firecrawl_search_options,
    compile_topic_search_plan,
)
from .web_ingestion import ActiveCrawlConflict, create_crawl_run


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield


app = FastAPI(title="Navigate API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def error_response(request: Request, status_code: int, code: str, message: str, details=None):
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "details": details,
            "request_id": request.headers.get("x-request-id", str(uuid4())),
        },
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return error_response(request, exc.status_code, f"http_{exc.status_code}", str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = []
    for item in exc.errors():
        error = dict(item)
        if error.get("ctx"):
            error["ctx"] = {key: str(value) for key, value in error["ctx"].items()}
        details.append(error)
    return error_response(request, 422, "validation_error", "请求参数无效", details)


def serialize_sources(db: Session, sources: list[Source]) -> list[SourceRead]:
    health_by_id = source_health_map(db, sources)
    return [_source_read(source, health_by_id.get(source.id, SourceHealth())) for source in sources]


def _source_read(source: Source, health: SourceHealth) -> SourceRead:
    return SourceRead.model_validate(source).model_copy(
        update={
            "last_run_status": health.last_run_status,
            "last_error_code": health.last_error_code,
            "consecutive_failures": health.consecutive_failures,
            "circuit_open": health.circuit_open,
            "last_finished_at": health.last_finished_at,
        }
    )


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(db: Session = Depends(get_db)):
    database_ready(db)
    return {"status": "ready"}


def _topic_read(db: Session, topic: InterestTopic) -> TopicRead:
    match_count = db.scalar(
        select(func.count())
        .select_from(TopicMatch)
        .where(TopicMatch.topic_id == topic.id, TopicMatch.decision == "include")
    )
    candidate_count = db.scalar(
        select(func.count())
        .select_from(TopicSourceCandidate)
        .where(TopicSourceCandidate.topic_id == topic.id)
    )
    return TopicRead(
        id=topic.id,
        name=topic.name,
        intent_text=topic.intent_text,
        compiled_intent=topic.compiled_intent,
        cadence=topic.cadence,
        status=topic.status,
        daily_credit_limit=topic.daily_credit_limit,
        match_count=int(match_count or 0),
        candidate_source_count=int(candidate_count or 0),
        created_at=topic.created_at,
        updated_at=topic.updated_at,
    )


def _topic_for_user(db: Session, topic_id: int, user: User) -> InterestTopic:
    topic = db.scalar(
        select(InterestTopic).where(InterestTopic.id == topic_id, InterestTopic.user_id == user.id)
    )
    if topic is None:
        raise HTTPException(404, "主题不存在")
    return topic


def _editorial_map(db: Session, content_ids: set[int]) -> dict[int, dict]:
    if not content_ids:
        return {}
    rows = db.scalars(
        select(LLMProcessingResult)
        .where(
            LLMProcessingResult.subject_type == "content_item",
            LLMProcessingResult.task_name == "content_editorial_zh",
            LLMProcessingResult.status == "succeeded",
            LLMProcessingResult.subject_key.in_([f"content:{item}" for item in content_ids]),
        )
        .order_by(LLMProcessingResult.id)
    )
    result: dict[int, dict] = {}
    for row in rows:
        try:
            content_id = int(row.subject_key.split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        result[content_id] = row.output or {}
    return result


def _topic_editorial_map(
    db: Session, topic_content_pairs: set[tuple[int, int]]
) -> dict[tuple[int, int], dict]:
    if not topic_content_pairs:
        return {}
    subject_keys = [
        f"topic:{topic_id}:content:{content_id}" for topic_id, content_id in topic_content_pairs
    ]
    rows = db.scalars(
        select(LLMProcessingResult)
        .where(
            LLMProcessingResult.subject_type == "topic_content",
            LLMProcessingResult.task_name == "topic_content_editorial",
            LLMProcessingResult.status == "succeeded",
            LLMProcessingResult.subject_key.in_(subject_keys),
        )
        .order_by(LLMProcessingResult.id)
    )
    result: dict[tuple[int, int], dict] = {}
    for row in rows:
        parts = row.subject_key.split(":")
        if len(parts) != 4:
            continue
        try:
            result[(int(parts[1]), int(parts[3]))] = row.output or {}
        except ValueError:
            continue
    return result


def _topic_daily_editorial(db: Session, topic: InterestTopic, content_ids: set[int]) -> dict:
    """Return Chinese topic-specific editorial fields, creating only missing cached items."""
    rows = list(
        db.execute(
            select(ContentItem, Source)
            .join(Source, Source.id == ContentItem.source_id)
            .where(ContentItem.id.in_(content_ids))
        )
    )
    artifacts = _topic_editorial_map(db, {(topic.id, content_id) for content_id in content_ids})
    missing = [pair for pair in rows if (topic.id, pair[0].id) not in artifacts]
    if missing:
        try:
            client = DeepSeekClient(api_key=require_secret("DEEPSEEK_API_KEY"))
        except MissingSecretError as exc:
            raise HTTPException(503, "中文编辑服务未配置") from exc
        try:
            for start in range(0, len(missing), 12):
                process_topic_contents(db, topic, missing[start : start + 12], client)
        except (RuntimeError, ValueError, ValidationError):
            db.rollback()
            return {
                "stories": [
                    {
                        "story_key": f"content:{content.id}",
                        "chinese_title": content.title,
                        "chinese_summary": content.excerpt
                        or " ".join((content.body or "").split())[:300],
                        "tags": [],
                    }
                    for content, _source in rows
                ]
            }
        artifacts = _topic_editorial_map(db, {(topic.id, content_id) for content_id in content_ids})
    stories = []
    for content_id in sorted(content_ids):
        artifact = artifacts.get((topic.id, content_id))
        if artifact is None:
            raise HTTPException(502, "主题中文编辑结果未生成")
        stories.append(
            {
                "story_key": f"content:{content_id}",
                "chinese_title": artifact.get("chinese_title"),
                "chinese_summary": artifact.get("chinese_summary"),
                "tags": [
                    {"label_zh": tag} for tag in artifact.get("tags_zh", []) if isinstance(tag, str)
                ],
            }
        )
    return {"stories": stories}


def _feed_items(
    db: Session,
    user: User,
    topic_id: int | None,
    limit: int,
    *,
    include_enrichment: bool = False,
) -> list[TopicFeedItem]:
    statement = (
        select(TopicMatch, InterestTopic, ContentItem, Source)
        .join(InterestTopic, InterestTopic.id == TopicMatch.topic_id)
        .join(ContentItem, ContentItem.id == TopicMatch.content_item_id)
        .join(Source, Source.id == ContentItem.source_id)
        .where(
            InterestTopic.user_id == user.id,
            InterestTopic.status == "active",
            TopicMatch.decision == "include",
            TopicMatch.input_content_hash == ContentItem.content_hash,
        )
    )
    if topic_id is not None:
        statement = statement.where(InterestTopic.id == topic_id)
    if topic_id is not None:
        statement = statement.order_by(
            TopicMatch.score.desc(),
            func.coalesce(ContentItem.published_at, ContentItem.discovered_at).desc(),
        )
    else:
        statement = statement.order_by(
            func.coalesce(ContentItem.published_at, ContentItem.discovered_at).desc(),
            TopicMatch.score.desc(),
        )
    rows = db.execute(statement.limit(limit * (10 if include_enrichment else 4))).all()
    grouped: dict[int, dict] = {}
    for match, topic, content, source in rows:
        if not include_enrichment and not is_reader_eligible(content):
            continue
        item = grouped.setdefault(
            content.id,
            {
                "match": match,
                "editorial_topic_id": topic.id,
                "content": content,
                "source": source,
                "topics": [],
            },
        )
        item["topics"].append(topic)
        if match.score > item["match"].score:
            item["match"] = match
            item["editorial_topic_id"] = topic.id
    editorial = _editorial_map(db, set(grouped))
    topic_editorial = _topic_editorial_map(
        db,
        {(item["editorial_topic_id"], item["content"].id) for item in grouped.values()},
    )
    result: list[TopicFeedItem] = []
    ordered_items = list(grouped.values())
    if include_enrichment:
        ordered_items.sort(
            key=lambda item: (
                not is_reader_eligible(item["content"]),
                -item["match"].score,
                item["content"].published_at or item["content"].discovered_at,
            )
        )
    for item in ordered_items[:limit]:
        content = item["content"]
        artifact = topic_editorial.get((item["editorial_topic_id"], content.id)) or editorial.get(
            content.id, {}
        )
        result.append(
            TopicFeedItem(
                content_id=content.id,
                title=artifact.get("chinese_title") or content.title,
                excerpt=artifact.get("chinese_summary") or content.excerpt,
                source_name=item["source"].name,
                url=content.canonical_url or content.original_url,
                published_at=content.published_at,
                discovered_at=content.discovered_at,
                language=content.language,
                topic_ids=[topic.id for topic in item["topics"]],
                topic_names=[topic.name for topic in item["topics"]],
                tags=artifact.get("tags_zh", []),
                match_score=item["match"].score,
                quality_tier=quality_tier(content),
                reader_eligible=is_reader_eligible(content),
            )
        )
    return result


def _forwarded_proto(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() or request.url.scheme


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_forwarded_proto(request) == "https",
        path="/",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_forwarded_proto(request) == "https",
    )


def _require_admin(user: User) -> User:
    if user.role != "admin":
        raise HTTPException(403, "仅管理员可以管理账号")
    return user


@app.post("/api/v1/auth/register", response_model=AuthResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    admin_exists = db.scalar(select(User.id).where(User.role == "admin").limit(1))
    try:
        user = create_user(
            db,
            email=payload.email,
            display_name=payload.display_name or payload.email,
            password=payload.password,
            role="admin" if admin_exists is None else "member",
            must_change_password=False,
        )
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(409 if "已注册" in message else 422, message) from exc
    _set_session_cookie(response, create_session(db, user), request)
    return AuthResponse(user=UserRead.model_validate(user))


@app.post("/api/v1/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    try:
        email = normalize_email(payload.email)
    except ValueError as exc:
        raise HTTPException(401, "账户名或密码错误") from exc
    user = db.scalar(select(User).where(User.email == email))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(401, "账户名或密码错误")
    _set_session_cookie(response, create_session(db, user), request)
    return AuthResponse(user=UserRead.model_validate(user))


@app.post("/api/v1/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    auth_session = session_from_token(db, request.cookies.get(COOKIE_NAME))
    if auth_session:
        auth_session.revoked_at = datetime.now(UTC)
        db.commit()
    _clear_session_cookie(response, request)


@app.get("/api/v1/auth/me", response_model=UserRead)
def me(user: User = Depends(current_user)):
    return user


@app.get("/api/v1/admin/users", response_model=list[UserRead])
def admin_list_users(
    user: User = Depends(current_user), db: Session = Depends(get_db)
):
    _require_admin(user)
    return list(db.scalars(select(User).order_by(User.created_at.desc(), User.id.desc())))


@app.post("/api/v1/admin/users", response_model=UserRead, status_code=201)
def admin_create_user(
    payload: AdminUserCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    try:
        return create_user(
            db,
            email=payload.email,
            display_name=payload.display_name or payload.email,
            password=payload.temporary_password,
            role=payload.role,
            must_change_password=True,
        )
    except ValueError as exc:
        raise HTTPException(409 if "已注册" in str(exc) else 422, str(exc)) from exc


@app.patch("/api/v1/admin/users/{user_id}", response_model=UserRead)
def admin_update_user(
    user_id: int,
    payload: AdminUserUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "账号不存在")
    if target.id == user.id and payload.is_active is False:
        raise HTTPException(422, "不能停用当前登录的管理员账号")
    if payload.is_active is not None:
        target.is_active = payload.is_active
    if payload.temporary_password:
        validate_password(payload.temporary_password)
        target.password_hash = hash_password(payload.temporary_password)
        target.must_change_password = True
        for auth_session in db.scalars(
            select(AuthSession).where(
                AuthSession.user_id == target.id, AuthSession.revoked_at.is_(None)
            )
        ):
            auth_session.revoked_at = datetime.now(UTC)
    db.commit()
    db.refresh(target)
    return target


@app.post("/api/v1/auth/change-password", status_code=204)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(401, "当前密码错误")
    try:
        validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(422, "新密码不能与当前密码相同")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    for auth_session in db.scalars(
        select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
    ):
        auth_session.revoked_at = datetime.now(UTC)
    db.commit()
    _clear_session_cookie(response, request)


@app.get("/api/v1/subscriptions", response_model=list[SubscriptionRead])
def list_subscriptions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(UserSubscription, Domain)
        .join(Domain, Domain.id == UserSubscription.domain_id)
        .where(UserSubscription.user_id == user.id)
        .order_by(Domain.name, UserSubscription.delivery_type)
    ).all()
    return [
        SubscriptionRead(
            id=subscription.id,
            domain_key=domain.key,
            domain_name=domain.name,
            delivery_type=subscription.delivery_type,
            status=subscription.status,
            created_at=subscription.created_at,
            updated_at=subscription.updated_at,
        )
        for subscription, domain in rows
    ]


@app.put("/api/v1/subscriptions/{domain_key}", response_model=SubscriptionRead)
def update_subscription(
    domain_key: str,
    payload: SubscriptionUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    domain = db.scalar(select(Domain).where(Domain.key == domain_key, Domain.is_enabled.is_(True)))
    if domain is None:
        raise HTTPException(404, "领域不存在或尚未开放")
    subscription = db.scalar(
        select(UserSubscription).where(
            UserSubscription.user_id == user.id,
            UserSubscription.domain_id == domain.id,
            UserSubscription.delivery_type == payload.delivery_type,
        )
    )
    if subscription is None:
        subscription = UserSubscription(
            user_id=user.id,
            domain_id=domain.id,
            delivery_type=payload.delivery_type,
            status=payload.status,
        )
        db.add(subscription)
    else:
        subscription.status = payload.status
        subscription.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(subscription)
    return SubscriptionRead(
        id=subscription.id,
        domain_key=domain.key,
        domain_name=domain.name,
        delivery_type=subscription.delivery_type,
        status=subscription.status,
        created_at=subscription.created_at,
        updated_at=subscription.updated_at,
    )


@app.post("/api/v1/topics", response_model=TopicPreview, status_code=201)
def create_topic(
    payload: TopicCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        compiled, intent_hash = compile_topic_intent(
            payload.intent_text,
            keywords=payload.keywords,
            excluded_keywords=payload.excluded_keywords,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    topic = InterestTopic(
        user_id=user.id,
        name=(payload.name or suggested_topic_name(payload.intent_text, compiled)).strip(),
        intent_text=payload.intent_text.strip(),
        compiled_intent=compiled,
        compiler_name=COMPILER_NAME,
        compiler_version=COMPILER_VERSION,
        intent_hash=intent_hash,
        cadence=payload.cadence,
        status="active",
        daily_credit_limit=payload.daily_credit_limit,
    )
    db.add(topic)
    db.flush()
    pool_window_end = datetime.now(UTC)
    refresh_topic_matches(
        db,
        topic,
        new_item_window_start=pool_window_end - timedelta(days=7),
        new_item_window_end=pool_window_end,
    )
    db.commit()
    db.refresh(topic)
    return TopicPreview(
        topic=_topic_read(db, topic),
        items=_feed_items(db, user, topic.id, 20, include_enrichment=True),
    )


@app.get("/api/v1/topics", response_model=list[TopicRead])
def list_topics(user: User = Depends(current_user), db: Session = Depends(get_db)):
    topics = db.scalars(
        select(InterestTopic)
        .where(InterestTopic.user_id == user.id)
        .order_by(InterestTopic.updated_at.desc(), InterestTopic.id.desc())
    )
    return [_topic_read(db, topic) for topic in topics]


@app.patch("/api/v1/topics/{topic_id}", response_model=TopicPreview)
def update_topic(
    topic_id: int,
    payload: TopicUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    topic = _topic_for_user(db, topic_id, user)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        topic.name = str(changes["name"]).strip()
    for field in ("cadence", "status", "daily_credit_limit"):
        if field in changes:
            setattr(topic, field, changes[field])
    intent_changed = any(
        field in changes for field in ("intent_text", "keywords", "excluded_keywords")
    )
    if intent_changed:
        intent_text = changes.get("intent_text", topic.intent_text)
        current = topic.compiled_intent or {}
        compiled, intent_hash = compile_topic_intent(
            intent_text,
            keywords=changes.get("keywords", current.get("positive_keywords", [])),
            excluded_keywords=changes.get(
                "excluded_keywords", current.get("excluded_keywords", [])
            ),
        )
        topic.intent_text = intent_text.strip()
        topic.compiled_intent = compiled
        topic.intent_hash = intent_hash
        topic.compiler_name = COMPILER_NAME
        topic.compiler_version = COMPILER_VERSION
    topic.updated_at = datetime.now(UTC)
    pool_window_end = datetime.now(UTC)
    refresh_topic_matches(
        db,
        topic,
        new_item_window_start=pool_window_end - timedelta(days=7),
        new_item_window_end=pool_window_end,
    )
    db.commit()
    db.refresh(topic)
    return TopicPreview(
        topic=_topic_read(db, topic),
        items=_feed_items(db, user, topic.id, 20, include_enrichment=True),
    )


@app.delete("/api/v1/topics/{topic_id}", status_code=204)
def delete_topic(
    topic_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    topic = _topic_for_user(db, topic_id, user)
    subject_prefix = f"topic:{topic.id}"
    db.execute(
        delete(LLMProcessingResult).where(
            or_(
                LLMProcessingResult.subject_key == subject_prefix,
                LLMProcessingResult.subject_key.like(f"{subject_prefix}:%"),
            )
        )
    )
    db.delete(topic)
    db.commit()
    return Response(status_code=204)


@app.get("/api/v1/topics/{topic_id}/preview", response_model=TopicPreview)
def preview_topic(
    topic_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    topic = _topic_for_user(db, topic_id, user)
    pool_window_end = datetime.now(UTC)
    refresh_topic_matches(
        db,
        topic,
        new_item_window_start=pool_window_end - timedelta(days=7),
        new_item_window_end=pool_window_end,
    )
    db.commit()
    return TopicPreview(
        topic=_topic_read(db, topic),
        items=_feed_items(db, user, topic.id, 20, include_enrichment=True),
    )


@app.get("/api/v1/topics/{topic_id}/feed", response_model=list[TopicFeedItem])
def topic_feed(
    topic_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _topic_for_user(db, topic_id, user)
    return _feed_items(db, user, topic_id, limit, include_enrichment=True)


@app.get(
    "/api/v1/topics/{topic_id}/daily-reports",
    response_model=list[DailyReportHistoryItem],
)
def topic_daily_report_history(
    topic_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    topic = _topic_for_user(db, topic_id, user)
    return [
        DailyReportHistoryItem(
            coverage_date=coverage_date,
            available_content_count=content_count,
        )
        for coverage_date, content_count in available_topic_report_dates(db, topic=topic)
    ]


@app.get(
    "/api/v1/topics/{topic_id}/daily-reports/{coverage_date}",
    response_class=HTMLResponse,
)
def topic_daily_report_document(
    topic_id: int,
    coverage_date: date,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    topic = _topic_for_user(db, topic_id, user)
    try:
        report = collect_topic_daily_report(db, topic=topic, coverage_date=coverage_date)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    editorial = _topic_daily_editorial(
        db, topic, {story.content_item_id for story in report.stories}
    )
    ordered_refs = [f"content:{story.content_item_id}" for story in report.stories]
    lead = editorial.get("daily_lead") or {}
    report = replace(
        report,
        editorial={
            **editorial,
            "daily_lead": {
                "deck": str(lead.get("deck") or topic.name).strip(),
                "text": str(lead.get("text") or "").strip(),
                "story_refs": list(lead.get("story_refs") or ordered_refs),
            },
            "sections": editorial.get("sections")
            or [
                {
                    "title": "本期资讯",
                    "intro": "",
                    "story_refs": ordered_refs,
                }
            ],
        },
    )
    return HTMLResponse(render_daily_report(report))


@app.get("/api/v1/feed/for-you", response_model=list[TopicFeedItem])
def for_you_feed(
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _feed_items(db, user, None, limit)


@app.get("/api/v1/daily-reports", response_model=list[DailyReportHistoryItem])
def daily_report_history(
    domain_key: str = Query(default="beauty", pattern=r"^[a-z0-9][a-z0-9_-]*$"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    del user
    try:
        return [
            DailyReportHistoryItem(
                coverage_date=coverage_date,
                available_content_count=content_count,
            )
            for coverage_date, content_count in available_report_dates(db, domain_key=domain_key)
        ]
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/v1/daily-reports/{coverage_date}", response_class=HTMLResponse)
def daily_report_document(
    coverage_date: date,
    domain_key: str = Query(default="beauty", pattern=r"^[a-z0-9][a-z0-9_-]*$"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    del user
    try:
        report = collect_daily_report(
            db,
            domain_key=domain_key,
            coverage_date=coverage_date,
            issue_date=coverage_date + timedelta(days=1),
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return HTMLResponse(render_daily_report(report))


@app.post("/api/v1/topics/{topic_id}/discover", response_model=TopicDiscoverResponse)
def discover_topic_sources(
    topic_id: int,
    payload: TopicDiscoverRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    topic = _topic_for_user(db, topic_id, user)
    shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    local_midnight = shanghai_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_midnight = local_midnight.astimezone(UTC)
    used_today = db.scalar(
        select(func.coalesce(func.sum(TopicRun.firecrawl_credits_used), 0)).where(
            TopicRun.topic_id == topic.id,
            TopicRun.started_at >= utc_midnight,
        )
    )
    if int(used_today or 0) + 2 > topic.daily_credit_limit:
        raise HTTPException(409, "该主题今日 Firecrawl 额度不足")
    remaining_before_run = max(0, topic.daily_credit_limit - int(used_today or 0))
    run = TopicRun(
        topic_id=topic.id,
        stage="firecrawl_discovery",
        status="running",
        firecrawl_credits_reserved=min(
            remaining_before_run,
            2 + min(payload.limit, SCRAPE_BATCH_MAX),
        ),
    )
    db.add(run)
    db.flush()
    try:
        client = FirecrawlClient.from_environment()
        llm_client = DeepSeekClient(api_key=require_secret("DEEPSEEK_API_KEY"))
        previous_discoveries = db.scalar(
            select(func.count())
            .select_from(TopicRun)
            .where(
                TopicRun.topic_id == topic.id,
                TopicRun.stage == "firecrawl_discovery",
                TopicRun.status.in_(("succeeded", "partial")),
                TopicRun.id != run.id,
            )
        )
        plan_result = compile_topic_search_plan(db, topic, llm_client)
        run.llm_tokens_used = plan_result.usage.total_tokens or 0
        is_initial_discovery = not bool(previous_discoveries)
        search_options = build_firecrawl_search_options(
            plan_result.plan, initial=is_initial_discovery
        )
        collection_window_end = datetime.now(UTC)
        collection_window_start = collection_window_end - timedelta(
            days=7 if is_initial_discovery else 1
        )
        response, cache_hit, search_credits = cached_search(
            db,
            client,
            query=plan_result.plan.query,
            limit=payload.limit,
            search_options=search_options,
        )
        results = search_results(response)
        candidates: list[TopicSourceCandidate] = []
        for item in results:
            url = normalize_url(str(item["url"]))
            candidate = db.scalar(
                select(TopicSourceCandidate).where(
                    TopicSourceCandidate.topic_id == topic.id,
                    TopicSourceCandidate.canonical_url == url,
                )
            )
            if candidate is None:
                candidate = TopicSourceCandidate(
                    topic_id=topic.id,
                    canonical_url=url,
                    host=(urlparse(url).hostname or "")[:255],
                    title=str(item.get("title") or "") or None,
                    description=str(item.get("description") or "") or None,
                    discovery_method="firecrawl_search",
                    confidence=0.5,
                    evidence={
                        "query": plan_result.plan.query,
                        "search_plan_schema": plan_result.plan.schema_version,
                    },
                )
                db.add(candidate)
            else:
                candidate.last_checked_at = datetime.now(UTC)
            candidates.append(candidate)
        db.flush()
        db.commit()
        candidate_by_url = {item.canonical_url: item for item in candidates}
        discovered_content_ids: list[int] = []
        fetched_pages = 0
        ingested_count = 0
        metadata_only_count = 0
        scrape_errors: list[dict] = []
        scrape_budget = min(
            SCRAPE_BATCH_MAX,
            max(
                0,
                topic.daily_credit_limit - int(used_today or 0) - search_credits,
            ),
        )
        scrape_calls = 0
        for item in results:
            url = normalize_url(str(item["url"]))
            candidate = candidate_by_url[url]
            content = existing_content_for_url(db, url)
            if content is not None and not content_needs_discovery_enrichment(content):
                candidate.source_id = content.source_id
                attach_discovered_match(
                    db,
                    topic=topic,
                    content=content,
                    candidate=candidate,
                    window_start=collection_window_start,
                    window_end=collection_window_end,
                )
                discovered_content_ids.append(content.id)
                db.commit()
                continue
            if content is not None and not enrichment_retry_due(content):
                candidate.source_id = content.source_id
                candidate.last_checked_at = datetime.now(UTC)
                attach_discovered_match(
                    db,
                    topic=topic,
                    content=content,
                    candidate=candidate,
                    window_start=collection_window_start,
                    window_end=collection_window_end,
                )
                discovered_content_ids.append(content.id)
                db.commit()
                continue
            if content is not None:
                content = enrich_discovered_content_from_web(
                    db, content=content, candidate=candidate
                )
                attach_discovered_match(
                    db,
                    topic=topic,
                    content=content,
                    candidate=candidate,
                    window_start=collection_window_start,
                    window_end=collection_window_end,
                )
                discovered_content_ids.append(content.id)
                db.commit()
                continue
            if scrape_calls >= scrape_budget:
                content, ingest_result = ingest_discovered_metadata(db, candidate=candidate)
                attach_discovered_match(
                    db,
                    topic=topic,
                    content=content,
                    candidate=candidate,
                    window_start=collection_window_start,
                    window_end=collection_window_end,
                )
                discovered_content_ids.append(content.id)
                ingested_count += int(ingest_result in {"new", "updated"})
                metadata_only_count += int(ingest_result != "reused")
                db.commit()
                continue
            scrape_calls += 1
            enrichment_attempted_at = datetime.now(UTC)
            try:
                scraped = client.scrape(url)
                content, ingest_result = ingest_discovered_page(
                    db,
                    candidate=candidate,
                    search_item=item,
                    scrape_payload=scraped,
                )
                attach_discovered_match(
                    db,
                    topic=topic,
                    content=content,
                    candidate=candidate,
                    window_start=collection_window_start,
                    window_end=collection_window_end,
                )
                discovered_content_ids.append(content.id)
                fetched_pages += 1
                ingested_count += int(ingest_result in {"new", "updated"})
                db.commit()
            except FirecrawlError as exc:
                db.rollback()
                scrape_errors.append({"url": url, "error_code": str(exc)})
                candidate = db.get(TopicSourceCandidate, candidate.id)
                if candidate is not None:
                    content, ingest_result = ingest_discovered_metadata(
                        db,
                        candidate=candidate,
                        enrichment_attempted_at=enrichment_attempted_at,
                    )
                    attach_discovered_match(
                        db,
                        topic=topic,
                        content=content,
                        candidate=candidate,
                        window_start=collection_window_start,
                        window_end=collection_window_end,
                    )
                    discovered_content_ids.append(content.id)
                    ingested_count += int(ingest_result in {"new", "updated"})
                    metadata_only_count += int(ingest_result != "reused")
                    db.commit()
        credits_used = search_credits + scrape_calls
        run.status = "partial" if scrape_errors else "succeeded"
        run.search_candidates = len(candidates)
        run.fetched_pages = fetched_pages
        run.matched_items = len(set(discovered_content_ids))
        run.firecrawl_credits_used = credits_used
        run.finished_at = datetime.now(UTC)
        run.output = {
            "cache_hit": cache_hit,
            "search_plan": plan_result.plan.model_dump(),
            "search_plan_cache_hit": plan_result.cache_hit,
            "search_request": {
                "query": plan_result.plan.query,
                "limit": payload.limit,
                **search_options,
            },
            "collection_window": {
                "schema_version": "collection-window.v2",
                "mode": "initial_7d" if is_initial_discovery else "incremental_1d",
                "start_at": collection_window_start.isoformat(),
                "end_at": run.finished_at.isoformat(),
            },
            "ingested_count": ingested_count,
            "metadata_only_count": metadata_only_count,
            "content_ids": sorted(set(discovered_content_ids)),
            "scrape_errors": scrape_errors,
        }
        topic.updated_at = datetime.now(UTC)
        db.commit()
    except (FirecrawlError, MissingSecretError, RuntimeError, ValueError) as exc:
        run.status = "failed"
        run.error_code = str(exc)
        run.finished_at = datetime.now(UTC)
        db.commit()
        if isinstance(exc, FirecrawlError):
            raise HTTPException(502, "Firecrawl 来源发现暂时失败") from exc
        raise HTTPException(503, "主题检索计划暂时无法生成") from exc
    return TopicDiscoverResponse(
        topic_id=topic.id,
        cache_hit=cache_hit,
        credits_used=credits_used,
        daily_credit_limit=topic.daily_credit_limit,
        fetched_pages=fetched_pages,
        ingested_count=ingested_count,
        metadata_only_count=metadata_only_count,
        candidates=[
            TopicSourceCandidateRead(
                id=item.id,
                topic_id=item.topic_id,
                canonical_url=item.canonical_url,
                host=item.host,
                title=item.title,
                description=item.description,
                discovery_method=item.discovery_method,
                status=item.status,
                confidence=item.confidence,
            )
            for item in candidates
        ],
        items=_feed_items(db, user, topic.id, 50, include_enrichment=True),
    )


@app.post("/api/v1/sources", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def create_source(payload: SourceCreate, db: Session = Depends(get_db)):
    start_url = str(payload.start_url)
    source = Source(
        catalog_id=payload.catalog_id,
        name=payload.name.strip(),
        channel_type=payload.channel_type,
        start_url=start_url,
        normalized_start_url=normalize_url(start_url),
        fetch_interval_seconds=payload.fetch_interval_seconds,
        parser_config=canonicalize_parser_config(payload.channel_type, payload.parser_config),
        processing_config=payload.processing_config,
        source_region=payload.source_region,
        source_type=payload.source_type,
        default_language=payload.default_language,
        source_tags=payload.source_tags,
        source_external_id=payload.source_external_id,
    )
    db.add(source)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "该网站来源已注册") from exc
    db.refresh(source)
    return source


@app.get("/api/v1/sources", response_model=list[SourceRead])
def list_sources(db: Session = Depends(get_db)):
    return serialize_sources(db, list(db.scalars(select(Source).order_by(Source.id))))


@app.patch("/api/v1/sources/{source_id}", response_model=SourceRead)
def update_source(source_id: int, payload: SourceUpdate, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "来源不存在")
    changes = payload.model_dump(exclude_unset=True)
    candidate_channel = changes.get("channel_type", source.channel_type)
    candidate_config = changes.get("parser_config", source.parser_config)
    try:
        validate_channel_config(candidate_channel, candidate_config)
    except ChannelConfigurationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if changes.get("start_url") is not None:
        changes["start_url"] = str(changes["start_url"])
        changes["normalized_start_url"] = normalize_url(changes["start_url"])
    if "parser_config" in changes or "channel_type" in changes:
        changes["parser_config"] = canonicalize_parser_config(candidate_channel, candidate_config)
    for field, value in changes.items():
        setattr(source, field, value.strip() if field == "name" else value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "该渠道与入口地址已注册") from exc
    db.refresh(source)
    return source


@app.post("/api/v1/sources/{source_id}/crawl", response_model=CrawlAccepted, status_code=202)
def trigger_crawl(
    source_id: int,
    tasks: BackgroundTasks,
    coverage_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "来源不存在")
    if not source.is_enabled:
        raise HTTPException(409, "来源已停用")
    strategy = str((source.parser_config or {}).get("crawl_strategy") or "")
    if strategy in {"blocked", "unavailable"}:
        raise HTTPException(409, "该来源当前按访问规则停爬或不可用")
    try:
        run, created = create_crawl_run(db, source, coverage_date=coverage_date)
    except ActiveCrawlConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if created:
        tasks.add_task(crawl_source, SessionLocal, source.id, run.id)
    return CrawlAccepted(
        run_id=run.id,
        status=run.status,
        coverage_date=run.coverage_date,
        publication_timezone=run.publication_timezone,
    )


@app.get("/api/v1/scheduler/due", response_model=list[SourceRead])
def list_due_sources(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    return serialize_sources(db, due_sources(db, limit=limit))


@app.post("/api/v1/scheduler/run-due", response_model=list[CrawlAccepted], status_code=202)
def trigger_due_sources(
    tasks: BackgroundTasks,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    scheduled = create_due_runs(db, limit=limit)
    for item in scheduled:
        tasks.add_task(crawl_source, SessionLocal, item.source_id, item.run_id)
    return [CrawlAccepted(run_id=item.run_id, status="pending") for item in scheduled]


@app.get("/api/v1/crawl-runs/{run_id}", response_model=CrawlRunRead)
def get_crawl_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(CrawlRun, run_id)
    if not run:
        raise HTTPException(404, "抓取任务不存在")
    return run


@app.post("/api/v1/crawl-runs/{run_id}/retry", response_model=CrawlAccepted, status_code=202)
def retry_crawl_run(run_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    run = db.get(CrawlRun, run_id)
    if not run:
        raise HTTPException(404, "抓取任务不存在")
    if run.status not in {"failed", "partial"}:
        raise HTTPException(409, "只有失败或部分失败的任务可以重跑")
    source = db.get(Source, run.source_id)
    if not source:
        raise HTTPException(404, "来源不存在")
    if not source.is_enabled:
        raise HTTPException(409, "来源已停用")
    retry_date, retry_timezone = resolve_run_coverage(
        source,
        reference_time=run.started_at,
        coverage_date=run.coverage_date,
        publication_timezone=run.publication_timezone,
    )
    try:
        retry, created = create_crawl_run(
            db,
            source,
            trigger="retry",
            coverage_date=retry_date,
            publication_timezone=retry_timezone,
            retry_of_run_id=run.id,
        )
    except ActiveCrawlConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if created:
        tasks.add_task(crawl_source, SessionLocal, source.id, retry.id)
    return CrawlAccepted(
        run_id=retry.id,
        status=retry.status,
        coverage_date=retry.coverage_date,
        publication_timezone=retry.publication_timezone,
    )


@app.get("/api/v1/page-snapshots", response_model=list[PageSnapshotRead])
def list_page_snapshots(
    crawl_run_id: int | None = None,
    before_id: int | None = None,
    include_body: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    statement = select(PageSnapshot)
    if crawl_run_id is not None:
        statement = statement.where(PageSnapshot.crawl_run_id == crawl_run_id)
    if before_id is not None:
        statement = statement.where(PageSnapshot.id < before_id)
    snapshots = db.scalars(statement.order_by(PageSnapshot.id.desc()).limit(limit))
    return [
        PageSnapshotRead(
            id=item.id,
            crawl_run_id=item.crawl_run_id,
            url=item.url,
            page_type=item.page_type,
            request_method=item.request_method,
            http_status=item.http_status,
            content_type=item.content_type,
            response_headers=item.response_headers,
            error_text=item.error_text,
            body_sha256=item.body_sha256,
            fetched_at=item.fetched_at,
            body=item.body if include_body else None,
        )
        for item in snapshots
    ]


@app.get("/api/v1/raw-items", response_model=list[RawItemRead])
def list_raw_items(
    source_id: int | None = None,
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    statement = select(RawItem)
    if source_id is not None:
        statement = statement.where(RawItem.source_id == source_id)
    if before_id is not None:
        statement = statement.where(RawItem.id < before_id)
    return list(db.scalars(statement.order_by(RawItem.id.desc()).limit(limit)))


@app.get("/api/v1/content-items", response_model=list[ContentItemRead])
def list_content_items(
    source_id: int | None = None,
    language: str | None = None,
    source_region: str | None = None,
    is_relevant: bool | None = None,
    domain_key: str | None = None,
    entity_id: int | None = None,
    domain_decision: str = Query(default="include", pattern="^(include|exclude|candidate)$"),
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    statement = (
        select(ContentItem, Source, RawItem, ContentProcessingResult)
        .join(Source, Source.id == ContentItem.source_id)
        .join(RawItem, RawItem.id == ContentItem.raw_item_id)
        .outerjoin(
            ContentProcessingResult,
            (ContentProcessingResult.content_item_id == ContentItem.id)
            & (ContentProcessingResult.processor_name == PROCESSOR_NAME)
            & (ContentProcessingResult.processor_version == PROCESSOR_VERSION)
            & (ContentProcessingResult.input_content_hash == ContentItem.content_hash),
        )
    )
    if source_id is not None:
        statement = statement.where(ContentItem.source_id == source_id)
    if language is not None:
        statement = statement.where(ContentItem.language == language)
    if source_region is not None:
        statement = statement.where(ContentItem.source_region == source_region)
    if is_relevant is not None:
        statement = statement.where(ContentProcessingResult.is_relevant == is_relevant)
    if domain_key is not None:
        current_domain_assignment = (
            select(ContentDomainAssignment.id)
            .join(Domain, Domain.id == ContentDomainAssignment.domain_id)
            .where(
                ContentDomainAssignment.content_item_id == ContentItem.id,
                Domain.key == domain_key,
                ContentDomainAssignment.decision == domain_decision,
                ContentDomainAssignment.input_content_hash == ContentItem.content_hash,
            )
            .exists()
        )
        statement = statement.where(current_domain_assignment)
    if entity_id is not None:
        current_entity_mention = (
            select(EntityMention.id)
            .join(
                EntityProcessingResult,
                EntityProcessingResult.id == EntityMention.processing_result_id,
            )
            .where(
                EntityMention.content_item_id == ContentItem.id,
                EntityMention.entity_id == entity_id,
                EntityProcessingResult.input_content_hash == ContentItem.content_hash,
                EntityProcessingResult.status == "succeeded",
            )
            .exists()
        )
        statement = statement.where(current_entity_mention)
    if before_id is not None:
        statement = statement.where(ContentItem.id < before_id)
    effective_time = func.coalesce(ContentItem.published_at, ContentItem.discovered_at)
    rows = db.execute(statement.order_by(effective_time.desc(), ContentItem.id.desc()).limit(limit))
    return [
        ContentItemRead(
            id=item.id,
            source_id=item.source_id,
            source_name=source.name,
            title=item.title,
            original_url=item.original_url,
            canonical_url=item.canonical_url,
            author=item.author,
            body=item.body,
            language=item.language,
            source_region=item.source_region,
            source_type=item.source_type,
            source_external_id=source.source_external_id,
            external_item_id=item.external_id,
            channel_type=source.channel_type,
            provider=str((source.parser_config or {}).get("provider") or "direct"),
            access_level=item.access_level,
            content_type=item.content_type,
            topics=item.topics,
            is_sponsored=item.is_sponsored,
            is_roundup=item.is_roundup,
            excerpt=item.excerpt,
            content_url=item.original_url or item.canonical_url,
            discovery_url=str(
                (source.parser_config or {}).get("discovery_url") or source.start_url
            ),
            crawl_run_id=raw.crawl_run_id,
            page_snapshot_id=raw.page_snapshot_id,
            updated_at=item.source_updated_at,
            word_count=count_words(item.body or ""),
            media=item.media,
            quality=item.quality,
            quality_tier=quality_tier(item),
            content_hash=item.content_hash,
            schema_version=item.schema_version,
            published_at=item.published_at,
            discovered_at=item.discovered_at,
            duplicate_of_id=item.duplicate_of_id,
            duplicate_rule=item.duplicate_rule,
            is_relevant=processing.is_relevant if processing else None,
            relevance_reason=processing.reason if processing else None,
            matched_topics=processing.matched_topics if processing else [],
            matched_events=processing.matched_events if processing else [],
        )
        for item, source, raw, processing in rows
    ]


@app.get("/api/v1/domains", response_model=list[DomainRead])
def list_domains(db: Session = Depends(get_db)):
    return list(db.scalars(select(Domain).order_by(Domain.id)))


@app.get("/api/v1/value-scores", response_model=list[ContentValueScoreRead])
def list_value_scores(
    domain_key: str,
    decision: str | None = Query(default=None, pattern="^(selected|full_pool)$"),
    run_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    domain = db.scalar(select(Domain).where(Domain.key == domain_key))
    if domain is None:
        raise HTTPException(status_code=404, detail="domain not found")
    score_run = None
    if run_id is not None:
        score_run = db.scalar(
            select(ContentValueScoreRun).where(
                ContentValueScoreRun.id == run_id,
                ContentValueScoreRun.domain_id == domain.id,
                ContentValueScoreRun.status == "succeeded",
            )
        )
    else:
        score_run = db.scalar(
            select(ContentValueScoreRun)
            .where(
                ContentValueScoreRun.domain_id == domain.id,
                ContentValueScoreRun.status == "succeeded",
            )
            .order_by(ContentValueScoreRun.as_of.desc(), ContentValueScoreRun.id.desc())
            .limit(1)
        )
    if score_run is None:
        return []
    statement = (
        select(ContentValueScore, ContentItem, Source)
        .join(ContentItem, ContentItem.id == ContentValueScore.content_item_id)
        .join(Source, Source.id == ContentItem.source_id)
        .where(
            ContentValueScore.run_id == score_run.id,
            ContentValueScore.input_content_hash == ContentItem.content_hash,
        )
    )
    if decision is not None:
        statement = statement.where(ContentValueScore.decision == decision)
    rows = db.execute(
        statement.order_by(ContentValueScore.total_score.desc(), ContentValueScore.id).limit(limit)
    )
    return [
        ContentValueScoreRead(
            id=score.id,
            run_id=score.run_id,
            content_item_id=item.id,
            title=item.title,
            source_id=source.id,
            source_name=source.name,
            published_at=item.published_at,
            total_score=score.total_score,
            component_scores=score.component_scores,
            penalties=score.penalties,
            gates=score.gates,
            decision=score.decision,
            reasons=score.reasons,
            scored_at=score.scored_at,
            algorithm_version=score_run.algorithm_version,
            schema_version=score_run.schema_version,
            as_of=score_run.as_of,
        )
        for score, item, source in rows
    ]


def _entity_read(db: Session, entity: Entity) -> EntityRead:
    current_mention = (
        select(EntityMention.id)
        .join(
            EntityProcessingResult,
            EntityProcessingResult.id == EntityMention.processing_result_id,
        )
        .join(ContentItem, ContentItem.id == EntityMention.content_item_id)
        .where(
            EntityMention.entity_id == entity.id,
            EntityProcessingResult.input_content_hash == ContentItem.content_hash,
            EntityProcessingResult.status == "succeeded",
        )
    )
    mention_count = db.scalar(select(func.count()).select_from(current_mention.subquery())) or 0
    return EntityRead(
        id=entity.id,
        registry_key=entity.registry_key,
        entity_type=entity.entity_type,
        canonical_name=entity.canonical_name,
        normalized_name=entity.normalized_name,
        description=entity.description,
        attributes=entity.attributes,
        status=entity.status,
        mention_count=mention_count,
    )


@app.get("/api/v1/entities", response_model=list[EntityRead])
def list_entities(
    entity_type: str | None = None,
    q: str | None = None,
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    statement = select(Entity).where(Entity.status == "active")
    if entity_type is not None:
        statement = statement.where(Entity.entity_type == entity_type)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        alias_match = (
            select(EntityAlias.id)
            .where(
                EntityAlias.entity_id == Entity.id,
                EntityAlias.alias.ilike(pattern),
            )
            .exists()
        )
        statement = statement.where(Entity.canonical_name.ilike(pattern) | alias_match)
    if before_id is not None:
        statement = statement.where(Entity.id < before_id)
    entities = db.scalars(statement.order_by(Entity.id.desc()).limit(limit))
    return [_entity_read(db, entity) for entity in entities]


@app.get("/api/v1/entities/{entity_id}", response_model=EntityDetailRead)
def get_entity(entity_id: int, db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if entity is None or entity.status != "active":
        raise HTTPException(404, "实体不存在")
    summary = _entity_read(db, entity)
    aliases = list(
        db.scalars(
            select(EntityAlias).where(EntityAlias.entity_id == entity.id).order_by(EntityAlias.id)
        )
    )
    rows = db.execute(
        select(EntityMention, ContentItem)
        .join(ContentItem, ContentItem.id == EntityMention.content_item_id)
        .join(
            EntityProcessingResult,
            EntityProcessingResult.id == EntityMention.processing_result_id,
        )
        .where(
            EntityMention.entity_id == entity.id,
            EntityProcessingResult.input_content_hash == ContentItem.content_hash,
            EntityProcessingResult.status == "succeeded",
        )
        .order_by(EntityMention.id.desc())
    )
    mentions = [
        EntityMentionRead(
            id=mention.id,
            content_item_id=content.id,
            content_title=content.title,
            entity_type=mention.entity_type,
            surface=mention.surface,
            field=mention.field,
            start_offset=mention.start_offset,
            end_offset=mention.end_offset,
            evidence_text=mention.evidence_text,
            confidence=mention.confidence,
            resolution_status=mention.resolution_status,
            extraction_method=mention.extraction_method,
        )
        for mention, content in rows
    ]
    return EntityDetailRead(
        **summary.model_dump(),
        aliases=[EntityAliasRead.model_validate(alias) for alias in aliases],
        mentions=mentions,
    )


@app.get(
    "/api/v1/entity-candidate-reviews",
    response_model=list[EntityCandidateReviewRead],
)
def list_entity_candidate_reviews(
    review_status: str = Query(default="pending", pattern="^(pending|confirmed|rejected)$"),
    entity_type: str | None = None,
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    statement = select(EntityCandidateReview).where(EntityCandidateReview.status == review_status)
    if entity_type is not None:
        statement = statement.where(EntityCandidateReview.entity_type == entity_type)
    if before_id is not None:
        statement = statement.where(EntityCandidateReview.id < before_id)
    return list(db.scalars(statement.order_by(EntityCandidateReview.id.desc()).limit(limit)))


@app.post(
    "/api/v1/entity-candidate-reviews/{review_id}/decision",
    response_model=EntityCandidateReviewRead,
)
def decide_entity_candidate_review(
    review_id: int,
    payload: EntityCandidateDecision,
    db: Session = Depends(get_db),
):
    try:
        decide_entity_candidate(
            db,
            review_id,
            action=payload.action,
            entity_id=payload.entity_id,
            canonical_name=payload.canonical_name,
            decided_by=payload.decided_by,
            reason=payload.reason,
        )
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(404 if "not found" in message else 409, message) from exc
    db.commit()
    return db.get(EntityCandidateReview, review_id)


def _event_read(db: Session, event: Event) -> EventRead:
    member_count, source_count = db.execute(
        select(func.count(EventMember.id), func.count(func.distinct(ContentItem.source_id)))
        .join(ContentItem, ContentItem.id == EventMember.content_item_id)
        .where(EventMember.event_id == event.id, EventMember.is_active.is_(True))
    ).one()
    return EventRead(
        id=event.id,
        canonical_title=event.canonical_title,
        representative_content_id=event.representative_content_id,
        first_published_at=event.first_published_at,
        last_published_at=event.last_published_at,
        member_count=member_count,
        source_count=source_count,
        membership_hash=event.membership_hash,
        cluster_version=event.cluster_version,
        manual_lock=event.manual_lock,
    )


@app.get("/api/v1/events", response_model=list[EventRead])
def list_events(
    before_id: int | None = None,
    min_members: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    member_count = (
        select(func.count(EventMember.id))
        .where(EventMember.event_id == Event.id, EventMember.is_active.is_(True))
        .scalar_subquery()
    )
    statement = select(Event).where(Event.status == "active", member_count >= min_members)
    if before_id is not None:
        statement = statement.where(Event.id < before_id)
    events = db.scalars(
        statement.order_by(Event.last_published_at.desc(), Event.id.desc()).limit(limit)
    )
    return [_event_read(db, event) for event in events]


@app.get("/api/v1/events/{event_id}", response_model=EventDetailRead)
def get_event(event_id: int, db: Session = Depends(get_db)):
    event = db.get(Event, event_id)
    if event is None or event.status != "active":
        raise HTTPException(404, "事件不存在")
    summary = _event_read(db, event)
    rows = db.execute(
        select(EventMember, ContentItem, Source)
        .join(ContentItem, ContentItem.id == EventMember.content_item_id)
        .join(Source, Source.id == ContentItem.source_id)
        .where(EventMember.event_id == event.id, EventMember.is_active.is_(True))
        .order_by(ContentItem.published_at.asc(), ContentItem.id.asc())
    )
    members = [
        EventMemberRead(
            content_item_id=content.id,
            title=content.title,
            source_id=source.id,
            source_name=source.name,
            canonical_url=content.canonical_url,
            published_at=content.published_at,
            confidence=member.confidence,
            reasons=member.reasons,
            decision_source=member.decision_source,
        )
        for member, content, source in rows
    ]
    return EventDetailRead(**summary.model_dump(), members=members)
