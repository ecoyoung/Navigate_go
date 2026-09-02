from datetime import UTC, datetime

from app.auth import create_user
from app.llm_editorial import LLMResponse, LLMUsage
from app.models import InterestTopic
from app.topic_matching import compile_topic_intent
from app.topic_search_plan import build_firecrawl_search_options, compile_topic_search_plan


class FakeDeepSeek:
    provider = "deepseek"
    model = "deepseek-v4-flash"
    generation_fingerprint = {"provider": "deepseek", "model": "deepseek-v4-flash"}

    def __init__(self, intent_hash):
        self.intent_hash = intent_hash
        self.calls = 0

    def generate_json(self, *, system_prompt, user_prompt):
        self.calls += 1
        assert "topic-search-plan.v1" in system_prompt
        assert "关注中国防晒新品" in user_prompt
        return LLMResponse(
            {
                "schema_version": "topic-search-plan.v1",
                "topic_id": 1,
                "topic_intent_hash": self.intent_hash,
                "query": "中国 防晒 新品 sunscreen launch",
                "languages": ["zh", "en"],
                "content_geographies": ["中国"],
                "search_location": None,
                "include_domains": [],
                "exclude_domains": [],
                "categories": [],
                "safe": True,
            },
            LLMUsage(20, 10, 30),
        )


def test_plan_is_cached_and_compiled_to_bounded_web_options(session_factory):
    with session_factory() as db:
        user = create_user(
            db,
            email="plan@example.com",
            display_name="计划",
            password="Plan-password-2026",
        )
        compiled_intent, intent_hash = compile_topic_intent("关注中国防晒新品")
        topic = InterestTopic(
            user_id=user.id,
            name="防晒新品",
            intent_text="关注中国防晒新品",
            compiled_intent=compiled_intent,
            intent_hash=intent_hash,
            compiler_name="local",
            compiler_version="topic-intent.v1",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(topic)
        db.commit()
        db.refresh(topic)
        fake = FakeDeepSeek(topic.intent_hash)
        first = compile_topic_search_plan(db, topic, fake)
        db.commit()
        second = compile_topic_search_plan(db, topic, fake)

        assert first.cache_hit is False
        assert second.cache_hit is True
        assert fake.calls == 1
        assert first.usage.total_tokens == 30
        assert build_firecrawl_search_options(first.plan, initial=True) == {
            "sources": ["web"],
            "safe": True,
            "tbs": "qdr:w",
        }
        assert build_firecrawl_search_options(first.plan, initial=False)["tbs"] == "qdr:d"
