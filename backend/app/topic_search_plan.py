"""Compile a subscription into a bounded Firecrawl search plan.

The model never receives authority to construct a provider request.  It can only
choose values from this internal contract; date windows and the final
``published_at`` admission check remain program-controlled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
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
from .models import InterestTopic

PLAN_TASK = "topic_search_plan"
PLAN_SCHEMA = "topic-search-plan.v1"
PLAN_VALIDATOR = "topic-search-plan-validator.v1"

PLAN_SYSTEM_PROMPT = """你是资讯检索计划编译器。把用户订阅意图转为严格 JSON，绝不编造事实或
忽略用户排除条件。schema_version 固定为 topic-search-plan.v1。query 必须是一条精确、可搜索的
查询式，可包含中英文同义词；不要加入日期词，因为日期窗口由系统控制。languages 和
content_geographies 只表达希望阅读的文章语言和内容地域，不得声称它们是搜索引擎硬过滤。
search_location 仅用于搜索本地化，留空除非用户明确要求搜索地点。include_domains 与
exclude_domains 只能放纯域名（无协议、路径），且不能同时使用。categories 只能选 research、pdf，
没有明确需要时为空。safe 默认为 true。不要解释。
JSON 形状只能是：{"schema_version":"topic-search-plan.v1","topic_id":1,
"topic_intent_hash":"sha256","query":"","languages":[],"content_geographies":[],
"search_location":null,"include_domains":[],"exclude_domains":[],"categories":[],"safe":true}。"""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _domain(value: str) -> str:
    normalized = value.strip().lower().rstrip(".")
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", normalized):
        raise ValueError("must be a hostname without protocol or path")
    return normalized


class TopicSearchPlan(StrictModel):
    schema_version: Literal["topic-search-plan.v1"]
    topic_id: int
    topic_intent_hash: str = Field(min_length=64, max_length=64)
    query: str = Field(min_length=2, max_length=280)
    languages: list[Literal["zh", "en"]] = Field(default_factory=list, max_length=2)
    content_geographies: list[str] = Field(default_factory=list, max_length=8)
    search_location: str | None = Field(default=None, max_length=100)
    include_domains: list[str] = Field(default_factory=list, max_length=10)
    exclude_domains: list[str] = Field(default_factory=list, max_length=10)
    categories: list[Literal["research", "pdf"]] = Field(default_factory=list, max_length=2)
    safe: bool = True

    @field_validator("query")
    @classmethod
    def compact_query(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def hostnames_only(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_domain(value) for value in values))

    @model_validator(mode="after")
    def exclusive_domains(self):
        if self.include_domains and self.exclude_domains:
            raise ValueError("include_domains and exclude_domains are mutually exclusive")
        return self


@dataclass(frozen=True)
class TopicSearchPlanResult:
    plan: TopicSearchPlan
    cache_hit: bool
    usage: LLMUsage


def _source(topic: InterestTopic) -> dict:
    compiled = topic.compiled_intent or {}
    return {
        "topic_id": topic.id,
        "topic_intent_hash": topic.intent_hash,
        "intent_text": topic.intent_text,
        "positive_keywords": compiled.get("positive_keywords", []),
        "excluded_keywords": compiled.get("excluded_keywords", []),
        "query_expansions": compiled.get("query_expansions", []),
    }


def _validate(plan: TopicSearchPlan, source: dict) -> None:
    if plan.topic_id != source["topic_id"]:
        raise ValueError("topic id mismatch")
    if plan.topic_intent_hash != source["topic_intent_hash"]:
        raise ValueError("topic intent hash mismatch")
    exclusions = " ".join([plan.query, *plan.exclude_domains]).casefold()
    # Exclusions are passed separately to editorial matching.  This check only
    # prevents the model from placing a user-excluded term into the query.
    for item in source["excluded_keywords"]:
        if item and str(item).casefold() in exclusions:
            raise ValueError("excluded keyword appears in search query")


def compile_topic_search_plan(
    session: Session, topic: InterestTopic, client: DeepSeekClient
) -> TopicSearchPlanResult:
    source = _source(topic)
    fingerprint = _cache_fingerprint(
        PLAN_TASK,
        PLAN_SCHEMA,
        PLAN_VALIDATOR,
        PLAN_SYSTEM_PROMPT,
        TopicSearchPlan.model_json_schema(),
        source,
        client,
    )
    cached = _cached(session, fingerprint.cache_key)
    if cached is not None:
        plan = TopicSearchPlan.model_validate(cached.output)
        _validate(plan, source)
        return TopicSearchPlanResult(plan, True, LLMUsage(None, None, None))

    prompt = "请编译以下订阅主题：\n" + json.dumps(source, ensure_ascii=False)
    response = client.generate_json(system_prompt=PLAN_SYSTEM_PROMPT, user_prompt=prompt)
    try:
        plan = TopicSearchPlan.model_validate(response.output)
        _validate(plan, source)
    except (ValidationError, ValueError) as exc:
        repair = client.generate_json(
            system_prompt=PLAN_SYSTEM_PROMPT,
            user_prompt=(
                prompt
                + f"\n上次输出校验失败：{exc}。只修复 JSON：\n"
                + json.dumps(response.output, ensure_ascii=False)
            ),
        )
        response = LLMResponse(repair.output, _usage_sum([response.usage, repair.usage]))
        plan = TopicSearchPlan.model_validate(response.output)
        _validate(plan, source)
    _store(
        session,
        "interest_topic",
        f"topic:{topic.id}",
        PLAN_TASK,
        fingerprint,
        client,
        plan.model_dump(),
        response.usage,
    )
    session.flush()
    return TopicSearchPlanResult(plan, False, response.usage)


def build_firecrawl_search_options(plan: TopicSearchPlan, *, initial: bool) -> dict:
    """Return only supported, program-owned `/v2/search` parameters.

    `tbs` applies to Firecrawl's web source only, so we pin source selection to
    web.  It is a recall window; page parsing and the project data contract make
    the final published-date decision.
    """
    options: dict = {"sources": ["web"], "safe": plan.safe, "tbs": "qdr:w" if initial else "qdr:d"}
    if plan.search_location:
        options["location"] = plan.search_location
    if plan.include_domains:
        options["includeDomains"] = plan.include_domains
    if plan.exclude_domains:
        options["excludeDomains"] = plan.exclude_domains
    if plan.categories:
        options["categories"] = plan.categories
    return options
