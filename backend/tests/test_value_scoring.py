from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models import (
    ContentDomainAssignment,
    ContentItem,
    ContentValueScore,
    ContentValueScoreRun,
    CrawlRun,
    Domain,
    Entity,
    EntityMention,
    EntityProcessingResult,
    Event,
    EventMember,
    Source,
)
from app.value_scoring import apply_value_score_plan, build_value_score_plan
from app.web_ingestion import ingest_article

AS_OF = datetime(2026, 8, 30, tzinfo=UTC)


def _content(session, *, suffix: str, published_at=AS_OF - timedelta(hours=12)):
    source = Source(
        name=f"Source {suffix}",
        channel_type="web",
        start_url=f"https://{suffix}.example/",
        normalized_start_url=f"https://{suffix}.example/",
        source_type="trade_media",
        parser_config={},
        processing_config={},
    )
    session.add(source)
    session.flush()
    run = CrawlRun(source_id=source.id, status="running")
    session.add(run)
    session.flush()
    result = ingest_article(
        session,
        source,
        run,
        {
            "title": f"Important verified story {suffix}",
            "canonical_url": f"https://{suffix}.example/story",
            "original_url": f"https://{suffix}.example/story",
            "published_at": published_at,
            "body": "Verified full article evidence. " * 80,
            "description": "Verified story summary.",
            "content_type": "article",
            "topics": [],
        },
    )
    assert result == "new"
    return session.scalar(select(ContentItem).where(ContentItem.source_id == source.id))


def _assign(session, domain, item):
    session.add(
        ContentDomainAssignment(
            content_item_id=item.id,
            domain_id=domain.id,
            classifier_name="test",
            classifier_version="v1",
            input_content_hash=item.content_hash,
            decision="include",
            confidence=0.8,
            reasons=[],
        )
    )


def test_score_is_explainable_gated_and_idempotent(session_factory):
    with session_factory() as session:
        domain = Domain(key="sample", name="Sample")
        session.add(domain)
        session.flush()
        first = _content(session, suffix="first")
        missing_date = _content(session, suffix="undated", published_at=None)
        _assign(session, domain, first)
        _assign(session, domain, missing_date)
        event = Event(
            representative_content_id=first.id,
            canonical_title=first.title,
            first_published_at=first.published_at,
            last_published_at=first.published_at,
            membership_hash="a" * 64,
            cluster_version="test",
        )
        session.add(event)
        session.flush()
        session.add(
            EventMember(
                event_id=event.id,
                content_item_id=first.id,
                confidence=1,
                algorithm_version="test",
            )
        )
        unresolved_result = EntityProcessingResult(
            content_item_id=first.id,
            extractor_name="test",
            extractor_version="v1",
            input_content_hash=first.content_hash,
            config_hash="b" * 64,
            status="succeeded",
        )
        session.add(unresolved_result)
        session.flush()
        session.add(
            EntityMention(
                processing_result_id=unresolved_result.id,
                content_item_id=first.id,
                entity_type="brand",
                surface="Verified",
                normalized_surface="verified",
                field="title",
                start_offset=0,
                end_offset=8,
                evidence_text=first.title,
                confidence=0.9,
                resolution_status="pending",
            )
        )
        session.commit()

        plan = build_value_score_plan(session, domain_key="sample", as_of=AS_OF)
        by_id = {score.content_item_id: score for score in plan.scores}
        assert len(plan.scores) == 2
        assert by_id[first.id].component_scores["confirmed_entities"]["normalized"] == 0
        assert by_id[missing_date.id].decision == "full_pool"
        assert by_id[missing_date.id].gates == ["missing_published_at"]
        assert set(by_id[first.id].component_scores) == {
            "recency",
            "source_quality",
            "completeness",
            "corroboration",
            "confirmed_entities",
            "domain_confidence",
        }

        applied = apply_value_score_plan(session, plan)
        session.commit()
        repeated = apply_value_score_plan(
            session, build_value_score_plan(session, domain_key="sample", as_of=AS_OF)
        )
        session.commit()
        assert repeated.reused_run is True
        assert repeated.run_id == applied.run_id
        assert session.scalar(select(func.count(ContentValueScoreRun.id))) == 1
        assert session.scalar(select(func.count(ContentValueScore.id))) == 2


def test_confirmed_entities_and_cross_source_event_raise_only_their_components(
    session_factory,
):
    with session_factory() as session:
        domain = Domain(key="sample", name="Sample")
        session.add(domain)
        session.flush()
        first = _content(session, suffix="one")
        second = _content(session, suffix="two")
        _assign(session, domain, first)
        _assign(session, domain, second)
        event = Event(
            representative_content_id=first.id,
            canonical_title=first.title,
            first_published_at=first.published_at,
            last_published_at=second.published_at,
            membership_hash="c" * 64,
            cluster_version="test",
        )
        session.add(event)
        session.flush()
        for item in (first, second):
            session.add(
                EventMember(
                    event_id=event.id,
                    content_item_id=item.id,
                    confidence=1,
                    algorithm_version="test",
                )
            )
        entity = Entity(
            entity_type="brand",
            canonical_name="Verified",
            normalized_name="verified",
        )
        session.add(entity)
        result = EntityProcessingResult(
            content_item_id=first.id,
            extractor_name="test",
            extractor_version="v1",
            input_content_hash=first.content_hash,
            config_hash="d" * 64,
            status="succeeded",
        )
        session.add(result)
        session.flush()
        session.add(
            EntityMention(
                processing_result_id=result.id,
                content_item_id=first.id,
                entity_id=entity.id,
                entity_type="brand",
                surface="Verified",
                normalized_surface="verified",
                field="title",
                start_offset=0,
                end_offset=8,
                evidence_text=first.title,
                confidence=1,
                resolution_status="resolved",
            )
        )
        session.commit()

        plan = build_value_score_plan(session, domain_key="sample", as_of=AS_OF)
        by_id = {score.content_item_id: score for score in plan.scores}
        assert by_id[first.id].component_scores["corroboration"]["normalized"] == 0.7
        assert by_id[second.id].component_scores["corroboration"]["normalized"] == 0.7
        assert by_id[first.id].component_scores["confirmed_entities"]["normalized"] == round(
            1 / 3, 6
        )
        assert by_id[second.id].component_scores["confirmed_entities"]["normalized"] == 0


def test_value_scores_api_returns_latest_run(client, session_factory):
    with session_factory() as session:
        domain = Domain(key="sample", name="Sample")
        session.add(domain)
        session.flush()
        item = _content(session, suffix="api")
        _assign(session, domain, item)
        session.commit()
        result = apply_value_score_plan(
            session, build_value_score_plan(session, domain_key="sample", as_of=AS_OF)
        )
        session.commit()

    response = client.get("/api/v1/value-scores?domain_key=sample")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["run_id"] == result.run_id
    assert payload[0]["content_item_id"] == item.id
    assert payload[0]["schema_version"] == "content-value-score.v1"
