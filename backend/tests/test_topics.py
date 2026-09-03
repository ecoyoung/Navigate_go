from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.auth import create_user
from app.daily_report import available_topic_report_dates
from app.firecrawl import FirecrawlClient, FirecrawlError
from app.llm_editorial import LLMUsage
from app.models import (
    ContentItem,
    CrawlRun,
    InterestTopic,
    PageSnapshot,
    RawItem,
    Source,
    TopicMatch,
    TopicRun,
    TopicSourceCandidate,
)
from app.topic_search_plan import TopicSearchPlan, TopicSearchPlanResult

PASSWORD = "Topic-Admin-2026!"


def seed_user_and_content(session_factory):
    with session_factory() as db:
        user = create_user(
            db,
            email="topics@example.com",
            display_name="主题读者",
            password=PASSWORD,
            role="admin",
        )
        source = Source(
            catalog_id="topic_fixture",
            name="测试来源",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        crawl = CrawlRun(source_id=source.id, status="succeeded")
        db.add(crawl)
        db.flush()
        raw = RawItem(
            source_id=source.id,
            crawl_run_id=crawl.id,
            identity_key="a" * 64,
            original_url="https://example.com/sunscreen",
            canonical_url="https://example.com/sunscreen",
            payload={"title": "防晒新品发布"},
            payload_sha256="b" * 64,
        )
        db.add(raw)
        db.flush()
        content = ContentItem(
            source_id=source.id,
            raw_item_id=raw.id,
            identity_key=raw.identity_key,
            title="国产品牌发布防晒新品",
            canonical_url=raw.canonical_url,
            excerpt="面向敏感肌的新配方进入市场",
            body="品牌公布产品配方、上市渠道、原料依据和适用人群。" * 12,
            language="zh",
            content_hash="c" * 64,
            published_at=datetime.now(UTC),
            quality={"body_complete": True, "metadata_only": False},
        )
        db.add(content)
        db.commit()
        return user.id, content.id


def login(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "topics@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200


def fake_search_plan(_db, topic, _client):
    return TopicSearchPlanResult(
        TopicSearchPlan(
            schema_version="topic-search-plan.v1",
            topic_id=topic.id,
            topic_intent_hash=topic.intent_hash,
            query=topic.intent_text,
        ),
        False,
        LLMUsage(10, 5, 15),
    )


def fail_direct_fetch(_db, *, source, run, url, article):
    return {
        **article,
        "validation_warnings": [
            *(article.get("validation_warnings") or []),
            "content_enrichment_failed:HTTPError",
        ],
    }


def article_html(*, title: str, published_at: str, body: str) -> str:
    return f"""
    <html><head>
      <title>{title}</title>
      <script type="application/ld+json">{{
        "@context":"https://schema.org",
        "@type":"NewsArticle",
        "headline":"{title}",
        "datePublished":"{published_at}"
      }}</script>
    </head><body>
      <h1>{title}</h1>
      <article>{body}</article>
    </body></html>
    """


def patch_direct_http(monkeypatch, pages: dict[str, httpx.Response]):
    class FakeHTTPClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url):
            target = str(url)
            if target in pages:
                return pages[target]
            if target.endswith("/robots.txt"):
                return httpx.Response(
                    200,
                    text="User-agent: *\nAllow: /\n",
                    request=httpx.Request("GET", target),
                )
            return httpx.Response(404, text="missing", request=httpx.Request("GET", target))

    monkeypatch.setattr("app.topic_discovery.httpx.Client", FakeHTTPClient)


def html_response(url: str, text: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        text=text,
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", url),
    )


def test_create_topic_previews_existing_pool(client, session_factory):
    seed_user_and_content(session_factory)
    login(client)

    created = client.post(
        "/api/v1/topics",
        json={
            "name": "防晒产品",
            "intent_text": "关注国产品牌防晒新品，排除促销软文",
            "keywords": ["防晒", "新品"],
            "excluded_keywords": ["促销软文"],
            "daily_credit_limit": 4,
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["topic"]["name"] == "防晒产品"
    assert body["topic"]["match_count"] == 1
    assert body["items"][0]["title"] == "国产品牌发布防晒新品"
    assert client.get("/api/v1/feed/for-you").json()[0]["topic_names"] == ["防晒产品"]


def test_discover_uses_bounded_cached_firecrawl_search(client, session_factory, monkeypatch):
    seed_user_and_content(session_factory)
    login(client)
    topic_id = client.post(
        "/api/v1/topics",
        json={"intent_text": "关注防晒新品", "daily_credit_limit": 10},
    ).json()["topic"]["id"]

    def fake_cached_search(_db, _client, *, query, limit, search_options):
        assert query == "关注防晒新品"
        assert limit == 5
        assert search_options["sources"] == ["web"]
        assert search_options["safe"] is True
        assert search_options["tbs"] in {"qdr:w", "qdr:d"}
        return (
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "url": "https://news.example.com/a",
                            "title": "防晒行业资讯",
                            "description": "候选来源",
                        }
                    ]
                },
            },
            False,
            2,
        )

    monkeypatch.setattr("app.main.cached_search", fake_cached_search)
    monkeypatch.setattr("app.main.compile_topic_search_plan", fake_search_plan)
    monkeypatch.setattr("app.topic_discovery._web_enrichment_detail", fail_direct_fetch)
    scrape_calls = []

    fresh_published_at = (datetime.now(UTC) - timedelta(hours=12)).isoformat()

    class FakeClient:
        def scrape(self, url):
            scrape_calls.append(url)
            return {
                "success": True,
                "data": {
                    "markdown": (
                        "# Pet sunscreen industry update\n\n"
                        "A brand details its product launch. " * 20
                    ),
                    "metadata": {
                        "title": "Pet sunscreen industry update",
                        "description": "候选来源的完整文章",
                        "statusCode": 200,
                        "publishedTime": fresh_published_at,
                    },
                },
            }

    monkeypatch.setattr("app.main.FirecrawlClient.from_environment", FakeClient)

    response = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 5})

    assert response.status_code == 200
    assert response.json()["credits_used"] == 3
    assert response.json()["fetched_pages"] == 1
    assert response.json()["ingested_count"] == 1
    assert "Pet sunscreen industry update" in {item["title"] for item in response.json()["items"]}
    assert response.json()["candidates"][0]["host"] == "news.example.com"
    repeated = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 5})
    assert repeated.status_code == 200
    assert repeated.json()["fetched_pages"] == 0
    assert repeated.json()["ingested_count"] == 0
    assert len(scrape_calls) == 1
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TopicSourceCandidate)) == 1
        assert db.scalar(select(func.count()).select_from(ContentItem)) == 2
        assert db.scalar(select(func.count()).select_from(PageSnapshot)) == 1
        run = db.scalar(select(TopicRun).order_by(TopicRun.id.asc()))
        assert run.status == "succeeded"
        assert run.firecrawl_credits_used == 3
        content = db.scalar(
            select(ContentItem).where(ContentItem.canonical_url == "https://news.example.com/a")
        )
        match = db.scalar(
            select(TopicMatch).where(
                TopicMatch.topic_id == topic_id,
                TopicMatch.content_item_id == content.id,
            )
        )
        assert match.matched_signals["collection_window"]["admitted"] is True
        topic = db.get(InterestTopic, topic_id)
        assert available_topic_report_dates(db, topic=topic)


def test_discover_rejects_content_outside_collection_window(
    client, session_factory, monkeypatch
):
    seed_user_and_content(session_factory)
    login(client)
    topic_id = client.post(
        "/api/v1/topics",
        json={"intent_text": "关注防晒新品", "daily_credit_limit": 10},
    ).json()["topic"]["id"]

    monkeypatch.setattr("app.main.compile_topic_search_plan", fake_search_plan)
    monkeypatch.setattr("app.topic_discovery._web_enrichment_detail", fail_direct_fetch)
    monkeypatch.setattr(
        "app.main.cached_search",
        lambda *_args, **_kwargs: (
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "url": "https://news.example.com/old",
                            "title": "防晒新品旧闻",
                            "description": "窗口外文章",
                        }
                    ]
                },
            },
            False,
            2,
        ),
    )

    class FakeClient:
        def scrape(self, _url):
            return {
                "success": True,
                "data": {
                    "markdown": "# 防晒新品旧闻\n\n" + "窗口外正文。" * 120,
                    "metadata": {
                        "title": "防晒新品旧闻",
                        "publishedTime": "2025-05-21T04:01:05Z",
                    },
                },
            }

    monkeypatch.setattr("app.main.FirecrawlClient.from_environment", FakeClient)
    response = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 1})

    assert response.status_code == 200
    assert "防晒新品旧闻" not in {item["title"] for item in response.json()["items"]}
    with session_factory() as db:
        content = db.scalar(
            select(ContentItem).where(ContentItem.canonical_url == "https://news.example.com/old")
        )
        match = db.scalar(
            select(TopicMatch).where(
                TopicMatch.topic_id == topic_id,
                TopicMatch.content_item_id == content.id,
            )
        )
        assert match.decision == "exclude"
        assert "outside_collection_window" in match.reasons
        assert match.matched_signals["collection_window"]["admitted"] is False


def test_user_can_delete_own_topic_and_related_state(client, session_factory):
    _user_id, content_id = seed_user_and_content(session_factory)
    login(client)
    topic_id = client.post(
        "/api/v1/topics",
        json={"intent_text": "关注防晒新品"},
    ).json()["topic"]["id"]

    response = client.delete(f"/api/v1/topics/{topic_id}")

    assert response.status_code == 204
    assert client.get("/api/v1/topics").json() == []
    with session_factory() as db:
        assert db.get(InterestTopic, topic_id) is None
        assert db.scalar(select(func.count()).select_from(TopicMatch)) == 0
        assert db.get(ContentItem, content_id) is not None


def test_firecrawl_client_sends_bearer_without_exposing_it(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"success": True, "data": []},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    response = FirecrawlClient("test-secret").search("beauty news", limit=10)

    assert response["success"] is True
    assert captured["url"].endswith("/v2/search")
    assert captured["headers"]["Authorization"] == "Bearer test-secret"
    assert captured["json"] == {"query": "beauty news", "limit": 10}


def test_firecrawl_client_allows_only_compiled_search_options(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["json"] = json
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"success": True, "data": []},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    FirecrawlClient("test-secret").search(
        "beauty news",
        limit=2,
        search_options={"sources": ["web"], "tbs": "qdr:d", "safe": True},
    )
    assert captured["json"]["tbs"] == "qdr:d"
    with pytest.raises(ValueError, match="Unsupported"):
        FirecrawlClient("test-secret").search("x", search_options={"country": "CN"})


def test_discover_retries_incomplete_existing_content_with_web_pipeline(
    client, session_factory, monkeypatch
):
    seed_user_and_content(session_factory)
    login(client)
    topic_id = client.post(
        "/api/v1/topics",
        json={"intent_text": "关注宠物电子产品出海", "daily_credit_limit": 10},
    ).json()["topic"]["id"]

    def fake_cached_search(_db, _client, *, query, limit, search_options):
        return (
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "url": "https://pet.example.com/export",
                            "title": "Smart pet devices expand overseas",
                            "description": "Chinese brands enter new markets.",
                        }
                    ]
                },
            },
            False,
            2,
        )

    attempts = 0

    class FakeClient:
        def scrape(self, _url):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise FirecrawlError("firecrawl_http_503")
            return {
                "success": True,
                "data": {
                    "markdown": "# Smart pet devices\n\nFull report about overseas expansion.",
                    "metadata": {"title": "Smart pet devices", "statusCode": 200},
                },
            }

    monkeypatch.setattr("app.main.cached_search", fake_cached_search)
    monkeypatch.setattr("app.main.compile_topic_search_plan", fake_search_plan)
    monkeypatch.setattr("app.topic_discovery._web_enrichment_detail", fail_direct_fetch)
    monkeypatch.setattr("app.main.FirecrawlClient.from_environment", FakeClient)

    first = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 1})
    assert first.status_code == 200
    assert first.json()["metadata_only_count"] == 1
    second = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 1})
    assert second.status_code == 200
    assert second.json()["fetched_pages"] == 0
    with session_factory() as db:
        content = db.scalar(
            select(ContentItem).where(ContentItem.canonical_url == "https://pet.example.com/export")
        )
        assert content is not None
        content.quality = {
            **content.quality,
            "last_enrichment_attempt_at": "2026-01-01T00:00:00+00:00",
        }
        db.commit()
    third = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 1})
    assert third.status_code == 200
    assert third.json()["fetched_pages"] == 1
    assert attempts == 2
    with session_factory() as db:
        content = db.scalar(
            select(ContentItem).where(ContentItem.canonical_url == "https://pet.example.com/export")
        )
        assert content is not None
        assert content.quality["last_enrichment_attempt_at"]


def _discover_search(monkeypatch, url: str, title: str = "防晒新品"):
    def fake_cached_search(_db, _client, *, query, limit, search_options):
        return (
            {
                "success": True,
                "data": {"web": [{"url": url, "title": title, "description": "候选来源"}]},
            },
            False,
            2,
        )

    monkeypatch.setattr("app.main.cached_search", fake_cached_search)
    monkeypatch.setattr("app.main.compile_topic_search_plan", fake_search_plan)


def test_discover_uses_direct_html_and_skips_firecrawl_scrape(
    client, session_factory, monkeypatch
):
    seed_user_and_content(session_factory)
    login(client)
    topic_id = client.post(
        "/api/v1/topics",
        json={"intent_text": "关注防晒新品", "daily_credit_limit": 10},
    ).json()["topic"]["id"]
    url = "https://news.example.com/a"
    published_at = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    html = article_html(
        title="防晒新品",
        published_at=published_at,
        body="网页解析后的完整正文" * 40,
    )
    scrape_calls = []

    class FakeClient:
        def scrape(self, target):
            scrape_calls.append(target)
            raise AssertionError("direct html success should not scrape")

    _discover_search(monkeypatch, url)
    monkeypatch.setattr("app.main.FirecrawlClient.from_environment", FakeClient)
    patch_direct_http(monkeypatch, {url: html_response(url, html)})

    response = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 1})
    assert response.status_code == 200
    assert response.json()["credits_used"] == 2
    assert response.json()["fetched_pages"] == 0
    assert response.json()["ingested_count"] == 1
    assert scrape_calls == []
    with session_factory() as db:
        content = db.scalar(select(ContentItem).where(ContentItem.canonical_url == url))
        assert content is not None
        assert content.published_at is not None
        assert "网页解析后的完整正文" in (content.body or "")


@pytest.mark.parametrize(
    ("status_code", "html"),
    [
        (200, "<html><body><div id='root'></div></body></html>"),
        (403, "denied"),
        (
            200,
            "<html><body><h1>防晒新品</h1><article>"
            + "没有发布日期的正文。" * 40
            + "</article></body></html>",
        ),
    ],
)
def test_discover_scrapes_when_direct_html_is_blocked_or_undated(
    client, session_factory, monkeypatch, status_code, html
):
    seed_user_and_content(session_factory)
    login(client)
    topic_id = client.post(
        "/api/v1/topics",
        json={"intent_text": "关注防晒新品", "daily_credit_limit": 10},
    ).json()["topic"]["id"]
    url = "https://news.example.com/js-shell"
    published_at = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    scrape_calls = []

    class FakeClient:
        def scrape(self, target):
            scrape_calls.append(target)
            return {
                "success": True,
                "data": {
                    "markdown": "补抓后的完整正文" * 40,
                    "metadata": {
                        "title": "防晒新品",
                        "statusCode": 200,
                        "publishedTime": published_at,
                    },
                },
            }

    _discover_search(monkeypatch, url)
    monkeypatch.setattr("app.main.FirecrawlClient.from_environment", FakeClient)
    patch_direct_http(monkeypatch, {url: html_response(url, html, status_code)})

    response = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 1})
    assert response.status_code == 200
    assert scrape_calls == [url]
    assert response.json()["fetched_pages"] == 1
    assert response.json()["credits_used"] == 3
    with session_factory() as db:
        content = db.scalar(select(ContentItem).where(ContentItem.canonical_url == url))
        assert content is not None
        assert content.published_at is not None
        assert len(content.body or "") >= 200


def test_discover_prefers_enabled_host_source_for_direct_html(
    client, session_factory, monkeypatch
):
    seed_user_and_content(session_factory)
    with session_factory() as db:
        db.add(
            Source(
                catalog_id="news_rss",
                name="新闻RSS",
                channel_type="rss",
                start_url="https://news.example.com/feed.xml",
                normalized_start_url="https://news.example.com/feed.xml",
                parser_config={"discovery_method": "feed"},
                is_enabled=True,
            )
        )
        db.commit()
    login(client)
    topic_id = client.post(
        "/api/v1/topics",
        json={"intent_text": "关注防晒新品", "daily_credit_limit": 10},
    ).json()["topic"]["id"]
    url = "https://news.example.com/a"
    published_at = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    html = article_html(title="防晒新品", published_at=published_at, body="RSS同源正文补全。" * 40)
    scrape_calls = []

    class FakeClient:
        def scrape(self, target):
            scrape_calls.append(target)
            raise AssertionError("enabled host source should not scrape")

    _discover_search(monkeypatch, url)
    monkeypatch.setattr("app.main.FirecrawlClient.from_environment", FakeClient)
    patch_direct_http(monkeypatch, {url: html_response(url, html)})

    response = client.post(f"/api/v1/topics/{topic_id}/discover", json={"limit": 1})
    assert response.status_code == 200
    assert scrape_calls == []
    with session_factory() as db:
        content = db.scalar(select(ContentItem).where(ContentItem.canonical_url == url))
        source = db.get(Source, content.source_id)
        assert source.channel_type == "rss"
        assert source.catalog_id == "news_rss"


def test_firecrawl_client_converts_network_failure_to_safe_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise httpx.ConnectError("provider unavailable")

    monkeypatch.setattr(httpx, "post", fail)
    client = FirecrawlClient("secret", base_url="https://provider.invalid")

    with pytest.raises(FirecrawlError, match="firecrawl_network_error"):
        client.search("bounded test", limit=1)
