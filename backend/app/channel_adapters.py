import logging
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import sessionmaker

from .execution_engines import (
    ExecutionEngineConfigurationError,
    crawl_source_with_engine,
    resolve_execution_engine_key,
)
from .models import CrawlRun, Source, utcnow

CHANNEL_TYPES = ("web", "rss", "api", "third_party_feed")
logger = logging.getLogger("navigate.crawl")


class ChannelConfigurationError(ValueError):
    """A source does not contain the rules required by its selected channel."""


class ChannelAdapter(Protocol):
    channel_type: str

    def validate(self, source: Source) -> None: ...

    async def crawl(self, factory: sessionmaker, source_id: int, run_id: int) -> None: ...


@dataclass(frozen=True)
class HttpChannelAdapter:
    channel_type: str
    discovery_methods: frozenset[str]
    require_provider: bool = False

    def validate(self, source: Source) -> None:
        config = source.parser_config or {}
        method = str(config.get("discovery_method") or "html")
        if method not in self.discovery_methods:
            allowed = ", ".join(sorted(self.discovery_methods))
            raise ChannelConfigurationError(
                f"channel {self.channel_type} requires discovery_method in: {allowed}"
            )
        if self.require_provider and not config.get("provider"):
            raise ChannelConfigurationError("third_party_feed requires parser_config.provider")
        if self.channel_type == "third_party_feed" and not config.get("discovery_url"):
            raise ChannelConfigurationError("third_party_feed requires parser_config.discovery_url")
        try:
            resolve_execution_engine_key(self.channel_type, config)
        except ExecutionEngineConfigurationError as exc:
            raise ChannelConfigurationError(str(exc)) from exc
        timezone_name = config.get("publication_timezone")
        if timezone_name is not None:
            if not isinstance(timezone_name, str) or not timezone_name.strip():
                raise ChannelConfigurationError(
                    "publication_timezone must be a non-empty IANA timezone"
                )
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ChannelConfigurationError(
                    "publication_timezone must be a valid IANA timezone"
                ) from exc
        if config.get("provider") == "redfox":
            if config.get("publication_date_mode") != "previous_day":
                raise ChannelConfigurationError(
                    "redfox requires publication_date_mode=previous_day"
                )
            timezone_name = config.get("publication_timezone")
            if not isinstance(timezone_name, str) or not timezone_name.strip():
                raise ChannelConfigurationError(
                    "redfox requires parser_config.publication_timezone"
                )
            if not config.get("detail_url"):
                raise ChannelConfigurationError(
                    "redfox requires parser_config.detail_url"
                )
            forbidden = {"skip_first_article", "skip_ad_titles", "skip_pinned"} & config.keys()
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ChannelConfigurationError(
                    f"redfox fixed-position selection is not allowed: {names}"
                )

    async def crawl(self, factory: sessionmaker, source_id: int, run_id: int) -> None:
        await crawl_source_with_engine(factory, source_id, run_id)


ADAPTERS: dict[str, ChannelAdapter] = {
    "web": HttpChannelAdapter("web", frozenset({"html", "sitemap"})),
    "rss": HttpChannelAdapter("rss", frozenset({"feed"})),
    "api": HttpChannelAdapter("api", frozenset({"json"})),
    "third_party_feed": HttpChannelAdapter(
        "third_party_feed", frozenset({"feed", "json"}), require_provider=True
    ),
}


def validate_channel_config(channel_type: str, parser_config: dict) -> None:
    adapter = ADAPTERS.get(channel_type)
    if not adapter:
        raise ChannelConfigurationError(f"unsupported channel_type: {channel_type}")
    source = Source(
        name="validation",
        channel_type=channel_type,
        start_url="https://example.invalid/",
        normalized_start_url="https://example.invalid/",
        parser_config=parser_config,
    )
    adapter.validate(source)


def canonicalize_parser_config(channel_type: str, parser_config: dict) -> dict:
    validate_channel_config(channel_type, parser_config)
    return {
        **(parser_config or {}),
        "execution_engine": resolve_execution_engine_key(
            channel_type, parser_config or {}
        ),
    }


async def crawl_source(factory: sessionmaker, source_id: int, run_id: int) -> None:
    with factory() as session:
        source = session.get(Source, source_id)
        run = session.get(CrawlRun, run_id)
        if not source or not run:
            return
        try:
            adapter = ADAPTERS[source.channel_type]
            adapter.validate(source)
        except (KeyError, ChannelConfigurationError) as exc:
            run.status = "failed"
            run.error_count = 1
            run.error_code = type(exc).__name__
            run.error_summary = str(exc)
            run.finished_at = utcnow()
            session.commit()
            return
    await adapter.crawl(factory, source_id, run_id)
    from .topic_distribution import distribute_crawl_run

    with factory() as session:
        try:
            distribute_crawl_run(session, run_id)
        except Exception:
            logger.exception("topic distribution failed run_id=%s", run_id)
            session.rollback()
