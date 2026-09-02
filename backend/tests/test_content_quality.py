from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.content_quality import is_reader_eligible, quality_tier
from app.contracts import build_contract
from app.topic_discovery import enrichment_retry_due


def _content(*, published_at, body, quality):
    return SimpleNamespace(published_at=published_at, body=body, quality=quality)


def test_quality_tier_requires_date_and_usable_body():
    now = datetime.now(UTC)
    verified = _content(
        published_at=now,
        body="正文" * 200,
        quality={"body_complete": True, "metadata_only": False},
    )
    partial = _content(
        published_at=now,
        body="正文" * 120,
        quality={"body_complete": None, "metadata_only": False},
    )
    undated = _content(
        published_at=None,
        body="正文" * 200,
        quality={"body_complete": True, "metadata_only": False},
    )
    metadata = _content(
        published_at=now,
        body="摘要" * 200,
        quality={"body_complete": False, "metadata_only": True},
    )

    assert quality_tier(verified) == "verified_full"
    assert quality_tier(partial) == "partial"
    assert quality_tier(undated) == "needs_enrichment"
    assert quality_tier(metadata) == "needs_enrichment"
    assert is_reader_eligible(verified) is True
    assert is_reader_eligible(partial) is True
    assert is_reader_eligible(undated) is False
    assert is_reader_eligible(metadata) is False


def test_api_without_date_is_kept_in_contract_but_flagged_for_enrichment():
    source = SimpleNamespace(
        id=1,
        name="测试 API",
        source_region="GLOBAL",
        source_type="trade_media",
        source_external_id=None,
        channel_type="api",
        default_language="zh-CN",
        parser_config={},
    )
    contract = build_contract(
        {
            "title": "API 文章",
            "original_url": "https://api.example.com/article/1",
            "canonical_url": "https://api.example.com/article/1",
            "body": "完整正文" * 100,
            "description": "摘要",
            "topics": [],
        },
        source,
    )

    assert contract.schema_version == "article.v1.1"
    assert "missing_published_at" in contract.quality.validation_warnings


def test_failed_metadata_enrichment_obeys_shared_url_cooldown():
    now = datetime.now(UTC)
    recent = _content(
        published_at=None,
        body="摘要",
        quality={"last_enrichment_attempt_at": now.isoformat()},
    )
    old = _content(
        published_at=None,
        body="摘要",
        quality={
            "last_enrichment_attempt_at": (
                now - timedelta(hours=25)
            ).isoformat()
        },
    )

    assert enrichment_retry_due(recent, now=now) is False
    assert enrichment_retry_due(old, now=now) is True
