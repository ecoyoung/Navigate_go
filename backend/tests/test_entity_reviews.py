from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.entity_extraction import normalize_entity_name
from app.entity_reviews import decide_entity_candidate, sync_entity_candidate_reviews
from app.models import (
    ContentItem,
    CrawlRun,
    Entity,
    EntityAlias,
    EntityCandidateReview,
    EntityMention,
    EntityProcessingResult,
    Source,
)
from app.web_ingestion import ingest_article


def add_review_fixture(session):
    source = Source(
        name="Review News",
        channel_type="web",
        start_url="https://review.example.com/",
        normalized_start_url="https://review.example.com/",
        parser_config={},
    )
    session.add(source)
    session.flush()
    run = CrawlRun(source_id=source.id, status="running")
    session.add(run)
    session.flush()
    ingest_article(
        session,
        source,
        run,
        {
            "title": "Acme launches NEXT50 2026",
            "canonical_url": "https://review.example.com/story",
            "original_url": "https://review.example.com/story",
            "author": None,
            "published_at": datetime(2026, 8, 30, tzinfo=UTC),
            "body": "Acme described the launch in a public article.",
            "description": "Acme launch announcement.",
            "content_type": "article",
            "topics": [],
        },
    )
    content = session.scalar(select(ContentItem))
    result = EntityProcessingResult(
        content_item_id=content.id,
        extractor_name="llm_entity_candidates",
        extractor_version="llm-entity-candidates.v1",
        input_content_hash=content.content_hash,
        config_hash="a" * 64,
        schema_version="entity-candidates.v1",
        status="succeeded",
        candidate_count=2,
        resolved_count=0,
        unresolved_count=2,
        output={},
    )
    session.add(result)
    session.flush()
    for surface, entity_type, start in (
        ("Acme", "brand", 0),
        ("NEXT50 2026", "product", 14),
    ):
        session.add(
            EntityMention(
                processing_result_id=result.id,
                content_item_id=content.id,
                entity_id=None,
                entity_type=entity_type,
                surface=surface,
                normalized_surface=normalize_entity_name(surface),
                field="title",
                start_offset=start,
                end_offset=start + len(surface),
                evidence_text=content.title,
                confidence=0.0,
                resolution_status="unresolved",
                extraction_method="llm_candidate",
            )
        )
    session.flush()
    return content, result


def test_sync_and_create_entity_are_auditable_and_idempotent(session_factory):
    with session_factory() as session:
        _, processing = add_review_fixture(session)
        first = sync_entity_candidate_reviews(session)
        second = sync_entity_candidate_reviews(session)
        review = session.scalar(
            select(EntityCandidateReview).where(EntityCandidateReview.proposed_name == "Acme")
        )
        decision = decide_entity_candidate(
            session,
            review.id,
            action="create",
            canonical_name="Acme",
            decided_by="tester",
            reason="title evidence confirms a named brand",
        )
        repeated = decide_entity_candidate(
            session,
            review.id,
            action="create",
            canonical_name="Acme",
            decided_by="tester",
            reason="same decision",
        )
        session.commit()

        assert first.created == 2
        assert second.skipped == 2
        assert decision.entity_id is not None
        assert decision.affected_mentions == 1
        assert repeated.reused is True
        assert review.status == "confirmed"
        assert review.decision_action == "create"
        assert review.decided_by == "tester"
        mention = session.scalar(
            select(EntityMention).where(EntityMention.surface == "Acme")
        )
        assert mention.entity_id == decision.entity_id
        assert mention.resolution_status == "resolved"
        assert processing.resolved_count == 1
        assert processing.unresolved_count == 1
        assert (
            session.scalar(
                select(func.count(EntityAlias.id)).where(
                    EntityAlias.entity_id == decision.entity_id
                )
            )
            >= 1
        )


def test_review_reject_api_and_link_type_guard(client, session_factory):
    with session_factory() as session:
        add_review_fixture(session)
        sync_entity_candidate_reviews(session)
        review = session.scalar(
            select(EntityCandidateReview).where(
                EntityCandidateReview.proposed_name == "NEXT50 2026"
            )
        )
        wrong_type = Entity(
            registry_key="organization:wrong",
            entity_type="organization",
            canonical_name="Wrong",
            normalized_name="wrong",
        )
        session.add(wrong_type)
        session.commit()
        review_id = review.id
        wrong_id = wrong_type.id

    mismatch = client.post(
        f"/api/v1/entity-candidate-reviews/{review_id}/decision",
        json={
            "action": "link",
            "entity_id": wrong_id,
            "decided_by": "tester",
            "reason": "test mismatch",
        },
    )
    assert mismatch.status_code == 409

    rejected = client.post(
        f"/api/v1/entity-candidate-reviews/{review_id}/decision",
        json={
            "action": "reject",
            "decided_by": "tester",
            "reason": "program name is outside the entity taxonomy",
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["decision_action"] == "reject"
    assert client.get("/api/v1/entity-candidate-reviews?review_status=rejected").json()[0][
        "id"
    ] == review_id

    with session_factory() as session:
        mention = session.scalar(
            select(EntityMention).where(EntityMention.surface == "NEXT50 2026")
        )
        assert mention.resolution_status == "rejected"


def test_decision_requires_review_and_reason(session_factory):
    with session_factory() as session:
        with pytest.raises(ValueError, match="not found"):
            decide_entity_candidate(
                session,
                999,
                action="reject",
                decided_by="tester",
                reason="not found",
            )
