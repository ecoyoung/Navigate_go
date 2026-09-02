import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
from dateutil import parser as date_parser

from .normalization import normalize_url


@dataclass(frozen=True)
class FeedSyncPlan:
    entries: list[dict]
    recent_entries: list[dict[str, str]]
    published_watermark: datetime | None
    updated_watermark: datetime | None
    has_backlog: bool


def parse_feed(feed_text: str) -> list[dict]:
    parsed = feedparser.parse(feed_text)
    if not parsed.entries and getattr(parsed, "bozo", False):
        raise ValueError(f"feed_parse_error:{parsed.get('bozo_exception')}")
    return list(parsed.entries)


def entry_identity(entry: dict) -> str:
    external_id = str(entry.get("id") or entry.get("guid") or "").strip()
    if external_id:
        return external_id
    link = str(entry.get("link") or "").strip()
    if link:
        return normalize_url(link)
    material = json.dumps(
        {
            "title": str(entry.get("title") or "").strip(),
            "published": str(entry.get("published") or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "fallback:" + hashlib.sha256(material.encode()).hexdigest()


def entry_fingerprint(entry: dict) -> str:
    content = entry.get("content") or []
    material = {
        "id": entry_identity(entry),
        "title": entry.get("title"),
        "link": normalize_url(str(entry.get("link") or "")) if entry.get("link") else None,
        "published": entry.get("published"),
        "updated": entry.get("updated"),
        "summary": entry.get("summary") or entry.get("description"),
        "content": [item.get("value") for item in content if isinstance(item, dict)],
        "tags": entry.get("tags") or entry.get("category"),
    }
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _entry_date(entry: dict, key: str) -> datetime | None:
    value = str(entry.get(key) or "").strip()
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _latest(values: list[datetime]) -> datetime | None:
    return max(values) if values else None


def plan_feed_sync(feed_text: str, config: dict, recent_entries: list | None) -> FeedSyncPlan:
    entries = parse_feed(feed_text)
    max_articles = min(max(int(config.get("max_articles", 10)), 1), 50)
    overlap = min(max(int(config.get("feed_overlap_entries", 5)), 0), max_articles)
    window_size = min(max(int(config.get("feed_recent_window", 100)), max_articles), 500)
    scan_limit = min(max(int(config.get("feed_scan_limit", window_size)), window_size), 1000)
    scanned = entries[:scan_limit]
    previous = {
        str(item.get("id")): str(item.get("fingerprint"))
        for item in (recent_entries or [])
        if isinstance(item, dict) and item.get("id") and item.get("fingerprint")
    }
    observed: list[tuple[dict, str, str]] = []
    for entry in scanned:
        identity = entry_identity(entry)
        fingerprint = entry_fingerprint(entry)
        observed.append((entry, identity, fingerprint))
    changed = [item for item in observed if previous.get(item[1]) != item[2]]
    selected_items = changed[:max_articles]
    selected_ids = {item[1] for item in selected_items}
    if len(selected_items) < max_articles:
        overlap_items = [
            item for item in observed[:overlap] if item[1] not in selected_ids
        ]
        selected_items.extend(overlap_items[: max_articles - len(selected_items)])
    processed = {item[1]: item[2] for item in selected_items}
    retained = {**previous, **processed}
    checkpoint: list[dict[str, str]] = []
    checkpoint_ids: set[str] = set()
    for _, identity, _ in observed:
        if identity in retained and identity not in checkpoint_ids:
            checkpoint.append({"id": identity, "fingerprint": retained[identity]})
            checkpoint_ids.add(identity)
    for identity, fingerprint in retained.items():
        if identity not in checkpoint_ids:
            checkpoint.append({"id": identity, "fingerprint": fingerprint})
    published_dates = [
        value
        for entry, _, _ in selected_items
        if (value := _entry_date(entry, "published")) is not None
    ]
    updated_dates = [
        value
        for entry, _, _ in selected_items
        if (value := _entry_date(entry, "updated")) is not None
    ]
    return FeedSyncPlan(
        entries=[item[0] for item in selected_items],
        recent_entries=checkpoint[:window_size],
        published_watermark=_latest(published_dates),
        updated_watermark=_latest(updated_dates),
        has_backlog=len(changed) > max_articles,
    )
