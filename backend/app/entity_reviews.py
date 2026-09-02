from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .entity_extraction import ENTITY_TYPES, normalize_entity_name
from .models import (
    ContentItem,
    Entity,
    EntityAlias,
    EntityCandidateReview,
    EntityMention,
    EntityProcessingResult,
    EntityResolutionCandidate,
    utcnow,
)


@dataclass(frozen=True)
class ReviewSyncResult:
    created: int
    updated: int
    applied_decisions: int
    skipped: int


@dataclass(frozen=True)
class ReviewDecisionResult:
    review_id: int
    entity_id: int | None
    action: str
    affected_mentions: int
    reused: bool


def candidate_key(entity_type: str, normalized_name: str) -> str:
    return hashlib.sha256(f"{entity_type}:{normalized_name}".encode()).hexdigest()


def _current_llm_mentions(session: Session) -> list[tuple[EntityMention, ContentItem]]:
    return list(
        session.execute(
            select(EntityMention, ContentItem)
            .join(ContentItem, ContentItem.id == EntityMention.content_item_id)
            .join(
                EntityProcessingResult,
                EntityProcessingResult.id == EntityMention.processing_result_id,
            )
            .where(
                EntityMention.extraction_method == "llm_candidate",
                EntityProcessingResult.input_content_hash == ContentItem.content_hash,
                EntityProcessingResult.status == "succeeded",
            )
            .order_by(EntityMention.id)
        )
    )


def _refresh_processing_counts(session: Session, result_ids: set[int]) -> None:
    session.flush()
    for result_id in result_ids:
        result = session.get(EntityProcessingResult, result_id)
        if result is None:
            continue
        statuses = list(
            session.scalars(
                select(EntityMention.resolution_status).where(
                    EntityMention.processing_result_id == result_id
                )
            )
        )
        result.resolved_count = sum(status == "resolved" for status in statuses)
        result.unresolved_count = sum(
            status in {"unresolved", "ambiguous"} for status in statuses
        )


def _apply_review_to_mentions(
    session: Session, review: EntityCandidateReview
) -> int:
    mentions = [
        mention
        for mention, _ in _current_llm_mentions(session)
        if mention.entity_type == review.entity_type
        and mention.normalized_surface == review.normalized_name
    ]
    affected_results = set()
    for mention in mentions:
        affected_results.add(mention.processing_result_id)
        candidates = list(
            session.scalars(
                select(EntityResolutionCandidate).where(
                    EntityResolutionCandidate.mention_id == mention.id
                )
            )
        )
        if review.status == "confirmed":
            mention.entity_id = review.resolved_entity_id
            mention.resolution_status = "resolved"
            mention.confidence = 1.0
            for candidate in candidates:
                candidate.status = (
                    "selected"
                    if candidate.candidate_entity_id == review.resolved_entity_id
                    else "rejected"
                )
        elif review.status == "rejected":
            mention.entity_id = None
            mention.resolution_status = "rejected"
            mention.confidence = 0.0
            for candidate in candidates:
                candidate.status = "rejected"
    _refresh_processing_counts(session, affected_results)
    return len(mentions)


def sync_entity_candidate_reviews(session: Session) -> ReviewSyncResult:
    grouped: dict[tuple[str, str], list[tuple[EntityMention, ContentItem]]] = {}
    for mention, content in _current_llm_mentions(session):
        if mention.resolution_status == "resolved":
            continue
        grouped.setdefault(
            (mention.entity_type, mention.normalized_surface), []
        ).append((mention, content))
    existing = {
        review.candidate_key: review
        for review in session.scalars(select(EntityCandidateReview))
    }
    created = updated = applied = skipped = 0
    for (entity_type, normalized_name), rows in grouped.items():
        key = candidate_key(entity_type, normalized_name)
        mention_ids = [mention.id for mention, _ in rows]
        evidence = [
            {
                "mention_id": mention.id,
                "content_item_id": content.id,
                "content_title": content.title,
                "surface": mention.surface,
                "field": mention.field,
                "start_offset": mention.start_offset,
                "end_offset": mention.end_offset,
                "evidence_text": mention.evidence_text,
            }
            for mention, content in rows[:10]
        ]
        review = existing.get(key)
        if review is None:
            review = EntityCandidateReview(
                candidate_key=key,
                entity_type=entity_type,
                proposed_name=rows[0][0].surface,
                normalized_name=normalized_name,
                mention_count=len(rows),
                mention_ids=mention_ids,
                evidence=evidence,
            )
            session.add(review)
            existing[key] = review
            created += 1
        else:
            changed = (
                review.mention_count != len(rows)
                or review.mention_ids != mention_ids
                or review.evidence != evidence
            )
            review.mention_count = len(rows)
            review.mention_ids = mention_ids
            review.evidence = evidence
            if review.status in {"confirmed", "rejected"}:
                applied += _apply_review_to_mentions(session, review)
            elif changed:
                updated += 1
            else:
                skipped += 1
    session.flush()
    return ReviewSyncResult(created, updated, applied, skipped)


def _add_aliases_for_review(
    session: Session, review: EntityCandidateReview, entity: Entity
) -> None:
    rows = [
        (mention, content)
        for mention, content in _current_llm_mentions(session)
        if mention.entity_type == review.entity_type
        and mention.normalized_surface == review.normalized_name
    ]
    aliases = {(review.proposed_name, "und")}
    aliases.update((mention.surface, content.language or "und") for mention, content in rows)
    existing = {
        (alias.normalized_alias, alias.language)
        for alias in session.scalars(
            select(EntityAlias).where(EntityAlias.entity_id == entity.id)
        )
    }
    for alias, language in sorted(aliases):
        normalized = normalize_entity_name(alias)
        if not normalized or (normalized, language) in existing:
            continue
        session.add(
            EntityAlias(
                entity_id=entity.id,
                alias=alias,
                normalized_alias=normalized,
                language=language,
                alias_type="confirmed",
                source="entity_candidate_review",
                confidence=1.0,
            )
        )
        existing.add((normalized, language))


def decide_entity_candidate(
    session: Session,
    review_id: int,
    *,
    action: str,
    decided_by: str,
    reason: str,
    entity_id: int | None = None,
    canonical_name: str | None = None,
) -> ReviewDecisionResult:
    review = session.get(EntityCandidateReview, review_id)
    if review is None:
        raise ValueError("entity candidate review not found")
    if action not in {"create", "link", "reject"}:
        raise ValueError("action must be create, link, or reject")
    if not decided_by.strip() or not reason.strip():
        raise ValueError("decided_by and reason are required")
    if review.status != "pending":
        expected_status = "rejected" if action == "reject" else "confirmed"
        expected_entity = None if action == "reject" else review.resolved_entity_id
        if review.status == expected_status and (
            action != "link" or entity_id == expected_entity
        ):
            return ReviewDecisionResult(
                review.id,
                review.resolved_entity_id,
                action,
                0,
                True,
            )
        raise ValueError("entity candidate review already decided")

    entity = None
    if action == "create":
        name = (canonical_name or review.proposed_name).strip()
        if review.entity_type not in ENTITY_TYPES or not normalize_entity_name(name):
            raise ValueError("invalid entity type or canonical name")
        entity = Entity(
            registry_key=f"reviewed:{review.candidate_key}",
            entity_type=review.entity_type,
            canonical_name=name,
            normalized_name=normalize_entity_name(name),
            attributes={"origin": "reviewed_llm_candidate"},
            status="active",
        )
        session.add(entity)
        session.flush()
    elif action == "link":
        entity = session.get(Entity, entity_id) if entity_id is not None else None
        if entity is None or entity.status != "active":
            raise ValueError("target entity not found or inactive")
        if entity.entity_type != review.entity_type:
            raise ValueError("target entity type does not match candidate")

    review.status = "rejected" if action == "reject" else "confirmed"
    review.resolved_entity_id = entity.id if entity is not None else None
    review.decision_action = action
    review.decision_reason = reason.strip()
    review.decided_by = decided_by.strip()
    review.decided_at = utcnow()
    if entity is not None:
        _add_aliases_for_review(session, review, entity)
    affected = _apply_review_to_mentions(session, review)
    session.flush()
    return ReviewDecisionResult(
        review.id,
        review.resolved_entity_id,
        action,
        affected,
        False,
    )
