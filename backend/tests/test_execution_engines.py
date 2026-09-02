import asyncio
from types import SimpleNamespace

import httpx

from app.execution_engines import ENGINES


def context(body: str, config: dict, start_url: str = "https://example.com/"):
    request = httpx.Request("GET", start_url)
    return SimpleNamespace(
        listing=httpx.Response(200, request=request, text=body),
        source=SimpleNamespace(start_url=start_url),
        config=config,
    )


def test_static_http_engine_owns_html_discovery():
    outcome = asyncio.run(
        ENGINES["static_http"].process_listing(
            context(
                '<main><a class="story" href="/news/one">One</a></main>',
                {
                    "link_selector": "a.story[href]",
                    "article_url_pattern": r"^https://example\.com/news/",
                },
            )
        )
    )

    assert outcome.article_urls == ["https://example.com/news/one"]


def test_sitemap_http_engine_owns_sitemap_discovery():
    outcome = asyncio.run(
        ENGINES["sitemap_http"].process_listing(
            context(
                """<?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>https://example.com/news/one</loc></url>
                </urlset>""",
                {"article_url_pattern": r"^https://example\.com/news/"},
            )
        )
    )

    assert outcome.article_urls == ["https://example.com/news/one"]


def test_json_api_engine_owns_json_listing_discovery():
    outcome = asyncio.run(
        ENGINES["json_api"].process_listing(
            context(
                '{"data":{"items":[{"id":1},{"id":2}]}}',
                {
                    "items_path": "data.items",
                    "article_url_template": "https://example.com/api/article/{id}",
                    "max_articles": 10,
                },
            )
        )
    )

    assert outcome.article_urls == [
        "https://example.com/api/article/1",
        "https://example.com/api/article/2",
    ]
