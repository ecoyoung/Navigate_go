from pathlib import Path

from sqlalchemy import select

from app.catalog import load_catalog
from app.channel_adapters import canonicalize_parser_config
from app.database import SessionLocal
from app.models import Source
from app.normalization import normalize_url

CATALOG_PATH = Path(__file__).parents[1] / "config" / "sites.json"
PROCESSING_KEYS = {
    "scope_mode",
    "industry_keywords",
    "event_keywords",
}


def main() -> None:
    catalog = load_catalog(CATALOG_PATH)
    created = updated = 0
    with SessionLocal() as session:
        for item in catalog:
            start_url = str(item.start_url)
            normalized = normalize_url(start_url)
            source = session.scalar(select(Source).where(Source.catalog_id == item.id))
            name_source = session.scalar(
                select(Source).where(Source.name == item.name).order_by(Source.id).limit(1)
            )
            source = source or name_source
            if source is None:
                url_sources = list(
                    session.scalars(
                        select(Source)
                        .where(
                            Source.channel_type == item.resolved_channel_type,
                            Source.normalized_start_url == normalized,
                        )
                        .limit(2)
                    )
                )
                source = url_sources[0] if len(url_sources) == 1 else None
            parser_config = canonicalize_parser_config(
                item.resolved_channel_type,
                {
                    key: value
                    for key, value in item.parser_config.items()
                    if key not in PROCESSING_KEYS
                },
            )
            processing_config = {
                **{
                    key: value
                    for key, value in item.parser_config.items()
                    if key in PROCESSING_KEYS
                },
                **item.processing_config,
            }
            values = {
                "catalog_id": item.id,
                "name": item.name,
                "channel_type": item.resolved_channel_type,
                "start_url": start_url,
                "normalized_start_url": normalized,
                "fetch_interval_seconds": item.fetch_interval_seconds,
                "parser_config": {
                    **parser_config,
                    "crawl_strategy": item.crawl_strategy,
                    "skip_reason": item.skip_reason,
                },
                "processing_config": processing_config,
                "source_region": item.source_region,
                "source_type": item.source_type,
                "default_language": item.default_language,
                "source_tags": item.source_tags,
                "source_external_id": item.source_external_id,
                "is_enabled": item.is_enabled,
            }
            if source:
                for key, value in values.items():
                    setattr(source, key, value)
                updated += 1
            else:
                session.add(Source(**values))
                created += 1
        session.commit()
    print(f"Catalog synced: {created} created, {updated} updated, {len(catalog)} total")


if __name__ == "__main__":
    main()
