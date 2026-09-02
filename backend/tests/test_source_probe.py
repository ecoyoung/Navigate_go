from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.outbound_policy import UnsafeOutboundURLError, validate_public_http_url
from app.source_probe import (
    ProbeDocument,
    SourcePipeline,
    analyze_probe_document,
    pipeline_to_legacy_parser_config,
)
from app.source_probe_fetch import ProbeFetchError, probe_public_url

FIXTURES = Path(__file__).parent / "fixtures" / "source_probe"
NOW = datetime(2026, 8, 28, 10, tzinfo=UTC)


def public_dns(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def fixture_document(name: str, content_type: str, url: str) -> ProbeDocument:
    return ProbeDocument(
        requested_url=url,
        final_url=url,
        observed_at=NOW,
        status_code=200,
        content_type=content_type,
        body=(FIXTURES / name).read_text(encoding="utf-8"),
        robots_status="allowed",
    )


def test_rss_full_feed_produces_verified_draft_pipeline():
    result = analyze_probe_document(
        fixture_document("rss-full.xml", "application/rss+xml", "https://example.com/feed.xml")
    )

    assert result.schema_version == "source-probe-result.v1"
    assert result.detected_format == "rss"
    assert result.subtype == "full_feed"
    assert result.outcome == "success"
    assert result.candidates[0].verified is True
    assert result.recommended_pipeline.schema_version == "source-pipeline.v1"
    assert result.recommended_pipeline.state == "draft"
    assert result.recommended_pipeline.channel_type == "rss"
    assert result.recommended_pipeline.content_chain[0] == "feed_full_content"


def test_atom_summary_is_distinct_but_uses_feed_pipeline():
    result = analyze_probe_document(
        fixture_document(
            "atom-summary.xml",
            "application/atom+xml",
            "https://example.com/atom.xml",
        )
    )

    assert result.detected_format == "atom"
    assert result.subtype == "summary_feed"
    assert result.recommended_pipeline.channel_type == "rss"
    assert result.recommended_pipeline.content_chain == ["feed_summary", "html_detail"]


@pytest.mark.parametrize(
    ("fixture", "expected_format", "required_verification"),
    [
        ("sitemap-urlset.xml", "sitemap_urlset", "verify_article_detail_samples"),
        ("sitemap-index.xml", "sitemap_index", "probe_child_sitemaps"),
    ],
)
def test_sitemap_routes_are_not_confused_with_article_pages(
    fixture: str, expected_format: str, required_verification: str
):
    result = analyze_probe_document(
        fixture_document(fixture, "application/xml", f"https://example.com/{fixture}")
    )

    assert result.detected_format == expected_format
    assert result.recommended_pipeline.engine == "sitemap_http"
    assert required_verification in result.recommended_pipeline.requires_verification
    if expected_format == "sitemap_index":
        assert result.article_samples == []
        assert "child_sitemaps_require_probe" in result.diagnostics


def test_json_probe_discovers_candidate_item_path_without_freezing_mapping():
    result = analyze_probe_document(
        fixture_document(
            "nested-list.json",
            "application/json",
            "https://example.com/api/news",
        )
    )

    assert result.detected_format == "json"
    assert result.subtype == "json_listing"
    assert result.json_item_paths == ["data.items"]
    assert result.recommended_pipeline.engine == "json_api"
    assert result.recommended_pipeline.source_family == "website"
    assert result.recommended_pipeline.state == "draft"
    assert "verify_json_mapping" in result.recommended_pipeline.requires_verification


def test_html_advertised_feed_remains_unverified_and_cannot_auto_activate():
    result = analyze_probe_document(
        fixture_document(
            "alternate-feed.html",
            "text/html",
            "https://example.com/news",
        )
    )

    assert result.detected_format == "html"
    assert result.subtype == "html_listing"
    assert result.outcome == "partial"
    feed = next(item for item in result.candidates if item.resource_kind == "rss")
    assert feed.url == "https://example.com/feed.xml"
    assert feed.verified is False
    assert result.recommended_pipeline.channel_type == "rss"
    assert "verify_feed_endpoint" in result.recommended_pipeline.requires_verification


def test_http_200_challenge_overrides_html_format():
    result = analyze_probe_document(
        fixture_document(
            "challenge.html",
            "text/html",
            "https://example.com/news",
        )
    )

    assert result.outcome == "blocked"
    assert result.detected_format == "blocked"
    assert result.subtype == "challenge"
    assert result.recommended_pipeline.state == "blocked"


def test_normal_article_that_mentions_captcha_is_not_a_challenge():
    body = """
    <html><head><title>Security industry analysis</title></head>
    <body><article><h1>Why captcha systems are changing</h1>
    <p>This long editorial article discusses captcha technology without presenting
    a verification form or access gate. It contains ordinary reporting and analysis
    for readers, so a keyword alone must never classify the response as blocked.</p>
    </article></body></html>
    """
    result = analyze_probe_document(
        ProbeDocument(
            requested_url="https://example.com/news/captcha-analysis",
            final_url="https://example.com/news/captcha-analysis",
            observed_at=NOW,
            status_code=200,
            content_type="text/html",
            body=body,
            robots_status="allowed",
        )
    )

    assert result.detected_format == "html"
    assert result.outcome != "blocked"


def test_probe_is_deterministic_for_the_same_response_facts():
    document = fixture_document(
        "rss-full.xml", "application/rss+xml", "https://example.com/feed.xml"
    )
    first = analyze_probe_document(document)
    second = analyze_probe_document(document)

    assert first == second


def test_only_manually_verified_pipeline_can_compile_to_current_parser_config():
    result = analyze_probe_document(
        fixture_document("rss-full.xml", "application/rss+xml", "https://example.com/feed.xml")
    )
    with pytest.raises(ValueError, match="verified pipeline"):
        pipeline_to_legacy_parser_config(result.recommended_pipeline)

    values = result.recommended_pipeline.model_dump()
    values["state"] = "verified"
    verified = SourcePipeline.model_validate(values)
    compiled = pipeline_to_legacy_parser_config(verified)

    assert compiled == {
        "pipeline_schema_version": "source-pipeline.v1",
        "pipeline_id": verified.pipeline_id,
        "probe_id": result.probe_id,
        "execution_engine": "feed_direct",
        "discovery_method": "feed",
        "access_level": "public",
        "discovery_url": "https://example.com/feed.xml",
        "ingest_feed_content": True,
        "content_completeness": "full",
    }


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "https://example.com:8443/",
        "https://user:pass@example.com/",
    ],
)
def test_outbound_policy_rejects_non_public_targets(url: str):
    with pytest.raises(UnsafeOutboundURLError):
        validate_public_http_url(url, resolver=public_dns)


def test_outbound_policy_rejects_dns_with_any_private_address():
    with pytest.raises(UnsafeOutboundURLError, match="dns_non_public_address"):
        validate_public_http_url(
            "https://example.com/",
            resolver=lambda _host, _port: ["93.184.216.34", "10.0.0.8"],
        )


@pytest.mark.asyncio
async def test_network_probe_uses_robots_and_mock_transport_only():
    rss = (FIXTURES / "rss-full.xml").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(
            200,
            text=rss,
            headers={"content-type": "application/rss+xml"},
            request=request,
        )

    result = await probe_public_url(
        "https://example.com/feed.xml",
        observed_at=NOW,
        resolver=public_dns,
        transport=httpx.MockTransport(handler),
    )

    assert result.outcome == "success"
    assert result.access.robots == "allowed"
    assert result.detected_format == "rss"


@pytest.mark.asyncio
async def test_missing_robots_file_allows_public_probe():
    rss = (FIXTURES / "rss-full.xml").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            text=rss,
            headers={"content-type": "application/rss+xml"},
            request=request,
        )

    result = await probe_public_url(
        "https://example.com/feed.xml",
        observed_at=NOW,
        resolver=public_dns,
        transport=httpx.MockTransport(handler),
    )

    assert result.outcome == "success"
    assert result.access.robots == "allowed"


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_rejected_before_second_request():
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
            request=request,
        )

    with pytest.raises(ProbeFetchError, match="non_public_ip"):
        await probe_public_url(
            "https://example.com/news",
            observed_at=NOW,
            resolver=public_dns,
            transport=httpx.MockTransport(handler),
        )

    assert all("127.0.0.1" not in item for item in requested_paths)


@pytest.mark.asyncio
async def test_response_size_limit_is_enforced():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(200, content=b"x" * 1025, request=request)

    with pytest.raises(ProbeFetchError, match="response_too_large"):
        await probe_public_url(
            "https://example.com/news",
            observed_at=NOW,
            resolver=public_dns,
            transport=httpx.MockTransport(handler),
            max_response_bytes=1024,
        )


def test_api_source_registration_rejects_secret_bearing_request_config(client):
    response = client.post(
        "/api/v1/sources",
        json={
            "name": "Unsafe provider",
            "channel_type": "third_party_feed",
            "start_url": "https://example.com/api",
            "parser_config": {
                "provider": "example",
                "discovery_method": "json",
                "discovery_url": "https://example.com/api",
                "request_headers_env": {"Authorization": "SECRET_TOKEN"},
            },
        },
    )

    assert response.status_code == 422
    assert "secret-bearing request fields" in response.json()["details"][0]["msg"]
    assert client.get("/api/v1/sources").json() == []
