import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .content_quality import quality_tier
from .contracts import build_contract, normalize_topics
from .feed_sync import parse_feed
from .models import (
    ContentItem,
    CrawlRun,
    PageSnapshot,
    RawItem,
    Source,
    SourceSyncState,
    utcnow,
)
from .normalization import identity_key, normalize_url
from .run_coverage import resolve_run_coverage

USER_AGENT = "NavigateBot/0.1 (+content intelligence; respectful crawler)"
SNAPSHOT_HEADER_NAMES = ("cache-control", "content-type", "date", "etag", "last-modified")
MANUAL_ARTICLE_FLOOR = 10
logger = logging.getLogger("navigate.crawl")


def article_limit(config: dict | None, *, trigger: str | None = None) -> int:
    raw = int((config or {}).get("max_articles") or 10)
    if trigger == "manual":
        raw = max(raw, MANUAL_ARTICLE_FLOOR)
    return min(max(raw, 1), 50)


def parser_config_for_run(source: Source, run: CrawlRun) -> dict:
    config = dict(source.parser_config or {})
    config["max_articles"] = article_limit(config, trigger=run.trigger)
    return config


class ActiveCrawlConflict(RuntimeError):
    """An active run exists for a different immutable crawl context."""


def _duration_seconds(started_at, finished_at) -> float:
    if started_at is None or finished_at is None:
        return 0.0
    start = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    end = finished_at if finished_at.tzinfo else finished_at.replace(tzinfo=UTC)
    return (end - start).total_seconds()


def _log_and_commit_run(session: Session, run: CrawlRun) -> None:
    logger.info(
        "crawl_run_finished source_id=%s run_id=%s status=%s fetched=%s new=%s "
        "updated=%s skipped=%s rejected=%s errors=%s duration_seconds=%.1f code=%s",
        run.source_id,
        run.id,
        run.status,
        run.fetched_count,
        run.new_count,
        run.updated_count,
        run.skipped_count,
        run.rejected_count,
        run.error_count,
        _duration_seconds(run.started_at, run.finished_at),
        run.error_code or "",
    )
    session.commit()


def _feed_request_headers(state: SourceSyncState | None) -> dict[str, str]:
    if state is None:
        return {}
    headers: dict[str, str] = {}
    if state.etag:
        headers["If-None-Match"] = state.etag
    if state.last_modified:
        headers["If-Modified-Since"] = state.last_modified
    return headers


def _latest_watermark(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if current is None:
        return candidate
    if candidate is None:
        return current
    current_value = current if current.tzinfo else current.replace(tzinfo=UTC)
    candidate_value = candidate if candidate.tzinfo else candidate.replace(tzinfo=UTC)
    return candidate if candidate_value >= current_value else current


def _commit_feed_state(
    session: Session,
    source_id: int,
    run_id: int,
    response: httpx.Response,
    *,
    plan=None,
    accept_validators: bool = True,
) -> SourceSyncState:
    state = session.get(SourceSyncState, source_id)
    if state is None:
        state = SourceSyncState(source_id=source_id)
        session.add(state)
    if accept_validators:
        state.etag = response.headers.get("etag") or state.etag
        state.last_modified = response.headers.get("last-modified") or state.last_modified
    if plan is not None:
        state.recent_entries = plan.recent_entries
        state.published_watermark = _latest_watermark(
            state.published_watermark, plan.published_watermark
        )
        state.updated_watermark = _latest_watermark(state.updated_watermark, plan.updated_watermark)
    state.last_committed_run_id = run_id
    state.updated_at = utcnow()
    return state


class ContentFormError(ValueError):
    """A fetched page is not a valid article/news document."""


def save_response_snapshot(
    session: Session,
    run: CrawlRun,
    *,
    url: str,
    page_type: str,
    request_method: str,
    response: httpx.Response | None = None,
    error_text: str | None = None,
) -> PageSnapshot:
    body = response.text if response is not None else ""
    snapshot = PageSnapshot(
        crawl_run_id=run.id,
        url=str(response.url) if response is not None else url,
        page_type=page_type,
        request_method=request_method,
        http_status=response.status_code if response is not None else 0,
        content_type=response.headers.get("content-type") if response is not None else None,
        response_headers=(
            {
                name: response.headers[name]
                for name in SNAPSHOT_HEADER_NAMES
                if name in response.headers
            }
            if response is not None
            else {}
        ),
        error_text=error_text,
        body=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
    )
    session.add(snapshot)
    session.commit()
    return snapshot


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    attempts: int = 3,
    method: str = "GET",
    data: dict | None = None,
    json_data: dict | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.request(method, url, data=data, json=json_data, headers=headers)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (2**attempt))
    assert last_error is not None
    raise last_error


def request_headers(config: dict) -> dict[str, str]:
    from .secrets import MissingSecretError, require_secret

    headers = {"User-Agent": USER_AGENT}
    for header, env_name in (config.get("request_headers_env") or {}).items():
        try:
            value = require_secret(str(env_name))
        except MissingSecretError as exc:
            raise ValueError(f"missing_request_header_env:{env_name}") from exc
        headers[str(header)] = value
    return headers


def robots_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def robots_allows(robots_text: str, url: str, user_agent: str = USER_AGENT) -> bool:
    parser = RobotFileParser()
    parser.parse(robots_text.splitlines())
    return parser.can_fetch(user_agent, url)


def discover_article_urls(listing_html: str, start_url: str, config: dict) -> list[str]:
    soup = BeautifulSoup(listing_html, "lxml")
    link_selector = config.get(
        "link_selector",
        "article a[href], main a[href], [class*='news'] a[href], [class*='article'] a[href]",
    )
    card_selector = config.get("card_selector", "article")
    exclude_card_selector = config.get("exclude_card_selector")
    pattern = (
        re.compile(config["article_url_pattern"]) if config.get("article_url_pattern") else None
    )
    start_host = urlsplit(start_url).hostname
    urls: list[str] = []
    seen: set[str] = set()
    ignored_parts = {
        "account",
        "author",
        "category",
        "contact",
        "login",
        "privacy",
        "search",
        "tag",
        "terms",
    }
    for link in soup.select(link_selector):
        card = link.find_parent(card_selector) if card_selector else None
        if exclude_card_selector and card and card.select_one(exclude_card_selector):
            continue
        href = link.get("href")
        if not href:
            continue
        url = normalize_url(urljoin(start_url, href))
        if urlsplit(url).hostname != start_host or url == normalize_url(start_url):
            continue
        path_parts = {part.lower() for part in urlsplit(url).path.split("/") if part}
        link_text = link.get_text(" ", strip=True)
        if not pattern and (path_parts & ignored_parts or len(link_text) < 8):
            continue
        if re.search(r"\.(?:jpg|jpeg|png|gif|svg|pdf|zip)$", urlsplit(url).path, re.I):
            continue
        if pattern and not pattern.search(url):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    limit = int(config.get("max_articles", 10))
    return urls[: min(max(limit, 1), 50)]


def discover_feed_urls(
    feed_text: str, config: dict, *, entries: list[dict] | None = None
) -> list[str]:
    feed_entries = entries if entries is not None else parse_feed(feed_text)
    urls: list[str] = []
    seen: set[str] = set()
    for entry in feed_entries:
        link = str(entry.get("link") or "").strip()
        if not link:
            continue
        normalized = normalize_url(link)
        if normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    limit = int(config.get("max_articles", 10))
    return urls[: min(max(limit, 1), 50)]


def discover_json_urls(payload_text: str, start_url: str, config: dict) -> list[str]:
    value = json.loads(payload_text)
    for key in str(config.get("items_path", "")).split("."):
        if key:
            value = value[key]
    if not isinstance(value, list):
        raise ValueError("json_items_not_list")
    template = str(config["article_url_template"])
    urls: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            url = normalize_url(urljoin(start_url, template.format(**item)))
        except (KeyError, ValueError):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    limit = min(max(int(config.get("max_articles", 10)), 1), 50)
    return urls[:limit]


def _json_value(value: object, path: str) -> object | None:
    for key in path.split("."):
        if not key:
            continue
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def extract_json_article(payload_text: str, url: str, config: dict) -> dict:
    payload = json.loads(payload_text)
    item = _json_value(payload, str(config.get("json_item_path", "")))
    if not isinstance(item, dict):
        raise ContentFormError("json_article_not_object")

    title = _json_value(item, str(config["json_title_path"]))
    body = _json_value(item, str(config["json_body_path"]))
    if not isinstance(title, str) or not title.strip():
        raise ContentFormError("missing_title")
    if not isinstance(body, str):
        raise ContentFormError("missing_body")
    body = re.sub(r"\s+", " ", body).strip()
    min_chars = int(config.get("min_content_chars", 120))
    if len(body) < min_chars:
        raise ContentFormError(f"content_too_short:{len(body)}")

    canonical = normalize_url(url)
    if config.get("json_canonical_url_template"):
        try:
            canonical = normalize_url(str(config["json_canonical_url_template"]).format(**item))
        except (KeyError, ValueError) as exc:
            raise ContentFormError("invalid_canonical_url_template") from exc
    description_path = config.get("json_description_path")
    author_path = config.get("json_author_path")
    date_path = config.get("json_date_path")
    description = _json_value(item, str(description_path)) if description_path else None
    author = _json_value(item, str(author_path)) if author_path else None
    published = _json_value(item, str(date_path)) if date_path else None
    tags_path = config.get("json_tags_path")
    external_id_path = config.get("json_external_id_path")
    updated_path = config.get("json_updated_path")
    media_path = config.get("json_media_path")
    return {
        "title": title.strip(),
        "canonical_url": canonical,
        "original_url": canonical,
        "author": str(author).strip() if author else None,
        "published_at": (
            _parse_date(str(published), config.get("publication_timezone")) if published else None
        ),
        "updated_at": (
            _parse_date(
                str(_json_value(item, str(updated_path))),
                config.get("publication_timezone"),
            )
            if updated_path
            else None
        ),
        "external_item_id": (
            _external_id(_json_value(item, str(external_id_path))) if external_id_path else None
        ),
        "body": body,
        "description": str(description).strip() if description else body[:500],
        "content_type": str(config.get("content_type", "article")),
        "topics": normalize_topics(
            _topic_values(_json_value(item, str(tags_path))) if tags_path else []
        ),
        "media": (
            _media_values(_json_value(item, str(media_path)), canonical) if media_path else []
        ),
        "content_completeness": config.get("content_completeness", "unknown"),
    }


def discover_sitemap_urls(xml_text: str, config: dict) -> list[str]:
    soup = BeautifulSoup(xml_text, "xml")
    pattern = (
        re.compile(config["article_url_pattern"]) if config.get("article_url_pattern") else None
    )
    urls: list[str] = []
    seen: set[str] = set()
    for location in soup.select("url > loc"):
        url = normalize_url(location.get_text(strip=True))
        if pattern and not pattern.search(url):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    limit = min(max(int(config.get("max_articles", 10)), 1), 50)
    return urls[:limit]


def _json_ld_articles(soup: BeautifulSoup) -> list[dict]:
    found: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        queue = value if isinstance(value, list) else [value]
        for item in queue:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
            kind = item.get("@type", "")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(name in {"Article", "NewsArticle", "BlogPosting"} for name in kinds):
                found.append(item)
    return found


def _meta(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str | None:
    selector = f'meta[name="{name}"]' if name else f'meta[property="{prop}"]'
    tag = soup.select_one(selector)
    return str(tag.get("content", "")).strip() or None if tag else None


def _text(soup: BeautifulSoup, selector: str | None) -> str | None:
    if not selector:
        return None
    tag = soup.select_one(selector)
    return tag.get_text(" ", strip=True) if tag else None


def _topic_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_topic_values(item))
        return values
    if isinstance(value, dict):
        return _topic_values(
            value.get("name") or value.get("term") or value.get("keyword") or value.get("@id")
        )
    return [str(value)]


def _external_id(value: object) -> str | None:
    if isinstance(value, list):
        return next((result for item in value if (result := _external_id(item))), None)
    if isinstance(value, dict):
        value = value.get("value") or value.get("@id") or value.get("id")
    text = str(value or "").strip()
    return text or None


def _media_values(value: object, base_url: str) -> list[dict]:
    raw = value if isinstance(value, list) else [value]
    media: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("src") or item.get("href") or "").strip()
        if not url:
            continue
        absolute = urljoin(base_url, url)
        if urlsplit(absolute).scheme not in {"http", "https"}:
            continue
        media.append(
            {
                "type": str(item.get("type") or "image"),
                "url": absolute,
                "alt": str(item.get("alt") or item.get("caption") or item.get("name") or "").strip()
                or None,
            }
        )
    return media


def _feed_topics(entry: object) -> list[str]:
    tags = getattr(entry, "tags", None) or []
    values = _topic_values(tags)
    if isinstance(entry, dict):
        values.extend(_topic_values(entry.get("category")))
        values.extend(_topic_values(entry.get("tags")))
    return values


def extract_html_topics(soup: BeautifulSoup, article_data: dict, config: dict) -> list[str]:
    values: list[str] = []
    values.extend(_topic_values(article_data.get("keywords")))
    values.extend(_topic_values(article_data.get("articleSection")))
    values.extend(_topic_values(article_data.get("about")))
    for meta in soup.select('meta[property="article:tag"]'):
        values.extend(_topic_values(meta.get("content")))
    for name in ("news_keywords", "keywords"):
        values.extend(_topic_values(_meta(soup, name=name)))
    if config.get("tag_selector"):
        for node in soup.select(str(config["tag_selector"])):
            values.append(node.get_text(" ", strip=True))
    for node in soup.select('a[rel="tag"]'):
        values.append(node.get_text(" ", strip=True))
    return normalize_topics(values)


def _parse_date(value: str | None, publication_timezone: str | None = None) -> datetime | None:
    if not value:
        return None
    value = re.sub(r"^Published\s+", "", value.strip(), flags=re.I)
    try:
        parsed = date_parser.parse(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=ZoneInfo(publication_timezone) if publication_timezone else UTC
            )
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


_DATE_META_NAMES = (
    "pubdate",
    "publishdate",
    "publish-date",
    "originalpublicationdate",
    "sailthru.date",
    "parsely-pub-date",
    "dc.date.issued",
    "dcterms.created",
    "datepublished",
    "date_published",
)
_DATE_META_PROPERTIES = (
    "article:published_time",
    "og:published_time",
    "article:published",
    "datepublished",
)
_URL_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)"),
    # Some publishers append their article ID directly after YYYYMMDD.
    re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})"),
)
_VISIBLE_DATE_PATTERN = re.compile(
    r"(?:20\d{2}\s*[年./-]\s*\d{1,2}\s*(?:[月./-]\s*\d{1,2}\s*日?)?|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+20\d{2}|"
    r"\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2})",
    flags=re.I,
)


def _parse_date_candidate(value: str | None, publication_timezone: str | None) -> datetime | None:
    if not value:
        return None
    normalized = re.sub(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", r"\1-\2-\3", value)
    parsed = _parse_date(normalized, publication_timezone)
    if parsed is None or not 1990 <= parsed.year <= datetime.now(UTC).year + 1:
        return None
    return parsed


def _attribute_date_candidates(soup: BeautifulSoup) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    accepted = {*_DATE_META_NAMES, *_DATE_META_PROPERTIES}
    for tag in soup.find_all("meta"):
        key = str(tag.get("name") or tag.get("property") or "").strip().casefold()
        if key in accepted:
            candidates.append(("meta", str(tag.get("content") or "")))
        if str(tag.get("itemprop") or "").strip().casefold() == "datepublished":
            candidates.append(("microdata", str(tag.get("content") or tag.get("datetime") or "")))
    for tag in soup.select('time[itemprop="datePublished"], time[datetime]'):
        candidates.append(("time", str(tag.get("datetime") or tag.get_text(" ", strip=True))))
    data_keys = (
        "data-published",
        "data-publish-date",
        "data-date",
        "data-dt",
        "data-time",
    )
    for tag in soup.select(", ".join(f"[{key}]" for key in data_keys)):
        value = next(
            (tag.get(key) for key in data_keys if tag.get(key)),
            None,
        )
        if value:
            candidates.append(("data_attribute", str(value)))
    return candidates


def _url_date_candidate(url: str) -> str | None:
    for pattern in _URL_DATE_PATTERNS:
        match = pattern.search(url)
        if match:
            return "-".join(match.groups())
    return None


def _visible_date_candidate(soup: BeautifulSoup) -> str | None:
    selectors = ", ".join(
        (
            "time",
            "[class*='date' i]",
            "[id*='date' i]",
            "[class*='time' i]",
            "[id*='time' i]",
            "[class*='publish' i]",
            "[id*='publish' i]",
        )
    )
    for tag in soup.select(selectors):
        match = _VISIBLE_DATE_PATTERN.search(tag.get_text(" ", strip=True))
        if match:
            return match.group(0)
    return None


def extract_publication_date(
    soup: BeautifulSoup, article_data: dict, url: str, config: dict
) -> tuple[datetime | None, str | None]:
    """Resolve date from ordered independent publisher and page signals."""
    timezone = config.get("publication_timezone")
    candidates: list[tuple[str, str | None]] = [
        ("json_ld", article_data.get("datePublished")),
        ("open_graph", _meta(soup, prop="article:published_time")),
        ("open_graph", _meta(soup, prop="og:published_time")),
    ]
    candidates.extend(_attribute_date_candidates(soup))
    candidates.append(("configured_selector", _text(soup, config.get("date_selector"))))
    candidates.append(("url", _url_date_candidate(url)))
    candidates.append(("visible_text", _visible_date_candidate(soup)))
    for origin, value in candidates:
        parsed = _parse_date_candidate(value, timezone)
        if parsed is not None:
            return parsed, origin
    return None, None


def extract_page_publication_date(
    html: str, url: str, config: dict
) -> tuple[datetime | None, str | None]:
    """Extract a date even when a page body is too thin to count as an article."""
    soup = BeautifulSoup(html, "lxml")
    structured = _json_ld_articles(soup)
    article_data = next(
        (item for item in structured if item.get("datePublished")),
        structured[0] if structured else {},
    )
    return extract_publication_date(soup, article_data, url, config)


def extract_feed_articles(
    feed_text: str, config: dict, *, entries: list[dict] | None = None
) -> list[dict]:
    feed_entries = entries if entries is not None else parse_feed(feed_text)
    articles: list[dict] = []
    min_chars = int(config.get("min_content_chars", 120))
    for entry in feed_entries:
        title = str(entry.get("title") or "").strip()
        link = str(entry.get("link") or "").strip()
        content_parts = entry.get("content") or []
        used_full_content = bool(content_parts and isinstance(content_parts[0], dict))
        raw_body = ""
        if content_parts and isinstance(content_parts[0], dict):
            raw_body = str(content_parts[0].get("value") or "")
        raw_body = raw_body or str(entry.get("summary") or entry.get("description") or "")
        body = BeautifulSoup(raw_body, "lxml").get_text(" ", strip=True)
        if not title or not link or len(body) < min_chars:
            continue
        articles.append(
            {
                "title": title,
                "canonical_url": normalize_url(link),
                "original_url": link,
                "author": str(entry.get("author") or "").strip() or None,
                "published_at": _parse_date(
                    entry.get("published"), config.get("publication_timezone")
                ),
                "updated_at": _parse_date(
                    entry.get("updated") if "updated" in entry else None,
                    config.get("publication_timezone"),
                ),
                "external_item_id": _external_id(entry.get("id") or entry.get("guid")),
                "body": body,
                "description": body[:500],
                "content_type": "news",
                "topics": normalize_topics(_feed_topics(entry)),
                "media": [
                    *_media_values(entry.get("media_content") or [], link),
                    *_media_values(entry.get("media_thumbnail") or [], link),
                    *_media_values(entry.get("enclosures") or [], link),
                    *_media_values(
                        [
                            {"url": image.get("src"), "alt": image.get("alt")}
                            for image in BeautifulSoup(raw_body, "lxml").select("img[src]")
                        ],
                        link,
                    ),
                ],
                "content_completeness": (
                    config.get("content_completeness")
                    or ("unknown" if used_full_content else "partial")
                ),
                "validation_warnings": [] if used_full_content else ["summary_only"],
            }
        )
    limit = min(max(int(config.get("max_articles", 10)), 1), 50)
    return articles[:limit]


def extract_article(html: str, url: str, config: dict) -> dict:
    soup = BeautifulSoup(html, "lxml")
    structured = _json_ld_articles(soup)
    article_data = structured[0] if structured else {}
    structured_type = article_data.get("@type", "")
    structured_types = structured_type if isinstance(structured_type, list) else [structured_type]
    content_type = "news" if "NewsArticle" in structured_types else "article"
    canonical_tag = soup.select_one('link[rel="canonical"]')
    canonical = (
        normalize_url(str(canonical_tag.get("href")))
        if canonical_tag and canonical_tag.get("href")
        else normalize_url(url)
    )
    title = (
        article_data.get("headline")
        or _meta(soup, prop="og:title")
        or _text(soup, config.get("title_selector", "h1"))
        or (soup.title.get_text(" ", strip=True) if soup.title else None)
    )
    if not title:
        raise ContentFormError("missing_title")
    configured_body_selector = config.get("body_selector")
    if configured_body_selector:
        body_nodes = soup.select(configured_body_selector)
    else:
        candidates = soup.select(
            "[itemprop='articleBody'], article, .article-content, .article__body, "
            ".post-content, .entry-content, .detail-content, .content-detail, main"
        )
        body_nodes = (
            [max(candidates, key=lambda node: len(node.get_text(" ", strip=True)))]
            if candidates
            else []
        )
    for node in body_nodes:
        for noise in node.select("script, style, nav, header, footer, aside, form, noscript"):
            noise.decompose()
    body = "\n\n".join(
        node.get_text(" ", strip=True) for node in body_nodes if node.get_text(" ", strip=True)
    )
    min_chars = int(config.get("min_content_chars", 120))
    if len(body) < min_chars:
        raise ContentFormError(f"content_too_short:{len(body)}")
    author_value = article_data.get("author")
    if isinstance(author_value, dict):
        author = author_value.get("name")
    elif isinstance(author_value, list):
        author = ", ".join(
            str(a.get("name", "")) if isinstance(a, dict) else str(a) for a in author_value
        )
    else:
        author = author_value
    author = author or _meta(soup, name="author") or _text(soup, config.get("author_selector"))
    published_at, published_origin = extract_publication_date(soup, article_data, url, config)
    updated_raw = (
        article_data.get("dateModified")
        or _meta(soup, prop="article:modified_time")
        or _text(soup, config.get("updated_date_selector"))
    )
    description = (
        article_data.get("description")
        or _meta(soup, name="description")
        or _meta(soup, prop="og:description")
    )
    return {
        "title": str(title).strip(),
        "canonical_url": canonical,
        "original_url": url,
        "author": str(author).strip() if author else None,
        "published_at": published_at,
        "updated_at": _parse_date(updated_raw, config.get("publication_timezone")),
        "external_item_id": _external_id(article_data.get("identifier")),
        "body": body,
        "description": description,
        "content_type": content_type,
        "topics": extract_html_topics(soup, article_data, config),
        "media": [
            *_media_values(article_data.get("image") or [], url),
            *_media_values(_meta(soup, prop="og:image") or [], url),
            *_media_values(
                [
                    {"url": image.get("src"), "alt": image.get("alt")}
                    for image in soup.select("img[src]")
                ],
                url,
            ),
        ],
        "content_completeness": config.get("content_completeness", "unknown"),
        "validation_warnings": (
            [f"published_at:{published_origin}"] if published_origin else ["missing_published_at"]
        ),
    }


def ingest_article(
    session: Session,
    source: Source,
    run: CrawlRun,
    extracted: dict,
    page_snapshot_id: int | None = None,
) -> str:
    contract = build_contract(extracted, source)
    canonical = contract.canonical_url
    legacy_identity = identity_key(
        None, canonical, contract.title, str(contract.published_at or "")
    )
    content = None
    if contract.external_item_id:
        matches = list(
            session.scalars(
                select(ContentItem)
                .where(
                    ContentItem.source_id == source.id,
                    ContentItem.external_id == contract.external_item_id,
                )
                .limit(2)
            )
        )
        if len(matches) > 1:
            raise ValueError("ambiguous_external_item_id")
        content = matches[0] if matches else None
    if content is None:
        content = session.scalar(
            select(ContentItem).where(
                ContentItem.source_id == source.id,
                ContentItem.identity_key == legacy_identity,
            )
        )
    if content is None:
        normalized_matches = [
            candidate
            for candidate in session.scalars(
                select(ContentItem).where(ContentItem.source_id == source.id)
            )
            if candidate.canonical_url and normalize_url(candidate.canonical_url) == canonical
        ]
        if len(normalized_matches) > 1:
            raise ValueError("ambiguous_normalized_canonical_url")
        content = normalized_matches[0] if normalized_matches else None
    item_identity = (
        content.identity_key
        if content
        else identity_key(
            contract.external_item_id,
            canonical,
            contract.title,
            str(contract.published_at or ""),
        )
    )
    payload = contract.model_dump(mode="json")
    semantic_payload = {key: value for key, value in payload.items() if key != "captured_at"}
    payload_text = json.dumps(semantic_payload, ensure_ascii=False, sort_keys=True)
    payload_sha = hashlib.sha256(payload_text.encode()).hexdigest()
    existing_raw = session.scalar(
        select(RawItem).where(
            RawItem.source_id == source.id,
            RawItem.identity_key == item_identity,
            RawItem.payload_sha256 == payload_sha,
        )
    )
    if existing_raw:
        return "skipped"
    raw = RawItem(
        source_id=source.id,
        crawl_run_id=run.id,
        page_snapshot_id=page_snapshot_id,
        external_id=contract.external_item_id,
        identity_key=item_identity,
        original_url=contract.original_url,
        canonical_url=canonical,
        payload=payload,
        payload_sha256=payload_sha,
    )
    session.add(raw)
    session.flush()
    values = dict(
        raw_item_id=raw.id,
        external_id=contract.external_item_id,
        title=contract.title,
        original_url=contract.original_url,
        canonical_url=canonical,
        author=contract.author,
        body=contract.body_text,
        language=contract.language,
        source_region=contract.source_region,
        source_type=contract.source_type,
        access_level=contract.access_level,
        content_type=contract.content_type,
        topics=contract.topics,
        is_sponsored=contract.is_sponsored,
        is_roundup=contract.is_roundup,
        excerpt=contract.excerpt,
        source_updated_at=contract.updated_at,
        media=[item.model_dump(mode="json") for item in contract.media],
        quality=contract.quality.model_dump(mode="json"),
        content_hash=contract.content_hash,
        schema_version=contract.schema_version,
        published_at=contract.published_at,
        normalizer_version="unified-v1.1",
    )
    old_content_hash = content.content_hash if content else ""
    if content:
        for key, value in values.items():
            setattr(content, key, value)
        result = "updated"
    else:
        session.add(ContentItem(source_id=source.id, identity_key=item_identity, **values))
        result = "new"
    session.flush()
    from .strict_deduplication import refresh_strict_hash_groups

    refresh_strict_hash_groups(session, {old_content_hash, contract.content_hash})
    return result


def extracted_needs_enrichment(extracted: dict, config: dict) -> bool:
    """A thin extraction gets one canonical-page enrichment pass in the same run."""
    min_chars = int(config.get("enrichment_min_body_chars", 400))
    return (
        extracted.get("published_at") is None
        or len(str(extracted.get("body") or "")) < min_chars
        or extracted.get("content_completeness") != "full"
    )


def api_needs_web_fallback(extracted: dict, config: dict) -> bool:
    """Compatibility helper for JSON API sources using the generic enrichment rule."""
    return bool(
        config.get("api_web_fallback", True)
        and config.get("article_response_format") == "json"
        and extracted_needs_enrichment(extracted, config)
    )


def merge_api_web_detail(api_article: dict, web_article: dict) -> dict:
    """Keep API identity while filling only missing or weaker content fields."""
    merged = merge_web_enrichment_detail(api_article, web_article)
    merged["validation_warnings"] = list(
        dict.fromkeys(
            [
                item
                for item in merged.get("validation_warnings", [])
                if item != "content_enrichment_web"
            ]
            + ["api_web_fallback"]
        )
    )
    return merged


def merge_web_enrichment_detail(article: dict, web_article: dict) -> dict:
    """Keep the original ingestion identity while filling weak webpage fields."""
    merged = dict(article)
    if len(str(web_article.get("body") or "")) > len(str(article.get("body") or "")):
        merged["body"] = web_article["body"]
        merged["content_completeness"] = web_article.get("content_completeness", "unknown")
    for field in ("published_at", "updated_at", "author", "description"):
        if not merged.get(field) and web_article.get(field):
            merged[field] = web_article[field]
    if web_article.get("media"):
        merged["media"] = web_article["media"]
    merged["topics"] = normalize_topics(
        [*(article.get("topics") or []), *(web_article.get("topics") or [])]
    )
    merged["validation_warnings"] = list(
        dict.fromkeys(
            [
                *(article.get("validation_warnings") or []),
                *(web_article.get("validation_warnings") or []),
                "content_enrichment_web",
            ]
        )
    )
    return merged


def preserve_verified_detail(extracted: dict, content: ContentItem) -> dict:
    """Do not downgrade a previously verified canonical page with a thin API payload."""
    merged = dict(extracted)
    merged["body"] = content.body or extracted["body"]
    merged["published_at"] = content.published_at or extracted.get("published_at")
    merged["updated_at"] = content.source_updated_at or extracted.get("updated_at")
    merged["author"] = content.author or extracted.get("author")
    merged["description"] = content.excerpt or extracted.get("description")
    merged["media"] = content.media or extracted.get("media") or []
    merged["topics"] = normalize_topics([*(content.topics or []), *(extracted.get("topics") or [])])
    merged["content_completeness"] = "full"
    merged["validation_warnings"] = list(
        dict.fromkeys([*(extracted.get("validation_warnings") or []), "api_reused_verified_detail"])
    )
    return merged


def source_publication_window_days(
    session: Session, source: Source, *, trigger: str | None = None
) -> int:
    """First successful ingestion gets a short backfill; later runs are D-1 only."""
    has_content = session.scalar(
        select(ContentItem.id).where(ContentItem.source_id == source.id).limit(1)
    )
    config = source.parser_config or {}
    if trigger == "manual":
        return max(0, int(config.get("manual_publication_window_days", 7)))
    key = (
        "incremental_publication_window_days" if has_content else "initial_publication_window_days"
    )
    default = 1 if has_content else 7
    return max(0, int(config.get(key, default)))


def within_publication_window(extracted: dict, run: CrawlRun, config: dict, *, days: int) -> bool:
    """Keep dated content within the frozen source publication window.

    Missing dates remain eligible so the canonical webpage fallback can establish them.
    """
    published_at = extracted.get("published_at")
    if not isinstance(published_at, datetime):
        return True
    if days <= 0:
        return True
    timezone = ZoneInfo(str(config.get("publication_timezone") or "UTC"))
    end_date = run.coverage_date or datetime.now(timezone).date()
    start_date = end_date - timedelta(days=days - 1)
    published_date = (
        (published_at if published_at.tzinfo else published_at.replace(tzinfo=UTC))
        .astimezone(timezone)
        .date()
    )
    return start_date <= published_date <= end_date


def api_within_publication_window(extracted: dict, run: CrawlRun, config: dict) -> bool:
    """Backward-compatible API helper; production crawling uses the source policy."""
    return within_publication_window(
        extracted,
        run,
        config,
        days=int(config.get("api_publication_window_days", 30)),
    )


async def crawl_http_source(
    factory: sessionmaker,
    source_id: int,
    run_id: int,
    *,
    engine=None,
) -> None:
    from .execution_engines import (
        CrawlStats,
        EngineContext,
        execution_engine_for_source,
    )

    with factory() as session:
        run, source = session.get(CrawlRun, run_id), session.get(Source, source_id)
        if not run or not source:
            return
        engine = engine or execution_engine_for_source(source)
        run.status = "running"
        session.commit()
        errors: list[str] = []
        rejections: list[str] = []
        fetched_count = new_count = updated_count = skipped_count = rejected_count = 0
        error_count = 0
        stats = CrawlStats()
        feed_plan = None
        direct_feed = False
        try:
            config = parser_config_for_run(source, run)
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=max(float(config.get("http_timeout_seconds", 12)), 5),
                headers=request_headers(config),
            ) as client:
                attempts = min(max(int(config.get("retry_attempts", 2)), 1), 4)
                discovery_url = str(config.get("discovery_url") or source.start_url)
                robots_response = await fetch_with_retry(
                    client, robots_url(discovery_url), attempts=attempts
                )
                if robots_response.status_code not in (200, 404):
                    raise ValueError(f"robots_unavailable:{robots_response.status_code}")
                robots_text = (
                    robots_response.text
                    if robots_response.status_code == 200
                    else "User-agent: *\nAllow: /"
                )
                if not robots_allows(robots_text, discovery_url):
                    raise PermissionError("robots_disallowed_listing")
                discovery_method = str(config.get("discovery_http_method", "GET")).upper()
                direct_feed = engine.uses_conditional_sync(source, discovery_method)
                sync_state = session.get(SourceSyncState, source.id) if direct_feed else None
                listing_type = engine.listing_page_type
                try:
                    listing = await fetch_with_retry(
                        client,
                        discovery_url,
                        attempts=attempts,
                        method=discovery_method,
                        data=config.get("discovery_form"),
                        json_data=config.get("discovery_json"),
                        headers=engine.discovery_headers(sync_state),
                    )
                except httpx.HTTPStatusError as exc:
                    save_response_snapshot(
                        session,
                        run,
                        url=discovery_url,
                        page_type=listing_type,
                        request_method=discovery_method,
                        response=exc.response,
                        error_text=str(exc),
                    )
                    raise
                except httpx.TransportError as exc:
                    save_response_snapshot(
                        session,
                        run,
                        url=discovery_url,
                        page_type=listing_type,
                        request_method=discovery_method,
                        error_text=str(exc),
                    )
                    raise
                listing_snapshot = save_response_snapshot(
                    session,
                    run,
                    url=discovery_url,
                    page_type=listing_type,
                    request_method=discovery_method,
                    response=listing,
                    error_text=(
                        f"http_status:{listing.status_code}" if listing.status_code >= 400 else None
                    ),
                )
                if direct_feed and listing.status_code == 304:
                    _commit_feed_state(session, source.id, run.id, listing)
                    run = session.get(CrawlRun, run_id)
                    run.status = "unchanged"
                    run.finished_at = utcnow()
                    _log_and_commit_run(session, run)
                    return
                listing.raise_for_status()
                outcome = await engine.process_listing(
                    EngineContext(
                        session=session,
                        source=source,
                        run=run,
                        client=client,
                        config=config,
                        attempts=attempts,
                        discovery_url=discovery_url,
                        discovery_method=discovery_method,
                        listing=listing,
                        listing_snapshot=listing_snapshot,
                        sync_state=sync_state,
                        stats=stats,
                    )
                )
                feed_plan = outcome.feed_plan
                article_urls = outcome.article_urls
                fetched_count = stats.fetched
                new_count = stats.new
                updated_count = stats.updated
                skipped_count = stats.skipped
                rejected_count = stats.rejected
                error_count = stats.errors
                errors = stats.error_messages
                rejections = stats.rejection_messages
                if not article_urls and not outcome.allow_empty:
                    raise ValueError("no_article_urls_discovered")
                delay = max(float(config.get("request_delay_seconds", 2)), 0.5)
                window_days = source_publication_window_days(session, source, trigger=run.trigger)
                for article_url in article_urls:
                    fetched_count += 1
                    try:
                        if not robots_allows(robots_text, article_url):
                            raise PermissionError("robots_disallowed_article")
                        await asyncio.sleep(delay)
                        article_type = (
                            "article_api"
                            if config.get("article_response_format") == "json"
                            else "article"
                        )
                        try:
                            response = await fetch_with_retry(
                                client, article_url, attempts=attempts
                            )
                        except httpx.HTTPStatusError as exc:
                            save_response_snapshot(
                                session,
                                run,
                                url=article_url,
                                page_type=article_type,
                                request_method="GET",
                                response=exc.response,
                                error_text=str(exc),
                            )
                            raise
                        except httpx.TransportError as exc:
                            save_response_snapshot(
                                session,
                                run,
                                url=article_url,
                                page_type=article_type,
                                request_method="GET",
                                error_text=str(exc),
                            )
                            raise
                        article_snapshot = save_response_snapshot(
                            session,
                            run,
                            url=article_url,
                            page_type=article_type,
                            request_method="GET",
                            response=response,
                            error_text=(
                                f"http_status:{response.status_code}"
                                if not response.is_success
                                else None
                            ),
                        )
                        response.raise_for_status()
                        extracted = engine.extract_detail(
                            response.text,
                            article_url,
                            config,
                        )
                        canonical_url = normalize_url(
                            str(extracted.get("canonical_url") or article_url)
                        )
                        existing = session.scalar(
                            select(ContentItem).where(
                                ContentItem.source_id == source.id,
                                ContentItem.canonical_url == canonical_url,
                            )
                        )
                        if existing is not None and quality_tier(existing) == "verified_full":
                            extracted = preserve_verified_detail(extracted, existing)
                        elif extracted_needs_enrichment(extracted, config):
                            if canonical_url == normalize_url(article_url):
                                extracted["validation_warnings"] = list(
                                    dict.fromkeys(
                                        [
                                            *(extracted.get("validation_warnings", []) or []),
                                            "content_enrichment_same_url",
                                        ]
                                    )
                                )
                            elif not robots_allows(robots_text, canonical_url):
                                extracted["validation_warnings"] = list(
                                    dict.fromkeys(
                                        [
                                            *(extracted.get("validation_warnings", []) or []),
                                            "content_enrichment_robots_disallowed",
                                        ]
                                    )
                                )
                            else:
                                try:
                                    await asyncio.sleep(delay)
                                    fallback_response = await fetch_with_retry(
                                        client, canonical_url, attempts=attempts
                                    )
                                    fallback_snapshot = save_response_snapshot(
                                        session,
                                        run,
                                        url=canonical_url,
                                        page_type="article_enrichment_web",
                                        request_method="GET",
                                        response=fallback_response,
                                        error_text=(
                                            f"http_status:{fallback_response.status_code}"
                                            if not fallback_response.is_success
                                            else None
                                        ),
                                    )
                                    fallback_response.raise_for_status()
                                    web_article = extract_article(
                                        fallback_response.text, canonical_url, config
                                    )
                                    extracted = merge_api_web_detail(extracted, web_article)
                                    extracted["fallback_page_snapshot_id"] = fallback_snapshot.id
                                except (httpx.HTTPError, ContentFormError, ValueError) as exc:
                                    extracted["validation_warnings"] = list(
                                        dict.fromkeys(
                                            [
                                                *(extracted.get("validation_warnings", []) or []),
                                                f"content_enrichment_failed:{type(exc).__name__}",
                                            ]
                                        )
                                    )
                        if not within_publication_window(extracted, run, config, days=window_days):
                            rejected_count += 1
                            rejections.append(f"{article_url}: publication_window:{window_days}d")
                            continue
                        extracted["validation_warnings"] = list(
                            dict.fromkeys(
                                [
                                    *(extracted.get("validation_warnings", []) or []),
                                    "collection_window:v1",
                                ]
                            )
                        )
                        result = ingest_article(
                            session, source, run, extracted, article_snapshot.id
                        )
                        if result == "new":
                            new_count += 1
                        elif result == "updated":
                            updated_count += 1
                        else:
                            skipped_count += 1
                        session.commit()
                        ingested_count = new_count + updated_count + skipped_count
                        if ingested_count >= int(config.get("max_articles", 10)):
                            break
                    except ContentFormError as exc:
                        session.rollback()
                        rejected_count += 1
                        rejections.append(f"{article_url}: {exc}")
                    except Exception as exc:
                        session.rollback()
                        error_count += 1
                        errors.append(f"{article_url}: {exc}")
            run = session.get(CrawlRun, run_id)
            run.fetched_count = fetched_count
            run.new_count = new_count
            run.updated_count = updated_count
            run.skipped_count = skipped_count
            run.rejected_count = rejected_count
            run.error_count = error_count
            run.status = (
                "partial"
                if error_count or rejected_count
                else (
                    "unchanged"
                    if direct_feed and new_count == 0 and updated_count == 0
                    else "succeeded"
                )
            )
            if errors:
                run.error_code, run.error_summary = "article_errors", "\n".join(errors)[:4000]
            elif rejections:
                run.error_code = "content_form_rejected"
                run.error_summary = "\n".join(rejections)[:4000]
            if direct_feed and feed_plan is not None and run.status != "partial":
                _commit_feed_state(
                    session,
                    source.id,
                    run.id,
                    listing,
                    plan=feed_plan,
                    accept_validators=not feed_plan.has_backlog,
                )
        except Exception as exc:
            session.rollback()
            run = session.get(CrawlRun, run_id)
            run.fetched_count = fetched_count
            run.new_count = new_count
            run.updated_count = updated_count
            run.skipped_count = skipped_count
            run.rejected_count = rejected_count
            run.status, run.error_count = "failed", max(error_count, 1)
            run.error_code, run.error_summary = type(exc).__name__, str(exc)[:1000]
        run.finished_at = utcnow()
        _log_and_commit_run(session, run)


def create_crawl_run(
    session: Session,
    source: Source,
    trigger: str = "manual",
    *,
    coverage_date: date | None = None,
    publication_timezone: str | None = None,
    retry_of_run_id: int | None = None,
    reference_time: datetime | None = None,
) -> tuple[CrawlRun, bool]:
    started_at = reference_time or utcnow()
    resolved_date, resolved_timezone = resolve_run_coverage(
        source,
        reference_time=started_at,
        coverage_date=coverage_date,
        publication_timezone=publication_timezone,
    )
    active = session.scalar(
        select(CrawlRun).where(
            CrawlRun.source_id == source.id, CrawlRun.status.in_(("pending", "running"))
        )
    )
    if active:
        if (
            active.coverage_date == resolved_date
            and active.publication_timezone == resolved_timezone
            and active.retry_of_run_id == retry_of_run_id
        ):
            return active, False
        raise ActiveCrawlConflict("active_crawl_has_different_coverage_context")
    run = CrawlRun(
        source_id=source.id,
        trigger=trigger,
        coverage_date=resolved_date,
        publication_timezone=resolved_timezone,
        retry_of_run_id=retry_of_run_id,
        status="pending",
        started_at=started_at,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        active = session.scalar(
            select(CrawlRun).where(
                CrawlRun.source_id == source.id, CrawlRun.status.in_(("pending", "running"))
            )
        )
        if active:
            if (
                active.coverage_date == resolved_date
                and active.publication_timezone == resolved_timezone
                and active.retry_of_run_id == retry_of_run_id
            ):
                return active, False
            raise ActiveCrawlConflict("active_crawl_has_different_coverage_context") from exc
        raise
    session.refresh(run)
    return run, True
