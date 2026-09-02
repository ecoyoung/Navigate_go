import json
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select

from app.content_processing import process_content_item
from app.models import (
    ContentItem,
    ContentProcessingResult,
    CrawlRun,
    PageSnapshot,
    RawItem,
    Source,
)
from app.web_ingestion import (
    api_needs_web_fallback,
    api_within_publication_window,
    discover_article_urls,
    extract_article,
    extract_json_article,
    extract_page_publication_date,
    ingest_article,
    merge_api_web_detail,
    request_headers,
    robots_allows,
    source_publication_window_days,
    within_publication_window,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = {
    "link_selector": ".articles article a[href]",
    "card_selector": "article",
    "exclude_card_selector": ".premium-bar",
    "article_url_pattern": r"^https://example\.com/articles/(?!tag:)[^?]+$",
    "body_selector": ".paywall .module.rich-text .container.boxed.text",
    "min_content_chars": 120,
}


def seed(session):
    source = Source(
        name="Fixture site",
        channel_type="web",
        start_url="https://example.com/articles",
        normalized_start_url="https://example.com/articles",
        fetch_interval_seconds=3600,
        parser_config=CONFIG,
        processing_config={
            "scope_mode": "keyword",
            "industry_keywords": ["beauty"],
            "event_keywords": ["launch"],
        },
    )
    session.add(source)
    session.commit()
    run = CrawlRun(source_id=source.id, trigger="manual", status="running")
    session.add(run)
    session.commit()
    return source, run


def test_discovery_filters_external_tags_and_member_cards():
    html = (FIXTURES / "listing.html").read_text()
    assert discover_article_urls(html, "https://example.com/articles", CONFIG) == [
        "https://example.com/articles/open-story"
    ]


def test_discovery_keeps_news_articles_regardless_of_business_relevance():
    html = """
    <main>
      <article><a href="/news/beauty">护肤品牌完成新一轮融资</a></article>
      <article><a href="/news/tutorial">夏日护肤教程</a></article>
      <article><a href="/news/cars">汽车公司完成融资</a></article>
    </main>
    """
    config = {
        "link_selector": "a[href]",
        "article_url_pattern": r"^https://example\.com/news/",
        "scope_mode": "keyword",
        "industry_keywords": ["护肤", "美妆"],
        "event_keywords": ["品牌", "融资"],
    }

    assert discover_article_urls(html, "https://example.com/news", config) == [
        "https://example.com/news/beauty",
        "https://example.com/news/tutorial",
        "https://example.com/news/cars",
    ]


def test_extract_article_resolves_date_from_ordered_fallback_signals():
    body = "这是足够长的正文。" * 40
    microdata = extract_article(
        f"<html><head><meta name='parsely-pub-date' content='2026-09-01T08:30:00+08:00'></head>"
        f"<body><h1>文章</h1><article>{body}</article></body></html>",
        "https://example.com/article/no-date",
        {"min_content_chars": 120},
    )
    assert microdata["published_at"] == datetime(2026, 9, 1, 0, 30, tzinfo=UTC)
    assert "published_at:meta" in microdata["validation_warnings"]

    url_date = extract_article(
        f"<html><body><h1>文章</h1><article>{body}</article></body></html>",
        "https://example.com/2026/08/31/article",
        {"min_content_chars": 120},
    )
    assert url_date["published_at"] == datetime(2026, 8, 31, tzinfo=UTC)
    assert "published_at:url" in url_date["validation_warnings"]

    visible = extract_article(
        f"<html><body><h1>文章</h1><div class='publish-date'>2026年8月30日</div>"
        f"<article>{body}</article></body></html>",
        "https://example.com/article",
        {"min_content_chars": 120},
    )
    assert visible["published_at"] == datetime(2026, 8, 30, tzinfo=UTC)
    assert "published_at:visible_text" in visible["validation_warnings"]


def test_page_date_can_be_recovered_from_url_when_html_is_not_an_article():
    published_at, origin = extract_page_publication_date(
        "<html><body>访问受限</body></html>",
        "https://example.com/a/202609013860555444.html",
        {},
    )
    assert published_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert origin == "url"


def test_extract_and_idempotent_ingestion(session_factory):
    extracted = extract_article(
        (FIXTURES / "article.html").read_text(), "https://example.com/articles/open-story", CONFIG
    )
    assert extracted["author"] == "Jane Editor"
    assert extracted["canonical_url"] == "https://example.com/articles/open-story"
    assert extracted["content_type"] == "news"
    assert extracted["topics"] == ["Beauty", "Retail", "skincare", "fragrance"]
    assert len(extracted["body"]) > 120
    with session_factory() as session:
        source, run = seed(session)
        snapshot = PageSnapshot(
            crawl_run_id=run.id,
            url=extracted["canonical_url"],
            page_type="article",
            http_status=200,
            content_type="text/html",
            body="raw html",
            body_sha256="c" * 64,
        )
        session.add(snapshot)
        session.commit()
        assert ingest_article(session, source, run, extracted, snapshot.id) == "new"
        session.commit()
        assert ingest_article(session, source, run, extracted, snapshot.id) == "skipped"
        assert session.scalar(select(func.count(RawItem.id))) == 1
        assert session.scalar(select(func.count(ContentItem.id))) == 1
        assert session.scalar(select(RawItem)).page_snapshot_id == snapshot.id
        assert session.scalar(select(ContentItem)).topics == [
            "Beauty",
            "Retail",
            "skincare",
            "fragrance",
        ]


def test_changed_article_adds_raw_version_and_updates_latest_content(session_factory):
    extracted = extract_article(
        (FIXTURES / "article.html").read_text(),
        "https://example.com/articles/open-story",
        CONFIG,
    )
    with session_factory() as session:
        source, run = seed(session)
        assert ingest_article(session, source, run, extracted) == "new"
        session.commit()
        changed = {**extracted, "body": extracted["body"] + " Updated analysis."}
        assert ingest_article(session, source, run, changed) == "updated"
        session.commit()
        assert session.scalar(select(func.count(RawItem.id))) == 2
        assert session.scalar(select(func.count(ContentItem.id))) == 1
        assert session.scalar(select(ContentItem)).body.endswith("Updated analysis.")


def test_external_id_binds_legacy_url_identity_and_survives_url_change(session_factory):
    extracted = extract_article(
        (FIXTURES / "article.html").read_text(),
        "https://example.com/articles/open-story",
        CONFIG,
    )
    with session_factory() as session:
        source, run = seed(session)
        assert ingest_article(session, source, run, extracted) == "new"
        session.commit()
        content = session.scalar(select(ContentItem))
        legacy_identity = content.identity_key

        with_external = {**extracted, "external_item_id": "publisher-123"}
        assert ingest_article(session, source, run, with_external) == "updated"
        session.commit()
        moved = {
            **with_external,
            "canonical_url": "https://example.com/articles/new-location",
            "original_url": "https://example.com/articles/new-location",
        }
        assert ingest_article(session, source, run, moved) == "updated"
        session.commit()
        assert ingest_article(session, source, run, moved) == "skipped"

        content = session.scalar(select(ContentItem))
        assert content.identity_key == legacy_identity
        assert content.external_id == "publisher-123"
        assert content.canonical_url.endswith("/new-location")
        assert session.scalar(select(func.count(ContentItem.id))) == 1
        assert session.scalar(select(func.count(RawItem.id))) == 3


def test_html_v1_1_extracts_identifier_update_and_media():
    html = """
    <html><head>
      <script type="application/ld+json">{
        "@type":"NewsArticle", "headline":"Contract story",
        "identifier":{"value":"story-7"},
        "datePublished":"2026-08-26T09:00:00Z",
        "dateModified":"2026-08-27T10:00:00Z",
        "image":{"url":"/cover.jpg", "caption":"Cover"}
      }</script>
    </head><body><article><p>
      This is a sufficiently long article body used to verify the normalized article
      contract extracts stable identifiers, update timestamps, and media evidence.
    </p><img src="/inside.jpg" alt="Inside" /></article></body></html>
    """

    extracted = extract_article(
        html,
        "https://example.com/story/7",
        {"min_content_chars": 80, "content_completeness": "full"},
    )

    assert extracted["external_item_id"] == "story-7"
    assert extracted["updated_at"].isoformat() == "2026-08-27T10:00:00+00:00"
    assert {item["url"] for item in extracted["media"]} == {
        "https://example.com/cover.jpg",
        "https://example.com/inside.jpg",
    }
    assert extracted["content_completeness"] == "full"


def test_midstream_processing_is_separate_and_versioned(session_factory):
    extracted = extract_article(
        (FIXTURES / "article.html").read_text(),
        "https://example.com/articles/open-story",
        CONFIG,
    )
    with session_factory() as session:
        source, run = seed(session)
        assert ingest_article(session, source, run, extracted) == "new"
        session.commit()
        content = session.scalar(select(ContentItem))

        result, created = process_content_item(session, content, source)
        session.commit()
        repeated, repeated_created = process_content_item(session, content, source)

        assert created
        assert not repeated_created
        assert repeated.id == result.id
        assert result.input_content_hash == content.content_hash
        assert session.scalar(select(func.count(ContentProcessingResult.id))) == 1


def test_midstream_processing_recomputes_when_content_hash_changes(session_factory):
    extracted = extract_article(
        (FIXTURES / "article.html").read_text(),
        "https://example.com/articles/open-story",
        CONFIG,
    )
    with session_factory() as session:
        source, run = seed(session)
        assert ingest_article(session, source, run, extracted) == "new"
        session.commit()
        content = session.scalar(select(ContentItem))
        first, processed = process_content_item(session, content, source)
        session.commit()
        first_id = first.id
        first_hash = first.input_content_hash

        changed = {**extracted, "topics": ["unrelated"], "body": "No matching topic remains."}
        assert ingest_article(session, source, run, changed) == "updated"
        session.commit()
        content = session.scalar(select(ContentItem))
        refreshed, reprocessed = process_content_item(session, content, source)
        session.commit()

        assert processed
        assert reprocessed
        assert refreshed.id == first_id
        assert refreshed.input_content_hash != first_hash
        assert refreshed.input_content_hash == content.content_hash
        assert refreshed.is_relevant is False
        assert session.scalar(select(func.count(ContentProcessingResult.id))) == 1


def test_robots_rules_are_enforced():
    rules = "User-agent: *\nDisallow: /account/\nAllow: /articles/"
    assert robots_allows(rules, "https://example.com/articles/story")
    assert not robots_allows(rules, "https://example.com/account/private")


def test_extract_json_report_metadata():
    summary = (
        "这是一段公开报告摘要，用于验证报告元数据可以进入统一内容契约。"
        "它不包含任何受限下载内容，但长度足以作为公开摘要保存。"
    )
    payload = json.dumps(
        {
            "List": {
                "id": 804,
                "reportname": "美妆行业周度市场观察",
                "shortcoutent": summary,
                "addtime": "2026-04-06",
                "tags": ["美妆", "护肤"],
            }
        }
    )
    config = {
        "json_item_path": "List",
        "json_title_path": "reportname",
        "json_body_path": "shortcoutent",
        "json_date_path": "addtime",
        "json_external_id_path": "id",
        "json_tags_path": "tags",
        "json_canonical_url_template": "https://example.com/report/detail?id={id}",
        "content_type": "report",
        "min_content_chars": 30,
    }

    extracted = extract_json_article(payload, "https://example.com/api/report?id=804", config)

    assert extracted["canonical_url"] == "https://example.com/report/detail?id=804"
    assert extracted["content_type"] == "report"
    assert extracted["author"] is None
    assert extracted["topics"] == ["美妆", "护肤"]
    assert extracted["published_at"].date().isoformat() == "2026-04-06"
    assert extracted["external_item_id"] == "804"


def test_request_headers_load_secrets_from_environment(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-value")

    headers = request_headers({"request_headers_env": {"X-API-KEY": "TEST_PROVIDER_KEY"}})

    assert headers["X-API-KEY"] == "secret-value"


def test_api_web_fallback_detects_thin_json_and_keeps_api_identity():
    api_article = {
        "external_item_id": "804",
        "canonical_url": "https://example.com/report/804",
        "body": "摘要",
        "published_at": None,
        "topics": ["美妆"],
        "content_completeness": "unknown",
    }
    web_article = {
        "body": "完整正文。" * 180,
        "published_at": datetime(2026, 8, 30, tzinfo=UTC),
        "topics": ["行业研究"],
        "media": [{"url": "https://example.com/cover.jpg"}],
        "content_completeness": "full",
    }

    assert api_needs_web_fallback(
        api_article, {"article_response_format": "json", "api_fallback_min_chars": 400}
    )
    assert not api_needs_web_fallback(api_article, {"article_response_format": "html"})

    merged = merge_api_web_detail(api_article, web_article)
    assert merged["external_item_id"] == "804"
    assert merged["canonical_url"] == "https://example.com/report/804"
    assert merged["published_at"] == web_article["published_at"]
    assert merged["body"] == web_article["body"]
    assert merged["topics"] == ["美妆", "行业研究"]
    assert "api_web_fallback" in merged["validation_warnings"]


def test_api_publication_window_uses_frozen_coverage_date():
    run = CrawlRun(
        source_id=1,
        coverage_date=date(2026, 8, 30),
        publication_timezone="Asia/Shanghai",
    )
    config = {"publication_timezone": "Asia/Shanghai", "api_publication_window_days": 30}

    assert api_within_publication_window(
        {"published_at": datetime(2026, 8, 1, 10, tzinfo=UTC)}, run, config
    )
    assert not api_within_publication_window(
        {"published_at": datetime(2026, 7, 31, 10, tzinfo=UTC)}, run, config
    )
    assert api_within_publication_window({"published_at": None}, run, config)


def test_source_window_is_seven_days_initial_then_one_day(session_factory):
    with session_factory() as session:
        source, run = seed(session)
        run.coverage_date = date(2026, 8, 30)
        run.publication_timezone = "UTC"
        assert source_publication_window_days(session, source) == 7
        assert within_publication_window(
            {"published_at": datetime(2026, 8, 24, 5, tzinfo=UTC)},
            run,
            {"publication_timezone": "UTC"},
            days=7,
        )
        assert not within_publication_window(
            {"published_at": datetime(2026, 8, 23, 5, tzinfo=UTC)},
            run,
            {"publication_timezone": "UTC"},
            days=7,
        )
        extracted = extract_article(
            (FIXTURES / "article.html").read_text(),
            "https://example.com/articles/open-story",
            CONFIG,
        )
        assert ingest_article(session, source, run, extracted) == "new"
        assert source_publication_window_days(session, source) == 1
