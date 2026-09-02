import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select

from app.catalog import load_catalog
from app.channel_adapters import (
    ADAPTERS,
    ChannelConfigurationError,
    crawl_source,
    validate_channel_config,
)
from app.crawl_scheduler import (
    create_due_runs,
    due_sources,
    recover_stale_runs,
    summarize_source_health,
)
from app.execution_engines import (
    ENGINE_KEYS,
    ENGINES,
    ExecutionEngineConfigurationError,
    resolve_execution_engine_key,
)
from app.models import CrawlRun, Source
from app.web_ingestion import save_response_snapshot


def make_source(
    session,
    *,
    name: str,
    channel_type: str = "web",
    parser_config: dict | None = None,
    interval: int = 3600,
    enabled: bool = True,
) -> Source:
    source = Source(
        name=name,
        channel_type=channel_type,
        start_url=f"https://example.com/{name}",
        normalized_start_url=f"https://example.com/{name}",
        fetch_interval_seconds=interval,
        parser_config=parser_config or {},
        is_enabled=enabled,
    )
    session.add(source)
    session.commit()
    return source


def test_catalog_resolves_channel_types_for_existing_sources():
    catalog = load_catalog(Path(__file__).parents[1] / "config" / "sites.json")
    channels = {item.id: item.resolved_channel_type for item in catalog}
    enabled = {item.id: item.is_enabled for item in catalog}

    assert channels["beautymatter"] == "web"
    assert channels["36kr"] == "rss"
    assert channels["c2cc"] == "api"
    assert channels["global_cosmetics_news"] == "web"
    assert channels["huazhuangpinbao_wechat"] == "third_party_feed"
    assert enabled["huazhuangpinbao_wechat"] is False


def test_all_channel_adapters_validate_their_contract():
    assert set(ADAPTERS) == {"web", "rss", "api", "third_party_feed"}
    validate_channel_config("web", {})
    validate_channel_config("rss", {"discovery_method": "feed"})
    validate_channel_config(
        "rss", {"discovery_method": "feed", "publication_timezone": "Europe/Paris"}
    )
    validate_channel_config("api", {"discovery_method": "json"})
    validate_channel_config(
        "third_party_feed",
        {
            "provider": "redfox",
            "discovery_method": "json",
            "discovery_url": "https://feed.example.com/account.json",
            "detail_url": "https://feed.example.com/detail.json",
            "publication_date_mode": "previous_day",
            "publication_timezone": "Asia/Shanghai",
        },
    )

    try:
        validate_channel_config(
            "third_party_feed",
            {
                "provider": "redfox",
                "discovery_method": "json",
                "discovery_url": "https://feed.example.com/account.json",
                "detail_url": "https://feed.example.com/detail.json",
                "publication_date_mode": "previous_day",
                "publication_timezone": "Shanghai/Typo",
            },
        )
    except ChannelConfigurationError as exc:
        assert "valid IANA timezone" in str(exc)
    else:
        raise AssertionError("invalid RedFox publication timezone was accepted")

    try:
        validate_channel_config(
            "rss",
            {"discovery_method": "feed", "publication_timezone": "Paris/Typo"},
        )
    except ChannelConfigurationError as exc:
        assert "valid IANA timezone" in str(exc)
    else:
        raise AssertionError("invalid generic publication timezone was accepted")

    try:
        validate_channel_config(
            "third_party_feed",
            {
                "provider": "redfox",
                "discovery_method": "json",
                "discovery_url": "https://feed.example.com/account.json",
                "detail_url": "https://feed.example.com/detail.json",
                "publication_date_mode": "previous_day",
                "publication_timezone": "Asia/Shanghai",
                "skip_first_article": True,
            },
        )
    except ChannelConfigurationError as exc:
        assert "publication_date_mode" in str(exc) or "fixed-position" in str(exc)
    else:
        raise AssertionError("legacy RedFox position selection was accepted")

    try:
        validate_channel_config("rss", {"discovery_method": "json"})
    except ChannelConfigurationError as exc:
        assert "requires discovery_method" in str(exc)
    else:
        raise AssertionError("mismatched channel configuration was accepted")


def test_execution_engine_registry_has_one_engine_per_pipeline_engine():
    assert tuple(ENGINES) == ENGINE_KEYS
    assert resolve_execution_engine_key("web", {}) == "static_http"
    assert (
        resolve_execution_engine_key("web", {"discovery_method": "sitemap"})
        == "sitemap_http"
    )
    assert (
        resolve_execution_engine_key("rss", {"discovery_method": "feed"})
        == "feed_direct"
    )
    assert (
        resolve_execution_engine_key("api", {"discovery_method": "json"})
        == "json_api"
    )
    assert (
        resolve_execution_engine_key(
            "third_party_feed",
            {"provider": "redfox", "discovery_method": "json"},
        )
        == "provider_api"
    )

    try:
        resolve_execution_engine_key(
            "web",
            {"discovery_method": "html", "execution_engine": "json_api"},
        )
    except ExecutionEngineConfigurationError as exc:
        assert "conflicts" in str(exc)
    else:
        raise AssertionError("conflicting explicit execution engine was accepted")


def test_channel_adapter_dispatches_through_resolved_engine(session_factory, monkeypatch):
    dispatched: list[str] = []

    async def fake_crawl(_factory, _source_id, _run_id, *, engine):
        dispatched.append(engine.key)

    monkeypatch.setattr("app.execution_engines.web.crawl_http_source", fake_crawl)
    with session_factory() as session:
        source = make_source(
            session,
            name="engine-dispatch",
            channel_type="rss",
            parser_config={
                "discovery_method": "feed",
                "execution_engine": "feed_direct",
            },
        )
        run = CrawlRun(source_id=source.id, trigger="test", status="pending")
        session.add(run)
        session.commit()

        asyncio.run(crawl_source(session_factory, source.id, run.id))

    assert dispatched == ["feed_direct"]


def test_due_scheduler_honors_interval_state_and_access_rules(session_factory):
    now = datetime(2026, 8, 27, 8, tzinfo=UTC)
    with session_factory() as session:
        never_run = make_source(session, name="never")
        recent = make_source(session, name="recent")
        old = make_source(session, name="old")
        make_source(session, name="disabled", enabled=False)
        make_source(
            session,
            name="blocked",
            parser_config={"crawl_strategy": "blocked"},
        )
        active = make_source(session, name="active")
        session.add_all(
            [
                CrawlRun(
                    source_id=recent.id,
                    trigger="schedule",
                    status="succeeded",
                    started_at=now - timedelta(minutes=30),
                ),
                CrawlRun(
                    source_id=old.id,
                    trigger="schedule",
                    status="succeeded",
                    started_at=now - timedelta(hours=2),
                ),
                CrawlRun(
                    source_id=active.id,
                    trigger="schedule",
                    status="running",
                    started_at=now - timedelta(minutes=5),
                ),
            ]
        )
        session.commit()

        assert [source.name for source in due_sources(session, now=now)] == [
            never_run.name,
            old.name,
        ]
        scheduled = create_due_runs(session, now=now)
        assert {item.source_id for item in scheduled} == {never_run.id, old.id}
        assert all(
            trigger == "schedule"
            for trigger in session.scalars(
                select(CrawlRun.trigger).where(CrawlRun.id.in_([item.run_id for item in scheduled]))
            )
        )


def test_scheduled_date_source_freezes_previous_shanghai_day(session_factory):
    now = datetime(2026, 8, 29, 0, tzinfo=UTC)
    with session_factory() as session:
        source = make_source(
            session,
            name="daily-provider",
            channel_type="third_party_feed",
            parser_config={
                "provider": "fixture",
                "publication_date_mode": "previous_day",
                "publication_timezone": "Asia/Shanghai",
            },
        )

        scheduled = create_due_runs(session, now=now)
        run = session.get(CrawlRun, scheduled[0].run_id)

        assert run.source_id == source.id
        assert run.coverage_date.isoformat() == "2026-08-28"
        assert run.publication_timezone == "Asia/Shanghai"
        assert run.started_at.replace(tzinfo=UTC) == now


def test_scheduler_opens_circuit_after_consecutive_failures(session_factory):
    now = datetime(2026, 8, 27, 8, tzinfo=UTC)
    with session_factory() as session:
        healthy = make_source(session, name="healthy", interval=60)
        broken = make_source(session, name="broken", interval=60)
        cooled = make_source(session, name="cooled", interval=60)
        session.add_all(
            [
                CrawlRun(
                    source_id=healthy.id,
                    trigger="schedule",
                    status="succeeded",
                    started_at=now - timedelta(hours=2),
                    finished_at=now - timedelta(hours=2),
                ),
                *[
                    CrawlRun(
                        source_id=broken.id,
                        trigger="schedule",
                        status="failed",
                        error_code="httpx.HTTPStatusError",
                        started_at=now - timedelta(hours=offset),
                        finished_at=now - timedelta(hours=offset),
                    )
                    for offset in (3, 2, 1)
                ],
                *[
                    CrawlRun(
                        source_id=cooled.id,
                        trigger="schedule",
                        status="failed",
                        error_code="TimeoutException",
                        started_at=now - timedelta(hours=offset),
                        finished_at=now - timedelta(hours=offset),
                    )
                    for offset in (20, 14, 8)
                ],
            ]
        )
        session.commit()

        assert [source.name for source in due_sources(session, now=now)] == [
            healthy.name,
            cooled.name,
        ]
        health = summarize_source_health(
            list(
                session.scalars(
                    select(CrawlRun)
                    .where(CrawlRun.source_id == broken.id)
                    .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
                    .limit(5)
                )
            ),
            now=now,
        )
        assert health.circuit_open
        assert health.consecutive_failures == 3
        assert health.last_error_code == "httpx.HTTPStatusError"


def test_unchanged_feed_run_is_healthy_and_breaks_failure_streak(session_factory):
    now = datetime(2026, 8, 27, 8, tzinfo=UTC)
    with session_factory() as session:
        source = make_source(session, name="not-modified", channel_type="rss")
        session.add_all(
            [
                CrawlRun(
                    source_id=source.id,
                    status="failed",
                    started_at=now - timedelta(hours=2),
                    finished_at=now - timedelta(hours=2),
                ),
                CrawlRun(
                    source_id=source.id,
                    status="unchanged",
                    started_at=now - timedelta(hours=1),
                    finished_at=now - timedelta(hours=1),
                ),
            ]
        )
        session.commit()
        runs = list(
            session.scalars(
                select(CrawlRun)
                .where(CrawlRun.source_id == source.id)
                .order_by(CrawlRun.started_at.desc())
            )
        )

        health = summarize_source_health(runs, now=now)

        assert health.consecutive_failures == 0
        assert not health.circuit_open


def test_scheduler_recovers_stale_active_run(session_factory):
    now = datetime(2026, 8, 27, 8, tzinfo=UTC)
    with session_factory() as session:
        source = make_source(session, name="stale", interval=60)
        run = CrawlRun(
            source_id=source.id,
            trigger="schedule",
            status="running",
            started_at=now - timedelta(hours=2),
        )
        session.add(run)
        session.commit()

        assert recover_stale_runs(session, now=now) == 1
        session.refresh(run)
        assert run.status == "failed"
        assert run.error_code == "stale_run_recovered"


def test_snapshot_keeps_response_evidence(session_factory):
    with session_factory() as session:
        source = make_source(session, name="snapshot")
        run = CrawlRun(source_id=source.id, trigger="test", status="running")
        session.add(run)
        session.commit()
        request = httpx.Request("GET", "https://example.com/feed")
        response = httpx.Response(
            503,
            request=request,
            text="maintenance",
            headers={"Content-Type": "text/html", "Set-Cookie": "secret=do-not-store"},
        )

        snapshot = save_response_snapshot(
            session,
            run,
            url=str(request.url),
            page_type="feed",
            request_method="GET",
            response=response,
            error_text="service unavailable",
        )

        assert snapshot.http_status == 503
        assert snapshot.body == "maintenance"
        assert snapshot.response_headers == {"content-type": "text/html"}
        assert "set-cookie" not in snapshot.response_headers
