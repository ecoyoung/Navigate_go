import argparse
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.database import SessionLocal
from app.firecrawl import SCRAPE_BATCH_MAX, FirecrawlClient, FirecrawlError
from app.models import InterestTopic, TopicRun, TopicSourceCandidate
from app.topic_discovery import (
    attach_discovered_match,
    content_is_metadata_only,
    existing_content_for_url,
    ingest_discovered_metadata,
    ingest_discovered_page,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persist already-discovered topic candidates into the shared content pool."
    )
    parser.add_argument("--topic-id", required=True, type=int)
    parser.add_argument("--max-pages", type=int, default=SCRAPE_BATCH_MAX)
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Persist search metadata without making Firecrawl Scrape calls.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    requested = max(1, min(args.max_pages, SCRAPE_BATCH_MAX))
    client = None if args.metadata_only else FirecrawlClient.from_environment()
    with SessionLocal() as db:
        topic = db.get(InterestTopic, args.topic_id)
        if topic is None:
            raise SystemExit("topic_not_found")
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        utc_midnight = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC)
        used_today = int(
            db.scalar(
                select(func.coalesce(func.sum(TopicRun.firecrawl_credits_used), 0)).where(
                    TopicRun.topic_id == topic.id,
                    TopicRun.started_at >= utc_midnight,
                )
            )
            or 0
        )
        allowance = min(requested, max(0, topic.daily_credit_limit - used_today))
        if allowance <= 0 and not args.metadata_only:
            raise SystemExit("topic_daily_credit_limit_reached")
        run = TopicRun(
            topic_id=topic.id,
            stage="firecrawl_backfill",
            status="running",
            firecrawl_credits_reserved=0 if args.metadata_only else allowance,
        )
        db.add(run)
        db.flush()
        db.commit()
        collection_window_end = run.started_at
        collection_window_start = collection_window_end - timedelta(days=7)
        candidates = list(
            db.scalars(
                select(TopicSourceCandidate)
                .where(TopicSourceCandidate.topic_id == topic.id)
                .order_by(TopicSourceCandidate.id.asc())
            )
        )
        calls = fetched = ingested = matched = reused = 0
        errors: list[dict] = []
        for candidate in candidates:
            content = existing_content_for_url(db, candidate.canonical_url)
            if content is not None and (
                args.metadata_only or not content_is_metadata_only(content)
            ):
                candidate.source_id = content.source_id
                attach_discovered_match(
                    db,
                    topic=topic,
                    content=content,
                    candidate=candidate,
                    window_start=collection_window_start,
                    window_end=collection_window_end,
                )
                reused += 1
                matched += 1
                db.commit()
                continue
            if args.metadata_only:
                content, result = ingest_discovered_metadata(
                    db, candidate=candidate
                )
                attach_discovered_match(
                    db,
                    topic=topic,
                    content=content,
                    candidate=candidate,
                    window_start=collection_window_start,
                    window_end=collection_window_end,
                )
                ingested += int(result in {"new", "updated"})
                matched += 1
                db.commit()
                continue
            if calls >= allowance:
                break
            calls += 1
            try:
                scraped = client.scrape(candidate.canonical_url)
                content, result = ingest_discovered_page(
                    db,
                    candidate=candidate,
                    search_item={
                        "url": candidate.canonical_url,
                        "title": candidate.title,
                        "description": candidate.description,
                    },
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
                fetched += 1
                ingested += int(result in {"new", "updated"})
                matched += 1
                db.commit()
            except FirecrawlError as exc:
                db.rollback()
                errors.append(
                    {"candidate_id": candidate.id, "error_code": str(exc)}
                )
        run.status = "partial" if errors else "succeeded"
        run.search_candidates = len(candidates)
        run.fetched_pages = fetched
        run.matched_items = matched
        run.firecrawl_credits_used = calls
        run.output = {
            "ingested_count": ingested,
            "reused_count": reused,
            "errors": errors,
        }
        run.finished_at = datetime.now(UTC)
        topic.updated_at = datetime.now(UTC)
        db.commit()
        print(f"topic_id={topic.id}")
        print(f"candidate_count={len(candidates)}")
        print(f"scrape_calls={calls} fetched_pages={fetched}")
        print(f"ingested_count={ingested} reused_count={reused} matched_count={matched}")
        print(f"error_count={len(errors)} credits_used={calls}")


if __name__ == "__main__":
    main()
