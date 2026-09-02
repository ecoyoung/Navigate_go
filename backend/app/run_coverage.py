from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def publication_timezone_for_source(source) -> str | None:
    config = source.parser_config or {}
    if config.get("publication_date_mode") != "previous_day":
        return None
    return str(config.get("publication_timezone") or "Asia/Shanghai")


def previous_coverage_date(reference_time: datetime, timezone_name: str) -> date:
    reference = (
        reference_time
        if reference_time.tzinfo is not None
        else reference_time.replace(tzinfo=UTC)
    )
    return reference.astimezone(ZoneInfo(timezone_name)).date() - timedelta(days=1)


def resolve_run_coverage(
    source,
    *,
    reference_time: datetime,
    coverage_date: date | None = None,
    publication_timezone: str | None = None,
) -> tuple[date | None, str | None]:
    source_timezone = publication_timezone_for_source(source)
    timezone_name = publication_timezone or source_timezone
    if coverage_date is not None and timezone_name is None:
        raise ValueError("coverage_date_requires_previous_day_source")
    if timezone_name is None:
        return None, None
    timezone = ZoneInfo(timezone_name)
    reference = (
        reference_time
        if reference_time.tzinfo is not None
        else reference_time.replace(tzinfo=UTC)
    )
    if coverage_date is not None and coverage_date >= reference.astimezone(timezone).date():
        raise ValueError("coverage_date_must_be_before_run_date")
    return coverage_date or previous_coverage_date(reference_time, timezone_name), timezone_name
