"""Regenerate Chinese reader cards for every displayed catalog article."""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.content_quality import is_reader_eligible
from app.database import SessionLocal
from app.llm_editorial import (
    READER_EDITORIAL_DAILY_LIMIT,
    DeepSeekClient,
    contents_missing_editorials,
    ensure_reader_editorials,
)
from app.models import ContentItem, Source
from app.secrets import MissingSecretError, require_secret


def _eligible(session) -> list[tuple[ContentItem, Source]]:
    rows = list(
        session.execute(
            select(ContentItem, Source)
            .join(Source, Source.id == ContentItem.source_id)
            .where(ContentItem.published_at.is_not(None))
            .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
        )
    )
    return [(content, source) for content, source in rows if is_reader_eligible(content)]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rewrite reader cards for all displayed catalog content"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--limit", type=int, default=READER_EDITORIAL_DAILY_LIMIT)
    args = parser.parse_args()
    try:
        client = DeepSeekClient(api_key=require_secret("DEEPSEEK_API_KEY"))
    except MissingSecretError as exc:
        raise SystemExit(str(exc)) from exc

    with SessionLocal() as session:
        while True:
            eligible = _eligible(session)
            missing = contents_missing_editorials(session, eligible)
            print(
                f"eligible={len(eligible)} missing={len(missing)} refresh={args.refresh}",
                flush=True,
            )
            if not eligible:
                break
            if not args.refresh and not missing:
                break
            stats = ensure_reader_editorials(
                session,
                eligible,
                client,
                limit=max(1, args.limit),
                refresh=args.refresh,
            )
            print(stats, flush=True)
            if stats.get("skipped") == "daily_limit":
                break
            if stats.get("processed", 0) == 0:
                break
            if args.refresh:
                break


if __name__ == "__main__":
    main()
