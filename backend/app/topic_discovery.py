from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .content_quality import quality_tier
from .firecrawl import FirecrawlError
from .models import (
    ContentItem,
    CrawlRun,
    InterestTopic,
    PageSnapshot,
    Source,
    TopicMatch,
    TopicSourceCandidate,
)
from .normalization import normalize_url
from .topic_matching import MATCHER_VERSION, match_content
from .web_ingestion import (
    ContentFormError,
    extract_article,
    extract_page_publication_date,
    ingest_article,
    merge_web_enrichment_detail,
    request_headers,
    robots_allows,
    robots_url,
    save_response_snapshot,
)

ENRICHMENT_RETRY_COOLDOWN = timedelta(hours=24)


def existing_content_for_url(db: Session, url: str) -> ContentItem | None:
    normalized = normalize_url(url)
    return db.scalar(
        select(ContentItem)
        .where(
            or_(
                ContentItem.canonical_url == normalized,
                ContentItem.original_url == normalized,
            )
        )
        .order_by(ContentItem.id.desc())
        .limit(1)
    )


def content_is_metadata_only(content: ContentItem) -> bool:
    return bool((content.quality or {}).get("metadata_only"))


def content_needs_discovery_enrichment(content: ContentItem) -> bool:
    """A substantial Firecrawl body without a date is still not reader-ready."""
    return quality_tier(content) == "needs_enrichment"


def enrichment_retry_due(content: ContentItem, now: datetime | None = None) -> bool:
    """Avoid charging repeated Scrape attempts for the same shared URL."""
    value = (content.quality or {}).get("last_enrichment_attempt_at")
    if not value:
        return True
    attempted_at = _parse_datetime(value)
    if attempted_at is None:
        return True
    return attempted_at + ENRICHMENT_RETRY_COOLDOWN <= (now or datetime.now(UTC))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def _source_for_page(db: Session, url: str, metadata: dict) -> Source:
    start_url = normalize_url(_origin(url))
    source = db.scalar(
        select(Source).where(
            Source.channel_type == "web",
            Source.normalized_start_url == start_url,
        )
    )
    if source is not None:
        return source
    host = urlsplit(url).hostname or "发现来源"
    site_name = str(metadata.get("siteName") or metadata.get("ogSiteName") or "").strip()
    catalog_id = f"firecrawl_{hashlib.sha256(start_url.encode()).hexdigest()[:16]}"
    source = Source(
        catalog_id=catalog_id,
        name=(site_name or host)[:200],
        channel_type="web",
        start_url=start_url,
        normalized_start_url=start_url,
        parser_config={
            "provider": "firecrawl",
            "discovery_method": "user_topic",
            "content_completeness": "full",
        },
        processing_config={},
        source_region="GLOBAL",
        source_type="discovered_web",
        default_language="und",
        source_tags=["user_discovered"],
        is_enabled=False,
    )
    db.add(source)
    db.flush()
    return source


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _scrape_data(payload: dict) -> tuple[dict, dict]:
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise FirecrawlError("firecrawl_invalid_content")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return data, metadata


def _generic_web_config(source: Source) -> dict:
    return {
        **(source.parser_config or {}),
        "content_completeness": "full",
        "enrichment_min_body_chars": 400,
    }


def _web_enrichment_detail(
    db: Session,
    *,
    source: Source,
    run: CrawlRun,
    url: str,
    article: dict,
) -> dict:
    """Fetch one canonical webpage through the standard parser and retain evidence."""
    config = _generic_web_config(source)
    headers = request_headers(config)
    try:
        with httpx.Client(
            headers=headers,
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            robots_response = client.get(robots_url(url))
            if robots_response.is_success and not robots_allows(robots_response.text, url):
                raise ContentFormError("content_enrichment_robots_disallowed")
            response = client.get(url)
            snapshot = save_response_snapshot(
                db,
                run,
                url=url,
                page_type="article_enrichment_web",
                request_method="GET",
                response=response,
                error_text=(
                    f"http_status:{response.status_code}" if not response.is_success else None
                ),
            )
            response.raise_for_status()
            try:
                web_article = extract_article(response.text, str(response.url), config)
            except ContentFormError:
                published_at, origin = extract_page_publication_date(
                    response.text, str(response.url), config
                )
                if published_at is None:
                    raise
                web_article = {
                    "published_at": published_at,
                    "validation_warnings": [f"published_at:{origin}"],
                }
            merged = merge_web_enrichment_detail(article, web_article)
            merged["fallback_page_snapshot_id"] = snapshot.id
            return merged
    except (httpx.HTTPError, ContentFormError, ValueError) as exc:
        result = dict(article)
        result["validation_warnings"] = list(
            dict.fromkeys(
                [
                    *(article.get("validation_warnings") or []),
                    f"content_enrichment_failed:{type(exc).__name__}",
                ]
            )
        )
        return result


def enrich_discovered_content_from_web(
    db: Session,
    *,
    content: ContentItem,
    candidate: TopicSourceCandidate,
) -> ContentItem:
    """Retry an existing incomplete discovery without another Firecrawl Scrape."""
    source = db.get(Source, content.source_id)
    if source is None:
        raise RuntimeError("discovered_content_source_missing")
    url = normalize_url(content.canonical_url or candidate.canonical_url)
    run = CrawlRun(source_id=source.id, trigger="topic_discovery_enrichment", status="running")
    db.add(run)
    db.flush()
    extracted = {
        "title": content.title or candidate.title or url,
        "canonical_url": url,
        "original_url": content.original_url or url,
        "content_url": url,
        "author": content.author,
        "published_at": content.published_at,
        "updated_at": content.source_updated_at,
        "external_item_id": content.external_id,
        "body": content.body or content.excerpt or "",
        "description": content.excerpt or "",
        "content_type": content.content_type or "article",
        "topics": content.topics or [],
        "media": content.media or [],
        "content_completeness": "full" if content.body else "metadata_only",
        "validation_warnings": list((content.quality or {}).get("validation_warnings") or []),
    }
    enriched = _web_enrichment_detail(db, source=source, run=run, url=url, article=extracted)
    result = ingest_article(db, source, run, enriched)
    run.status = "succeeded"
    run.finished_at = datetime.now(UTC)
    run.fetched_count = 1
    run.new_count = int(result == "new")
    run.updated_count = int(result == "updated")
    run.skipped_count = int(result == "skipped")
    refreshed = existing_content_for_url(db, url)
    if refreshed is None:
        raise RuntimeError("discovered_content_missing_after_enrichment")
    refreshed.quality = {
        **(refreshed.quality or {}),
        "last_enrichment_attempt_at": datetime.now(UTC).isoformat(),
    }
    candidate.source_id = source.id
    candidate.last_checked_at = datetime.now(UTC)
    db.flush()
    return refreshed


def ingest_discovered_page(
    db: Session,
    *,
    candidate: TopicSourceCandidate,
    search_item: dict,
    scrape_payload: dict,
) -> tuple[ContentItem, str]:
    url = normalize_url(candidate.canonical_url)
    existing = existing_content_for_url(db, url)
    if existing is not None and not content_needs_discovery_enrichment(existing):
        candidate.source_id = existing.source_id
        candidate.last_checked_at = datetime.now(UTC)
        return existing, "reused"

    data, metadata = _scrape_data(scrape_payload)
    markdown = str(data.get("markdown") or "").strip()
    if not markdown:
        raise FirecrawlError("firecrawl_empty_content")
    title = str(
        metadata.get("title") or metadata.get("ogTitle") or search_item.get("title") or url
    ).strip()
    description = str(
        metadata.get("description")
        or metadata.get("ogDescription")
        or search_item.get("description")
        or ""
    ).strip()
    source = _source_for_page(db, url, metadata)
    candidate.source_id = source.id
    candidate.last_checked_at = datetime.now(UTC)
    run = CrawlRun(source_id=source.id, trigger="topic_discovery", status="running")
    db.add(run)
    db.flush()
    snapshot = PageSnapshot(
        crawl_run_id=run.id,
        url=url,
        page_type="article",
        request_method="POST",
        http_status=int(metadata.get("statusCode") or 200),
        content_type=str(metadata.get("contentType") or "text/markdown"),
        response_headers={},
        body=markdown,
        body_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
    )
    db.add(snapshot)
    db.flush()
    published = _parse_datetime(
        metadata.get("publishedTime")
        or metadata.get("datePublished")
        or metadata.get("article:published_time")
    )
    extracted = {
        "title": title,
        "canonical_url": url,
        "original_url": url,
        "content_url": url,
        "author": metadata.get("author"),
        "published_at": published,
        "updated_at": _parse_datetime(metadata.get("modifiedTime")),
        "external_item_id": None,
        "body": markdown,
        "description": description or markdown[:240],
        "content_type": "article",
        "topics": metadata.get("keywords") or [],
        "media": [],
        "content_completeness": "full" if len(markdown) >= 400 else "partial",
    }
    if extracted["published_at"] is None or len(markdown) < 400:
        extracted = _web_enrichment_detail(db, source=source, run=run, url=url, article=extracted)
    result = ingest_article(db, source, run, extracted, snapshot.id)
    run.status = "succeeded"
    run.finished_at = datetime.now(UTC)
    run.fetched_count = 1
    run.new_count = int(result == "new")
    run.updated_count = int(result == "updated")
    run.skipped_count = int(result == "skipped")
    content = existing_content_for_url(db, url)
    if content is None:
        raise RuntimeError("discovered_content_missing_after_ingest")
    if content_needs_discovery_enrichment(content):
        content.quality = {
            **(content.quality or {}),
            "last_enrichment_attempt_at": datetime.now(UTC).isoformat(),
        }
        db.flush()
    return content, result


def ingest_discovered_metadata(
    db: Session,
    *,
    candidate: TopicSourceCandidate,
    enrichment_attempted_at: datetime | None = None,
) -> tuple[ContentItem, str]:
    url = normalize_url(candidate.canonical_url)
    existing = existing_content_for_url(db, url)
    if existing is not None:
        candidate.source_id = existing.source_id
        candidate.last_checked_at = datetime.now(UTC)
        if enrichment_attempted_at is not None:
            existing.quality = {
                **(existing.quality or {}),
                "last_enrichment_attempt_at": enrichment_attempted_at.isoformat(),
            }
            db.flush()
        return existing, "reused"
    title = (candidate.title or candidate.host or url).strip()
    description = (candidate.description or title).strip()
    source = _source_for_page(db, url, {})
    candidate.source_id = source.id
    candidate.last_checked_at = datetime.now(UTC)
    run = CrawlRun(
        source_id=source.id,
        trigger="topic_discovery_metadata",
        status="running",
    )
    db.add(run)
    db.flush()
    evidence = json.dumps(
        {"title": title, "description": description, "url": url},
        ensure_ascii=False,
        sort_keys=True,
    )
    snapshot = PageSnapshot(
        crawl_run_id=run.id,
        url=url,
        page_type="search_result",
        request_method="POST",
        http_status=200,
        content_type="application/json",
        response_headers={},
        body=evidence,
        body_sha256=hashlib.sha256(evidence.encode()).hexdigest(),
    )
    db.add(snapshot)
    db.flush()
    result = ingest_article(
        db,
        source,
        run,
        {
            "title": title,
            "canonical_url": url,
            "original_url": url,
            "content_url": url,
            "author": None,
            "published_at": None,
            "updated_at": None,
            "external_item_id": None,
            "body": description,
            "description": description,
            "content_type": "article",
            "topics": [],
            "media": [],
            "content_completeness": "metadata_only",
        },
        snapshot.id,
    )
    run.status = "succeeded"
    run.finished_at = datetime.now(UTC)
    run.fetched_count = 0
    run.new_count = int(result == "new")
    run.updated_count = int(result == "updated")
    run.skipped_count = int(result == "skipped")
    content = existing_content_for_url(db, url)
    if content is None:
        raise RuntimeError("discovered_metadata_missing_after_ingest")
    if enrichment_attempted_at is not None:
        content.quality = {
            **(content.quality or {}),
            "last_enrichment_attempt_at": enrichment_attempted_at.isoformat(),
        }
        db.flush()
    return content, result


def attach_discovered_match(
    db: Session,
    *,
    topic: InterestTopic,
    content: ContentItem,
    candidate: TopicSourceCandidate,
    window_start: datetime,
    window_end: datetime,
) -> TopicMatch:
    def as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    published_at = as_utc(content.published_at) if content.published_at else None
    start_utc = as_utc(window_start)
    end_utc = max(as_utc(window_end), datetime.now(UTC))
    match = db.scalar(
        select(TopicMatch).where(
            TopicMatch.topic_id == topic.id,
            TopicMatch.content_item_id == content.id,
            TopicMatch.matcher_version == MATCHER_VERSION,
        )
    )
    prior_window = (match.matched_signals or {}).get("collection_window") if match else None
    currently_admitted = bool(published_at and start_utc <= published_at <= end_utc)
    previously_admitted = bool((prior_window or {}).get("admitted"))
    admitted_by_time = currently_admitted or previously_admitted
    decision = match_content(topic, content)
    excluded_by_rule = "excluded_keyword" in decision.reasons
    included = admitted_by_time and not excluded_by_rule
    time_reason = (
        None
        if admitted_by_time
        else "missing_published_at"
        if published_at is None
        else "outside_collection_window"
    )
    reasons = [*decision.reasons]
    if time_reason:
        reasons.append(time_reason)
    if included:
        reasons.append("firecrawl_discovery")
    values = {
        "input_content_hash": content.content_hash,
        "decision": "include" if included else "exclude",
        "score": max(decision.score, 0.65) if included else 0.0,
        "reasons": list(dict.fromkeys(reasons)),
        "matched_signals": {
            **decision.signals,
            "collection_window": prior_window
            if previously_admitted
            else {
                "schema_version": "collection-window.v2",
                "mode": "firecrawl_discovery",
                "start_at": start_utc.isoformat(),
                "end_at": end_utc.isoformat(),
                "published_at": published_at.isoformat() if published_at else None,
                "admitted": currently_admitted,
            },
            "discovery": {
                "candidate_id": candidate.id,
                "method": candidate.discovery_method,
                "intent_hash": topic.intent_hash,
            },
        },
        "matched_at": datetime.now(UTC),
    }
    if match is None:
        match = TopicMatch(
            topic_id=topic.id,
            content_item_id=content.id,
            matcher_version=MATCHER_VERSION,
            **values,
        )
        db.add(match)
    else:
        for field, value in values.items():
            setattr(match, field, value)
    db.flush()
    return match
