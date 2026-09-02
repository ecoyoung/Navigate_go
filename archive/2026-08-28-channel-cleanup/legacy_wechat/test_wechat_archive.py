import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.crawl_scheduler import due_sources
from app.models import ContentItem, CrawlRun, RawItem, Source
from app.wechat_archive import (
    archive_catalog_id,
    import_archive_articles,
    load_archive_articles,
)


def write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    accounts = {
        "accounts": [
            {"name": "行业媒体", "fakeid": "biz-media"},
            {"name": "品牌号", "fakeid": "biz-brand"},
        ]
    }
    rows = [
        {
            "name": "行业媒体",
            "status": "ok",
            "title": "美妆行业观察",
            "url": "https://mp.weixin.qq.com/s?mid=1&__biz=biz-media#rd",
            "published_at": "2026-08-25 17:48:24+00:00",
            "author": "行业媒体",
            "body": "这是一条已经下载到本地的正文。",
            "workUuid": "work-1",
        },
        {
            "name": "品牌号",
            "status": "meta_only",
            "title": "只有元数据",
            "url": "https://mp.weixin.qq.com/s?mid=2",
            "body": "",
        },
    ]
    account_path = tmp_path / "accounts.json"
    archive_path = tmp_path / "archive.jsonl"
    account_path.write_text(json.dumps(accounts, ensure_ascii=False), encoding="utf-8")
    archive_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    return archive_path, account_path


def test_archive_loader_only_accepts_valid_ok_rows(tmp_path):
    archive_path, account_path = write_fixture(tmp_path)

    articles = load_archive_articles(archive_path, account_path)

    assert len(articles) == 1
    assert articles[0].account["fakeid"] == "biz-media"
    assert articles[0].extracted()["canonical_url"].endswith("__biz=biz-media&mid=1")


def test_archive_loader_validates_every_ok_account_before_writing(tmp_path):
    archive_path, account_path = write_fixture(tmp_path)
    with archive_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n"
            + json.dumps(
                {
                    "name": "未知账号",
                    "status": "ok",
                    "title": "标题",
                    "url": "https://mp.weixin.qq.com/s?mid=3",
                    "body": "正文",
                },
                ensure_ascii=False,
            )
        )

    with pytest.raises(ValueError, match="unknown account"):
        load_archive_articles(archive_path, account_path)


def test_archive_import_is_content_idempotent_and_sources_are_disabled(
    session_factory, tmp_path
):
    archive_path, account_path = write_fixture(tmp_path)
    articles = load_archive_articles(archive_path, account_path)

    with session_factory() as session:
        first = import_archive_articles(session, articles)
        session.commit()
        second = import_archive_articles(session, articles)
        session.commit()

        assert first.new == 1
        assert second.skipped == 1
        assert session.scalar(select(func.count(RawItem.id))) == 1
        assert session.scalar(select(func.count(ContentItem.id))) == 1
        assert session.scalar(select(func.count(CrawlRun.id))) == 2
        source = session.scalar(select(Source))
        content = session.scalar(select(ContentItem))
        assert source.catalog_id == archive_catalog_id(articles[0].account)
        assert source.is_enabled is False
        assert source.parser_config["crawl_strategy"] == "offline_import"
        assert content.schema_version == "article.v1.1"
        assert content.external_id == "work-1"
        assert source.source_external_id == "biz-media"
        assert content.quality["body_complete"] is None
        assert "media_not_archived" in content.quality["validation_warnings"]
        assert due_sources(session) == []
