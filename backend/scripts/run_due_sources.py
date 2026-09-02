import argparse
import asyncio

from app.channel_adapters import crawl_source
from app.crawl_scheduler import create_due_runs, due_sources
from app.database import SessionLocal


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
    return len(scheduled)


async def watch(limit: int, poll_seconds: int) -> None:
    while True:
        count = await run(limit, dry_run=False)
        print(f"Scheduled sources: {count}")
        await asyncio.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl enabled sources whose interval is due")
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
