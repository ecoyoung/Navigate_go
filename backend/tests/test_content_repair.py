from datetime import datetime

from sqlalchemy import func, select

from app.content_repair import (
    TIMEZONE_REPAIR_VERSION,
    repair_redfox_publication_timezone,
)
from app.models import ContentItem, CrawlRun, RawItem, Source
from app.web_ingestion import ingest_article


def test_redfox_timezone_repair_appends_raw_and_is_idempotent(session_factory):
    with session_factory() as session:
        source = Source(
            name="Archived account",
            channel_type="third_party_feed",
            start_url="https://example.com/account",
            normalized_start_url="https://example.com/account",
            parser_config={"provider": "redfox_archive"},
        )
        session.add(source)
        session.flush()
        import_run = CrawlRun(source_id=source.id, trigger="import", status="succeeded")
        session.add(import_run)
        session.flush()
        assert (
            ingest_article(
                session,
                source,
                import_run,
                {
                    "title": "时区修复测试",
                    "original_url": "https://example.com/article",
                    "canonical_url": "https://example.com/article",
                    "external_item_id": "work-1",
                    "published_at": datetime(2026, 8, 26, 18, 20, 10),
                    "body": "这是一篇用于验证历史发布时间修复且正文内容保持不变的完整测试文章。",
                    "description": "测试摘要",
                    "topics": ["行业"],
                    "content_completeness": "full",
                },
            )
            == "new"
        )
        session.commit()
        old_raw = session.scalar(select(RawItem))
        old_payload = dict(old_raw.payload)
        old_content_hash = session.scalar(select(ContentItem.content_hash))

        dry_run = repair_redfox_publication_timezone(session)
        assert dry_run.candidates == 1
        assert session.scalar(select(func.count(RawItem.id))) == 1

        applied = repair_redfox_publication_timezone(
            session, apply=True, expected_count=1
        )
        session.commit()
        content = session.scalar(select(ContentItem))
        new_raw = session.get(RawItem, content.raw_item_id)

        assert applied.inserted_raw == 1
        assert session.scalar(select(func.count(RawItem.id))) == 2
        assert session.get(RawItem, old_raw.id).payload == old_payload
        assert new_raw.payload["published_at"] == "2026-08-26T10:20:10+00:00"
        assert content.published_at == datetime(2026, 8, 26, 10, 20, 10)
        assert content.content_hash == old_content_hash
        assert content.normalizer_version == TIMEZONE_REPAIR_VERSION

        repeated = repair_redfox_publication_timezone(session, apply=True, expected_count=0)
        session.commit()
        assert repeated.candidates == 0
        assert session.scalar(select(func.count(RawItem.id))) == 2
