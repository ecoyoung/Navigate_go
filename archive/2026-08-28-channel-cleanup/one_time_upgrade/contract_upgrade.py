import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ContentItem, PageSnapshot, RawItem, Source
from .normalization import normalize_url
from .web_ingestion import ingest_article


@dataclass(frozen=True)
class ContractUpgradeSummary:
    scanned: int = 0
    upgraded: int = 0
    skipped: int = 0
    external_ids_bound: int = 0
    snapshots_bound: int = 0


def load_archive_external_ids(path: Path) -> dict[str, str]:
    external_ids: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") != "ok":
            continue
        url = str(row.get("url") or "").strip()
        work_uuid = str(row.get("workUuid") or "").strip()
        if not url or not work_uuid:
            raise ValueError(f"line {line_number}: ok row requires url and workUuid")
        normalized = normalize_url(url)
        if normalized in external_ids and external_ids[normalized] != work_uuid:
            raise ValueError(f"line {line_number}: conflicting workUuid for {normalized}")
        external_ids[normalized] = work_uuid
    return external_ids


def _infer_snapshot_id(
    session: Session, content: ContentItem, raw: RawItem, source: Source
) -> int | None:
    if raw.page_snapshot_id is not None:
        return raw.page_snapshot_id
    snapshots = list(
        session.scalars(
            select(PageSnapshot).where(PageSnapshot.crawl_run_id == raw.crawl_run_id)
        )
    )
    target_urls = {
        normalize_url(value)
        for value in (content.original_url, content.canonical_url)
        if value
    }
    exact = [snapshot for snapshot in snapshots if normalize_url(snapshot.url) in target_urls]
    if len(exact) == 1:
        return exact[0].id
    if source.channel_type == "rss":
        feeds = [snapshot for snapshot in snapshots if snapshot.page_type == "feed"]
        if len(feeds) == 1:
            return feeds[0].id
    return None


def _source_external_id(source: Source) -> str | None:
    config = source.parser_config or {}
    value = source.source_external_id or config.get("account_fakeid")
    return str(value).strip() if value else None


def upgrade_current_contracts(
    session: Session, *, archive_external_ids: dict[str, str]
) -> ContractUpgradeSummary:
    contents = list(session.scalars(select(ContentItem).order_by(ContentItem.id)))
    upgraded = skipped = external_ids_bound = snapshots_bound = 0
    for content in contents:
        source = session.get(Source, content.source_id)
        raw = session.get(RawItem, content.raw_item_id)
        if not source or not raw:
            raise ValueError(f"content {content.id}: missing source or raw item")
        source.source_external_id = _source_external_id(source)
        canonical = normalize_url(content.canonical_url or content.original_url or "")
        external_id = content.external_id or archive_external_ids.get(canonical)
        snapshot_id = _infer_snapshot_id(session, content, raw, source)
        warnings = ["contract_upgrade_from_v1"]
        if not content.media:
            warnings.append("media_not_captured")
        if snapshot_id is None:
            warnings.append("snapshot_unavailable")
        completeness = str((source.parser_config or {}).get("content_completeness") or "")
        if not completeness:
            completeness = "partial" if content.access_level == "partial" else "unknown"
        extracted = {
            "title": content.title,
            "canonical_url": canonical,
            "original_url": content.original_url or canonical,
            "author": content.author,
            "published_at": content.published_at,
            "updated_at": content.source_updated_at,
            "external_item_id": external_id,
            "body": content.body or "",
            "description": content.excerpt,
            "content_type": content.content_type,
            "topics": content.topics or [],
            "media": content.media or [],
            "content_completeness": completeness,
            "validation_warnings": warnings,
        }
        result = ingest_article(session, source, raw_run(session, raw), extracted, snapshot_id)
        upgraded += int(result in {"new", "updated"})
        skipped += int(result == "skipped")
        external_ids_bound += int(bool(external_id))
        snapshots_bound += int(snapshot_id is not None)
    return ContractUpgradeSummary(
        scanned=len(contents),
        upgraded=upgraded,
        skipped=skipped,
        external_ids_bound=external_ids_bound,
        snapshots_bound=snapshots_bound,
    )


def raw_run(session: Session, raw: RawItem):
    from .models import CrawlRun

    run = session.get(CrawlRun, raw.crawl_run_id)
    if not run:
        raise ValueError(f"raw item {raw.id}: crawl run missing")
    return run
