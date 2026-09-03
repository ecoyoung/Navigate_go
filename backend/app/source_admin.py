from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .crawl_scheduler import SourceHealth
from .execution_engines import ExecutionEngineConfigurationError, resolve_execution_engine_key
from .models import CrawlRun, Source
from .normalization import normalize_url
from .source_probe import SourcePipeline, SourceProbeResult
from .web_ingestion import ActiveCrawlConflict, create_crawl_run

PATH_LABELS = {
    "html": ("HTML 列表", "static_http"),
    "feed": ("RSS / Atom", "feed_direct"),
    "rss": ("RSS / Atom", "feed_direct"),
    "atom": ("RSS / Atom", "feed_direct"),
    "sitemap": ("Sitemap", "sitemap_http"),
    "json": ("JSON API", "json_api"),
    "json_api": ("JSON API", "json_api"),
    "provider": ("供应商接口", "provider_api"),
}


def is_user_topic_source(source: Source) -> bool:
    return str((source.parser_config or {}).get("discovery_method") or "") == "user_topic"


def is_website_source(source: Source) -> bool:
    return source.channel_type in {"web", "rss", "api"} and not is_user_topic_source(source)


CATALOG_REMOVED_KEY = "removed_from_catalog"


def is_removed_from_catalog(source: Source) -> bool:
    return bool((source.parser_config or {}).get(CATALOG_REMOVED_KEY))


def website_sources(session: Session) -> list[Source]:
    sources = list(session.scalars(select(Source).order_by(Source.id)))
    return [
        source
        for source in sources
        if is_website_source(source) and not is_removed_from_catalog(source)
    ]


def restore_website_source(source: Source) -> None:
    config = dict(source.parser_config or {})
    config.pop(CATALOG_REMOVED_KEY, None)
    source.parser_config = config
    source.is_enabled = True


def retire_website_source(session: Session, source: Source) -> str:
    if not is_website_source(source) or is_removed_from_catalog(source):
        raise ValueError("来源不存在")
    has_history = session.scalar(
        select(CrawlRun.id).where(CrawlRun.source_id == source.id).limit(1)
    )
    if has_history is not None:
        config = dict(source.parser_config or {})
        config[CATALOG_REMOVED_KEY] = True
        source.parser_config = config
        source.is_enabled = False
        return "hidden"
    session.delete(source)
    return "deleted"


def configured_path_key(source: Source) -> str | None:
    config = source.parser_config or {}
    if str(config.get("discovery_method") or "") == "user_topic":
        return None
    if str(config.get("provider") or "direct") == "redfox":
        return "provider"
    method = str(config.get("discovery_method") or "html")
    if method == "json":
        return "json"
    if method == "feed":
        return "feed"
    return method


def _path_status(source: Source, health: SourceHealth, *, configured: bool) -> str:
    strategy = str((source.parser_config or {}).get("crawl_strategy") or "")
    if strategy in {"blocked", "unavailable"}:
        return "blocked"
    if not configured:
        return "candidate"
    if health.last_run_status in {"succeeded", "partial"}:
        return "verified"
    if health.last_run_status == "failed":
        return "failing"
    return "configured"


def source_viable_paths(source: Source, health: SourceHealth) -> list[dict]:
    configured = configured_path_key(source)
    paths: list[dict] = []
    seen: set[str] = set()

    def add(key: str, *, configured_path: bool, url: str | None = None) -> None:
        spec = PATH_LABELS.get(key)
        if spec is None or key in seen:
            return
        seen.add(key)
        label, engine = spec
        paths.append(
            {
                "key": "feed" if key in {"rss", "atom"} else key,
                "label": label,
                "engine": engine,
                "status": _path_status(source, health, configured=configured_path),
                "url": url,
            }
        )

    if configured:
        add(configured, configured_path=True, url=source.start_url)
    for item in (source.parser_config or {}).get("detected_paths") or []:
        if not isinstance(item, dict):
            continue
        add(
            str(item.get("kind") or ""),
            configured_path=False,
            url=str(item.get("url") or "") or None,
        )
    return paths


def source_execution_engine(source: Source) -> str | None:
    try:
        return resolve_execution_engine_key(source.channel_type, source.parser_config or {})
    except (ExecutionEngineConfigurationError, ValueError):
        return None


def parser_config_from_pipeline(pipeline: SourcePipeline) -> dict:
    if pipeline.engine is None or pipeline.channel_type is None:
        raise ValueError("probe_pipeline_incomplete")
    method_by_engine = {
        "static_http": "html",
        "feed_direct": "feed",
        "sitemap_http": "sitemap",
        "json_api": "json",
        "provider_api": "json",
    }
    discovery_method = method_by_engine.get(pipeline.engine)
    if not discovery_method:
        raise ValueError("pipeline engine is not executable")
    config: dict = {
        "pipeline_schema_version": pipeline.schema_version,
        "pipeline_id": pipeline.pipeline_id,
        "probe_id": pipeline.provenance.probe_id,
        "execution_engine": pipeline.engine,
        "discovery_method": discovery_method,
        "pipeline_state": pipeline.state,
        "access_level": "public",
    }
    if pipeline.discovery_url:
        config["discovery_url"] = pipeline.discovery_url
    if pipeline.provider != "direct":
        config["provider"] = pipeline.provider
    if "feed_full_content" in pipeline.content_chain:
        config["ingest_feed_content"] = True
        config["content_completeness"] = "full"
    elif "feed_summary" in pipeline.content_chain:
        config["content_completeness"] = "partial"
    if "json_detail" in pipeline.content_chain:
        config["article_response_format"] = "json"
    return config


def detected_paths_from_probe(result: SourceProbeResult) -> list[dict]:
    return [
        {
            "kind": candidate.resource_kind,
            "url": candidate.url,
            "verified": candidate.verified,
            "confidence": candidate.confidence,
        }
        for candidate in result.candidates
    ]


def probe_preview(result: SourceProbeResult) -> dict:
    recommended = None
    if result.recommended_pipeline.engine and result.recommended_pipeline.channel_type:
        key = {
            "static_http": "html",
            "feed_direct": "feed",
            "sitemap_http": "sitemap",
            "json_api": "json",
            "provider_api": "provider",
        }.get(result.recommended_pipeline.engine)
        if key:
            label, engine = PATH_LABELS[key]
            recommended = {
                "key": key,
                "label": label,
                "engine": engine,
                "status": result.recommended_pipeline.state,
                "url": result.recommended_pipeline.discovery_url
                or result.recommended_pipeline.start_url,
            }
    return {
        "outcome": result.outcome,
        "final_url": result.final_url,
        "recommended": recommended,
        "paths": [
            {
                "key": "feed" if item.resource_kind in {"rss", "atom"} else item.resource_kind,
                "label": PATH_LABELS.get(
                    item.resource_kind, (item.resource_kind, item.resource_kind)
                )[0],
                "engine": PATH_LABELS.get(item.resource_kind, ("", ""))[1],
                "status": "verified" if item.verified else "candidate",
                "url": item.url,
            }
            for item in result.candidates
            if item.resource_kind in PATH_LABELS
        ],
    }


def registration_from_probe(result: SourceProbeResult) -> tuple[str, str, dict]:
    pipeline = result.recommended_pipeline
    if pipeline.state in {"blocked", "unsupported"} or pipeline.engine is None:
        raise ValueError(pipeline.reason_code or "probe_path_unavailable")
    parser_config = parser_config_from_pipeline(pipeline)
    parser_config["detected_paths"] = detected_paths_from_probe(result)
    start_url = pipeline.discovery_url or pipeline.start_url or result.final_url
    return pipeline.channel_type or "web", start_url, parser_config


def catalog_id_for_url(session: Session, url: str) -> str:
    host = (urlsplit(url).hostname or "site").lower().removeprefix("www.")
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")[:48] or "site"
    if slug[0].isdigit():
        slug = f"site-{slug}"
    existing = {item for item in session.scalars(select(Source.catalog_id)) if item}
    if slug not in existing:
        return slug
    suffix = hashlib.sha256(normalize_url(url).encode()).hexdigest()[:8]
    candidate = f"{slug}-{suffix}"
    if candidate not in existing:
        return candidate
    raise ValueError("catalog_id_conflict")


def display_name_for_url(url: str, name: str | None) -> str:
    cleaned = (name or "").strip()
    if cleaned:
        return cleaned[:200]
    host = (urlsplit(url).hostname or "未命名网站").removeprefix("www.")
    return host[:200]


def queue_source_crawl(
    session: Session,
    source: Source,
    *,
    trigger: str = "manual",
):
    strategy = str((source.parser_config or {}).get("crawl_strategy") or "")
    if not source.is_enabled:
        raise ValueError("来源已停用")
    if strategy in {"blocked", "unavailable"}:
        raise ValueError("该来源当前按访问规则停爬或不可用")
    try:
        run, created = create_crawl_run(session, source, trigger=trigger)
    except ActiveCrawlConflict as exc:
        raise ValueError(str(exc)) from exc
    return run, created
