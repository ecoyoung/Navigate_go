from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.entity_extraction import (
    load_entity_policy,
    normalize_entity_name,
    process_entities,
)
from app.models import (
    ContentItem,
    CrawlRun,
    Entity,
    EntityMention,
    EntityProcessingResult,
    EntityResolutionCandidate,
    Source,
)
from app.web_ingestion import ingest_article


def policy() -> dict:
    return {
        "schema_version": "entity-extraction-policy.v1",
        "policy_key": "test.v1",
        "entities": [
            {
                "registry_key": "brand:rabanne",
                "entity_type": "brand",
                "canonical_name": "Rabanne",
                "language": "en",
                "aliases": [],
            },
            {
                "registry_key": "organization:acme_holdings",
                "entity_type": "organization",
                "canonical_name": "Acme Holdings",
                "language": "en",
                "aliases": ["Acme"],
            },
            {
                "registry_key": "brand:acme",
                "entity_type": "brand",
                "canonical_name": "Acme Brand",
                "language": "en",
                "aliases": ["Acme"],
            },
        ],
    }


def add_content(session):
    source = Source(
        name="General News",
        channel_type="web",
        start_url="https://example.com/",
        normalized_start_url="https://example.com/",
        parser_config={},
        processing_config={"scope_mode": "dedicated"},
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
            "title": "Rabanne partners with Acme",
            "canonical_url": "https://example.com/story",
            "original_url": "https://example.com/story",
            "author": None,
            "published_at": datetime(2026, 8, 30, tzinfo=UTC),
            "body": "A public article without any additional configured aliases. " * 5,
            "description": "A partnership announcement.",
            "content_type": "article",
            "topics": [],
        },
    )
    return session.scalar(select(ContentItem))


def test_deterministic_mentions_keep_evidence_and_ambiguity(session_factory):
    with session_factory() as session:
        content = add_content(session)
        first = process_entities(session, [content], policy())
        second = process_entities(session, [content], policy())
        session.commit()

        assert first.processed == 1
        assert first.candidates == 2
        assert first.resolved == 1
        assert first.unresolved == 1
        assert second.processed == 0
        assert second.skipped == 1
        assert session.scalar(select(func.count(EntityProcessingResult.id))) == 1

        mentions = list(session.scalars(select(EntityMention).order_by(EntityMention.id)))
        resolved = next(item for item in mentions if item.surface == "Rabanne")
        ambiguous = next(item for item in mentions if item.surface == "Acme")
        assert content.title[resolved.start_offset : resolved.end_offset] == resolved.surface
        assert resolved.entity_type == "brand"
        assert resolved.resolution_status == "resolved"
        assert ambiguous.entity_id is None
        assert ambiguous.entity_type == "unknown"
        assert ambiguous.resolution_status == "ambiguous"
        assert "Acme" in ambiguous.evidence_text
        assert (
            session.scalar(
                select(func.count(EntityResolutionCandidate.id)).where(
                    EntityResolutionCandidate.mention_id == ambiguous.id
                )
            )
            == 2
        )


def test_entity_api_and_content_filter_use_current_result(client, session_factory):
    with session_factory() as session:
        content = add_content(session)
        process_entities(session, [content], policy())
        entity = session.scalar(select(Entity).where(Entity.registry_key == "brand:rabanne"))
        session.commit()
        entity_id = entity.id
        content_id = content.id

    listed = client.get("/api/v1/entities?entity_type=brand&q=RABANNE")
    assert listed.status_code == 200
    assert listed.json()[0]["canonical_name"] == "Rabanne"
    assert listed.json()[0]["mention_count"] == 1

    detail = client.get(f"/api/v1/entities/{entity_id}")
    assert detail.status_code == 200
    assert detail.json()["aliases"][0]["alias"] == "Rabanne"
    assert detail.json()["mentions"][0]["content_item_id"] == content_id
    assert detail.json()["mentions"][0]["evidence_text"]

    filtered = client.get(f"/api/v1/content-items?entity_id={entity_id}")
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()] == [content_id]
    assert client.get("/api/v1/entities/99999").status_code == 404

    with session_factory() as session:
        content = session.get(ContentItem, content_id)
        content.content_hash = "f" * 64
        session.commit()
    assert client.get(f"/api/v1/content-items?entity_id={entity_id}").json() == []


def test_policy_validation_and_name_normalization(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        '{"schema_version":"entity-extraction-policy.v1","entities":'
        '[{"registry_key":"bad","entity_type":"topic","canonical_name":"Topic"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported entity type"):
        load_entity_policy(invalid)
    assert normalize_entity_name(" L’Oréal 集团 ") == "loréal集团"
