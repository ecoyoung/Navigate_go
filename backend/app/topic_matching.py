from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ContentItem, InterestTopic, Source, TopicMatch

COMPILER_NAME = "local_topic_compiler"
COMPILER_VERSION = "topic-intent.v1"
MATCHER_VERSION = "topic-matcher.v1"
_SPLIT = re.compile(r"[，,。；;、\n]|以及|或者|或是|和|与")
_PREFIX = re.compile(r"^(?:我想|请|持续|重点)?(?:关注|追踪|跟踪|订阅|了解)")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = value.strip().casefold()
        if 2 <= len(clean) <= 40 and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result[:16]


def compile_topic_intent(
    intent_text: str,
    *,
    keywords: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
) -> tuple[dict, str]:
    clean_intent = " ".join(intent_text.strip().split())
    if not clean_intent:
        raise ValueError("主题描述不能为空")
    positive_text, _, excluded_text = clean_intent.partition("排除")
    derived = [_PREFIX.sub("", item).strip() for item in _SPLIT.split(positive_text)]
    english = re.findall(r"[A-Za-z][A-Za-z0-9+._-]{1,30}", positive_text)
    positive = _unique([*(keywords or []), *derived, *english])
    excluded = _unique([*(excluded_keywords or []), *_SPLIT.split(excluded_text)])
    if not positive:
        positive = [clean_intent.casefold()[:40]]
    compiled = {
        "schema_version": "topic-intent.v1",
        "positive_keywords": positive,
        "excluded_keywords": excluded,
        "original_language": "zh-CN",
    }
    intent_hash = hashlib.sha256(
        (clean_intent + "\n" + "\n".join(positive) + "\n--\n" + "\n".join(excluded)).encode()
    ).hexdigest()
    return compiled, intent_hash


def suggested_topic_name(intent_text: str, compiled: dict) -> str:
    keywords = compiled.get("positive_keywords") or []
    if keywords:
        return str(keywords[0])[:24]
    return intent_text.strip()[:24]


@dataclass(frozen=True)
class MatchDecision:
    decision: str
    score: float
    reasons: list[str]
    signals: dict


def match_content(topic: InterestTopic, content: ContentItem) -> MatchDecision:
    compiled = topic.compiled_intent or {}
    positives = [str(item).casefold() for item in compiled.get("positive_keywords") or []]
    exclusions = [str(item).casefold() for item in compiled.get("excluded_keywords") or []]
    title = (content.title or "").casefold()
    excerpt = (content.excerpt or "").casefold()
    body = (content.body or "").casefold()
    topic_text = " ".join(content.topics or []).casefold()
    combined = f"{title}\n{excerpt}\n{body}\n{topic_text}"
    excluded_hits = [item for item in exclusions if item and item in combined]
    if excluded_hits:
        return MatchDecision("exclude", 0.0, ["excluded_keyword"], {"excluded": excluded_hits})
    title_hits = [item for item in positives if item and item in title]
    excerpt_hits = [item for item in positives if item and item in excerpt]
    body_hits = [item for item in positives if item and item in body]
    topic_hits = [item for item in positives if item and item in topic_text]
    score = min(
        1.0,
        len(title_hits) * 0.42
        + len(excerpt_hits) * 0.24
        + len(topic_hits) * 0.28
        + len(body_hits) * 0.10,
    )
    decision = "include" if score >= 0.2 else "review" if score >= 0.1 else "exclude"
    reasons = [name for name, hits in (
        ("title_keyword", title_hits),
        ("excerpt_keyword", excerpt_hits),
        ("topic_keyword", topic_hits),
        ("body_keyword", body_hits),
    ) if hits]
    return MatchDecision(
        decision,
        score,
        reasons or ["no_keyword_evidence"],
        {"title": title_hits, "excerpt": excerpt_hits, "topics": topic_hits, "body": body_hits},
    )


def refresh_topic_matches(
    db: Session,
    topic: InterestTopic,
    *,
    limit: int = 300,
    new_item_window_start: datetime | None = None,
    new_item_window_end: datetime | None = None,
) -> dict:
    rows = list(
        db.execute(
            select(ContentItem, Source)
            .join(Source, Source.id == ContentItem.source_id)
            .where(ContentItem.duplicate_of_id.is_(None))
            .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
            .limit(limit)
        )
    )
    existing = {
        item.content_item_id: item
        for item in db.scalars(
            select(TopicMatch).where(
                TopicMatch.topic_id == topic.id,
                TopicMatch.matcher_version == MATCHER_VERSION,
            )
        )
    }
    included = reviewed = excluded = 0
    for content, _source in rows:
        match = existing.get(content.id)
        if match is None and new_item_window_start is not None:
            published_at = content.published_at
            if published_at is None:
                continue
            published_utc = (
                published_at.replace(tzinfo=UTC)
                if published_at.tzinfo is None
                else published_at.astimezone(UTC)
            )
            start_utc = (
                new_item_window_start.replace(tzinfo=UTC)
                if new_item_window_start.tzinfo is None
                else new_item_window_start.astimezone(UTC)
            )
            end_value = new_item_window_end or datetime.now(UTC)
            end_utc = (
                end_value.replace(tzinfo=UTC)
                if end_value.tzinfo is None
                else end_value.astimezone(UTC)
            )
            if not start_utc <= published_utc <= end_utc:
                continue
            collection_window = {
                "schema_version": "collection-window.v2",
                "mode": "shared_pool",
                "start_at": start_utc.isoformat(),
                "end_at": end_utc.isoformat(),
                "published_at": published_utc.isoformat(),
                "admitted": True,
            }
        else:
            collection_window = (
                (match.matched_signals or {}).get("collection_window") if match else None
            )
        decision = match_content(topic, content)
        included += int(decision.decision == "include")
        reviewed += int(decision.decision == "review")
        excluded += int(decision.decision == "exclude")
        values = {
            "input_content_hash": content.content_hash,
            "decision": decision.decision,
            "score": decision.score,
            "reasons": decision.reasons,
            "matched_signals": {
                **decision.signals,
                **({"collection_window": collection_window} if collection_window else {}),
            },
            "matched_at": datetime.now(UTC),
        }
        if match is None:
            db.add(
                TopicMatch(
                    topic_id=topic.id,
                    content_item_id=content.id,
                    matcher_version=MATCHER_VERSION,
                    **values,
                )
            )
        else:
            for field, value in values.items():
                setattr(match, field, value)
    db.flush()
    return {"scanned": len(rows), "included": included, "review": reviewed, "excluded": excluded}
