from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .content_quality import is_reader_eligible
from .event_clustering import refresh_event_clusters
from .llm_editorial import (
    READER_BACKFILL_LIMIT,
    READER_CRAWL_LIMIT,
    DeepSeekClient,
    ensure_reader_editorials,
)
from .models import ContentItem, CrawlRun, InterestTopic, Source, TopicMatch
from .secrets import MissingSecretError, require_secret
from .topic_intelligence import compile_topic_with_llm, process_topic_contents
from .topic_matching import (
    MATCHER_VERSION,
    contents_from_crawl_run,
    match_contents_to_topics,
    recent_reader_contents,
)

logger = logging.getLogger("navigate.topics")


def _active_topics(session: Session) -> list[InterestTopic]:
    return list(session.scalars(select(InterestTopic).where(InterestTopic.status == "active")))


def _llm_client() -> DeepSeekClient | None:
    try:
        return DeepSeekClient(api_key=require_secret("DEEPSEEK_API_KEY"))
    except MissingSecretError:
        return None
    except Exception:
        logger.exception("llm client unavailable")
        return None


def _ensure_bilingual_intents(
    session: Session, topics: list[InterestTopic], client: DeepSeekClient
) -> int:
    compiled = 0
    for topic in topics:
        current = topic.compiled_intent or {}
        if current.get("query_expansions"):
            continue
        try:
            compile_topic_with_llm(session, topic, client)
            compiled += 1
        except Exception:
            logger.exception("topic intent compile failed topic_id=%s", topic.id)
    return compiled


def _review_articles(
    session: Session, topic: InterestTopic, content_ids: list[int], *, limit: int
) -> list[tuple[ContentItem, Source]]:
    if not content_ids:
        return []
    rows = list(
        session.execute(
            select(ContentItem, Source, TopicMatch)
            .join(Source, Source.id == ContentItem.source_id)
            .join(TopicMatch, TopicMatch.content_item_id == ContentItem.id)
            .where(
                TopicMatch.topic_id == topic.id,
                TopicMatch.matcher_version == MATCHER_VERSION,
                TopicMatch.content_item_id.in_(content_ids),
                TopicMatch.decision == "review",
            )
            .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
            .limit(limit)
        )
    )
    return [(content, source) for content, source, _match in rows]


def _ensure_reader_editorials(
    session: Session, content_ids: list[int], *, limit: int = READER_CRAWL_LIMIT
) -> dict:
    if not content_ids:
        return {"processed": 0}
    client = _llm_client()
    if client is None:
        return {"skipped": "missing_llm_key"}
    rows = list(
        session.execute(
            select(ContentItem, Source)
            .join(Source, Source.id == ContentItem.source_id)
            .where(ContentItem.id.in_(content_ids))
            .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
        )
    )
    try:
        return ensure_reader_editorials(session, rows, client, limit=limit)
    except Exception:
        logger.exception("reader editorials failed")
        session.rollback()
        return {"error": True}


def backfill_reader_editorials(session: Session, *, limit: int = READER_BACKFILL_LIMIT) -> dict:
    rows = list(
        session.execute(
            select(ContentItem, Source)
            .join(Source, Source.id == ContentItem.source_id)
            .where(ContentItem.published_at.is_not(None))
            .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
        )
    )
    eligible = [
        (content, source) for content, source in rows if is_reader_eligible(content)
    ]
    client = _llm_client()
    if client is None:
        return {"skipped": "missing_llm_key"}
    try:
        return ensure_reader_editorials(session, eligible, client, limit=limit)
    except Exception:
        logger.exception("reader editorial backfill failed")
        session.rollback()
        return {"error": True}


def refine_topic_matches_with_llm(
    session: Session,
    content_ids: list[int],
    *,
    max_per_topic: int = 12,
) -> dict:
    if not content_ids:
        return {"topics": 0, "processed": 0}
    client = _llm_client()
    if client is None:
        return {"skipped": "missing_llm_key"}
    topics = _active_topics(session)
    compiled = _ensure_bilingual_intents(session, topics, client)
    contents = list(
        session.scalars(select(ContentItem).where(ContentItem.id.in_(content_ids)))
    )
    rematch = match_contents_to_topics(session, contents, topics=topics)
    session.commit()
    processed = 0
    for topic in topics:
        articles = _review_articles(session, topic, content_ids, limit=max_per_topic)
        if not articles:
            continue
        try:
            batch, _cache_hit, _usage = process_topic_contents(
                session, topic, articles, client
            )
            processed += len(batch.items)
        except Exception:
            logger.exception("topic llm refine failed topic_id=%s", topic.id)
    return {
        "topics": len(topics),
        "compiled_intents": compiled,
        "rematch": rematch,
        "processed": processed,
    }


def distribute_crawl_run(session: Session, run_id: int) -> dict:
    run = session.get(CrawlRun, run_id)
    if run is None or run.status not in {"succeeded", "partial", "unchanged"}:
        return {"skipped": "run_not_distributable"}
    contents = [
        item for item in contents_from_crawl_run(session, run_id) if is_reader_eligible(item)
    ]
    if not contents:
        return {"contents": 0, "topics": 0}
    stats = match_contents_to_topics(session, contents)
    session.commit()
    editorial_stats = _ensure_reader_editorials(session, [item.id for item in contents])
    llm_stats: dict = {}
    if run.trigger == "manual":
        llm_stats = refine_topic_matches_with_llm(session, [item.id for item in contents])
    cluster_stats: dict = {}
    try:
        result = refresh_event_clusters(session)
        session.commit()
        cluster_stats = {
            "run_id": result.run_id,
            "event_count": result.event_count,
            "created": result.created_event_count,
            "reused_run": result.reused_run,
        }
    except Exception:
        logger.exception("event clustering failed run_id=%s", run_id)
        session.rollback()
        cluster_stats = {"error": True}
    return {**stats, "editorials": editorial_stats, "llm": llm_stats, "events": cluster_stats}


def distribute_recent_pool(session: Session, *, days: int = 7, refine: bool = True) -> dict:
    contents = [
        item for item in recent_reader_contents(session, days=days) if is_reader_eligible(item)
    ]
    if not contents:
        return {"contents": 0, "topics": 0}
    stats = match_contents_to_topics(session, contents)
    session.commit()
    editorial_stats = _ensure_reader_editorials(
        session, [item.id for item in contents], limit=READER_CRAWL_LIMIT
    )
    llm_stats: dict = {}
    if refine:
        llm_stats = refine_topic_matches_with_llm(session, [item.id for item in contents])
    cluster_stats: dict = {}
    try:
        result = refresh_event_clusters(session)
        session.commit()
        cluster_stats = {
            "run_id": result.run_id,
            "event_count": result.event_count,
            "created": result.created_event_count,
            "reused_run": result.reused_run,
        }
    except Exception:
        logger.exception("event clustering failed for recent pool")
        session.rollback()
        cluster_stats = {"error": True}
    return {**stats, "editorials": editorial_stats, "llm": llm_stats, "events": cluster_stats}
