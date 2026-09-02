from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CrawlRun, Source
from .web_ingestion import create_crawl_run

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN = timedelta(hours=6)


@dataclass(frozen=True)
class ScheduledRun:
    source_id: int
    run_id: int


@dataclass(frozen=True)
class SourceHealth:
    last_run_status: str | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    circuit_open: bool = False
    last_finished_at: datetime | None = None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def source_is_due(source: Source, last_started_at: datetime | None, now: datetime) -> bool:
    if not source.is_enabled:
        return False
    strategy = str((source.parser_config or {}).get("crawl_strategy") or "")
    if strategy in {"blocked", "unavailable"}:
        return False
    if last_started_at is None:
        return True
    return _as_utc(last_started_at) + timedelta(seconds=source.fetch_interval_seconds) <= now


def _recent_runs(session: Session, source_id: int, limit: int = 5) -> list[CrawlRun]:
    return list(
        session.scalars(
            select(CrawlRun)
            .where(CrawlRun.source_id == source_id)
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
            .limit(limit)
        )
    )


def consecutive_failures(runs: list[CrawlRun]) -> int:
    count = 0
    for run in runs:
        if run.status == "failed":
            count += 1
            continue
        break
    return count


def circuit_is_open(
    runs: list[CrawlRun],
    now: datetime,
    *,
    threshold: int = CIRCUIT_FAILURE_THRESHOLD,
    cooldown: timedelta = CIRCUIT_COOLDOWN,
) -> bool:
    if consecutive_failures(runs) < threshold:
        return False
    latest = runs[0]
    last_finished = _as_utc(latest.finished_at or latest.started_at)
    return last_finished + cooldown > now


def summarize_source_health(
    runs: list[CrawlRun], now: datetime | None = None
) -> SourceHealth:
    if not runs:
        return SourceHealth()
    latest = runs[0]
    effective_now = _as_utc(now or datetime.now(UTC))
    return SourceHealth(
        last_run_status=latest.status,
        last_error_code=latest.error_code,
        consecutive_failures=consecutive_failures(runs),
        circuit_open=circuit_is_open(runs, effective_now),
        last_finished_at=latest.finished_at,
    )


def source_health_map(
    session: Session, sources: list[Source], *, now: datetime | None = None
) -> dict[int, SourceHealth]:
    return {
        source.id: summarize_source_health(_recent_runs(session, source.id), now=now)
        for source in sources
    }


def due_sources(session: Session, *, now: datetime | None = None, limit: int = 100) -> list[Source]:
    effective_now = _as_utc(now or datetime.now(UTC))
    sources = list(session.scalars(select(Source).order_by(Source.id)))
    due: list[Source] = []
    for source in sources:
        recent_runs = _recent_runs(session, source.id)
        latest_run = recent_runs[0] if recent_runs else None
        if latest_run and latest_run.status in {"pending", "running"}:
            continue
        if circuit_is_open(recent_runs, effective_now):
            continue
        last_started = latest_run.started_at if latest_run else None
        if source_is_due(source, last_started, effective_now):
            due.append(source)
        if len(due) >= limit:
            break
    return due


def recover_stale_runs(
    session: Session,
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=1),
) -> int:
    effective_now = _as_utc(now or datetime.now(UTC))
    recovered = 0
    active_runs = session.scalars(
        select(CrawlRun).where(CrawlRun.status.in_(("pending", "running")))
    )
    for run in active_runs:
        if _as_utc(run.started_at) + stale_after > effective_now:
            continue
        run.status = "failed"
        run.error_count = max(run.error_count, 1)
        run.error_code = "stale_run_recovered"
        run.error_summary = "采集进程未正常结束，调度器已回收陈旧任务。"
        run.finished_at = effective_now
        recovered += 1
    if recovered:
        session.commit()
    return recovered


def create_due_runs(
    session: Session, *, now: datetime | None = None, limit: int = 100
) -> list[ScheduledRun]:
    effective_now = _as_utc(now or datetime.now(UTC))
    recover_stale_runs(session, now=effective_now)
    scheduled: list[ScheduledRun] = []
    for source in due_sources(session, now=effective_now, limit=limit):
        run, created = create_crawl_run(
            session, source, trigger="schedule", reference_time=effective_now
        )
        if created:
            scheduled.append(ScheduledRun(source_id=source.id, run_id=run.id))
    return scheduled
