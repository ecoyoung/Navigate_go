import hashlib
import json
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import NormalizedArticle
from .models import ContentItem, CrawlRun, RawItem, Source, utcnow

REDFOX_PROVIDERS = frozenset({"redfox", "redfox_archive"})
REDFOX_PUBLICATION_TIMEZONE = "Asia/Shanghai"
TIMEZONE_REPAIR_VERSION = "timezone-repair.v1"


@dataclass(frozen=True)
class TimezoneRepairResult:
    candidates: int
    sources: int
    inserted_raw: int = 0
    reused_raw: int = 0
    updated_content: int = 0


def _naive_published_at(payload: dict) -> datetime | None:
    value = payload.get("published_at")
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = date_parser.parse(value)
    return parsed if parsed.tzinfo is None else None


def _payload_sha(payload: dict) -> str:
    semantic = {key: value for key, value in payload.items() if key != "captured_at"}
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _redfox_timezone_candidates(
    session: Session,
) -> list[tuple[ContentItem, RawItem, Source, datetime]]:
    rows = session.execute(
        select(ContentItem, RawItem, Source)
        .join(RawItem, ContentItem.raw_item_id == RawItem.id)
        .join(Source, ContentItem.source_id == Source.id)
        .order_by(ContentItem.id)
    )
    candidates = []
    for content, raw, source in rows:
        if raw.payload.get("provider") not in REDFOX_PROVIDERS:
            continue
        published_at = _naive_published_at(raw.payload)
        if published_at is not None:
            candidates.append((content, raw, source, published_at))
    return candidates


def repair_redfox_publication_timezone(
    session: Session,
    *,
    apply: bool = False,
    expected_count: int | None = None,
) -> TimezoneRepairResult:
    """Append corrected Raw versions and repoint the current content projection.

    Legacy RedFox timestamps were serialized without an offset even though their
    source contract is Asia/Shanghai. This repair localizes that wall clock, then
    stores the instant as UTC. It never mutates or deletes an existing RawItem.
    """

    candidates = _redfox_timezone_candidates(session)
    if expected_count is not None and len(candidates) != expected_count:
        raise ValueError(
            f"redfox_timezone_candidate_count:{len(candidates)}!={expected_count}"
        )
    source_counts = Counter(content.source_id for content, _, _, _ in candidates)
    result = TimezoneRepairResult(
        candidates=len(candidates),
        sources=len(source_counts),
    )
    if not apply or not candidates:
        return result

    repair_started_at = utcnow()
    repair_runs: dict[int, CrawlRun] = {}
    for source_id, count in source_counts.items():
        run = CrawlRun(
            source_id=source_id,
            trigger="repair",
            coverage_date=None,
            publication_timezone=REDFOX_PUBLICATION_TIMEZONE,
            status="succeeded",
            started_at=repair_started_at,
            finished_at=repair_started_at,
            fetched_count=count,
            updated_count=count,
        )
        session.add(run)
        repair_runs[source_id] = run
    session.flush()

    timezone = ZoneInfo(REDFOX_PUBLICATION_TIMEZONE)
    inserted_raw = reused_raw = updated_content = 0
    for content, old_raw, _, naive_time in candidates:
        corrected_time = naive_time.replace(tzinfo=timezone).astimezone(UTC)
        payload = deepcopy(old_raw.payload)
        payload["published_at"] = corrected_time.isoformat()
        contract = NormalizedArticle.model_validate(payload)
        if contract.content_hash != content.content_hash:
            raise ValueError(f"content_hash_changed:{content.id}")
        payload_sha = _payload_sha(payload)
        corrected_raw = session.scalar(
            select(RawItem).where(
                RawItem.source_id == old_raw.source_id,
                RawItem.identity_key == old_raw.identity_key,
                RawItem.payload_sha256 == payload_sha,
            )
        )
        if corrected_raw is None:
            corrected_raw = RawItem(
                source_id=old_raw.source_id,
                crawl_run_id=repair_runs[old_raw.source_id].id,
                page_snapshot_id=old_raw.page_snapshot_id,
                external_id=old_raw.external_id,
                identity_key=old_raw.identity_key,
                original_url=old_raw.original_url,
                canonical_url=old_raw.canonical_url,
                payload=payload,
                payload_sha256=payload_sha,
            )
            session.add(corrected_raw)
            session.flush()
            inserted_raw += 1
        else:
            reused_raw += 1
        content.raw_item_id = corrected_raw.id
        content.published_at = corrected_time
        content.normalizer_version = TIMEZONE_REPAIR_VERSION
        updated_content += 1

    session.flush()
    return TimezoneRepairResult(
        candidates=len(candidates),
        sources=len(source_counts),
        inserted_raw=inserted_raw,
        reused_raw=reused_raw,
        updated_content=updated_content,
    )
