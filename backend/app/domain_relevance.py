from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ContentItem, ContentProcessingResult, Source, utcnow

DEFAULT_POLICY_DIR = Path(__file__).resolve().parents[1] / "config" / "domains"
POLICY_SCHEMA_VERSION = "domain-relevance-policy.v1"


@dataclass(frozen=True)
class DomainRelevanceDecision:
    is_relevant: bool
    matched_content_keywords: list[str]
    matched_source_tags: list[str]
    reason: str


def load_domain_relevance_policy(
    domain_key: str,
    *,
    config_dir: Path = DEFAULT_POLICY_DIR,
) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", domain_key):
        raise ValueError(f"Invalid domain key: {domain_key!r}")
    path = config_dir / f"{domain_key}.v1.json"
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid domain relevance policy at {path}: {exc}") from exc
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError(f"Unsupported domain relevance policy: {path}")
    if policy.get("domain_key") != domain_key:
        raise ValueError(f"Domain key mismatch in relevance policy: {path}")
    for field in (
        "domain_name",
        "definition",
        "classifier_name",
        "classifier_version",
        "llm_classifier_name",
        "llm_classifier_version",
    ):
        if not str(policy.get(field, "")).strip():
            raise ValueError(f"Missing {field} in relevance policy: {path}")
    return policy


def _contains(text: str, keyword: str) -> bool:
    keyword = keyword.strip().casefold()
    if not keyword:
        return False
    if keyword.isascii() and any(character.isalnum() for character in keyword):
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text


def _matches(text: str, keywords: list[str]) -> list[str]:
    folded = text.casefold()
    return sorted({keyword for keyword in keywords if _contains(folded, keyword)})


def evaluate_domain_relevance(
    content: ContentItem,
    source: Source,
    policy: dict,
) -> DomainRelevanceDecision:
    source_tags = [str(item).strip() for item in (source.source_tags or []) if str(item).strip()]
    dedicated_tags = {
        str(item).strip().casefold()
        for item in policy.get("dedicated_source_tags", [])
        if str(item).strip()
    }
    matched_source_tags = sorted(
        {tag for tag in source_tags if tag.casefold() in dedicated_tags}
    )
    if matched_source_tags:
        return DomainRelevanceDecision(
            True,
            [],
            matched_source_tags,
            "dedicated_domain_source",
        )

    text = " ".join(
        value
        for value in (
            content.title,
            content.excerpt or "",
            content.body or "",
            " ".join(str(topic) for topic in (content.topics or [])),
        )
        if value
    )
    matched_keywords = _matches(
        text,
        [str(item).strip() for item in policy.get("content_keywords", [])],
    )
    if matched_keywords:
        return DomainRelevanceDecision(
            False,
            matched_keywords,
            [],
            "needs_llm_domain_review",
        )
    return DomainRelevanceDecision(False, [], [], "no_domain_evidence")


def process_domain_relevance(
    session: Session,
    content: ContentItem,
    source: Source,
    policy: dict,
) -> tuple[ContentProcessingResult, bool]:
    processor_name = str(policy["classifier_name"])
    processor_version = str(policy["classifier_version"])
    existing = session.scalar(
        select(ContentProcessingResult).where(
            ContentProcessingResult.content_item_id == content.id,
            ContentProcessingResult.processor_name == processor_name,
            ContentProcessingResult.processor_version == processor_version,
        )
    )
    if existing and existing.input_content_hash == content.content_hash:
        return existing, False

    decision = evaluate_domain_relevance(content, source, policy)
    values = {
        "input_content_hash": content.content_hash,
        "is_relevant": decision.is_relevant,
        "matched_topics": decision.matched_content_keywords,
        "matched_events": decision.matched_source_tags,
        "reason": decision.reason,
        "processed_at": utcnow(),
    }
    if existing is None:
        existing = ContentProcessingResult(
            content_item_id=content.id,
            processor_name=processor_name,
            processor_version=processor_version,
            **values,
        )
        session.add(existing)
    else:
        for field, value in values.items():
            setattr(existing, field, value)
    return existing, True
