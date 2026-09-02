from datetime import datetime
from urllib.parse import urljoin

import httpx

from .outbound_policy import Resolver, UnsafeOutboundURLError, validate_public_http_url
from .source_probe import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ProbeDocument,
    RobotsStatus,
    SourceProbeResult,
    analyze_probe_document,
)
from .web_ingestion import USER_AGENT, robots_allows, robots_url

MAX_REDIRECTS = 3


class ProbeFetchError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def _fetch_bounded(
    client: httpx.AsyncClient,
    url: str,
    *,
    resolver: Resolver | None,
    max_response_bytes: int,
) -> tuple[httpx.Response, str]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_http_url(current, resolver=resolver)
        async with client.stream("GET", current) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ProbeFetchError("redirect_without_location")
                current = urljoin(str(response.url), location)
                continue
            length = response.headers.get("content-length")
            if length:
                try:
                    declared_length = int(length)
                except ValueError as exc:
                    raise ProbeFetchError("invalid_content_length") from exc
                if declared_length > max_response_bytes:
                    raise ProbeFetchError("response_too_large")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_response_bytes:
                    raise ProbeFetchError("response_too_large")
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
            body = b"".join(chunks).decode(encoding, errors="replace")
            return response, body
    raise ProbeFetchError("too_many_redirects")


async def probe_public_url(
    url: str,
    *,
    observed_at: datetime,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> SourceProbeResult:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must include timezone")
    validate_public_http_url(url, resolver=resolver)
    timeout = httpx.Timeout(12.0, connect=5.0, read=8.0)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,
        timeout=timeout,
        transport=transport,
        trust_env=False,
    ) as client:
        rules_url = robots_url(url)
        robots_status: RobotsStatus
        try:
            robots_response, robots_body = await _fetch_bounded(
                client,
                rules_url,
                resolver=resolver,
                max_response_bytes=min(max_response_bytes, 256 * 1024),
            )
            if robots_response.status_code == 404:
                robots_status = "allowed"
            elif robots_response.status_code == 200:
                robots_status = (
                    "allowed" if robots_allows(robots_body, url) else "disallowed"
                )
            else:
                robots_status = "unavailable"
        except (httpx.HTTPError, ProbeFetchError, UnsafeOutboundURLError):
            robots_status = "unavailable"

        if robots_status == "disallowed":
            return analyze_probe_document(
                ProbeDocument(
                    requested_url=url,
                    final_url=url,
                    observed_at=observed_at,
                    status_code=0,
                    body="robots disallowed",
                    robots_status="disallowed",
                    robots_url=rules_url,
                )
            )
        if robots_status == "unavailable":
            raise ProbeFetchError("robots_unavailable")

        try:
            response, body = await _fetch_bounded(
                client,
                url,
                resolver=resolver,
                max_response_bytes=max_response_bytes,
            )
        except httpx.TimeoutException as exc:
            raise ProbeFetchError("request_timeout") from exc
        except httpx.HTTPError as exc:
            raise ProbeFetchError("transport_error") from exc
        except UnsafeOutboundURLError as exc:
            raise ProbeFetchError(str(exc)) from exc
        return analyze_probe_document(
            ProbeDocument(
                requested_url=url,
                final_url=str(response.url),
                observed_at=observed_at,
                status_code=response.status_code,
                content_type=response.headers.get("content-type"),
                body=body,
                robots_status=robots_status,
                robots_url=rules_url,
            )
        )
