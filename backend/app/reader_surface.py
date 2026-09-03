from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from xml.sax.saxutils import escape

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .content_quality import is_reader_eligible
from .models import (
    ContentItem,
    ContentValueScore,
    ContentValueScoreRun,
    Event,
    EventMember,
    InterestTopic,
    LLMProcessingResult,
    Source,
    TopicMatch,
    User,
)
from .reader_cards import card_paragraphs, editorial_title, unwrap_editorial
from .source_admin import is_website_source
from .topic_matching import (
    _extract_intent_terms,
    _normalize_text,
    _term_in_text,
    _with_bilingual_aliases,
)

READER_EVENT_WINDOW_DAYS = 7
SEARCH_BODY_CHARS = 4000
SEARCH_LIMIT = 50


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def latest_value_decisions(db: Session) -> dict[int, str]:
    latest_runs = (
        select(
            ContentValueScoreRun.domain_id,
            func.max(ContentValueScoreRun.id).label("run_id"),
        )
        .where(ContentValueScoreRun.status == "succeeded")
        .group_by(ContentValueScoreRun.domain_id)
        .subquery()
    )
    rows = db.execute(
        select(ContentValueScore.content_item_id, ContentValueScore.decision).join(
            latest_runs, latest_runs.c.run_id == ContentValueScore.run_id
        )
    )
    result: dict[int, str] = {}
    for content_id, decision in rows:
        if result.get(content_id) != "selected":
            result[content_id] = decision
    return result


def is_curated_keep(content_id: int, decisions: dict[int, str]) -> bool:
    return decisions.get(content_id) != "full_pool"


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


def _artifact_title(artifact: dict, fallback: str, content_id: int | None = None) -> str:
    return editorial_title(unwrap_editorial(artifact, content_id), fallback)


def _artifact_summary(artifact: dict, fallback: str | None, content=None) -> str | None:
    if content is not None:
        paragraphs = card_paragraphs(content, artifact)
        if paragraphs:
            return " ".join(paragraphs)
    unwrapped = unwrap_editorial(artifact)
    summary = unwrapped.get("chinese_summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    units = unwrapped.get("summary_units") or []
    texts = [
        unit.get("text_zh", "").strip()
        for unit in units
        if isinstance(unit, dict) and unit.get("text_zh")
    ]
    if texts:
        return " ".join(texts)
    return fallback


def _included_reader_contents(
    db: Session,
    user: User,
    topic_id: int | None,
    *,
    within_days: int | None = READER_EVENT_WINDOW_DAYS,
) -> list[tuple[InterestTopic, ContentItem, Source]]:
    statement = (
        select(InterestTopic, ContentItem, Source)
        .join(TopicMatch, TopicMatch.topic_id == InterestTopic.id)
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
    cutoff = datetime.now(UTC) - timedelta(days=within_days) if within_days is not None else None
    rows: list[tuple[InterestTopic, ContentItem, Source]] = []
    for topic, content, source in db.execute(statement):
        if not is_reader_eligible(content):
            continue
        if cutoff is not None:
            published = _aware(content.published_at)
            if content.published_at is None or published < cutoff:
                continue
        rows.append((topic, content, source))
    return rows


def _event_payloads(
    db: Session,
    events: list[Event],
    *,
    topic_id: int | None,
    include_members: bool,
) -> list[dict]:
    if not events:
        return []
    event_ids = [event.id for event in events]
    member_rows = list(
        db.execute(
            select(EventMember, ContentItem, Source)
            .join(ContentItem, ContentItem.id == EventMember.content_item_id)
            .join(Source, Source.id == ContentItem.source_id)
            .where(EventMember.event_id.in_(event_ids), EventMember.is_active.is_(True))
            .order_by(ContentItem.published_at.asc(), ContentItem.id.asc())
        )
    )
    members_by_event: dict[int, list[tuple[EventMember, ContentItem, Source]]] = {
        event.id: [] for event in events
    }
    content_ids: set[int] = set()
    topic_pairs: set[tuple[int, int]] = set()
    for member, content, source in member_rows:
        members_by_event.setdefault(member.event_id, []).append((member, content, source))
        content_ids.add(content.id)
        if topic_id is not None:
            topic_pairs.add((topic_id, content.id))
    editorial = _editorial_map(db, content_ids)
    topic_editorial = _topic_editorial_map(db, topic_pairs)
    scores = latest_value_decisions(db)
    payloads: list[dict] = []
    for event in events:
        members = members_by_event.get(event.id, [])
        readable = [
            (member, content, source)
            for member, content, source in members
            if is_reader_eligible(content)
        ]
        if len(readable) < 2:
            continue
        representative = next(
            (
                content
                for _member, content, _source in readable
                if content.id == event.representative_content_id
            ),
            readable[0][1],
        )
        artifact = {}
        if topic_id is not None:
            artifact = topic_editorial.get((topic_id, representative.id), {})
        artifact = artifact or editorial.get(representative.id, {})
        sources: list[dict] = []
        seen_sources: set[str] = set()
        for _member, content, source in readable:
            url = content.canonical_url or content.original_url
            key = f"{source.name}|{url or ''}"
            if key in seen_sources:
                continue
            seen_sources.add(key)
            sources.append({"source_name": source.name, "url": url})
        member_payloads = []
        if include_members:
            for _member, content, source in readable:
                item_artifact = {}
                if topic_id is not None:
                    item_artifact = topic_editorial.get((topic_id, content.id), {})
                item_artifact = item_artifact or editorial.get(content.id, {})
                member_payloads.append(
                    {
                        "content_id": content.id,
                        "title": _artifact_title(item_artifact, content.title, content.id),
                        "excerpt": _artifact_summary(item_artifact, content.excerpt, content),
                        "paragraphs": card_paragraphs(content, item_artifact),
                        "source_name": source.name,
                        "url": content.canonical_url or content.original_url,
                        "published_at": content.published_at,
                    }
                )
        selected_bonus = sum(
            1 for _member, content, _source in readable if scores.get(content.id) == "selected"
        )
        payloads.append(
            {
                "id": event.id,
                "title": _artifact_title(artifact, event.canonical_title, representative.id),
                "summary": _artifact_summary(artifact, representative.excerpt, representative),
                "paragraphs": card_paragraphs(representative, artifact),
                "first_published_at": event.first_published_at,
                "last_published_at": event.last_published_at,
                "source_count": len(sources),
                "member_count": len(readable),
                "sources": sources,
                "members": member_payloads,
                "_heat": (
                    len(sources),
                    selected_bonus,
                    _aware(event.last_published_at),
                    len(readable),
                    event.id,
                ),
            }
        )
    payloads.sort(key=lambda item: item["_heat"], reverse=True)
    for item in payloads:
        item.pop("_heat", None)
    return payloads


def list_reader_events(
    db: Session,
    user: User,
    topic_id: int | None,
    *,
    limit: int = 20,
    include_members: bool = False,
) -> list[dict]:
    included = _included_reader_contents(db, user, topic_id)
    content_ids = {content.id for _topic, content, _source in included}
    if not content_ids:
        return []
    event_ids = list(
        db.scalars(
            select(EventMember.event_id)
            .where(
                EventMember.is_active.is_(True),
                EventMember.content_item_id.in_(content_ids),
            )
            .distinct()
        )
    )
    if not event_ids:
        return []
    events = list(
        db.scalars(
            select(Event).where(Event.id.in_(event_ids), Event.status == "active")
        )
    )
    return _event_payloads(db, events, topic_id=topic_id, include_members=include_members)[:limit]


def reader_event_detail(
    db: Session,
    user: User,
    event_id: int,
    topic_id: int | None,
) -> dict | None:
    visible = {item["id"] for item in list_reader_events(db, user, topic_id, limit=200)}
    if event_id not in visible:
        return None
    event = db.get(Event, event_id)
    if event is None or event.status != "active":
        return None
    payloads = _event_payloads(db, [event], topic_id=topic_id, include_members=True)
    return payloads[0] if payloads else None


def search_terms(query: str) -> list[str]:
    raw = _normalize_text(query)
    terms = _with_bilingual_aliases(_extract_intent_terms(query), query)
    if raw and raw not in terms:
        terms = [raw, *terms]
    return [term for term in terms if term]


def _content_blob(content: ContentItem, artifact: dict) -> str:
    parts = [
        _artifact_title(artifact, content.title, content.id),
        _artifact_summary(artifact, content.excerpt, content) or "",
        content.title or "",
        content.excerpt or "",
        (content.body or "")[:SEARCH_BODY_CHARS],
    ]
    return _normalize_text(" ".join(parts))


def blob_matches(query: str, blob: str) -> bool:
    terms = search_terms(query)
    if not terms or not blob:
        return False
    return any(_term_in_text(term, blob) for term in terms)


def search_topic_contents(
    db: Session,
    user: User,
    topic_id: int | None,
    query: str,
    *,
    limit: int = SEARCH_LIMIT,
) -> list[tuple[InterestTopic, ContentItem, Source, dict]]:
    included = _included_reader_contents(db, user, topic_id, within_days=None)
    content_ids = {content.id for _topic, content, _source in included}
    editorial = _editorial_map(db, content_ids)
    topic_pairs = {(topic.id, content.id) for topic, content, _source in included}
    topic_editorial = _topic_editorial_map(db, topic_pairs)
    matched: list[tuple[InterestTopic, ContentItem, Source, dict]] = []
    for topic, content, source in included:
        artifact = topic_editorial.get((topic.id, content.id)) or editorial.get(content.id, {})
        if blob_matches(query, _content_blob(content, artifact)):
            matched.append((topic, content, source, artifact))
    matched.sort(
        key=lambda item: (_aware(item[1].published_at or item[1].discovered_at), item[1].id),
        reverse=True,
    )
    return matched[:limit]


def search_explore_contents(
    db: Session,
    query: str,
    *,
    limit: int = SEARCH_LIMIT,
) -> list[tuple[ContentItem, Source, dict]]:
    rows = db.execute(
        select(ContentItem, Source)
        .join(Source, Source.id == ContentItem.source_id)
        .order_by(
            func.coalesce(ContentItem.published_at, ContentItem.discovered_at).desc(),
            ContentItem.id.desc(),
        )
        .limit(max(limit * 12, 80))
    ).all()
    editorial = _editorial_map(db, {content.id for content, _source in rows})
    matched: list[tuple[ContentItem, Source, dict]] = []
    for content, source in rows:
        if not is_website_source(source) or not is_reader_eligible(content):
            continue
        artifact = editorial.get(content.id, {})
        if blob_matches(query, _content_blob(content, artifact)):
            matched.append((content, source, artifact))
        if len(matched) >= limit:
            break
    return matched


def render_topic_rss(
    *,
    topic: InterestTopic,
    items: list[dict],
    self_link: str,
) -> str:
    channel_title = escape(f"Navigate · {topic.name}")
    description = escape(topic.intent_text or topic.name)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{channel_title}</title>",
        f"<link>{escape(self_link)}</link>",
        f"<description>{description}</description>",
    ]
    for item in items:
        url = item.get("url")
        if not url:
            continue
        published = item.get("published_at")
        pub_date = format_datetime(_aware(published)) if published else ""
        excerpt = item.get("excerpt") or ""
        parts.extend(
            [
                "<item>",
                f"<title>{escape(item['title'])}</title>",
                f"<link>{escape(url)}</link>",
                f"<guid isPermaLink=\"true\">{escape(url)}</guid>",
                f"<description>{escape(excerpt)}</description>",
                f"<source>{escape(item.get('source_name') or '')}</source>",
            ]
        )
        if pub_date:
            parts.append(f"<pubDate>{escape(pub_date)}</pubDate>")
        parts.append("</item>")
    parts.extend(["</channel>", "</rss>"])
    return "".join(parts)
