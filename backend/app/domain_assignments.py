from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ContentDomainAssignment, ContentItem, ContentProcessingResult, Domain


@dataclass(frozen=True)
class AssignmentSyncResult:
    domain_id: int
    created: int
    updated: int
    skipped: int


def active_domain_classifier(domain: Domain) -> tuple[str, str] | None:
    active = (domain.config or {}).get("active_classifier") or {}
    name = str(active.get("name") or "").strip()
    version = str(active.get("version") or "").strip()
    return (name, version) if name and version else None


def ensure_domain(
    session: Session,
    *,
    key: str,
    name: str,
    description: str | None = None,
    config: dict | None = None,
) -> Domain:
    domain = session.scalar(select(Domain).where(Domain.key == key))
    if domain is None:
        domain = Domain(
            key=key,
            name=name,
            description=description,
            config=config or {},
        )
        session.add(domain)
        session.flush()
    return domain


def sync_processing_results_to_domain(
    session: Session,
    *,
    domain_key: str,
    domain_name: str,
    processor_name: str,
    processor_version: str,
    description: str | None = None,
    domain_config: dict | None = None,
    activate_classifier: bool = False,
) -> AssignmentSyncResult:
    """Project a versioned legacy processor into the generic multi-domain layer."""
    domain = ensure_domain(
        session,
        key=domain_key,
        name=domain_name,
        description=description,
        config=domain_config,
    )
    if activate_classifier:
        domain.config = {
            **(domain.config or {}),
            "active_classifier": {
                "name": processor_name,
                "version": processor_version,
            },
        }
    rows = session.execute(
        select(ContentItem, ContentProcessingResult).join(
            ContentProcessingResult,
            (ContentProcessingResult.content_item_id == ContentItem.id)
            & (ContentProcessingResult.processor_name == processor_name)
            & (ContentProcessingResult.processor_version == processor_version)
            & (ContentProcessingResult.input_content_hash == ContentItem.content_hash),
        )
    )
    existing = {
        (item.content_item_id, item.classifier_name, item.classifier_version): item
        for item in session.scalars(
            select(ContentDomainAssignment).where(ContentDomainAssignment.domain_id == domain.id)
        )
    }
    created = updated = skipped = 0
    for content, processing in rows:
        key = (content.id, processor_name, processor_version)
        assignment = existing.get(key)
        decision = "include" if processing.is_relevant else "exclude"
        confidence = 1.0
        reasons = [
            {
                "code": processing.reason,
                "matched_topics": processing.matched_topics,
                "matched_events": processing.matched_events,
            }
        ]
        values = {
            "input_content_hash": content.content_hash,
            "decision": decision,
            "confidence": confidence,
            "reasons": reasons,
        }
        if assignment is None:
            session.add(
                ContentDomainAssignment(
                    content_item_id=content.id,
                    domain_id=domain.id,
                    classifier_name=processor_name,
                    classifier_version=processor_version,
                    **values,
                )
            )
            created += 1
        elif all(getattr(assignment, field) == value for field, value in values.items()):
            skipped += 1
        else:
            for field, value in values.items():
                setattr(assignment, field, value)
            updated += 1
    return AssignmentSyncResult(domain.id, created, updated, skipped)
