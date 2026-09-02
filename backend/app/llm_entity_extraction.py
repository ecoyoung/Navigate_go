from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .entity_extraction import normalize_entity_name
from .llm_editorial import DeepSeekClient, EvidenceSpan, LLMUsage
from .models import (
    ContentItem,
    Entity,
    EntityAlias,
    EntityMention,
    EntityProcessingResult,
    EntityResolutionCandidate,
    LLMProcessingResult,
)

TASK_NAME = "entity_extraction"
SCHEMA_VERSION = "entity-candidates.v1"
VALIDATOR_VERSION = "entity-candidate-validator.v1"
EXTRACTOR_NAME = "llm_entity_candidates"
EXTRACTOR_VERSION = "llm-entity-candidates.v1"
MAX_BATCH_SIZE = 5
MAX_EXCERPT_CHARS = 1000
MAX_BODY_CHARS = 2100

SYSTEM_PROMPT = """你是多语言资讯实体标注器。只能依据本次 evidence 输出 JSON，不得使用背景知识补充实体。
schema_version 固定为 entity-candidates.v1；每个 content_ref 按输入顺序输出一次。只提取原文逐字出现且对资讯检索有意义的命名实体，类型只能是 organization、brand、person、product、location、substance、regulation、technology。
surface 必须逐字复制自所引用 evidence_ref；canonical_name_candidate 只能做大小写、全半角和明显标点规范化，不能补充原文没有的全称、母公司或关系。相同实体在同一 evidence_ref 中只输出一次。
输出形状：{"schema_version":"entity-candidates.v1","items":[{"content_ref":"content:id","input_content_hash":"sha256","mentions":[{"surface":"原文字符串","entity_type":"brand","canonical_name_candidate":"规范名称候选","evidence_ref":"ref","confidence":0.9}]}]}。不要解释。"""  # noqa: E501


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityCandidate(StrictModel):
    surface: str = Field(min_length=1, max_length=200)
    entity_type: Literal[
        "organization",
        "brand",
        "person",
        "product",
        "location",
        "substance",
        "regulation",
        "technology",
    ]
    canonical_name_candidate: str = Field(min_length=1, max_length=200)
    evidence_ref: str
    confidence: float = Field(ge=0, le=1)


class EntityCandidateItem(StrictModel):
    content_ref: str
    input_content_hash: str
    mentions: list[EntityCandidate] = Field(default_factory=list, max_length=80)


class EntityCandidateBatch(StrictModel):
    schema_version: Literal["entity-candidates.v1"]
    items: list[EntityCandidateItem] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


@dataclass(frozen=True)
class LLMEntityResult:
    processed: int
    skipped: int
    mentions: int
    resolved: int
    unresolved: int
    cache_hit: bool
    usage: LLMUsage


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _span(content: ContentItem, field: str, text: str, start: int, end: int) -> EvidenceSpan:
    value = text[start:end]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    ref = f"content:{content.id}@{content.content_hash[:12]}:{field}:{start}-{end}:{digest}"
    return EvidenceSpan(ref=ref, field=field, start_char=start, end_char=end, text=value)


def build_entity_input(content: ContentItem) -> dict:
    evidence = []
    if content.title:
        evidence.append(_span(content, "title", content.title, 0, len(content.title)))
    if content.excerpt:
        end = min(len(content.excerpt), MAX_EXCERPT_CHARS)
        evidence.append(_span(content, "excerpt", content.excerpt, 0, end))
    if content.body:
        end = min(len(content.body), MAX_BODY_CHARS)
        evidence.append(_span(content, "body", content.body, 0, end))
    if not evidence:
        raise ValueError(f"Content {content.id} has no entity evidence")
    return {
        "content_ref": f"content:{content.id}",
        "input_content_hash": content.content_hash,
        "language": content.language,
        "evidence": [item.model_dump() for item in evidence],
    }


def _processing_config_hash(client: DeepSeekClient) -> str:
    return _stable_hash(
        {
            "extractor": EXTRACTOR_VERSION,
            "validator": VALIDATOR_VERSION,
            "prompt": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "schema": _stable_hash(EntityCandidateBatch.model_json_schema()),
            "client": client.generation_fingerprint,
            "excerpt_chars": MAX_EXCERPT_CHARS,
            "body_chars": MAX_BODY_CHARS,
        }
    )


def _validate_batch(batch: EntityCandidateBatch, inputs: list[dict]) -> None:
    if [item.content_ref for item in batch.items] != [item["content_ref"] for item in inputs]:
        raise ValueError("content refs do not exactly match input order")
    by_ref = {item["content_ref"]: item for item in inputs}
    for item in batch.items:
        source = by_ref[item.content_ref]
        if item.input_content_hash != source["input_content_hash"]:
            raise ValueError(f"content hash mismatch for {item.content_ref}")
        evidence = {span["ref"]: span for span in source["evidence"]}
        seen = set()
        for mention in item.mentions:
            span = evidence.get(mention.evidence_ref)
            if span is None:
                raise ValueError(f"unknown evidence ref for {item.content_ref}")
            if mention.surface not in span["text"]:
                raise ValueError(
                    f"surface is not an exact evidence substring for {item.content_ref}: "
                    f"{mention.surface}"
                )
            key = (mention.surface, mention.entity_type, mention.evidence_ref)
            if key in seen:
                raise ValueError(f"duplicate entity candidate for {item.content_ref}")
            seen.add(key)


def _current_result(
    session: Session, content: ContentItem, config_hash: str
) -> EntityProcessingResult | None:
    return session.scalar(
        select(EntityProcessingResult).where(
            EntityProcessingResult.content_item_id == content.id,
            EntityProcessingResult.extractor_name == EXTRACTOR_NAME,
            EntityProcessingResult.extractor_version == EXTRACTOR_VERSION,
            EntityProcessingResult.input_content_hash == content.content_hash,
            EntityProcessingResult.config_hash == config_hash,
            EntityProcessingResult.status == "succeeded",
        )
    )


def _entity_candidates(session: Session, candidate: EntityCandidate) -> list[Entity]:
    normalized = {
        normalize_entity_name(candidate.surface),
        normalize_entity_name(candidate.canonical_name_candidate),
    }
    normalized.discard("")
    if not normalized:
        return []
    return list(
        session.scalars(
            select(Entity)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(
                Entity.status == "active",
                Entity.entity_type == candidate.entity_type,
                EntityAlias.normalized_alias.in_(normalized),
            )
            .distinct()
            .order_by(Entity.id)
        )
    )


def _positions(text: str, surface: str) -> list[int]:
    starts = []
    cursor = 0
    while True:
        start = text.find(surface, cursor)
        if start < 0:
            return starts
        starts.append(start)
        cursor = start + len(surface)


def _materialize_item(
    session: Session,
    content: ContentItem,
    source: dict,
    item: EntityCandidateItem,
    config_hash: str,
) -> tuple[int, int, int]:
    result = EntityProcessingResult(
        content_item_id=content.id,
        extractor_name=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
        input_content_hash=content.content_hash,
        config_hash=config_hash,
        schema_version=SCHEMA_VERSION,
        status="succeeded",
    )
    session.add(result)
    session.flush()
    evidence = {span["ref"]: span for span in source["evidence"]}
    output_mentions = []
    mention_count = resolved_count = unresolved_count = 0
    for candidate in item.mentions:
        span = evidence[candidate.evidence_ref]
        possible = _entity_candidates(session, candidate)
        entity = possible[0] if len(possible) == 1 else None
        status = "resolved" if entity is not None else (
            "ambiguous" if possible else "unresolved"
        )
        for local_start in _positions(span["text"], candidate.surface):
            start = int(span["start_char"]) + local_start
            end = start + len(candidate.surface)
            context_start = max(0, local_start - 60)
            context_end = min(len(span["text"]), local_start + len(candidate.surface) + 60)
            mention = EntityMention(
                processing_result_id=result.id,
                content_item_id=content.id,
                entity_id=entity.id if entity is not None else None,
                entity_type=candidate.entity_type,
                surface=candidate.surface,
                normalized_surface=normalize_entity_name(candidate.surface),
                field=str(span["field"]),
                start_offset=start,
                end_offset=end,
                evidence_text=span["text"][context_start:context_end].strip(),
                confidence=candidate.confidence if entity is not None else 0.0,
                resolution_status=status,
                extraction_method="llm_candidate",
            )
            session.add(mention)
            session.flush()
            for possible_entity in possible if entity is None else []:
                session.add(
                    EntityResolutionCandidate(
                        mention_id=mention.id,
                        candidate_entity_id=possible_entity.id,
                        score=candidate.confidence,
                        signals={"reason": "llm_candidate_alias_ambiguity"},
                    )
                )
            mention_count += 1
            resolved_count += int(entity is not None)
            unresolved_count += int(entity is None)
            output_mentions.append(
                {
                    **candidate.model_dump(),
                    "mention_id": mention.id,
                    "entity_id": mention.entity_id,
                    "field": mention.field,
                    "start_offset": start,
                    "end_offset": end,
                    "resolution_status": status,
                }
            )
    result.candidate_count = len(item.mentions)
    result.resolved_count = resolved_count
    result.unresolved_count = unresolved_count
    result.output = {
        "schema_version": SCHEMA_VERSION,
        "content_ref": item.content_ref,
        "input_content_hash": item.input_content_hash,
        "mentions": output_mentions,
    }
    return mention_count, resolved_count, unresolved_count


def process_llm_entity_batch(
    session: Session,
    contents: list[ContentItem],
    client: DeepSeekClient,
    *,
    refresh: bool = False,
) -> LLMEntityResult:
    unique = sorted({content.id: content for content in contents}.values(), key=lambda x: x.id)
    if not 1 <= len(unique) <= MAX_BATCH_SIZE:
        raise ValueError(f"LLM entity batch must contain 1-{MAX_BATCH_SIZE} contents")
    config_hash = _processing_config_hash(client)
    missing = [
        content
        for content in unique
        if _current_result(session, content, config_hash) is None
    ]
    if not missing:
        return LLMEntityResult(
            0, len(unique), 0, 0, 0, True, LLMUsage(None, None, None)
        )
    inputs = [build_entity_input(content) for content in missing]
    batch_source = {"items": inputs}
    input_hash = _stable_hash(batch_source)
    prompt_hash = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    schema_hash = _stable_hash(EntityCandidateBatch.model_json_schema())
    cache_key = _stable_hash(
        {
            "task": TASK_NAME,
            "schema": SCHEMA_VERSION,
            "validator": VALIDATOR_VERSION,
            "input": input_hash,
            "prompt": prompt_hash,
            "schema_hash": schema_hash,
            "client": client.generation_fingerprint,
        }
    )
    cached = None if refresh else session.scalar(
        select(LLMProcessingResult).where(
            LLMProcessingResult.cache_key == cache_key,
            LLMProcessingResult.status == "succeeded",
        )
    )
    if cached is not None:
        batch = EntityCandidateBatch.model_validate(cached.output)
        usage = LLMUsage(cached.prompt_tokens, cached.completion_tokens, cached.total_tokens)
        cache_hit = True
    else:
        response = client.generate_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt="请处理以下实体证据并输出 JSON：\n"
            + json.dumps(batch_source, ensure_ascii=False),
        )
        batch = EntityCandidateBatch.model_validate(response.output)
        usage = response.usage
        cache_hit = False
        cached = LLMProcessingResult(
            subject_type="content_batch",
            subject_key=",".join(str(content.id) for content in missing),
            task_name=TASK_NAME,
            task_version=SCHEMA_VERSION,
            input_hash=input_hash,
            provider=client.provider,
            model=client.model,
            cache_key=cache_key,
            prompt_hash=prompt_hash,
            schema_version=SCHEMA_VERSION,
            schema_hash=schema_hash,
            validator_version=VALIDATOR_VERSION,
            status="succeeded",
            output=batch.model_dump(),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        )
        session.add(cached)
    _validate_batch(batch, inputs)
    by_ref = {item["content_ref"]: item for item in inputs}
    content_by_ref = {f"content:{content.id}": content for content in missing}
    mentions = resolved = unresolved = 0
    for item in batch.items:
        counts = _materialize_item(
            session,
            content_by_ref[item.content_ref],
            by_ref[item.content_ref],
            item,
            config_hash,
        )
        mentions += counts[0]
        resolved += counts[1]
        unresolved += counts[2]
    session.commit()
    return LLMEntityResult(
        len(missing),
        len(unique) - len(missing),
        mentions,
        resolved,
        unresolved,
        cache_hit,
        usage,
    )
