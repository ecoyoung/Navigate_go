from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ContentItem, ContentProcessingResult, Source, utcnow

PROCESSOR_NAME = "industry_rules"
PROCESSOR_VERSION = "industry-rules.v1"


@dataclass(frozen=True)
class RelevanceDecision:
    is_relevant: bool
    matched_topics: list[str]
    matched_events: list[str]
    reason: str


def _matches(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return sorted({keyword for keyword in keywords if keyword.lower() in lowered})


def evaluate_relevance(content: ContentItem, config: dict) -> RelevanceDecision:
    if config.get("scope_mode", "dedicated") == "dedicated":
        return RelevanceDecision(True, [], [], "dedicated_source")

    text = " ".join(
        value for value in (content.title, content.excerpt or "", content.body or "") if value
    )
    tags = [str(item).strip() for item in (getattr(content, "topics", None) or []) if item]
    topic_keywords = [str(item).strip() for item in config.get("industry_keywords", []) if item]
    event_keywords = [str(item).strip() for item in config.get("event_keywords", []) if item]
    matched_from_tags = _matches(" ".join(tags), topic_keywords)
    matched_topics = sorted(set(_matches(text, topic_keywords) + matched_from_tags))
    matched_events = _matches(text, event_keywords)

    if not matched_topics:
        return RelevanceDecision(False, [], matched_events, "no_industry_match")
    if event_keywords and not matched_events:
        return RelevanceDecision(False, matched_topics, [], "no_event_match")
    reason = "tag_match" if matched_from_tags else "rule_match"
    return RelevanceDecision(True, matched_topics, matched_events, reason)


def process_content_item(
    session: Session,
    content: ContentItem,
    source: Source,
    processor_version: str = PROCESSOR_VERSION,
) -> tuple[ContentProcessingResult, bool]:
    existing = session.scalar(
        select(ContentProcessingResult).where(
            ContentProcessingResult.content_item_id == content.id,
            ContentProcessingResult.processor_name == PROCESSOR_NAME,
            ContentProcessingResult.processor_version == processor_version,
        )
    )
    if existing:
        if existing.input_content_hash == content.content_hash:
            return existing, False
        decision = evaluate_relevance(content, source.processing_config or {})
        existing.input_content_hash = content.content_hash
        existing.is_relevant = decision.is_relevant
        existing.matched_topics = decision.matched_topics
        existing.matched_events = decision.matched_events
        existing.reason = decision.reason
        existing.processed_at = utcnow()
        return existing, True

    decision = evaluate_relevance(content, source.processing_config or {})
    result = ContentProcessingResult(
        content_item_id=content.id,
        processor_name=PROCESSOR_NAME,
        processor_version=processor_version,
        input_content_hash=content.content_hash,
        is_relevant=decision.is_relevant,
        matched_topics=decision.matched_topics,
        matched_events=decision.matched_events,
        reason=decision.reason,
    )
    session.add(result)
    return result, True
