import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .contracts import normalize_topics


@dataclass(frozen=True)
class RedFoxListPage:
    items: list[dict]
    total: int | None


def parse_list_page(payload_text: str) -> RedFoxListPage:
    payload = json.loads(payload_text)
    if payload.get("code") not in (200, 2000):
        raise ValueError(f"redfox_list_error:{payload.get('code')}:{payload.get('msg')}")
    data = payload.get("data") or {}
    if isinstance(data, list):
        return RedFoxListPage(
            items=[item for item in data if isinstance(item, dict)], total=None
        )
    if isinstance(data, dict):
        items = data.get("list") or data.get("articles") or data.get("records") or []
        total = data.get("total")
        try:
            parsed_total = int(total) if total is not None else None
        except (TypeError, ValueError):
            parsed_total = None
        return RedFoxListPage(
            items=[item for item in items if isinstance(item, dict)], total=parsed_total
        )
    return RedFoxListPage(items=[], total=None)


def parse_list_payload(payload_text: str) -> list[dict]:
    return parse_list_page(payload_text).items


def _title(item: dict) -> str:
    return str(item.get("title") or item.get("name") or "").strip()


def parse_publish_time(value: object, timezone_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("redfox_missing_publish_time")
    try:
        parsed = date_parser.parse(raw)
        timezone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, OverflowError, KeyError) as exc:
        raise ValueError(f"redfox_invalid_publish_time:{raw}") from exc
    localized = parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed
    return localized.astimezone(UTC)


def target_publication_date(reference_time: datetime, timezone_name: str) -> date:
    reference = reference_time if reference_time.tzinfo else reference_time.replace(tzinfo=UTC)
    return reference.astimezone(ZoneInfo(timezone_name)).date() - timedelta(days=1)


def _dated_items(articles: list[dict], timezone_name: str) -> list[tuple[dict, date]]:
    dated = [
        (item, parse_publish_time(item.get("publishTime"), timezone_name).astimezone(
            ZoneInfo(timezone_name)
        ).date())
        for item in articles
        if item.get("workUuid")
    ]
    dates = [published for _, published in dated]
    if dates != sorted(dates, reverse=True):
        raise ValueError("redfox_list_not_sorted_by_publish_time")
    return dated


def _truthy_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().lower() in {"true", "yes", "y", "1"}


def _label_values(value: object) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    labels: list[str] = []

    def append_label(label: object) -> None:
        labels.extend(
            part.strip().lower()
            for part in re.split(r"[,，;；|]+", str(label or ""))
            if part.strip()
        )

    for item in raw:
        if isinstance(item, dict):
            for key in ("name", "label", "title", "value", "tagName"):
                if item.get(key):
                    append_label(item[key])
                    break
        elif item is not None:
            append_label(item)
    return [label for label in labels if label]


def explicit_exclusion_reason(item: dict, config: dict) -> str | None:
    if config.get("exclude_explicit_pinned", True):
        if any(
            _truthy_flag(item.get(key))
            for key in ("isTop", "isPinned", "pinned", "topFlag")
        ):
            return "explicit_pinned_flag"
    if config.get("exclude_explicit_advertising", True):
        if any(
            _truthy_flag(item.get(key))
            for key in ("isAd", "isAdvertisement", "advertisement", "sponsored")
        ):
            return "explicit_advertising_flag"
        content_type = str(
            item.get("itemType") or item.get("contentType") or ""
        ).strip().lower()
        if content_type in {"ad", "advertisement", "promotion", "sponsored"}:
            return "explicit_advertising_type"
    labels: list[str] = []
    for key in ("tags", "tagList", "labels", "categories", "contentKeywords"):
        labels.extend(_label_values(item.get(key)))
    if config.get("exclude_explicit_pinned", True) and set(labels) & {
        "置顶",
        "pinned",
        "top",
    }:
        return "explicit_pinned_label"
    if config.get("exclude_explicit_advertising", True) and set(labels) & {
        "广告",
        "推广",
        "商业推广",
        "赞助",
        "ad",
        "advertisement",
        "promotion",
        "sponsored",
    }:
        return "explicit_advertising_label"
    return None


def redfox_page_needs_next(
    page: RedFoxListPage,
    *,
    offset: int,
    target_date: date,
    timezone_name: str,
) -> bool:
    dated = _dated_items(page.items, timezone_name)
    if not dated:
        return bool(page.total and offset < page.total)
    if any(published < target_date for _, published in dated):
        return False
    next_offset = offset + len(page.items)
    if page.total is not None and next_offset >= page.total:
        return False
    return True


def pick_articles(
    articles: list[dict],
    config: dict,
    *,
    reference_time: datetime | None = None,
    target_date: date | None = None,
    publication_timezone: str | None = None,
) -> list[dict]:
    if config.get("publication_date_mode") != "previous_day":
        raise ValueError("redfox_requires_previous_day_mode")
    timezone_name = str(
        publication_timezone or config.get("publication_timezone") or "Asia/Shanghai"
    )
    if target_date is None:
        if reference_time is None:
            raise ValueError("redfox_target_date_required")
        target_date = target_publication_date(reference_time, timezone_name)
    picked = [
        item
        for item, published in _dated_items(articles, timezone_name)
        if published == target_date and explicit_exclusion_reason(item, config) is None
    ]
    limit = min(max(int(config.get("max_articles_per_day", 100)), 1), 500)
    if len(picked) > limit:
        raise ValueError(f"redfox_daily_article_limit_exceeded:{len(picked)}>{limit}")
    return picked


def _plain_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "<" in text and ">" in text:
        return BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _content_media(value: object) -> list[dict]:
    text = str(value or "")
    if "<" not in text or ">" not in text:
        return []
    return [
        {
            "type": "image",
            "url": str(node.get("data-src") or node.get("src") or "").strip(),
            "alt": str(node.get("alt") or "").strip() or None,
        }
        for node in BeautifulSoup(text, "lxml").select("img[data-src], img[src]")
        if node.get("data-src") or node.get("src")
    ]


def detail_to_extracted(
    payload_text: str,
    fallback: dict | None = None,
    min_content_chars: int = 80,
    publication_timezone: str = "Asia/Shanghai",
) -> dict:
    payload = json.loads(payload_text)
    if payload.get("code") not in (200, 2000):
        raise ValueError(f"redfox_detail_error:{payload.get('code')}:{payload.get('msg')}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    fallback = fallback or {}
    title = str(data.get("title") or _title(fallback)).strip()
    raw_content = data.get("content")
    body = _plain_text(raw_content)
    if not title:
        raise ValueError("missing_title")
    if len(body) < min_content_chars:
        raise ValueError(f"content_too_short:{len(body)}")
    url = str(data.get("workUrl") or fallback.get("workUrl") or fallback.get("url") or "").strip()
    summary = _plain_text(data.get("summary") or fallback.get("summary")) or body[:500]
    return {
        "title": title,
        "canonical_url": url,
        "original_url": url,
        "author": str(data.get("author") or fallback.get("author") or "").strip() or None,
        "published_at": parse_publish_time(
            data.get("publishTime") or fallback.get("publishTime"), publication_timezone
        ),
        "updated_at": None,
        "external_item_id": str(data.get("workUuid") or fallback.get("workUuid") or "").strip()
        or None,
        "body": body,
        "description": summary,
        "content_type": "article",
        "topics": normalize_topics(data.get("contentKeywords") or fallback.get("topics")),
        "media": _content_media(raw_content),
        "content_completeness": "full",
    }
