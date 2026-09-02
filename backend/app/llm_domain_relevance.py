from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
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
from .models import ContentItem, ContentProcessingResult, Source, utcnow

TASK_NAME = "domain_relevance_llm"
SCHEMA_VERSION = "domain-relevance.llm.v1"
VALIDATOR_VERSION = "domain-relevance-validator.v2"
LLM_REVIEW_REASONS = frozenset({"needs_llm_domain_review", "dedicated_domain_source"})
SYSTEM_PROMPT = (
    "你是行业资讯准入编辑。判断每篇文章的核心主题是否属于输入 domain，而不是判断正文是否"
    "偶然出现领域词。媒体简介、页脚、推荐阅读、行业列表、广告和顺带举例都不能作为准入证据。"
    "只能依据每篇 evidence 判断。按输入顺序逐篇输出一次，不得遗漏或新增。decision 只能为 "
    "include 或 exclude；reason_zh 用一句中文说明核心主题；evidence_quote 必须逐字摘自该篇 "
    "evidence，优先直接复制 title；不要改写标点、空格、大小写或词语。输出 JSON："
    '{"schema_version":"domain-relevance.llm.v1","items":['
    '{"content_ref":"content:id","input_content_hash":"sha256",'
    '"decision":"include|exclude","confidence":0.0,"reason_zh":"中文理由",'
    '"evidence_quote":"原文短句"}]}'
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DomainLLMDecision(StrictModel):
    content_ref: str = Field(pattern=r"^content:\d+$")
    input_content_hash: str = Field(min_length=64, max_length=64)
    decision: Literal["include", "exclude"]
    confidence: float = Field(ge=0, le=1)
    reason_zh: str = Field(min_length=4, max_length=120)
    evidence_quote: str = Field(min_length=2, max_length=240)


class DomainLLMBatch(StrictModel):
    schema_version: Literal["domain-relevance.llm.v1"]
    items: list[DomainLLMDecision] = Field(min_length=1)


@dataclass(frozen=True)
class DomainLLMRunResult:
    processed: int
    included: int
    excluded: int
    cache_hits: int
    usage: LLMUsage


def _evidence(content: ContentItem, source: Source) -> dict:
    excerpt = (content.excerpt or "").strip()[:900]
    body = (content.body or "").strip()[:1600]
    return {
        "content_ref": f"content:{content.id}",
        "input_content_hash": content.content_hash,
        "source_name": source.name,
        "title": content.title,
        "excerpt": excerpt,
        "body_lead": body,
        "topics": [str(item) for item in (content.topics or [])[:8]],
    }


def _validate(batch: DomainLLMBatch, evidence: list[dict]) -> None:
    if [item.content_ref for item in batch.items] != [item["content_ref"] for item in evidence]:
        raise ValueError("content refs do not exactly match input order")
    by_ref = {item["content_ref"]: item for item in evidence}
    for decision in batch.items:
        source = by_ref[decision.content_ref]
        if decision.input_content_hash != source["input_content_hash"]:
            raise ValueError(f"content hash mismatch for {decision.content_ref}")
        searchable = " ".join(
            [source["title"], source["excerpt"], source["body_lead"], *source["topics"]]
        )
        if _normalize_evidence(decision.evidence_quote) not in _normalize_evidence(searchable):
            raise ValueError(f"evidence quote is not verbatim for {decision.content_ref}")


def _normalize_evidence(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    normalized = normalized.translate(
        str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-"})
    )
    return re.sub(r"\s+", "", normalized).casefold()


def _save_projection(
    session: Session,
    *,
    content: ContentItem,
    decision: DomainLLMDecision,
    policy: dict,
    artifact_key: str,
    reason_prefix: str = "llm_domain",
) -> None:
    processor_name = str(policy["llm_classifier_name"])
    processor_version = str(policy["llm_classifier_version"])
    row = session.scalar(
        select(ContentProcessingResult).where(
            ContentProcessingResult.content_item_id == content.id,
            ContentProcessingResult.processor_name == processor_name,
            ContentProcessingResult.processor_version == processor_version,
        )
    )
    values = {
        "input_content_hash": content.content_hash,
        "is_relevant": decision.decision == "include",
        "matched_topics": [],
        "matched_events": [],
        "reason": f"{reason_prefix}_{decision.decision}:{artifact_key[:12]}",
        "processed_at": utcnow(),
    }
    if row is None:
        session.add(
            ContentProcessingResult(
                content_item_id=content.id,
                processor_name=processor_name,
                processor_version=processor_version,
                **values,
            )
        )
    else:
        for field, value in values.items():
            setattr(row, field, value)


def process_domain_candidates(
    session: Session,
    articles: list[tuple[ContentItem, Source]],
    client: DeepSeekClient,
    *,
    policy: dict,
    batch_size: int = 8,
) -> DomainLLMRunResult:
    if batch_size < 1 or batch_size > 12:
        raise ValueError("batch_size must be between 1 and 12")
    ordered = sorted(
        {item.id: (item, source) for item, source in articles}.values(),
        key=lambda pair: pair[0].id,
    )
    usages: list[LLMUsage] = []
    processed = included = excluded = cache_hits = 0
    for start in range(0, len(ordered), batch_size):
        chunk = ordered[start : start + batch_size]
        evidence = [_evidence(item, source) for item, source in chunk]
        source_payload = {
            "domain": {
                "key": policy["domain_key"],
                "name": policy["domain_name"],
                "definition": policy["definition"],
            },
            "items": evidence,
        }
        fingerprint = _cache_fingerprint(
            TASK_NAME,
            SCHEMA_VERSION,
            VALIDATOR_VERSION,
            SYSTEM_PROMPT,
            DomainLLMBatch.model_json_schema(),
            source_payload,
            client,
        )
        cached = _cached(session, fingerprint.cache_key)
        if cached is not None:
            batch = DomainLLMBatch.model_validate(cached.output)
            _validate(batch, evidence)
            cache_hits += 1
        else:
            prompt = "请判断以下文章是否属于目标行业：\n" + json.dumps(
                source_payload, ensure_ascii=False
            )
            response = client.generate_json(system_prompt=SYSTEM_PROMPT, user_prompt=prompt)
            try:
                batch = DomainLLMBatch.model_validate(response.output)
                _validate(batch, evidence)
            except (ValidationError, ValueError) as exc:
                repair = client.generate_json(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=(
                        prompt
                        + f"\n上次输出未通过校验：{exc}。请只修复 JSON，不要解释。\n"
                        + json.dumps(response.output, ensure_ascii=False)
                    ),
                )
                response = LLMResponse(
                    repair.output,
                    _usage_sum([response.usage, repair.usage]),
                )
                batch = DomainLLMBatch.model_validate(response.output)
                _validate(batch, evidence)
            usages.append(response.usage)
            _store(
                session,
                "domain_batch",
                f"{policy['domain_key']}:{fingerprint.input_hash[:20]}",
                TASK_NAME,
                fingerprint,
                client,
                batch.model_dump(),
                response.usage,
            )
        by_id = {item.id: item for item, _ in chunk}
        for decision in batch.items:
            content_id = int(decision.content_ref.removeprefix("content:"))
            _save_projection(
                session,
                content=by_id[content_id],
                decision=decision,
                policy=policy,
                artifact_key=fingerprint.cache_key,
            )
            processed += 1
            included += int(decision.decision == "include")
            excluded += int(decision.decision == "exclude")
        session.commit()
    return DomainLLMRunResult(
        processed,
        included,
        excluded,
        cache_hits,
        _usage_sum(usages),
    )


def project_deterministic_baseline(
    session: Session,
    rows: list[tuple[ContentItem, ContentProcessingResult]],
    *,
    policy: dict,
) -> int:
    projected = 0
    for content, baseline in rows:
        if baseline.reason in LLM_REVIEW_REASONS:
            continue
        decision = DomainLLMDecision(
            content_ref=f"content:{content.id}",
            input_content_hash=content.content_hash,
            decision="include" if baseline.is_relevant else "exclude",
            confidence=1.0,
            reason_zh=(
                "来源具有明确的目标行业标签。"
                if baseline.is_relevant
                else "文章没有目标行业证据。"
            ),
            evidence_quote=content.title,
        )
        _save_projection(
            session,
            content=content,
            decision=decision,
            policy=policy,
            artifact_key="deterministic",
            reason_prefix="domain_rule",
        )
        projected += 1
    session.commit()
    return projected
