"""Backfill non-destructive reader-quality warnings without fetching any URL."""

from sqlalchemy import select

from app.content_quality import MIN_PARTIAL_BODY_CHARS
from app.database import SessionLocal
from app.models import ContentItem, Source


def main() -> None:
    updated = 0
    with SessionLocal() as session:
        rows = session.execute(
            select(ContentItem, Source).join(Source, Source.id == ContentItem.source_id)
        )
        for content, source in rows:
            quality = dict(content.quality or {})
            warnings = list(quality.get("validation_warnings") or [])
            required = []
            if content.published_at is None:
                required.append("missing_published_at")
            if (
                not quality.get("metadata_only")
                and len(content.body or "") < MIN_PARTIAL_BODY_CHARS
            ):
                required.append("insufficient_body_for_reader")
            if source.channel_type == "api" and content.published_at is None:
                required.append("api_date_mapping_missing")
            merged = list(dict.fromkeys([*warnings, *required]))
            if merged != warnings:
                content.quality = {**quality, "validation_warnings": merged}
                updated += 1
        session.commit()
    print(f"quality backfill complete: updated={updated}")


if __name__ == "__main__":
    main()
