from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ContentItem, InterestTopic, TopicMatch, TopicRun


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def main() -> None:
    repaired = excluded = 0
    with SessionLocal() as db:
        topics = list(db.scalars(select(InterestTopic).order_by(InterestTopic.id)))
        for topic in topics:
            runs = list(
                db.scalars(
                    select(TopicRun)
                    .where(
                        TopicRun.topic_id == topic.id,
                        TopicRun.stage == "firecrawl_discovery",
                        TopicRun.status.in_(("succeeded", "partial")),
                    )
                    .order_by(TopicRun.started_at, TopicRun.id)
                )
            )
            matches = list(
                db.scalars(
                    select(TopicMatch)
                    .where(TopicMatch.topic_id == topic.id)
                    .order_by(TopicMatch.id)
                )
            )
            for match in matches:
                signals = dict(match.matched_signals or {})
                content = db.get(ContentItem, match.content_item_id)
                if content is None:
                    continue
                matching_run = next(
                    (
                        run
                        for run in runs
                        if content.id in ((run.output or {}).get("content_ids") or [])
                    ),
                    None,
                )
                if matching_run is None:
                    end_at = _utc(topic.created_at)
                    mode = "shared_pool"
                    days = 7
                else:
                    end_at = _utc(matching_run.finished_at or matching_run.started_at)
                    mode = "initial_7d" if matching_run.id == runs[0].id else "incremental_1d"
                    days = 7 if mode == "initial_7d" else 1
                start_at = end_at - timedelta(days=days)
                published_at = _utc(content.published_at) if content.published_at else None
                admitted = bool(published_at and start_at <= published_at <= end_at)
                signals["collection_window"] = {
                    "schema_version": "collection-window.v2",
                    "mode": mode,
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat(),
                    "published_at": published_at.isoformat() if published_at else None,
                    "admitted": admitted,
                }
                match.matched_signals = signals
                if not admitted:
                    match.decision = "exclude"
                    match.score = 0.0
                    reason = (
                        "missing_published_at"
                        if published_at is None
                        else "outside_collection_window"
                    )
                    match.reasons = list(dict.fromkeys([*(match.reasons or []), reason]))
                    excluded += 1
                else:
                    match.reasons = [
                        reason
                        for reason in (match.reasons or [])
                        if reason not in {"missing_published_at", "outside_collection_window"}
                    ]
                    if (
                        "firecrawl_discovery" in match.reasons
                        and "excluded_keyword" not in match.reasons
                        and "llm_topic_relevance" not in match.reasons
                    ):
                        match.decision = "include"
                        match.score = max(match.score, 0.65)
                repaired += 1
        db.commit()
    print(f"repaired={repaired}")
    print(f"excluded={excluded}")


if __name__ == "__main__":
    main()
