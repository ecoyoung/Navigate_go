import asyncio

import httpx
from sqlalchemy import func, select

from app.feed_sync import entry_identity, parse_feed, plan_feed_sync
from app.models import ContentItem, CrawlRun, PageSnapshot, RawItem, Source, SourceSyncState
from app.web_ingestion import crawl_http_source, extract_article


def feed(items: list[tuple[str, str, str]]) -> str:
    rows = "".join(
        f"""<item><guid>{guid}</guid><title>{title}</title>
        <link>https://example.com/{guid}</link>
        <pubDate>Thu, 27 Aug 2026 10:00:00 +0800</pubDate>
        <description><![CDATA[<p>{body}</p>]]></description></item>"""
        for guid, title, body in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{rows}</channel></rss>'


def test_feed_plan_prioritizes_unseen_items_and_preserves_backlog():
    initial = feed(
        [
            ("known", "Known", "Known content long enough for a feed item."),
            ("new-1", "New one", "First new content long enough for a feed item."),
            ("new-2", "New two", "Second new content long enough for a feed item."),
        ]
    )
    baseline = plan_feed_sync(initial, {"max_articles": 3}, None)
    known = [baseline.recent_entries[0]]

    first = plan_feed_sync(initial, {"max_articles": 1, "feed_overlap_entries": 1}, known)
    assert first.entries[0]["id"] == "new-1"
    assert first.has_backlog
    assert {item["id"] for item in first.recent_entries} == {"known", "new-1"}

    second = plan_feed_sync(
        initial,
        {"max_articles": 1, "feed_overlap_entries": 1},
        first.recent_entries,
    )
    assert second.entries[0]["id"] == "new-2"
    assert not second.has_backlog


def test_atom_id_and_normalized_link_are_stable_entry_identities():
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>tag:example.com,2026:story-1</id><title>Story</title>
      <link href="https://example.com/story"/></entry></feed>"""
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>Story</title><link>https://example.com/story?utm_source=feed</link>
      </item></channel></rss>"""

    assert entry_identity(parse_feed(atom)[0]) == "tag:example.com,2026:story-1"
    assert entry_identity(parse_feed(rss)[0]) == "https://example.com/story"


def test_premium_beauty_news_feed_date_and_article_selectors():
    rss = """<?xml version="1.0"?><rss version="2.0"
      xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
      <item xml:lang="en"><guid isPermaLink="true">https://www.premiumbeautynews.com/en/story,1</guid>
      <title>Public industry story</title>
      <link>https://www.premiumbeautynews.com/en/story,1</link>
      <dc:date>2026-08-28T20:02:29Z</dc:date>
      <description><![CDATA[
      <p>A sufficiently long public summary for feed discovery.</p>
      ]]></description>
      </item></channel></rss>"""
    entries = parse_feed(rss)
    assert entries[0]["link"] == "https://www.premiumbeautynews.com/en/story,1"
    assert entries[0]["updated"] == "2026-08-28T20:02:29Z"

    html = """<html><head><link rel="canonical"
      href="https://www.premiumbeautynews.com/en/story,1">
      <script type="application/ld+json">{"@type":"NewsArticle",
      "headline":"Public industry story","datePublished":"2026-08-28 22:02:29"}</script>
      </head><body>
      <article class="content"><div class="article-rubtitre"><a>Industry buzz</a></div>
      <h1>Public industry story</h1><div class="header-date"><span>28 August 2026</span></div>
      <div class="article-text"><p>This is the first paragraph of the public article and it contains
      enough factual material for the normalized content pool.</p><p>This second paragraph makes the
      extracted body comfortably longer than the minimum content threshold used by the
      crawler.</p></div>
      <aside>Related navigation must not enter the article body.</aside></article></body></html>"""
    extracted = extract_article(
        html,
        "https://www.premiumbeautynews.com/en/story,1",
        {
            "body_selector": ".article-text",
            "date_selector": ".header-date span",
            "tag_selector": ".article-rubtitre a",
            "publication_timezone": "Europe/Paris",
        },
    )
    assert extracted["published_at"].isoformat() == "2026-08-28T20:02:29+00:00"
    assert extracted["topics"] == ["Industry buzz"]
    assert "Related navigation" not in extracted["body"]


def test_rss_conditional_request_304_update_and_failed_checkpoint(
    session_factory, monkeypatch
):
    first_body = "First version contains enough text to enter the normalized content pool."
    changed_body = "Changed version contains enough text to create a new raw content version."
    responses = [
        (200, feed([("story-1", "Story", first_body)]), {"ETag": '"v1"'}),
        (304, "", {"ETag": '"v1"'}),
        (200, feed([("story-1", "Story", changed_body)]), {"ETag": '"v2"'}),
        (200, "<not-a-feed>", {"ETag": '"v3"'}),
    ]
    feed_headers: list[dict[str, str]] = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, **kwargs):
            request = httpx.Request(method, url, headers=kwargs.get("headers"))
            if str(url).endswith("/robots.txt"):
                return httpx.Response(404, request=request)
            feed_headers.append(dict(request.headers))
            status, body, headers = responses.pop(0)
            return httpx.Response(status, request=request, text=body, headers=headers)

    monkeypatch.setattr("app.web_ingestion.httpx.AsyncClient", FakeAsyncClient)

    with session_factory() as session:
        source = Source(
            name="Incremental feed",
            channel_type="rss",
            start_url="https://example.com/feed.xml",
            normalized_start_url="https://example.com/feed.xml",
            fetch_interval_seconds=3600,
            parser_config={
                "discovery_method": "feed",
                "ingest_feed_content": True,
                "min_content_chars": 30,
                "max_articles": 10,
                "feed_overlap_entries": 1,
                "request_delay_seconds": 0,
            },
        )
        session.add(source)
        session.commit()
        run_ids = []
        for _ in range(4):
            run = CrawlRun(source_id=source.id, trigger="test", status="pending")
            session.add(run)
            session.commit()
            run_ids.append(run.id)
            asyncio.run(crawl_http_source(session_factory, source.id, run.id))

        session.expire_all()
        runs = [session.get(CrawlRun, run_id) for run_id in run_ids]
        state = session.get(SourceSyncState, source.id)
        snapshots = list(
            session.scalars(
                select(PageSnapshot)
                .where(PageSnapshot.crawl_run_id == run_ids[1])
                .order_by(PageSnapshot.id)
            )
        )

        assert [run.status for run in runs] == [
            "succeeded",
            "unchanged",
            "succeeded",
            "failed",
        ]
        assert runs[2].updated_count == 1
        assert feed_headers[0].get("if-none-match") is None
        assert [headers.get("if-none-match") for headers in feed_headers[1:]] == [
            '"v1"',
            '"v1"',
            '"v2"',
        ]
        assert snapshots[-1].http_status == 304
        assert snapshots[-1].body == ""
        assert snapshots[-1].error_text is None
        assert state.etag == '"v2"'
        assert state.last_committed_run_id == run_ids[2]
        assert session.scalar(select(func.count(ContentItem.id))) == 1
        assert session.scalar(select(func.count(RawItem.id))) == 2
