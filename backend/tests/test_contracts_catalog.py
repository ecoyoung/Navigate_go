from pathlib import Path
from types import SimpleNamespace

from app.catalog import load_catalog, load_redfox_wechat_accounts
from app.content_processing import evaluate_relevance
from app.contracts import build_contract, detect_language
from app.web_ingestion import (
    discover_feed_urls,
    discover_json_urls,
    discover_sitemap_urls,
    extract_feed_articles,
)


def test_language_detection_and_contract_source_labels():
    source = SimpleNamespace(
        id=7,
        name="中文行业媒体",
        source_region="CN",
        source_type="trade_media",
        default_language="zh-CN",
        parser_config={"access_level": "public"},
    )
    extracted = {
        "title": "美妆品牌发布年度增长战略",
        "body": "该品牌宣布扩建供应链，并计划进入更多零售渠道。",
        "description": "关注品牌、渠道与供应链变化。",
        "original_url": "https://example.cn/news/1",
        "canonical_url": "https://example.cn/news/1",
        "author": "编辑部",
        "published_at": None,
    }

    contract = build_contract(extracted, source)

    assert detect_language(extracted["title"] + extracted["body"]) == "zh-CN"
    assert contract.language == "zh-CN"
    assert contract.source_region == "CN"
    assert contract.source_type == "trade_media"
    assert contract.schema_version == "article.v1.1"
    assert contract.content_type == "article"
    assert contract.topics == []

    tagged = build_contract(
        {**extracted, "topics": ["护肤", "美妆", "护肤", "https://example.com/tag"]},
        source,
    )
    assert tagged.topics == ["护肤", "美妆"]


def test_midstream_relevance_requires_industry_and_event_terms():
    config = {
        "scope_mode": "keyword",
        "industry_keywords": ["美妆", "护肤", "beauty"],
        "event_keywords": ["融资", "发布", "acquisition"],
    }

    relevant = evaluate_relevance(
        SimpleNamespace(title="护肤品牌完成新一轮融资", excerpt=None, body="", topics=[]), config
    )
    tutorial = evaluate_relevance(
        SimpleNamespace(title="夏日护肤教程", excerpt=None, body="步骤与心得", topics=[]), config
    )
    cars = evaluate_relevance(
        SimpleNamespace(title="汽车公司完成融资", excerpt=None, body="", topics=[]), config
    )
    tagged = evaluate_relevance(
        SimpleNamespace(title="公司完成新一轮融资", excerpt=None, body="", topics=["护肤"]),
        config,
    )

    assert relevant.is_relevant
    assert relevant.matched_topics == ["护肤"]
    assert not tutorial.is_relevant and tutorial.reason == "no_event_match"
    assert not cars.is_relevant and cars.reason == "no_industry_match"
    assert tagged.is_relevant and tagged.reason == "tag_match"
    assert tagged.matched_topics == ["护肤"]


def test_feed_discovery_deduplicates_and_honors_limit():
    feed = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><link>https://example.com/news/1?utm_source=rss</link></item>
      <item><link>https://example.com/news/1</link></item>
      <item><link>https://example.com/news/2</link></item>
    </channel></rss>"""

    assert discover_feed_urls(feed, {"max_articles": 1}) == ["https://example.com/news/1"]


def test_feed_discovery_keeps_articles_before_midstream_filtering():
    feed = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><title>汽车公司完成融资</title><link>https://example.com/cars</link></item>
      <item><title>美妆品牌完成融资</title><link>https://example.com/beauty</link></item>
    </channel></rss>"""
    config = {
        "scope_mode": "keyword",
        "industry_keywords": ["美妆"],
        "event_keywords": ["品牌", "融资"],
    }

    assert discover_feed_urls(feed, config) == [
        "https://example.com/cars",
        "https://example.com/beauty",
    ]


def test_feed_content_can_be_normalized_without_fetching_article_page():
    feed = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><item>
      <title>行业新闻样本</title><link>https://example.com/news/1</link>
      <guid>feed-item-1</guid>
      <category>美妆</category><category>融资</category>
      <pubDate>Wed, 27 Aug 2026 10:00:00 +0800</pubDate>
      <media:thumbnail url="https://example.com/cover.jpg" />
      <description><![CDATA[<p>这是一段足够长的新闻正文，用于验证官方 Feed
      可以直接进入统一内容格式，而不必再次请求客户端渲染的文章详情页面。</p>]]></description>
    </item></channel></rss>"""

    articles = extract_feed_articles(feed, {"max_articles": 1, "min_content_chars": 30})

    assert articles[0]["content_type"] == "news"
    assert articles[0]["canonical_url"] == "https://example.com/news/1"
    assert articles[0]["topics"] == ["美妆", "融资"]
    assert articles[0]["external_item_id"] == "feed-item-1"
    assert articles[0]["media"][0]["url"] == "https://example.com/cover.jpg"
    assert articles[0]["content_completeness"] == "partial"
    assert "summary_only" in articles[0]["validation_warnings"]
    assert "官方 Feed" in articles[0]["body"]


def test_json_listing_discovers_article_urls_from_configured_items():
    payload = '{"newsList":[{"news_id":1001},{"news_id":1002}]}'
    config = {
        "items_path": "newsList",
        "article_url_template": "/news/{news_id}.html",
        "max_articles": 1,
    }

    assert discover_json_urls(payload, "https://example.com/", config) == [
        "https://example.com/news/1001.html"
    ]


def test_sitemap_discovery_keeps_configured_article_paths():
    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/news/story</loc></url>
      <url><loc>https://example.com/about</loc></url>
    </urlset>"""
    config = {"article_url_pattern": r"/news/", "max_articles": 1}

    assert discover_sitemap_urls(sitemap, config) == ["https://example.com/news/story"]


def test_catalog_has_unique_bilingual_source_inventory():
    catalog_path = Path(__file__).parents[1] / "config" / "sites.json"
    catalog = load_catalog(catalog_path)

    assert len(catalog) == 277
    assert len({item.id for item in catalog}) == 277
    assert sum(item.default_language == "en" for item in catalog) == 18
    assert sum(item.default_language == "zh-CN" for item in catalog) == 259
    assert all(item.source_region and item.source_type and item.source_tags for item in catalog)
    semantic_keys = {"scope_mode", "industry_keywords", "event_keywords"}
    assert all(not (semantic_keys & item.parser_config.keys()) for item in catalog)
    assert all(item.processing_config.get("scope_mode") for item in catalog)
    redfox = next(item for item in catalog if item.id == "huazhuangpinbao_wechat")
    assert redfox.parser_config["publication_date_mode"] == "previous_day"
    assert redfox.parser_config["publication_timezone"] == "Asia/Shanghai"
    assert redfox.parser_config["exclude_explicit_pinned"] is True
    assert redfox.parser_config["exclude_explicit_advertising"] is True
    assert not {
        "skip_first_article",
        "skip_ad_titles",
        "skip_pinned",
    } & redfox.parser_config.keys()


def test_redfox_wechat_registry_has_complete_disabled_unique_accounts():
    account_path = Path(__file__).parents[1] / "config" / "wechat_accounts.json"
    accounts = load_redfox_wechat_accounts(account_path)

    assert len(accounts) == 244
    assert len({item.catalog_id for item in accounts}) == 244
    all_names = [name for item in accounts for name in (item.name, *item.aliases)]
    assert len(all_names) == 247
    assert len(set(all_names)) == 247
    assert sum(item.status == "ready" for item in accounts) == 244
    assert sum(item.status == "pending" for item in accounts) == 0
    assert not any(item.is_enabled for item in accounts)
    assert {"卡诗Kerastase", "潘婷", "尼尔森IQ", "凯度KANTAR"} <= set(
        all_names
    )


def test_premium_beauty_news_uses_its_official_english_feed():
    catalog = load_catalog(Path(__file__).parents[1] / "config" / "sites.json")
    source = next(item for item in catalog if item.id == "premium_beauty_news")

    assert str(source.start_url) == (
        "https://www.premiumbeautynews.com/spip.php?page=backend&lang=en"
    )
    assert source.resolved_channel_type == "rss"
    assert source.crawl_strategy == "feed"
    assert source.parser_config["discovery_method"] == "feed"
    assert source.parser_config["body_selector"] == ".article-text"
    assert source.parser_config["date_selector"] == ".header-date span"
    assert source.parser_config["publication_timezone"] == "Europe/Paris"
    assert source.parser_config["request_delay_seconds"] == 1.0
