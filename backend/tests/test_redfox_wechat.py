import asyncio
import json
from datetime import UTC, date, datetime

import httpx
from sqlalchemy import func, select

from app.models import ContentItem, CrawlRun, Source
from app.redfox_wechat import (
    RedFoxListPage,
    detail_to_extracted,
    parse_list_payload,
    pick_articles,
    redfox_page_needs_next,
)
from app.web_ingestion import crawl_http_source


def test_pick_previous_day_excludes_only_explicit_pinned_and_advertising_evidence():
    payload = json.dumps(
        {
            "code": 2000,
            "data": {
                "list": [
                    {
                        "title": "置顶头条",
                        "workUuid": "pin",
                        "orderNum": 0,
                        "publishTime": "2026-08-27 23:59:59",
                        "isPinned": True,
                    },
                    {
                        "title": "618大促开抢",
                        "workUuid": "ad",
                        "orderNum": 1,
                        "publishTime": "2026-08-27 20:00:00",
                        "tags": [{"name": "广告"}],
                    },
                    {
                        "title": "广告行业观察",
                        "workUuid": "title-only",
                        "orderNum": 0,
                        "publishTime": "2026-08-27 10:00:00",
                    },
                    {
                        "title": "普通文章",
                        "workUuid": "normal",
                        "orderNum": 2,
                        "publishTime": "2026-08-27 00:00:00",
                    },
                    {
                        "title": "前一天之外",
                        "workUuid": "old",
                        "orderNum": 2,
                        "publishTime": "2026-08-26 23:59:59",
                    },
                ]
            },
        }
    )
    picked = pick_articles(
        parse_list_payload(payload),
        {
            "publication_date_mode": "previous_day",
            "publication_timezone": "Asia/Shanghai",
        },
        reference_time=datetime(2026, 8, 27, 17, tzinfo=UTC),
    )
    assert [item["workUuid"] for item in picked] == ["title-only", "normal"]


def test_pick_previous_day_can_return_healthy_empty_result():
    picked = pick_articles(
        [
            {
                "title": "较早文章",
                "workUuid": "old",
                "publishTime": "2026-08-25 12:00:00",
            }
        ],
        {"publication_date_mode": "previous_day"},
        reference_time=datetime(2026, 8, 28, 1, tzinfo=UTC),
    )
    assert picked == []


def test_pick_uses_frozen_run_timezone_instead_of_mutated_source_config():
    picked = pick_articles(
        [
            {
                "title": "跨 UTC 日期文章",
                "workUuid": "boundary",
                "publishTime": "2026-08-26T16:30:00Z",
            }
        ],
        {
            "publication_date_mode": "previous_day",
            "publication_timezone": "UTC",
        },
        target_date=date(2026, 8, 27),
        publication_timezone="Asia/Shanghai",
    )

    assert [item["workUuid"] for item in picked] == ["boundary"]


def test_redfox_pagination_must_cross_target_date_boundary():
    target = date(2026, 8, 27)
    current_page = RedFoxListPage(
        items=[
            {"workUuid": "new", "publishTime": "2026-08-28 08:00:00"},
            {"workUuid": "target", "publishTime": "2026-08-27 08:00:00"},
        ],
        total=20,
    )
    crossed_page = RedFoxListPage(
        items=[
            {"workUuid": "target", "publishTime": "2026-08-27 01:00:00"},
            {"workUuid": "old", "publishTime": "2026-08-26 23:59:59"},
        ],
        total=20,
    )

    assert redfox_page_needs_next(
        current_page,
        offset=0,
        target_date=target,
        timezone_name="Asia/Shanghai",
    )
    assert not redfox_page_needs_next(
        crossed_page,
        offset=2,
        target_date=target,
        timezone_name="Asia/Shanghai",
    )


def test_work_detail_extracts_plain_body():
    payload = json.dumps(
        {
            "code": 2000,
            "data": {
                "title": "防脱家族扩容",
                "content": "<p>这是足够长的公众号正文，用来验证红狐详情可以进入统一内容契约。</p>",
                "summary": "摘要",
                "workUrl": "https://mp.weixin.qq.com/s/example",
                "author": "化妆品报",
                "publishTime": "2026-08-26 18:20:10",
                "workUuid": "work-123",
            },
        }
    )
    extracted = detail_to_extracted(payload, min_content_chars=20)
    assert extracted["title"] == "防脱家族扩容"
    assert "足够长的公众号正文" in extracted["body"]
    assert extracted["canonical_url"] == "https://mp.weixin.qq.com/s/example"
    assert extracted["content_type"] == "article"
    assert extracted["external_item_id"] == "work-123"
    assert extracted["content_completeness"] == "full"
    assert extracted["published_at"].isoformat() == "2026-08-26T10:20:10+00:00"


def test_redfox_crawl_pages_until_previous_day_is_complete(session_factory, monkeypatch):
    list_offsets: list[int] = []
    detail_ids: list[str] = []

    def list_response(request: httpx.Request, offset: int) -> httpx.Response:
        pages = {
            0: [
                {"workUuid": "today", "title": "今天", "publishTime": "2026-08-28 08:00:00"},
                {"workUuid": "target-1", "title": "昨日一", "publishTime": "2026-08-27 20:00:00"},
            ],
            2: [
                {"workUuid": "target-2", "title": "昨日二", "publishTime": "2026-08-27 00:00:00"},
                {"workUuid": "old", "title": "更早", "publishTime": "2026-08-26 23:59:59"},
            ],
        }
        return httpx.Response(
            200,
            request=request,
            json={"code": 2000, "data": {"list": pages[offset], "total": 4}},
        )

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, **kwargs):
            request = httpx.Request(method, url)
            if str(url).endswith("/robots.txt"):
                return httpx.Response(404, request=request)
            payload = kwargs.get("json") or {}
            if str(url).endswith("queryWorkList"):
                offset = int(payload.get("offset", 0))
                list_offsets.append(offset)
                return list_response(request, offset)
            work_uuid = str(payload["workUuid"])
            detail_ids.append(work_uuid)
            publish_time = (
                "2026-08-27 20:00:00"
                if work_uuid == "target-1"
                else "2026-08-27 00:00:00"
            )
            return httpx.Response(
                200,
                request=request,
                json={
                    "code": 2000,
                    "data": {
                        "workUuid": work_uuid,
                        "title": work_uuid,
                        "publishTime": publish_time,
                        "workUrl": f"https://mp.weixin.qq.com/s/{work_uuid}",
                        "content": (
                            "这是一段足够长的公众号文章正文，"
                            "用于验证前一自然日完整分页采集。"
                        ),
                    },
                },
            )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.web_ingestion.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.web_ingestion.asyncio.sleep", no_sleep)
    with session_factory() as session:
        source = Source(
            name="RedFox previous day",
            channel_type="third_party_feed",
            start_url="https://redfox.hk/",
            normalized_start_url="https://redfox.hk/",
            fetch_interval_seconds=86400,
            parser_config={
                "provider": "redfox",
                "discovery_method": "json",
                "discovery_http_method": "POST",
                "discovery_url": "https://redfox.hk/story/api/gzh/data/queryWorkList",
                "discovery_json": {"account": "fixture", "offset": 0},
                "detail_url": "https://redfox.hk/story/api/gzh/data/workDetail",
                "publication_date_mode": "previous_day",
                "publication_timezone": "Asia/Shanghai",
                "max_listing_pages": 5,
                "min_content_chars": 20,
            },
        )
        session.add(source)
        session.commit()
        run = CrawlRun(
            source_id=source.id,
            trigger="test",
            status="pending",
            started_at=datetime(2026, 8, 28, 0, tzinfo=UTC),
        )
        session.add(run)
        session.commit()

        asyncio.run(crawl_http_source(session_factory, source.id, run.id))
        session.expire_all()
        run = session.get(CrawlRun, run.id)
        published = list(
            session.scalars(select(ContentItem.published_at).order_by(ContentItem.published_at))
        )

        assert list_offsets == [0, 2]
        assert detail_ids == ["target-1", "target-2"]
        assert run.status == "succeeded"
        assert run.new_count == 2
        assert session.scalar(select(func.count(ContentItem.id))) == 2
        assert [value.isoformat() for value in published] == [
            "2026-08-26T16:00:00",
            "2026-08-27T12:00:00",
        ]
