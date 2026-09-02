"""Refresh incomplete topic-discovery content through the direct web parser."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.models import ContentItem, TopicMatch, TopicSourceCandidate
from app.topic_discovery import enrich_discovered_content_from_web


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(ContentItem, TopicSourceCandidate)
                .join(TopicMatch, TopicMatch.content_item_id == ContentItem.id)
                .join(
                    TopicSourceCandidate,
                    (TopicSourceCandidate.topic_id == TopicMatch.topic_id)
                    & (TopicSourceCandidate.source_id == ContentItem.source_id),
                )
                .where(
                    TopicMatch.topic_id == args.topic_id,
                    TopicMatch.decision == "include",
                    ContentItem.published_at.is_(None),
                )
                .order_by(ContentItem.id)
                .limit(args.limit)
            )
        )
        refreshed = dated = 0
        for content, candidate in rows:
            updated = enrich_discovered_content_from_web(db, content=content, candidate=candidate)
            db.commit()
            refreshed += 1
            dated += int(updated.published_at is not None)
            print(f"content_id={updated.id} dated={updated.published_at is not None}")
    print(f"refreshed={refreshed} dated={dated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
