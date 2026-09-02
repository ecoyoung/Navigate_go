import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.entity_extraction import process_entities
from app.llm_editorial import LLMResponse, LLMUsage
from app.llm_entity_extraction import process_llm_entity_batch
from app.models import (
    ContentItem,
    CrawlRun,
    EntityMention,
    EntityProcessingResult,
    LLMProcessingResult,
    Source,
)
from app.web_ingestion import ingest_article


class FakeEntityClient:
    model = "deepseek-v4-flash"
    provider = "deepseek"
    api_key = "fixture"
    generation_fingerprint = {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "format": "json_object",
    }

    def __init__(self, *, invalid_surface: bool = False):
        self.calls = 0
        self.invalid_surface = invalid_surface

    def generate_json(self, *, system_prompt: str, user_prompt: str) -> LLMResponse:
        assert "不得使用背景知识" in system_prompt
        self.calls += 1
        source = json.loads(user_prompt.split("\n", 1)[1])
        items = []
        for item in source["items"]:
            title = next(span for span in item["evidence"] if span["field"] == "title")
            items.append(
                {
                    "content_ref": item["content_ref"],
                    "input_content_hash": item["input_content_hash"],
                    "mentions": [
                        {
                            "surface": "Not in evidence" if self.invalid_surface else "Rabanne",
                            "entity_type": "brand",
                            "canonical_name_candidate": "Rabanne",
                            "evidence_ref": title["ref"],
                            "confidence": 0.98,
                        },
                        {
                            "surface": "Puig",
                            "entity_type": "organization",
                            "canonical_name_candidate": "Puig",
                            "evidence_ref": title["ref"],
                            "confidence": 0.9,
                        },
                    ],
                }
            )
        return LLMResponse(
            {"schema_version": "entity-candidates.v1", "items": items},
            LLMUsage(300, 100, 400),
        )


def add_content(session):
    source = Source(
        name="Entity News",
        channel_type="web",
        start_url="https://entities.example.com/",
        normalized_start_url="https://entities.example.com/",
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
            "title": "Rabanne partners with Puig in Paris",
            "canonical_url": "https://entities.example.com/story",
            "original_url": "https://entities.example.com/story",
            "author": None,
            "published_at": datetime(2026, 8, 30, tzinfo=UTC),
            "body": "The article discusses a documented partnership.",
            "description": "Rabanne and Puig announced a partnership.",
            "content_type": "article",
            "topics": [],
        },
    )
    return session.scalar(select(ContentItem))


def seed_rabanne(session, content):
    policy = {
        "schema_version": "entity-extraction-policy.v1",
        "policy_key": "test.v1",
        "entities": [
            {
                "registry_key": "brand:rabanne",
                "entity_type": "brand",
                "canonical_name": "Rabanne",
                "language": "en",
                "aliases": [],
            }
        ],
    }
    process_entities(session, [content], policy)


def test_llm_candidates_are_grounded_resolved_and_cached(session_factory):
    with session_factory() as session:
        content = add_content(session)
        seed_rabanne(session, content)
        client = FakeEntityClient()
        first = process_llm_entity_batch(session, [content], client)
        repeated = process_llm_entity_batch(session, [content], client)

        assert first.processed == 1
        assert first.mentions == 2
        assert first.resolved == 1
        assert first.unresolved == 1
        assert first.usage.total_tokens == 400
        assert repeated.processed == 0
        assert repeated.skipped == 1
        assert repeated.cache_hit is True
        assert client.calls == 1
        assert session.scalar(select(func.count(LLMProcessingResult.id))) == 1
        assert (
            session.scalar(
                select(func.count(EntityProcessingResult.id)).where(
                    EntityProcessingResult.extractor_name == "llm_entity_candidates"
                )
            )
            == 1
        )
        mentions = list(
            session.scalars(
                select(EntityMention)
                .where(EntityMention.extraction_method == "llm_candidate")
                .order_by(EntityMention.id)
            )
        )
        rabanne = next(item for item in mentions if item.surface == "Rabanne")
        puig = next(item for item in mentions if item.surface == "Puig")
        assert content.title[rabanne.start_offset : rabanne.end_offset] == "Rabanne"
        assert rabanne.entity_id is not None
        assert puig.entity_id is None
        assert puig.resolution_status == "unresolved"


def test_llm_candidate_not_in_evidence_is_rejected(session_factory):
    with session_factory() as session:
        content = add_content(session)
        client = FakeEntityClient(invalid_surface=True)
        with pytest.raises(ValueError, match="not an exact evidence substring"):
            process_llm_entity_batch(session, [content], client)
        session.rollback()
        assert session.scalar(select(func.count(LLMProcessingResult.id))) == 0


def test_llm_batch_is_limited_to_five(session_factory):
    with session_factory() as session:
        content = add_content(session)
        with pytest.raises(ValueError, match="1-5"):
            process_llm_entity_batch(session, [content] * 0, FakeEntityClient())
