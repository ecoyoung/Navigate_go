import json

from sqlalchemy import func, select

from app.auth import create_user
from app.llm_editorial import LLMResponse, LLMUsage
from app.models import (
    ContentItem,
    CrawlRun,
    InterestTopic,
    LLMProcessingResult,
    RawItem,
    Source,
    TopicMatch,
)
from app.topic_intelligence import (
    _normalize_content_output,
    _normalize_intent_output,
    run_topic_intelligence,
)
from app.topic_matching import MATCHER_VERSION


class FakeDeepSeekClient:
    provider = "deepseek"
    model = "deepseek-v4-flash"

    def __init__(self):
        self.calls = 0

    @property
    def generation_fingerprint(self):
        return {
            "provider": self.provider,
            "model": self.model,
            "test": "topic-intelligence",
        }

    def generate_json(self, *, system_prompt, user_prompt):
        self.calls += 1
        payload = json.loads(user_prompt.split("\n", 1)[1])
        if "source_intent_hash" in payload:
            output = {
                "schema_version": "topic-intent.llm.v1",
                "topic_id": payload["topic_id"],
                "source_intent_hash": payload["source_intent_hash"],
                "industries": ["宠物行业"],
                "products": ["智能宠物设备"],
                "entities": [],
                "event_types": ["出海"],
                "geographies": ["海外"],
                "positive_keywords": ["宠物电子产品", "出海"],
                "excluded_keywords": payload["user_excluded_keywords"],
                "query_expansions": ["smart pet devices export"],
            }
        else:
            items = []
            for index, item in enumerate(payload["items"]):
                relevant = index == 0
                items.append(
                    {
                        "content_ref": item["content_ref"],
                        "input_content_hash": item["input_content_hash"],
                        "relevant": relevant,
                        "relevance_score": 0.93 if relevant else 0.08,
                        "reason_zh": "核心内容符合主题" if relevant else "核心内容不属于主题",
                        "chinese_title": "智能宠物设备加速出海" if relevant else "普通家居产品资讯",
                        "chinese_summary": (
                            "文章介绍智能宠物设备进入海外市场。"
                            if relevant
                            else "文章主要介绍普通家居用品。"
                        ),
                        "tags_zh": ["宠物科技", "品牌出海"] if relevant else [],
                        "event_type_zh": "品牌出海" if relevant else None,
                        "entities": ["测试品牌"] if relevant else [],
                        "evidence_quote": item["title"],
                    }
                )
            output = {
                "schema_version": "topic-content-editorial.v1",
                "topic_id": payload["topic_intent"]["topic_id"],
                "topic_intent_hash": payload["topic_intent_hash"],
                "items": items,
            }
        return LLMResponse(output, LLMUsage(100, 20, 120))


def _seed_topic(session_factory):
    with session_factory() as db:
        user = create_user(
            db,
            email="topic-llm@example.com",
            display_name="主题编辑",
            password="Topic-LLM-2026!",
            role="admin",
        )
        topic = InterestTopic(
            user_id=user.id,
            name="中国宠物电子产品出海",
            intent_text="关注中国宠物电子产品出海，排除宠物食品",
            compiled_intent={
                "positive_keywords": ["宠物电子产品", "出海"],
                "excluded_keywords": ["宠物食品"],
            },
            intent_hash="1" * 64,
        )
        source = Source(
            catalog_id="topic_llm_source",
            name="测试行业媒体",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add_all([topic, source])
        db.flush()
        crawl = CrawlRun(source_id=source.id, status="succeeded")
        db.add(crawl)
        db.flush()
        contents = []
        for index, title in enumerate(
            ["Smart pet devices expand overseas", "Home furniture product update"]
        ):
            raw = RawItem(
                source_id=source.id,
                crawl_run_id=crawl.id,
                identity_key=str(index + 2) * 64,
                original_url=f"https://example.com/{index}",
                canonical_url=f"https://example.com/{index}",
                payload={"title": title},
                payload_sha256=str(index + 4) * 64,
            )
            db.add(raw)
            db.flush()
            content = ContentItem(
                source_id=source.id,
                raw_item_id=raw.id,
                identity_key=raw.identity_key,
                title=title,
                canonical_url=raw.canonical_url,
                excerpt="Evidence excerpt",
                body="Evidence body",
                language="en",
                content_hash=str(index + 6) * 64,
            )
            db.add(content)
            db.flush()
            db.add(
                TopicMatch(
                    topic_id=topic.id,
                    content_item_id=content.id,
                    matcher_version=MATCHER_VERSION,
                    input_content_hash=content.content_hash,
                    decision="include",
                    score=0.5,
                )
            )
            contents.append(content)
        db.commit()
        return topic.id, [content.id for content in contents]


def test_topic_intelligence_is_batched_persistent_and_cached(session_factory):
    topic_id, content_ids = _seed_topic(session_factory)
    first_client = FakeDeepSeekClient()
    with session_factory() as db:
        topic = db.get(InterestTopic, topic_id)
        articles = list(
            db.execute(
                select(ContentItem, Source)
                .join(Source, Source.id == ContentItem.source_id)
                .where(ContentItem.id.in_(content_ids))
            )
        )
        first = run_topic_intelligence(db, topic, articles, first_client)

    assert first_client.calls == 2
    assert first.processed == 2
    assert first.included == 1
    assert first.excluded == 1
    assert first.usage.total_tokens == 240

    second_client = FakeDeepSeekClient()
    with session_factory() as db:
        topic = db.get(InterestTopic, topic_id)
        articles = list(
            db.execute(
                select(ContentItem, Source)
                .join(Source, Source.id == ContentItem.source_id)
                .where(ContentItem.id.in_(content_ids))
            )
        )
        second = run_topic_intelligence(db, topic, articles, second_client)
        matches = list(
            db.scalars(select(TopicMatch).order_by(TopicMatch.content_item_id))
        )
        item_artifacts = list(
            db.scalars(
                select(LLMProcessingResult).where(
                    LLMProcessingResult.subject_type == "topic_content"
                )
            )
        )
        assert topic.compiler_version == "topic-intent.llm.v1"
        assert topic.compiled_intent["excluded_keywords"] == ["宠物食品"]
        assert [match.decision for match in matches] == ["include", "exclude"]
        assert [match.score for match in matches] == [0.93, 0.08]
        assert len(item_artifacts) == 2
        assert item_artifacts[0].output["tags_zh"] == ["宠物科技", "品牌出海"]
        assert db.scalar(select(func.sum(LLMProcessingResult.total_tokens))) == 240

    assert second_client.calls == 0
    assert second.intent_cache_hit is True
    assert second.content_cache_hit is True
    assert second.usage.total_tokens is None


def test_topic_intent_provider_aliases_are_normalized_without_semantic_changes():
    normalized = _normalize_intent_output(
        {
            "name": "显示名不属于契约",
            "regions": ["中国", "海外"],
            "search_extensions": ["smart pet export"],
            "positive_keywords": ["宠物科技"],
        }
    )

    assert normalized == {
        "geographies": ["中国", "海外"],
        "query_expansions": ["smart pet export"],
        "positive_keywords": ["宠物科技"],
    }


def test_topic_content_collection_alias_is_normalized_without_editing_items():
    contents = [
        {
            "content_ref": "content:1",
            "relevant": True,
            "title": "中文标题",
            "summary": "中文摘要内容",
            "tags": ["宠物科技"],
            "event_type": "品牌出海",
        }
    ]

    assert _normalize_content_output(
        {"schema_version": "v1", "contents": contents}, "a" * 64
    ) == {
        "schema_version": "v1",
        "topic_intent_hash": "a" * 64,
        "items": [
            {
                "content_ref": "content:1",
                "relevant": True,
                "relevance_score": 0.85,
                "reason_zh": "文章核心内容与订阅主题相关",
                "chinese_title": "中文标题",
                "chinese_summary": "中文摘要内容",
                "tags_zh": ["宠物科技"],
                "event_type_zh": "品牌出海",
            }
        ],
    }


def test_topic_content_normalizer_repairs_identity_and_boolean_from_score():
    normalized = _normalize_content_output(
        {
            "schema_version": "topic-content-editorial.v1",
            "topic_id": "wrong-hash",
            "topic_intent_hash": "wrong-hash",
            "items": [{"content_ref": "content:1", "relevance_score": 0.72}],
        },
        "a" * 64,
        7,
    )

    assert normalized["topic_id"] == 7
    assert normalized["topic_intent_hash"] == "a" * 64
    assert normalized["items"][0]["relevant"] is True
