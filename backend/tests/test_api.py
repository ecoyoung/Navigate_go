from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.auth import create_user
from app.content_processing import PROCESSOR_NAME, PROCESSOR_VERSION
from app.models import ContentItem, ContentProcessingResult, CrawlRun, RawItem, Source

PASSWORD = "Admin-password-2026"


@pytest.fixture
def operator(client, session_factory):
    with session_factory() as db:
        create_user(
            db,
            email="operator@example.com",
            display_name="管理员",
            password=PASSWORD,
            role="admin",
        )
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "operator@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200


def test_health_and_source_lifecycle(client, operator):
    assert client.get("/health/live").json() == {"status": "ok"}
    created = client.post(
        "/api/v1/sources",
        json={
            "name": "Beauty Wire",
            "start_url": "https://example.com/news?utm_source=test",
            "fetch_interval_seconds": 1800,
        },
    )
    assert created.status_code == 201
    source_id = created.json()["id"]
    assert client.get("/api/v1/sources").json()[0]["name"] == "Beauty Wire"
    duplicate = client.post(
        "/api/v1/sources", json={"name": "Duplicate", "start_url": "https://EXAMPLE.com/news"}
    )
    assert duplicate.status_code == 409
    disabled = client.patch(f"/api/v1/sources/{source_id}", json={"is_enabled": False})
    assert disabled.json()["is_enabled"] is False
    assert client.post(f"/api/v1/sources/{source_id}/crawl").status_code == 409


def test_source_validation_and_not_found(client, operator):
    assert client.post("/api/v1/sources", json={"name": "", "start_url": "bad"}).status_code == 422
    assert client.patch("/api/v1/sources/999", json={"is_enabled": False}).status_code == 404


def test_source_registration_validates_channel_rules(client, operator):
    created = client.post(
        "/api/v1/sources",
        json={
            "catalog_id": "official_feed",
            "name": "Official Feed",
            "channel_type": "rss",
            "start_url": "https://example.com/feed.xml",
            "parser_config": {"discovery_method": "feed"},
        },
    )
    assert created.status_code == 201
    assert created.json()["catalog_id"] == "official_feed"
    assert created.json()["channel_type"] == "rss"
    assert created.json()["parser_config"]["execution_engine"] == "feed_direct"

    updated = client.patch(
        f"/api/v1/sources/{created.json()['id']}",
        json={
            "channel_type": "api",
            "start_url": "https://example.com/api/v2",
            "parser_config": {"discovery_method": "json"},
        },
    )
    assert updated.status_code == 200
    assert updated.json()["channel_type"] == "api"
    assert updated.json()["start_url"] == "https://example.com/api/v2"
    assert updated.json()["parser_config"]["execution_engine"] == "json_api"

    invalid = client.post(
        "/api/v1/sources",
        json={
            "name": "Wrong API",
            "channel_type": "api",
            "start_url": "https://example.com/api",
            "parser_config": {"discovery_method": "feed"},
        },
    )
    assert invalid.status_code == 422

    conflicting_engine = client.post(
        "/api/v1/sources",
        json={
            "name": "Conflicting engine",
            "channel_type": "web",
            "start_url": "https://example.com/conflict-engine",
            "parser_config": {
                "discovery_method": "html",
                "execution_engine": "json_api",
            },
        },
    )
    assert conflicting_engine.status_code == 422
    assert "conflicts" in conflicting_engine.text


def test_scheduler_lists_due_sources_and_manual_trigger_respects_stop_rules(client, operator):
    due = client.post(
        "/api/v1/sources",
        json={"name": "Due Source", "start_url": "https://example.com/due"},
    )
    blocked = client.post(
        "/api/v1/sources",
        json={
            "name": "Blocked Source",
            "start_url": "https://example.com/blocked",
            "parser_config": {"crawl_strategy": "blocked"},
        },
    )

    response = client.get("/api/v1/scheduler/due")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [due.json()["id"]]
    assert client.post(f"/api/v1/sources/{blocked.json()['id']}/crawl").status_code == 409


def test_content_api_filters_midstream_relevance(client, session_factory):
    with session_factory() as session:
        source = Source(
            name="General News",
            channel_type="web",
            start_url="https://example.com/news",
            normalized_start_url="https://example.com/news",
            processing_config={"scope_mode": "keyword"},
        )
        session.add(source)
        session.flush()
        run = CrawlRun(source_id=source.id, trigger="test", status="succeeded")
        session.add(run)
        session.flush()
        raw = RawItem(
            source_id=source.id,
            crawl_run_id=run.id,
            identity_key="a" * 64,
            original_url="https://example.com/news/1",
            canonical_url="https://example.com/news/1",
            payload={},
            payload_sha256="b" * 64,
        )
        session.add(raw)
        session.flush()
        content = ContentItem(
            source_id=source.id,
            raw_item_id=raw.id,
            identity_key="a" * 64,
            title="General business story",
            original_url=raw.original_url,
            canonical_url=raw.canonical_url,
            body="A complete news article body.",
            language="en",
        )
        session.add(content)
        session.flush()
        session.add(
            ContentProcessingResult(
                content_item_id=content.id,
                processor_name=PROCESSOR_NAME,
                processor_version=PROCESSOR_VERSION,
                input_content_hash=content.content_hash,
                is_relevant=False,
                matched_topics=[],
                matched_events=[],
                reason="no_industry_match",
            )
        )
        session.commit()

    assert client.get("/api/v1/content-items?is_relevant=true").json() == []
    response = client.get("/api/v1/content-items?is_relevant=false")
    assert response.status_code == 200
    assert response.json()[0]["relevance_reason"] == "no_industry_match"
    assert response.json()[0]["crawl_run_id"] == run.id
    assert response.json()[0]["page_snapshot_id"] is None
    assert response.json()[0]["channel_type"] == "web"
    assert response.json()[0]["provider"] == "direct"

    with session_factory() as session:
        content = session.scalar(select(ContentItem))
        content.content_hash = "c" * 64
        session.commit()

    assert client.get("/api/v1/content-items?is_relevant=false").json() == []
    unfiltered = client.get("/api/v1/content-items").json()
    assert unfiltered[0]["is_relevant"] is None
    assert unfiltered[0]["relevance_reason"] is None


def test_source_health_and_failed_run_retry(client, session_factory, operator):
    created = client.post(
        "/api/v1/sources",
        json={"name": "Unstable Source", "start_url": "https://example.com/unstable"},
    )
    source_id = created.json()["id"]
    with session_factory() as session:
        now = datetime(2026, 8, 27, 8, tzinfo=UTC)
        failed = CrawlRun(
            source_id=source_id,
            trigger="schedule",
            status="failed",
            error_code="robots_disallowed_listing",
            started_at=now,
            finished_at=now + timedelta(seconds=12),
        )
        session.add(failed)
        session.commit()
        run_id = failed.id

    listed = client.get("/api/v1/sources").json()[0]
    assert listed["last_run_status"] == "failed"
    assert listed["last_error_code"] == "robots_disallowed_listing"
    assert listed["consecutive_failures"] == 1
    assert listed["circuit_open"] is False

    run = client.get(f"/api/v1/crawl-runs/{run_id}").json()
    assert run["duration_seconds"] == 12

    with patch("app.main.crawl_source"):
        retry = client.post(f"/api/v1/crawl-runs/{run_id}/retry")
        assert retry.status_code == 202
        assert retry.json()["status"] == "pending"
        assert retry.json()["run_id"] != run_id
        assert client.post(f"/api/v1/crawl-runs/{run_id}/retry").status_code == 202


def test_date_backfill_and_retry_keep_frozen_coverage(client, session_factory, operator):
    created = client.post(
        "/api/v1/sources",
        json={
            "name": "Daily provider",
            "channel_type": "third_party_feed",
            "start_url": "https://provider.example.com/",
            "parser_config": {
                "provider": "fixture",
                "discovery_method": "json",
                "discovery_url": "https://provider.example.com/list",
                "publication_date_mode": "previous_day",
                "publication_timezone": "Asia/Shanghai",
            },
        },
    )
    source_id = created.json()["id"]
    with patch("app.main.crawl_source"):
        backfill = client.post(
            f"/api/v1/sources/{source_id}/crawl?coverage_date=2026-08-27"
        )
    assert backfill.status_code == 202
    assert backfill.json()["coverage_date"] == "2026-08-27"
    assert backfill.json()["publication_timezone"] == "Asia/Shanghai"

    with session_factory() as session:
        pending = session.get(CrawlRun, backfill.json()["run_id"])
        pending.status = "failed"
        pending.finished_at = pending.started_at
        session.commit()

    with patch("app.main.crawl_source"):
        retry = client.post(f"/api/v1/crawl-runs/{backfill.json()['run_id']}/retry")
    assert retry.status_code == 202
    assert retry.json()["coverage_date"] == "2026-08-27"
    with session_factory() as session:
        retried = session.get(CrawlRun, retry.json()["run_id"])
        assert retried.coverage_date == date(2026, 8, 27)
        assert retried.retry_of_run_id == backfill.json()["run_id"]


def test_non_date_source_rejects_explicit_coverage_date(client, operator):
    created = client.post(
        "/api/v1/sources",
        json={"name": "Ordinary web", "start_url": "https://example.com/ordinary"},
    )

    response = client.post(
        f"/api/v1/sources/{created.json()['id']}/crawl?coverage_date=2026-08-27"
    )

    assert response.status_code == 422
    assert "coverage_date_requires_previous_day_source" in response.text


def test_active_run_with_different_coverage_date_returns_conflict(client, operator):
    created = client.post(
        "/api/v1/sources",
        json={
            "name": "Date provider conflict",
            "channel_type": "third_party_feed",
            "start_url": "https://provider.example.com/conflict",
            "parser_config": {
                "provider": "fixture",
                "discovery_method": "json",
                "discovery_url": "https://provider.example.com/list",
                "publication_date_mode": "previous_day",
                "publication_timezone": "Asia/Shanghai",
            },
        },
    )
    source_id = created.json()["id"]
    with patch("app.main.crawl_source"):
        first = client.post(
            f"/api/v1/sources/{source_id}/crawl?coverage_date=2026-08-27"
        )
        repeated = client.post(
            f"/api/v1/sources/{source_id}/crawl?coverage_date=2026-08-27"
        )
        conflict = client.post(
            f"/api/v1/sources/{source_id}/crawl?coverage_date=2026-08-26"
        )

    assert repeated.status_code == 202
    assert repeated.json()["run_id"] == first.json()["run_id"]
    assert conflict.status_code == 409
    assert "different_coverage_context" in conflict.text
