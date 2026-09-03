import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.channel_adapters import crawl_source
from app.crawl_scheduler import (
    SCHEDULE_TIMEZONE,
    create_due_runs,
    due_sources,
    next_schedule_slot_start,
)
from app.database import SessionLocal
from app.llm_editorial import READER_BACKFILL_LIMIT
from app.models import ContentItem, RawItem
from app.topic_distribution import backfill_reader_editorials, refine_topic_matches_with_llm


async def run(limit: int, dry_run: bool) -> int:
    with SessionLocal() as session:
        if dry_run:
            sources = due_sources(session, limit=limit)
            for source in sources:
                print(f"{source.id}\t{source.channel_type}\t{source.name}")
            return len(sources)
        scheduled = create_due_runs(session, limit=limit)
    for item in scheduled:
        await crawl_source(SessionLocal, item.source_id, item.run_id)
    run_ids = [item.run_id for item in scheduled]
    with SessionLocal() as session:
        if run_ids:
            content_ids = list(
                session.scalars(
                    select(ContentItem.id)
                    .join(RawItem, RawItem.id == ContentItem.raw_item_id)
                    .where(RawItem.crawl_run_id.in_(run_ids))
                )
            )
            if content_ids:
                refine_topic_matches_with_llm(session, content_ids)
        editorial_stats = backfill_reader_editorials(session, limit=READER_BACKFILL_LIMIT)
        print(f"Reader editorials: {editorial_stats}", flush=True)
    return len(scheduled)


async def watch(limit: int, poll_seconds: int) -> None:
    while True:
        count = await run(limit, dry_run=False)
        now = datetime.now(UTC)
        nxt = next_schedule_slot_start(now).astimezone(SCHEDULE_TIMEZONE).isoformat()
        print(f"Scheduled sources: {count}; next Beijing slot: {nxt}", flush=True)
        await asyncio.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl enabled catalog sources at Beijing 09:30 and 18:00"
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.dry_run and args.watch:
        parser.error("--dry-run cannot be combined with --watch")
    if args.watch:
        asyncio.run(watch(max(1, min(args.limit, 500)), max(args.poll_seconds, 10)))
        return
    count = asyncio.run(run(max(1, min(args.limit, 500)), args.dry_run))
    print(f"Due sources: {count}")


if __name__ == "__main__":
    main()
