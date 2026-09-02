from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx
from sqlalchemy.orm import Session, sessionmaker

from . import web_ingestion as web
from .feed_sync import FeedSyncPlan, plan_feed_sync
from .models import CrawlRun, PageSnapshot, Source, SourceSyncState
from .redfox_wechat import (
    detail_to_extracted,
    parse_list_page,
    pick_articles,
    redfox_page_needs_next,
    target_publication_date,
)

STATIC_HTTP = "static_http"
FEED_DIRECT = "feed_direct"
SITEMAP_HTTP = "sitemap_http"
JSON_API = "json_api"
PROVIDER_API = "provider_api"
ENGINE_KEYS = (STATIC_HTTP, FEED_DIRECT, SITEMAP_HTTP, JSON_API, PROVIDER_API)


class ExecutionEngineConfigurationError(ValueError):
    """A source configuration cannot resolve to one executable engine."""


@dataclass
class CrawlStats:
    fetched: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0
    rejected: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)
    rejection_messages: list[str] = field(default_factory=list)

    def record_ingestion(self, result: str) -> None:
        if result == "new":
            self.new += 1
        elif result == "updated":
            self.updated += 1
        else:
            self.skipped += 1


@dataclass(frozen=True)
class EngineContext:
    session: Session
    source: Source
    run: CrawlRun
    client: httpx.AsyncClient
    config: dict
    attempts: int
    discovery_url: str
    discovery_method: str
    listing: httpx.Response
    listing_snapshot: PageSnapshot
    sync_state: SourceSyncState | None
    stats: CrawlStats


@dataclass(frozen=True)
class ListingOutcome:
    article_urls: list[str] = field(default_factory=list)
    feed_plan: FeedSyncPlan | None = None
    allow_empty: bool = False


class ExecutionEngine(Protocol):
    key: str
    discovery_method: str
    listing_page_type: str

    def uses_conditional_sync(self, source: Source, http_method: str) -> bool: ...

    def discovery_headers(
        self, sync_state: SourceSyncState | None
    ) -> dict[str, str] | None: ...

    async def process_listing(self, context: EngineContext) -> ListingOutcome: ...

    def extract_detail(self, payload: str, url: str, config: dict) -> dict: ...


class BaseExecutionEngine:
    key = ""
    discovery_method = ""
    listing_page_type = "listing"

    def uses_conditional_sync(self, source: Source, http_method: str) -> bool:
        return False

    def discovery_headers(
        self, sync_state: SourceSyncState | None
    ) -> dict[str, str] | None:
        return None

    def extract_detail(self, payload: str, url: str, config: dict) -> dict:
        if config.get("article_response_format") == "json":
            return web.extract_json_article(payload, url, config)
        return web.extract_article(payload, url, config)


class StaticHttpEngine(BaseExecutionEngine):
    key = STATIC_HTTP
    discovery_method = "html"
    listing_page_type = "listing"

    async def process_listing(self, context: EngineContext) -> ListingOutcome:
        return ListingOutcome(
            article_urls=web.discover_article_urls(
                context.listing.text, context.source.start_url, context.config
            )
        )


class SitemapHttpEngine(BaseExecutionEngine):
    key = SITEMAP_HTTP
    discovery_method = "sitemap"
    listing_page_type = "sitemap"

    async def process_listing(self, context: EngineContext) -> ListingOutcome:
        return ListingOutcome(
            article_urls=web.discover_sitemap_urls(
                context.listing.text, context.config
            )
        )


class JsonApiEngine(BaseExecutionEngine):
    key = JSON_API
    discovery_method = "json"
    listing_page_type = "listing_api"

    async def process_listing(self, context: EngineContext) -> ListingOutcome:
        return ListingOutcome(
            article_urls=web.discover_json_urls(
                context.listing.text,
                context.source.start_url,
                context.config,
            )
        )


class FeedDirectEngine(BaseExecutionEngine):
    key = FEED_DIRECT
    discovery_method = "feed"
    listing_page_type = "feed"

    def uses_conditional_sync(self, source: Source, http_method: str) -> bool:
        return source.channel_type == "rss" and http_method == "GET"

    def discovery_headers(
        self, sync_state: SourceSyncState | None
    ) -> dict[str, str] | None:
        return web._feed_request_headers(sync_state)

    async def process_listing(self, context: EngineContext) -> ListingOutcome:
        plan = None
        if self.uses_conditional_sync(context.source, context.discovery_method):
            plan = plan_feed_sync(
                context.listing.text,
                context.config,
                context.sync_state.recent_entries if context.sync_state else None,
            )
            if not plan.recent_entries:
                raise ValueError("no_feed_entries")
        entries = plan.entries if plan is not None else None
        if context.config.get("ingest_feed_content"):
            articles = web.extract_feed_articles(
                context.listing.text,
                context.config,
                entries=entries,
            )
            if not articles and (plan is None or plan.entries):
                raise ValueError("no_valid_feed_articles")
            for extracted in articles:
                context.stats.fetched += 1
                result = web.ingest_article(
                    context.session,
                    context.source,
                    context.run,
                    extracted,
                    context.listing_snapshot.id,
                )
                context.stats.record_ingestion(result)
            return ListingOutcome(feed_plan=plan, allow_empty=True)
        urls = web.discover_feed_urls(
            context.listing.text,
            context.config,
            entries=entries,
        )
        return ListingOutcome(
            article_urls=urls,
            feed_plan=plan,
            allow_empty=bool(plan is not None and not plan.entries),
        )


async def _fetch_provider_snapshot(
    context: EngineContext,
    *,
    url: str,
    page_type: str,
    method: str,
    json_data: dict,
) -> tuple[httpx.Response, PageSnapshot]:
    try:
        response = await web.fetch_with_retry(
            context.client,
            url,
            attempts=context.attempts,
            method=method,
            json_data=json_data,
        )
    except httpx.HTTPStatusError as exc:
        web.save_response_snapshot(
            context.session,
            context.run,
            url=url,
            page_type=page_type,
            request_method=method,
            response=exc.response,
            error_text=str(exc),
        )
        raise
    except httpx.TransportError as exc:
        web.save_response_snapshot(
            context.session,
            context.run,
            url=url,
            page_type=page_type,
            request_method=method,
            error_text=str(exc),
        )
        raise
    snapshot = web.save_response_snapshot(
        context.session,
        context.run,
        url=url,
        page_type=page_type,
        request_method=method,
        response=response,
        error_text=(
            f"http_status:{response.status_code}" if not response.is_success else None
        ),
    )
    response.raise_for_status()
    return response, snapshot


class RedFoxProviderEngine(BaseExecutionEngine):
    key = PROVIDER_API
    discovery_method = "json"
    listing_page_type = "listing_api"

    async def process_listing(self, context: EngineContext) -> ListingOutcome:
        timezone_name = str(
            context.run.publication_timezone
            or context.config.get("publication_timezone")
            or "Asia/Shanghai"
        )
        target_date = context.run.coverage_date or target_publication_date(
            context.run.started_at, timezone_name
        )
        page = parse_list_page(context.listing.text)
        listed_items = list(page.items)
        discovery_payload = dict(context.config.get("discovery_json") or {})
        offset = int(discovery_payload.get("offset", 0))
        page_count = 1
        max_pages = min(
            max(int(context.config.get("max_listing_pages", 10)), 1), 50
        )
        while redfox_page_needs_next(
            page,
            offset=offset,
            target_date=target_date,
            timezone_name=timezone_name,
        ):
            if page_count >= max_pages:
                raise ValueError(f"redfox_date_boundary_not_reached:{target_date}")
            next_offset = offset + len(page.items)
            if next_offset <= offset:
                raise ValueError("redfox_pagination_did_not_advance")
            discovery_payload["offset"] = next_offset
            next_listing, _ = await _fetch_provider_snapshot(
                context,
                url=context.discovery_url,
                page_type=self.listing_page_type,
                method=context.discovery_method,
                json_data=discovery_payload,
            )
            page = parse_list_page(next_listing.text)
            listed_items.extend(page.items)
            offset = next_offset
            page_count += 1

        picked = pick_articles(
            listed_items,
            context.config,
            target_date=target_date,
            publication_timezone=timezone_name,
        )
        delay = max(float(context.config.get("request_delay_seconds", 2)), 0.5)
        detail_url = str(context.config["detail_url"])
        min_chars = int(context.config.get("min_content_chars", 80))
        for item in picked:
            context.stats.fetched += 1
            try:
                await web.asyncio.sleep(delay)
                work_uuid = str(item.get("workUuid") or "")
                detail, snapshot = await _fetch_provider_snapshot(
                    context,
                    url=detail_url,
                    page_type="article_api",
                    method="POST",
                    json_data={"workUuid": work_uuid},
                )
                extracted = detail_to_extracted(
                    detail.text,
                    item,
                    min_content_chars=min_chars,
                    publication_timezone=timezone_name,
                )
                result = web.ingest_article(
                    context.session,
                    context.source,
                    context.run,
                    extracted,
                    snapshot.id,
                )
                context.stats.record_ingestion(result)
                context.session.commit()
            except web.ContentFormError as exc:
                context.session.rollback()
                context.stats.rejected += 1
                context.stats.rejection_messages.append(
                    f"{item.get('workUuid')}: {exc}"
                )
            except Exception as exc:
                context.session.rollback()
                context.stats.errors += 1
                context.stats.error_messages.append(f"{item.get('workUuid')}: {exc}")
        return ListingOutcome(allow_empty=True)


ENGINES: dict[str, ExecutionEngine] = {
    engine.key: engine
    for engine in (
        StaticHttpEngine(),
        FeedDirectEngine(),
        SitemapHttpEngine(),
        JsonApiEngine(),
        RedFoxProviderEngine(),
    )
}


def resolve_execution_engine_key(channel_type: str, parser_config: dict) -> str:
    config = parser_config or {}
    method = str(config.get("discovery_method") or "html")
    provider = str(config.get("provider") or "direct")
    if provider == "redfox" and method != "json":
        raise ExecutionEngineConfigurationError(
            "redfox requires discovery_method=json"
        )
    derived = (
        PROVIDER_API
        if provider == "redfox"
        else {
            "html": STATIC_HTTP,
            "feed": FEED_DIRECT,
            "sitemap": SITEMAP_HTTP,
            "json": JSON_API,
        }.get(method)
    )
    if derived is None:
        raise ExecutionEngineConfigurationError(
            f"unsupported discovery_method: {method}"
        )
    explicit = config.get("execution_engine")
    if explicit is not None and str(explicit) != derived:
        raise ExecutionEngineConfigurationError(
            f"execution_engine {explicit} conflicts with source configuration ({derived})"
        )
    allowed_by_channel = {
        "web": {STATIC_HTTP, SITEMAP_HTTP},
        "rss": {FEED_DIRECT},
        "api": {JSON_API},
        "third_party_feed": {FEED_DIRECT, JSON_API, PROVIDER_API},
    }
    if derived not in allowed_by_channel.get(channel_type, set()):
        raise ExecutionEngineConfigurationError(
            f"channel {channel_type} cannot use execution_engine {derived}"
        )
    if derived == PROVIDER_API and provider != "redfox":
        raise ExecutionEngineConfigurationError(f"unsupported provider: {provider}")
    return derived


def execution_engine_for_source(source: Source) -> ExecutionEngine:
    key = resolve_execution_engine_key(source.channel_type, source.parser_config or {})
    return ENGINES[key]


async def crawl_source_with_engine(
    factory: sessionmaker, source_id: int, run_id: int
) -> None:
    engine: ExecutionEngine | None = None
    with factory() as session:
        source = session.get(Source, source_id)
        if source is not None:
            engine = execution_engine_for_source(source)
    if engine is None:
        return
    await web.crawl_http_source(factory, source_id, run_id, engine=engine)
