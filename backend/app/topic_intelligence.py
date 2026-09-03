from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .llm_editorial import (
    DeepSeekClient,
    LLMResponse,
    LLMUsage,
    _cache_fingerprint,
    _cached,
    _store,
    _usage_sum,
)
from .models import (
    ContentItem,
    InterestTopic,
    LLMProcessingResult,
    Source,
    TopicMatch,
    TopicRun,
)
from .topic_matching import MATCHER_VERSION, compile_topic_intent

INTENT_TASK = "topic_intent_llm"
INTENT_SCHEMA = "topic-intent.llm.v1"
INTENT_VALIDATOR = "topic-intent-validator.v1"
CONTENT_TASK = "topic_content_editorial"
CONTENT_SCHEMA = "topic-content-editorial.v1"
CONTENT_VALIDATOR = "topic-content-editorial-validator.v1"

INTENT_SYSTEM_PROMPT = """你是资讯订阅主题分析器。只分析用户输入，不扩展成无关行业。
输出严格 JSON，schema_version 固定为 topic-intent.llm.v1。
把自然语言拆为行业、产品、实体、事件类型、地区、正向关键词、排除关键词和中英搜索扩展。
positive_keywords 与 query_expansions 必须同时覆盖中文和英文等价说法，
包括常见缩写、连字符与空格变体，但不要扩到无关行业。
excluded_keywords 也要中英对照；用户明确排除项不得删除。不要解释。
JSON 形状只能是：{"schema_version":"topic-intent.llm.v1","topic_id":1,
"source_intent_hash":"sha256","industries":[],"products":[],"entities":[],
"event_types":[],"geographies":[],"positive_keywords":[],"excluded_keywords":[],
"query_expansions":[]}。不得增加 name、regions 或 search_extensions 等字段。"""

CONTENT_SYSTEM_PROMPT = """你是面向中文读者的个性化资讯编辑。
只依据 topic_intent 和每篇 evidence 输出严格 JSON，schema_version 固定为
topic-content-editorial.v1。每个 content_ref 必须按输入顺序出现一次，不得遗漏、新增或改写
input_content_hash。判断文章核心内容是否真正符合主题；搜索命中不等于相关。用户排除条件优先。
为相关内容生成忠实的中文标题、中文摘要、中文标签、事件类型和实体；不得补充 evidence 中没有的
事实、数字、因果或评价。evidence_quote 必须逐字复制自该篇 title、excerpt 或 body_lead。
即使文章不相关，也要给出中文标题和简短中文理由。不要解释。
顶层必须使用 items；每项必须使用 relevance_score、reason_zh、chinese_title、
chinese_summary、tags_zh、event_type_zh，禁止使用 title、summary、tags、event_type 简写。"""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TopicIntentLLM(StrictModel):
    schema_version: Literal["topic-intent.llm.v1"]
    topic_id: int
    source_intent_hash: str = Field(min_length=64, max_length=64)
    industries: list[str] = Field(default_factory=list, max_length=8)
    products: list[str] = Field(default_factory=list, max_length=16)
    entities: list[str] = Field(default_factory=list, max_length=16)
    event_types: list[str] = Field(default_factory=list, max_length=16)
    geographies: list[str] = Field(default_factory=list, max_length=16)
    positive_keywords: list[str] = Field(min_length=1, max_length=24)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=24)
    query_expansions: list[str] = Field(default_factory=list, max_length=12)


class TopicContentDecision(StrictModel):
    content_ref: str = Field(pattern=r"^content:\d+$")
    input_content_hash: str = Field(min_length=64, max_length=64)
    relevant: bool
    relevance_score: float = Field(ge=0, le=1)
    reason_zh: str = Field(min_length=4, max_length=160)
    chinese_title: str = Field(min_length=4, max_length=140)
    chinese_summary: str = Field(min_length=8, max_length=300)
    tags_zh: list[str] = Field(default_factory=list, max_length=8)
    event_type_zh: str | None = Field(default=None, max_length=40)
    entities: list[str] = Field(default_factory=list, max_length=12)
    evidence_quote: str = Field(min_length=2, max_length=300)

    @field_validator("chinese_title", "chinese_summary", "reason_zh")
    @classmethod
    def require_chinese(cls, value: str) -> str:
        if not re.search(r"[\u3400-\u9fff]", value):
            raise ValueError("Chinese editorial fields must contain Chinese text")
        return value


class TopicContentBatch(StrictModel):
    schema_version: Literal["topic-content-editorial.v1"]
    topic_id: int
    topic_intent_hash: str = Field(min_length=64, max_length=64)
    items: list[TopicContentDecision] = Field(min_length=1, max_length=12)


@dataclass(frozen=True)
class TopicIntelligenceResult:
    intent_cache_hit: bool
    content_cache_hit: bool
    processed: int
    included: int
    excluded: int
    usage: LLMUsage


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_intent_output(value: dict) -> dict:
    """Normalize known provider synonyms without changing semantic values."""
    result = dict(value)
    aliases = {
        "regions": "geographies",
        "search_extensions": "query_expansions",
    }
    for provider_key, contract_key in aliases.items():
        if contract_key not in result and provider_key in result:
            result[contract_key] = result[provider_key]
        result.pop(provider_key, None)
    result.pop("name", None)
    return result


def _normalize_content_output(
    value: dict, topic_intent_hash: str, topic_id: int | None = None
) -> dict:
    """Normalize provider field synonyms and project boolean relevance to a score."""
    result = dict(value)
    if "items" not in result and "contents" in result:
        result["items"] = result["contents"]
    result.pop("contents", None)
    result["topic_intent_hash"] = topic_intent_hash
    if topic_id is not None:
        result["topic_id"] = topic_id
    aliases = {
        "title": "chinese_title",
        "summary": "chinese_summary",
        "tags": "tags_zh",
        "event_type": "event_type_zh",
        "reason": "reason_zh",
        "score": "relevance_score",
        "is_relevant": "relevant",
    }
    normalized_items = []
    for original in result.get("items", []):
        item = dict(original)
        for provider_key, contract_key in aliases.items():
            if contract_key not in item and provider_key in item:
                item[contract_key] = item[provider_key]
            item.pop(provider_key, None)
        if "relevance_score" not in item and isinstance(item.get("relevant"), bool):
            item["relevance_score"] = 0.85 if item["relevant"] else 0.15
        if "relevant" not in item and isinstance(item.get("relevance_score"), (int, float)):
            item["relevant"] = item["relevance_score"] >= 0.5
        if "reason_zh" not in item and isinstance(item.get("relevant"), bool):
            item["reason_zh"] = (
                "文章核心内容与订阅主题相关"
                if item["relevant"]
                else "文章核心内容与订阅主题不相关"
            )
        normalized_items.append(item)
    result["items"] = normalized_items
    return result


def _intent_source(topic: InterestTopic) -> dict:
    current = topic.compiled_intent or {}
    local, _hash = compile_topic_intent(topic.intent_text or topic.name or "主题")
    user_positive = list(current.get("user_positive_keywords") or [])
    user_excluded = list(local.get("excluded_keywords") or [])
    return {
        "topic_id": topic.id,
        "name": topic.name,
        "intent_text": topic.intent_text,
        "source_intent_hash": _stable_hash(
            {
                "intent_text": topic.intent_text,
                "user_positive_keywords": user_positive,
                "user_excluded_keywords": user_excluded,
            }
        ),
        "user_positive_keywords": user_positive,
        "user_excluded_keywords": user_excluded,
    }


def _validate_intent(output: TopicIntentLLM, source: dict) -> None:
    if output.topic_id != source["topic_id"]:
        raise ValueError("topic id mismatch")
    if output.source_intent_hash != source["source_intent_hash"]:
        raise ValueError("source intent hash mismatch")
    required_exclusions = {
        str(item).strip().casefold() for item in source["user_excluded_keywords"] if item
    }
    output_exclusions = {item.strip().casefold() for item in output.excluded_keywords}
    if not required_exclusions.issubset(output_exclusions):
        raise ValueError("user exclusions were removed")


def compile_topic_with_llm(
    session: Session,
    topic: InterestTopic,
    client: DeepSeekClient,
) -> tuple[TopicIntentLLM, bool, LLMUsage]:
    source = _intent_source(topic)
    fingerprint = _cache_fingerprint(
        INTENT_TASK,
        INTENT_SCHEMA,
        INTENT_VALIDATOR,
        INTENT_SYSTEM_PROMPT,
        TopicIntentLLM.model_json_schema(),
        source,
        client,
    )
    cached = _cached(session, fingerprint.cache_key)
    if cached is not None:
        output = TopicIntentLLM.model_validate(cached.output)
        _validate_intent(output, source)
        usage = LLMUsage(None, None, None)
        cache_hit = True
    else:
        prompt = "请编译以下订阅主题：\n" + json.dumps(source, ensure_ascii=False)
        response = client.generate_json(
            system_prompt=INTENT_SYSTEM_PROMPT,
            user_prompt=prompt,
        )
        try:
            output = TopicIntentLLM.model_validate(
                _normalize_intent_output(response.output)
            )
            _validate_intent(output, source)
        except (ValidationError, ValueError) as exc:
            repair = client.generate_json(
                system_prompt=INTENT_SYSTEM_PROMPT,
                user_prompt=(
                    prompt
                    + f"\n上次输出校验失败：{exc}。只修复 JSON：\n"
                    + json.dumps(response.output, ensure_ascii=False)
                ),
            )
            response = LLMResponse(
                repair.output,
                _usage_sum([response.usage, repair.usage]),
            )
            output = TopicIntentLLM.model_validate(
                _normalize_intent_output(response.output)
            )
            _validate_intent(output, source)
        usage = response.usage
        cache_hit = False
        _store(
            session,
            "interest_topic",
            f"topic:{topic.id}",
            INTENT_TASK,
            fingerprint,
            client,
            output.model_dump(),
            usage,
        )
    topic.compiled_intent = {
        **output.model_dump(),
        "user_positive_keywords": source["user_positive_keywords"],
        "user_excluded_keywords": source["user_excluded_keywords"],
        "original_language": "zh-CN",
    }
    topic.compiler_name = "deepseek_topic_compiler"
    topic.compiler_version = INTENT_SCHEMA
    topic.intent_hash = _stable_hash(output.model_dump())
    topic.updated_at = datetime.now(UTC)
    session.commit()
    return output, cache_hit, usage


def _content_input(content: ContentItem, source: Source) -> dict:
    return {
        "content_ref": f"content:{content.id}",
        "input_content_hash": content.content_hash,
        "source_name": source.name,
        "language": content.language,
        "title": (content.title or "")[:500],
        "excerpt": (content.excerpt or "")[:1200],
        "body_lead": (content.body or "")[:2400],
        "metadata_only": bool((content.quality or {}).get("metadata_only")),
    }


def _validate_content_batch(
    batch: TopicContentBatch,
    topic: InterestTopic,
    inputs: list[dict],
) -> None:
    if batch.topic_id != topic.id or batch.topic_intent_hash != topic.intent_hash:
        raise ValueError("topic identity mismatch")
    if [item.content_ref for item in batch.items] != [
        item["content_ref"] for item in inputs
    ]:
        raise ValueError("content refs do not match input order")
    by_ref = {item["content_ref"]: item for item in inputs}
    for decision in batch.items:
        source = by_ref[decision.content_ref]
        if decision.input_content_hash != source["input_content_hash"]:
            raise ValueError(f"content hash mismatch for {decision.content_ref}")
        evidence = "\n".join(
            [source["title"], source["excerpt"], source["body_lead"]]
        )
        if decision.evidence_quote not in evidence:
            raise ValueError(f"evidence quote mismatch for {decision.content_ref}")


def _store_topic_item(
    session: Session,
    *,
    topic: InterestTopic,
    decision: TopicContentDecision,
    batch_fingerprint,
    client: DeepSeekClient,
) -> None:
    cache_key = _stable_hash(
        {
            "batch_cache_key": batch_fingerprint.cache_key,
            "content_ref": decision.content_ref,
        }
    )
    row = session.scalar(
        select(LLMProcessingResult).where(LLMProcessingResult.cache_key == cache_key)
    )
    if row is None:
        row = LLMProcessingResult(
            subject_type="topic_content",
            subject_key=f"topic:{topic.id}:{decision.content_ref}",
            task_name=CONTENT_TASK,
            task_version=CONTENT_SCHEMA,
            input_hash=decision.input_content_hash,
            provider=client.provider,
            model=client.model,
            cache_key=cache_key,
        )
    row.prompt_hash = batch_fingerprint.prompt_hash
    row.schema_version = CONTENT_SCHEMA
    row.schema_hash = batch_fingerprint.schema_hash
    row.validator_version = CONTENT_VALIDATOR
    row.status = "succeeded"
    row.output = decision.model_dump()
    row.error_text = None
    row.prompt_tokens = None
    row.completion_tokens = None
    row.total_tokens = None
    session.add(row)


def process_topic_contents(
    session: Session,
    topic: InterestTopic,
    articles: list[tuple[ContentItem, Source]],
    client: DeepSeekClient,
) -> tuple[TopicContentBatch, bool, LLMUsage]:
    ordered = sorted(
        {content.id: (content, source) for content, source in articles}.values(),
        key=lambda pair: pair[0].id,
    )
    if not ordered or len(ordered) > 12:
        raise ValueError("topic content batch must contain 1-12 unique items")
    inputs = [_content_input(content, source) for content, source in ordered]
    source_payload = {
        "topic_intent": topic.compiled_intent,
        "topic_intent_hash": topic.intent_hash,
        "items": inputs,
    }
    fingerprint = _cache_fingerprint(
        CONTENT_TASK,
        CONTENT_SCHEMA,
        CONTENT_VALIDATOR,
        CONTENT_SYSTEM_PROMPT,
        TopicContentBatch.model_json_schema(),
        source_payload,
        client,
    )
    cached = _cached(session, fingerprint.cache_key)
    if cached is not None:
        batch = TopicContentBatch.model_validate(cached.output)
        _validate_content_batch(batch, topic, inputs)
        usage = LLMUsage(None, None, None)
        cache_hit = True
    else:
        prompt = "请统一比较并加工以下主题文章：\n" + json.dumps(
            source_payload, ensure_ascii=False
        )
        response = client.generate_json(
            system_prompt=CONTENT_SYSTEM_PROMPT,
            user_prompt=prompt,
        )
        try:
            batch = TopicContentBatch.model_validate(
                _normalize_content_output(response.output, topic.intent_hash, topic.id)
            )
            _validate_content_batch(batch, topic, inputs)
        except (ValidationError, ValueError) as exc:
            repair = client.generate_json(
                system_prompt=CONTENT_SYSTEM_PROMPT,
                user_prompt=(
                    prompt
                    + f"\n上次输出校验失败：{exc}。只修复完整 JSON：\n"
                    + json.dumps(response.output, ensure_ascii=False)
                ),
            )
            response = LLMResponse(
                repair.output,
                _usage_sum([response.usage, repair.usage]),
            )
            batch = TopicContentBatch.model_validate(
                _normalize_content_output(response.output, topic.intent_hash, topic.id)
            )
            _validate_content_batch(batch, topic, inputs)
        usage = response.usage
        cache_hit = False
        _store(
            session,
            "topic_content_batch",
            f"topic:{topic.id}:{fingerprint.input_hash[:20]}",
            CONTENT_TASK,
            fingerprint,
            client,
            batch.model_dump(),
            usage,
        )
    content_by_ref = {f"content:{content.id}": content for content, _ in ordered}
    for decision in batch.items:
        content = content_by_ref[decision.content_ref]
        match = session.scalar(
            select(TopicMatch).where(
                TopicMatch.topic_id == topic.id,
                TopicMatch.content_item_id == content.id,
                TopicMatch.matcher_version == MATCHER_VERSION,
            )
        )
        if match is None:
            match = TopicMatch(
                topic_id=topic.id,
                content_item_id=content.id,
                matcher_version=MATCHER_VERSION,
                input_content_hash=content.content_hash,
                decision="include" if decision.relevant else "exclude",
                score=decision.relevance_score,
            )
            session.add(match)
        collection_window = (match.matched_signals or {}).get("collection_window")
        admitted_by_time = not collection_window or bool(collection_window.get("admitted"))
        match.input_content_hash = content.content_hash
        match.decision = "include" if decision.relevant and admitted_by_time else "exclude"
        match.score = decision.relevance_score if admitted_by_time else 0.0
        match.reasons = ["llm_topic_relevance", decision.reason_zh]
        if not admitted_by_time:
            match.reasons.append("outside_collection_window")
        match.matched_signals = {
            **(match.matched_signals or {}),
            "llm_topic": {
                "schema_version": CONTENT_SCHEMA,
                "intent_hash": topic.intent_hash,
                "relevant": decision.relevant,
                "event_type": decision.event_type_zh,
                "tags": decision.tags_zh,
                "entities": decision.entities,
            },
        }
        match.matched_at = datetime.now(UTC)
        _store_topic_item(
            session,
            topic=topic,
            decision=decision,
            batch_fingerprint=fingerprint,
            client=client,
        )
    session.commit()
    return batch, cache_hit, usage


def run_topic_intelligence(
    session: Session,
    topic: InterestTopic,
    articles: list[tuple[ContentItem, Source]],
    client: DeepSeekClient,
) -> TopicIntelligenceResult:
    _, intent_cache_hit, intent_usage = compile_topic_with_llm(session, topic, client)
    batch, content_cache_hit, content_usage = process_topic_contents(
        session, topic, articles, client
    )
    usage = _usage_sum([intent_usage, content_usage])
    run = TopicRun(
        topic_id=topic.id,
        stage="llm_topic_intelligence",
        status="succeeded",
        pool_candidates=len(articles),
        matched_items=sum(item.relevant for item in batch.items),
        llm_tokens_used=int(usage.total_tokens or 0),
        output={
            "intent_cache_hit": intent_cache_hit,
            "content_cache_hit": content_cache_hit,
            "included": sum(item.relevant for item in batch.items),
            "excluded": sum(not item.relevant for item in batch.items),
            "schema_version": CONTENT_SCHEMA,
        },
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    return TopicIntelligenceResult(
        intent_cache_hit=intent_cache_hit,
        content_cache_hit=content_cache_hit,
        processed=len(batch.items),
        included=sum(item.relevant for item in batch.items),
        excluded=sum(not item.relevant for item in batch.items),
        usage=usage,
    )
