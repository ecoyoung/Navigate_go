import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

CONTRACT_VERSION = "article.v1.1"


class MediaAsset(BaseModel):
    type: str = "image"
    url: str
    alt: str | None = None


class ContentQuality(BaseModel):
    body_complete: bool | None = None
    metadata_only: bool = False
    validation_warnings: list[str] = Field(default_factory=list)


class NormalizedArticle(BaseModel):
    schema_version: str = CONTRACT_VERSION
    source_id: int
    source_name: str
    source_region: str
    source_type: str
    source_external_id: str | None = None
    external_item_id: str | None = None
    channel_type: str
    provider: str = "direct"
    language: str
    access_level: str = "public"
    content_type: str = "article"
    title: str
    original_url: str
    canonical_url: str
    content_url: str
    discovery_url: str
    author: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    excerpt: str | None = None
    body_text: str
    word_count: int
    topics: list[str] = Field(default_factory=list)
    media: list[MediaAsset] = Field(default_factory=list)
    quality: ContentQuality = Field(default_factory=ContentQuality)
    is_sponsored: bool = False
    is_roundup: bool = False
    content_hash: str


def normalize_topics(values: object) -> list[str]:
    if values is None:
        return []
    raw: list[object]
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, list):
        raw = values
    else:
        raw = [values]
    topics: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name") or item.get("term") or item.get("keyword") or ""
        text = str(item).strip()
        if text.lower().startswith(("http://", "https://")):
            continue
        for part in re.split(r"[,;，、|]+", text):
            topic = re.sub(r"\s+", " ", part).strip(" #")
            if not topic or len(topic) > 40:
                continue
            key = topic.casefold()
            if key in seen:
                continue
            seen.add(key)
            topics.append(topic)
            if len(topics) >= 20:
                return topics
    return topics


def normalize_media(values: object) -> list[MediaAsset]:
    raw = values if isinstance(values, list) else [values]
    media: list[MediaAsset] = []
    seen: set[str] = set()
    for item in raw:
        if not item:
            continue
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("src") or item.get("href") or "").strip()
        if urlsplit(url).scheme not in {"http", "https"} or url in seen:
            continue
        seen.add(url)
        media.append(
            MediaAsset(
                type=str(item.get("type") or "image").split("/", 1)[0],
                url=url,
                alt=str(item.get("alt") or "").strip() or None,
            )
        )
        if len(media) >= 50:
            break
    return media


def count_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|[\u3400-\u9fff]", text))


def detect_language(text: str, default: str = "und") -> str:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return default
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", compact))
    letter_count = len(re.findall(r"[A-Za-z\u3400-\u9fff]", compact))
    if letter_count and cjk_count / letter_count >= 0.08:
        return "zh-CN"
    if re.search(r"[A-Za-z]", compact):
        return "en"
    return default


def article_content_hash(
    title: str, body_text: str, excerpt: str | None, topics: list[str]
) -> str:
    payload = {
        "title": re.sub(r"\s+", " ", title).strip(),
        "body_text": re.sub(r"\s+", " ", body_text).strip(),
        "excerpt": re.sub(r"\s+", " ", excerpt or "").strip(),
        "topics": normalize_topics(topics),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_contract(
    extracted: dict,
    source,
) -> NormalizedArticle:
    config = source.parser_config or {}
    language = detect_language(
        f"{extracted['title']}\n{extracted['body']}", source.default_language
    )
    normalized_text = re.sub(r"\s+", " ", extracted["body"]).strip()
    excerpt = extracted.get("description")
    topics = normalize_topics(extracted.get("topics"))
    content_hash = article_content_hash(extracted["title"], normalized_text, excerpt, topics)
    source_external_id = (
        getattr(source, "source_external_id", None)
        or config.get("source_external_id")
        or config.get("account_fakeid")
        or (config.get("discovery_json") or {}).get("bizInfo")
    )
    warnings = [str(item) for item in extracted.get("validation_warnings", []) if item]
    if getattr(source, "channel_type", "web") == "api" and not extracted.get(
        "published_at"
    ) and "missing_published_at" not in warnings:
        warnings.append("missing_published_at")
    return NormalizedArticle(
        source_id=source.id,
        source_name=source.name,
        source_region=source.source_region,
        source_type=source.source_type,
        source_external_id=str(source_external_id).strip() if source_external_id else None,
        external_item_id=str(extracted.get("external_item_id") or "").strip() or None,
        channel_type=str(getattr(source, "channel_type", "web")),
        provider=str(config.get("provider") or "direct"),
        language=language,
        access_level=config.get("access_level", "public"),
        content_type=extracted.get("content_type") or config.get("content_type", "article"),
        title=extracted["title"],
        original_url=extracted["original_url"],
        canonical_url=extracted["canonical_url"],
        content_url=extracted.get("content_url") or extracted["original_url"],
        discovery_url=str(config.get("discovery_url") or getattr(source, "start_url", "")),
        author=extracted.get("author"),
        published_at=extracted.get("published_at"),
        updated_at=extracted.get("updated_at"),
        excerpt=excerpt,
        body_text=extracted["body"],
        word_count=count_words(normalized_text),
        is_sponsored=any(
            keyword.lower() in extracted["title"].lower()
            for keyword in config.get("sponsored_keywords", ["sponsored", "partner content"])
        ),
        is_roundup=any(
            keyword.lower() in extracted["title"].lower()
            for keyword in config.get("roundup_keywords", ["roundup", "weekly review"])
        ),
        topics=topics,
        media=normalize_media(extracted.get("media", [])),
        quality=_content_quality(extracted, config, warnings),
        content_hash=content_hash,
    )


def _content_quality(extracted: dict, config: dict, warnings: list[str]) -> ContentQuality:
    completeness = str(
        extracted.get("content_completeness")
        or config.get("content_completeness")
        or "unknown"
    )
    if completeness == "full":
        body_complete: bool | None = True
    elif completeness in {"partial", "metadata_only"}:
        body_complete = False
    else:
        body_complete = None
        if "completeness_unknown" not in warnings:
            warnings.append("completeness_unknown")
    if completeness == "partial" and "partial_content" not in warnings:
        warnings.append("partial_content")
    if completeness == "metadata_only" and "metadata_only" not in warnings:
        warnings.append("metadata_only")
    return ContentQuality(
        body_complete=body_complete,
        metadata_only=completeness == "metadata_only",
        validation_warnings=warnings,
    )
