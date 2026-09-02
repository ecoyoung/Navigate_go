from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ProviderRequestCache
from .secrets import require_secret

BASE_URL = "https://api.firecrawl.dev/v2"
SEARCH_LIMIT_MAX = 50
SCRAPE_BATCH_MAX = 50


class FirecrawlError(RuntimeError):
    pass


class FirecrawlClient:
    def __init__(self, api_key: str, *, base_url: str = BASE_URL, timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> FirecrawlClient:
        return cls(require_secret("FIRECRAWL_API_KEY"))

    def _post(self, path: str, payload: dict) -> dict:
        try:
            response = httpx.post(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise FirecrawlError("firecrawl_network_error") from exc
        if response.status_code == 402:
            raise FirecrawlError("firecrawl_credit_limit")
        if response.status_code == 429:
            raise FirecrawlError("firecrawl_rate_limit")
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FirecrawlError(f"firecrawl_http_{response.status_code}") from exc
        if data.get("success") is False:
            raise FirecrawlError("firecrawl_request_failed")
        return data

    def search(self, query: str, *, limit: int = 10, search_options: dict | None = None) -> dict:
        if not 1 <= limit <= SEARCH_LIMIT_MAX:
            raise ValueError(f"Firecrawl search limit must be 1-{SEARCH_LIMIT_MAX}")
        allowed = {
            "sources",
            "safe",
            "tbs",
            "location",
            "includeDomains",
            "excludeDomains",
            "categories",
        }
        options = dict(search_options or {})
        unexpected = set(options) - allowed
        if unexpected:
            raise ValueError(f"Unsupported Firecrawl search options: {sorted(unexpected)}")
        if options.get("includeDomains") and options.get("excludeDomains"):
            raise ValueError("includeDomains and excludeDomains are mutually exclusive")
        return self._post("/search", {"query": query, "limit": limit, **options})

    def scrape(self, url: str) -> dict:
        return self._post(
            "/scrape",
            {"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def cached_search(
    db: Session,
    client: FirecrawlClient,
    *,
    query: str,
    limit: int,
    search_options: dict | None = None,
    ttl: timedelta = timedelta(hours=12),
) -> tuple[dict, bool, int]:
    payload = {"query": " ".join(query.split()), "limit": limit, **(search_options or {})}
    request_hash = _request_hash(payload)
    cached = db.scalar(
        select(ProviderRequestCache).where(
            ProviderRequestCache.provider == "firecrawl",
            ProviderRequestCache.operation == "search",
            ProviderRequestCache.request_hash == request_hash,
        )
    )
    now = datetime.now(UTC)
    cached_expires = (
        cached.expires_at.replace(tzinfo=UTC)
        if cached and cached.expires_at.tzinfo is None
        else cached.expires_at
        if cached
        else None
    )
    if cached and cached_expires and cached_expires > now:
        return cached.response, True, 0
    response = client.search(payload["query"], limit=limit, search_options=search_options)
    if cached is None:
        cached = ProviderRequestCache(
            provider="firecrawl",
            operation="search",
            request_hash=request_hash,
            response=response,
            credits_used=2,
            expires_at=now + ttl,
        )
        db.add(cached)
    else:
        cached.response = response
        cached.credits_used = 2
        cached.created_at = now
        cached.expires_at = now + ttl
    db.flush()
    return response, False, 2


def search_results(payload: dict) -> list[dict]:
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = data.get("web") or data.get("results") or []
    return [item for item in data if isinstance(item, dict) and item.get("url")]
