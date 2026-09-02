"""Backfill the audited topic-discovery window marker without network access."""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.models import ContentItem, TopicMatch


def main() -> int:
    updated = 0
    with SessionLocal() as db:
        for content in db.scalars(
            select(ContentItem)
            .join(TopicMatch, TopicMatch.content_item_id == ContentItem.id)
            .where(TopicMatch.decision == "include", ContentItem.published_at.is_not(None))
        ):
            warnings = list((content.quality or {}).get("validation_warnings") or [])
            if "collection_window:v1" in warnings:
                continue
            content.quality = {
                **(content.quality or {}),
                "validation_warnings": [*warnings, "collection_window:v1"],
            }
            updated += 1
        db.commit()
    print(f"updated={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
