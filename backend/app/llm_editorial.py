# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .daily_report import DailyReportData, ReportStory
from .editorial_policy import EditorialPolicy, load_editorial_policy
from .models import ContentItem, EventMember, LLMProcessingResult, Source

logger = logging.getLogger("navigate.editorial")
PROVIDER, DEFAULT_BASE_URL, DEFAULT_MODEL = "deepseek", "https://api.deepseek.com", "deepseek-v4-flash"
CONTENT_TASK_NAME, CONTENT_SCHEMA_VERSION = "content_editorial_zh", "content_editorial.zh.v2"
CONTENT_VALIDATOR_VERSION = "content-editorial-validator.v6"
EDITION_TASK_NAME, EDITION_SCHEMA_VERSION = "daily_edition_zh", "daily_edition.zh.v1"
EDITION_VALIDATOR_VERSION = "daily-edition-validator.v2"
EVIDENCE_VERSION, DEFAULT_CONTENT_BATCH_SIZE = "content-evidence.v3", 4
READER_EVIDENCE_EXCERPT_CHARS = 1200
READER_EVIDENCE_BODY_CHARS = 2500
READER_CONTENT_BATCH_SIZE = 2
READER_BACKFILL_LIMIT = 4
READER_EDITORIAL_DAILY_LIMIT = 100
READER_CRAWL_LIMIT = READER_EDITORIAL_DAILY_LIMIT
TASK_NAME, TASK_VERSION = EDITION_TASK_NAME, EDITION_SCHEMA_VERSION
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceSpan(StrictModel):
    ref: str
    field: Literal["title", "excerpt", "body", "topics"]
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    text: str = Field(min_length=1)


class ContentEditorialInput(StrictModel):
    content_ref: str
    input_content_hash: str
    source_name: str
    language: str | None
    published_at: str | None
    evidence_version: Literal["content-evidence.v3"]
    evidence: list[EvidenceSpan] = Field(min_length=1)


class SummaryUnit(StrictModel):
    claim_ref: str
    text_zh: str = Field(min_length=4, max_length=180)
    evidence_refs: list[str] = Field(min_length=1)


class EditorialTag(StrictModel):
    tag_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", min_length=2, max_length=80)
    label_zh: str = Field(min_length=1, max_length=32)
    kind: Literal["entity", "topic", "event", "product", "geography", "other"]
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(min_length=1)

    @field_validator("label_zh")
    @classmethod
    def chinese_label(cls, value: str) -> str:
        if not re.search(r"[\u3400-\u9fff]", value):
            raise ValueError("tag label must contain Chinese text")
        return value


class ContentEditorial(StrictModel):
    content_ref: str
    input_content_hash: str
    chinese_title: str = Field(min_length=4, max_length=140)
    title_evidence_refs: list[str] = Field(min_length=1)
    summary_units: list[SummaryUnit] = Field(min_length=1, max_length=3)
    tags: list[EditorialTag] = Field(default_factory=list, max_length=8)

    @field_validator("chinese_title")
    @classmethod
    def chinese_title_required(cls, value: str) -> str:
        if not re.search(r"[\u3400-\u9fff]", value):
            raise ValueError("title must contain Chinese text")
        return value

    @model_validator(mode="after")
    def unique_keys(self):
        for values in ([x.claim_ref for x in self.summary_units], [x.tag_key for x in self.tags]):
            if len(values) != len(set(values)):
                raise ValueError("claim refs and tag keys must be unique")
        return self

    @property
    def chinese_summary(self) -> str:
        return "".join(item.text_zh for item in self.summary_units)


class ContentEditorialBatch(StrictModel):
    schema_version: Literal["content_editorial.zh.v2"]
    items: list[ContentEditorial] = Field(min_length=1)


class DailyLead(StrictModel):
    deck: str = Field(min_length=6, max_length=100)
    text: str = Field(min_length=12, max_length=360)
    story_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def chinese_copy(self):
        if not re.search(r"[\u3400-\u9fff]", f"{self.deck} {self.text}"):
            raise ValueError("daily lead must contain Chinese text")
        return self


class EditionSection(StrictModel):
    section_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", min_length=2, max_length=80)
    title: str = Field(min_length=2, max_length=40)
    intro: str = Field(min_length=4, max_length=180)
    intro_story_refs: list[str] = Field(min_length=1)
    story_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def chinese_copy(self):
        if not re.search(r"[\u3400-\u9fff]", f"{self.title} {self.intro}"):
            raise ValueError("section copy must contain Chinese text")
        return self


class DailyEdition(StrictModel):
    schema_version: Literal["daily_edition.zh.v1"]
    daily_lead: DailyLead
    sections: list[EditionSection] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sections(self):
        keys = [item.section_key for item in self.sections]
        if len(keys) != len(set(keys)):
            raise ValueError("section keys must be unique")
        return self


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class LLMResponse:
    output: dict
    usage: LLMUsage


@dataclass(frozen=True)
class CacheFingerprint:
    input_hash: str
    cache_key: str
    prompt_hash: str
    schema_hash: str
    schema_version: str
    validator_version: str


class DeepSeekClient:
    provider = PROVIDER

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL, timeout_seconds: float = 180.0):
        self.api_key, self.model = api_key.strip(), model
        self.base_url, self.timeout_seconds = base_url.rstrip("/"), timeout_seconds

    @property
    def generation_fingerprint(self) -> dict:
        return {"provider": self.provider, "model": self.model, "base_url_hash": hashlib.sha256(self.base_url.encode()).hexdigest(), "thinking": "disabled", "temperature": 0, "format": "json_object"}

    def generate_json(self, *, system_prompt: str, user_prompt: str) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("No valid cached editorial result and no DeepSeek API key")
        # Do not set max_tokens/max_completion_tokens; JSON cards must not be cut off.
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json=payload)
                if response.status_code >= 400 and response.status_code not in {408, 429} and response.status_code < 500:
                    raise RuntimeError(f"DeepSeek request rejected with HTTP {response.status_code}")
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("DeepSeek response was truncated")
                content = choice["message"]["content"]
                if not content or not str(content).strip():
                    raise ValueError("DeepSeek returned empty content")
                usage = body.get("usage") or {}
                return LLMResponse(json.loads(content), LLMUsage(usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")))
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        raise RuntimeError(f"DeepSeek request failed: {last_error}") from last_error


CONTENT_SYSTEM_PROMPT = """你是中文资讯编辑。仅依据 evidence 和本次 domain_policy 输出 JSON。schema_version 固定为 content_editorial.zh.v2，每个 content_ref 按原顺序输出一次。生成中文标题及 title_evidence_refs；标题必须包含自然中文表达，品牌和产品专名可以保留原文，但不能让整条标题只有外文、数字或符号。生成1—3 个 summary_units，每句 claim_ref 格式为 content:id#summary:序号；0—8 个 tags。tags 的 tag_key 和 label_zh 必须逐字取自 domain_policy.tag_catalog，不得创造近义标签；kind 只能为 entity/topic/event/product/geography/other。所有标题、摘要句和标签必须绑定该篇 evidence_refs。不得补充背景、常识、因果、影响、预测或评价。数字不是核心事实时应省略；确需使用时必须在 evidence 中存在，保留原数字形式、单位和百分号，不得换算或改写。JSON 形状：{"schema_version":"content_editorial.zh.v2","items":[{"content_ref":"content:id","input_content_hash":"sha256","chinese_title":"string","title_evidence_refs":["ref"],"summary_units":[{"claim_ref":"content:id#summary:1","text_zh":"string","evidence_refs":["ref"]}],"tags":[{"tag_key":"policy_key","label_zh":"政策中的中文名","kind":"topic","confidence":0.8,"evidence_refs":["ref"]}]}]}"""
READER_CONTENT_SYSTEM_PROMPT = (
    CONTENT_SYSTEM_PROMPT
    + " 本任务面向站内内容卡片。必须把英文标题和正文改写成自然中文，品牌和产品专名可保留原文。"
    "生成1—3句事实摘要，不要写成目录、流程说明或评价。"
    "禁止输出作者、编辑、图源、阅读时长、导航、广告、页脚、登录或分享引导。"
    "domain_policy 为空时 tags 可为0—8个中文标签，label_zh 必须含汉字，禁止 IPO、TikTok Shop 这类纯外文标签；没有合适中文标签就输出空数组。"
    "摘要里的数字必须逐字出现在 evidence 中，禁止换算、补全或估算。"
)

EDITION_SYSTEM_PROMPT = """你是中文日报编排编辑。只使用已校验的单篇工件、事件关系、确定性 metrics 和领域 policy 输出 JSON。schema_version 固定为 daily_edition.zh.v1。只能使用 policy.section_catalog 中的 section_key，title 必须逐字复制对应栏目标题；只输出有故事的栏目并遵守 layout_policy 与 ranking_policy。输入会额外提供 expected_story_refs；输出前必须逐项核对：expected_story_refs 中每一项都要在所有 section.story_refs 中恰好出现一次，不得新增、遗漏、重复、改写或合并；sections 数组和每个 story_refs 数组的顺序就是最终版面顺序。intro_story_refs 必须是本栏目 story_refs 的非空子集。不得用正文长度直接判断重要性，不得从共同标签推断趋势，不得补充因果、影响、预测或市场结论。daily_lead 只写本期事实要点，禁止“本期整理/收录N条”“与某某相关的已发布资讯”等计数或流程说明。JSON 形状：{"schema_version":"daily_edition.zh.v1","daily_lead":{"deck":"string","text":"string","story_refs":["story_ref"]},"sections":[{"section_key":"policy_key","title":"政策中的栏目标题","intro":"中文导语","intro_story_refs":["story_ref"],"story_refs":["story_ref"]}]}"""


def _stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _chunk_ranges(value: str, limit: int = 700) -> list[tuple[int, int]]:
    ranges = []
    for paragraph in re.finditer(r"[^\n]+", value):
        start, finish = paragraph.span()
        while start < finish:
            end = min(start + limit, finish)
            ranges.append((start, end))
            start = end
    return ranges


def _evidence_field_text(content: ContentItem) -> dict[str, str]:
    from .event_signature import clean_title
    from .reader_cards import sanitize_article_text

    title = clean_title(content.title) or (content.title or "")
    excerpt = sanitize_article_text(content.excerpt, title=title)[:READER_EVIDENCE_EXCERPT_CHARS]
    body = sanitize_article_text(content.body, title=title)[:READER_EVIDENCE_BODY_CHARS]
    topics = "\n".join(str(x) for x in (content.topics or []) if x)
    return {
        "title": title.strip(),
        "excerpt": excerpt.strip(),
        "body": body.strip(),
        "topics": topics.strip(),
    }


def build_evidence_manifest(content: ContentItem) -> list[EvidenceSpan]:
    values = _evidence_field_text(content)
    spans = []
    for field, value in values.items():
        if not value.strip():
            continue
        for start, end in (_chunk_ranges(value) if field == "body" else [(0, len(value))]):
            text = value[start:end]
            digest = hashlib.sha256(text.encode()).hexdigest()
            ref = f"content:{content.id}@{content.content_hash[:12]}:{field}:{start}-{end}:{digest[:8]}"
            spans.append(EvidenceSpan(ref=ref, field=field, start_char=start, end_char=end, text=text))
    if not spans:
        raise ValueError(f"Content {content.id} has no evidence text")
    return spans


def build_content_editorial_input(content: ContentItem, source: Source) -> ContentEditorialInput:
    return ContentEditorialInput(content_ref=f"content:{content.id}", input_content_hash=content.content_hash, source_name=source.name, language=content.language, published_at=content.published_at.isoformat() if content.published_at else None, evidence_version=EVIDENCE_VERSION, evidence=build_evidence_manifest(content))


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?%?", value))


def _is_supported_number(
    number: str,
    evidence_text: str,
    output_text: str = "",
) -> bool:
    if number in _numbers(evidence_text):
        return True
    if number.endswith("%"):
        return False
    try:
        target = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return False
    for source in _numbers(evidence_text):
        try:
            if not source.endswith("%") and Decimal(source.replace(",", "")) == target:
                return True
        except InvalidOperation:
            pass
    for pattern, multiplier in ((r"(?:billion|bn)", 10), (r"(?:million|mn)", 100)):
        for value in re.findall(rf"(\d+(?:[.,]\d+)?)\s*{pattern}\b", evidence_text, re.I):
            if Decimal(value.replace(",", "")) * multiplier == target:
                return True
    for suffix, multiplier in (("B", 10), ("M", 100)):
        for value in re.findall(rf"\$\s*(\d+(?:[.,]\d+)?)\s*{suffix}\b", evidence_text):
            if Decimal(value.replace(",", "")) * multiplier == target:
                return True
    chinese_unit = None
    for unit in ("亿", "万"):
        if re.search(rf"{re.escape(number)}\s*{unit}", output_text):
            chinese_unit = unit
            break
    if chinese_unit:
        scale = Decimal(100000000) if chinese_unit == "亿" else Decimal(10000)
        for source in _numbers(evidence_text):
            if source.endswith("%"):
                continue
            try:
                if Decimal(source.replace(",", "")) == target * scale:
                    return True
            except InvalidOperation:
                pass
        conversions = {
            "亿": ((r"(?:billion|bn)", Decimal(10)), (r"(?:million|mn)", Decimal("0.01"))),
            "万": ((r"(?:billion|bn)", Decimal(100000)), (r"(?:million|mn)", Decimal(100))),
        }
        for pattern, multiplier in conversions[chinese_unit]:
            for value in re.findall(
                rf"(\d+(?:[.,]\d+)?)\s*{pattern}\b", evidence_text, re.I
            ):
                if Decimal(value.replace(",", "")) * multiplier == target:
                    return True
        for suffix, multiplier in (
            (("B", Decimal(10)), ("M", Decimal("0.01")))
            if chinese_unit == "亿"
            else (("B", Decimal(100000)), ("M", Decimal(100)))
        ):
            for value in re.findall(
                rf"\$\s*(\d+(?:[.,]\d+)?)\s*{suffix}\b", evidence_text
            ):
                if Decimal(value.replace(",", "")) * multiplier == target:
                    return True
    month_names = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    if target == target.to_integral_value() and 1 <= int(target) <= 12:
        month_name = month_names[int(target) - 1]
        if re.search(rf"\b{month_name}\b", evidence_text, flags=re.IGNORECASE):
            return True
    return False


def _cache_fingerprint(task: str, version: str, validator: str, prompt: str, schema: dict, source: object, client: DeepSeekClient) -> CacheFingerprint:
    input_hash, prompt_hash, schema_hash = _stable_hash(source), hashlib.sha256(prompt.encode()).hexdigest(), _stable_hash(schema)
    client_fp = getattr(client, "generation_fingerprint", {"provider": getattr(client, "provider", PROVIDER), "model": client.model})
    key = _stable_hash({"task": task, "version": version, "validator": validator, "prompt": prompt_hash, "schema": schema_hash, "client": client_fp, "input": input_hash})
    return CacheFingerprint(input_hash, key, prompt_hash, schema_hash, version, validator)


def _cached(session: Session, key: str) -> LLMProcessingResult | None:
    return session.scalar(select(LLMProcessingResult).where(LLMProcessingResult.cache_key == key, LLMProcessingResult.status == "succeeded"))


def _store(session: Session, subject_type: str, subject_key: str, task: str, fp: CacheFingerprint, client: DeepSeekClient, output: dict, usage: LLMUsage) -> None:
    row = session.scalar(select(LLMProcessingResult).where(LLMProcessingResult.cache_key == fp.cache_key)) or LLMProcessingResult(subject_type=subject_type, subject_key=subject_key, task_name=task, task_version=fp.schema_version, input_hash=fp.input_hash, provider=getattr(client, "provider", PROVIDER), model=client.model, cache_key=fp.cache_key)
    row.prompt_hash, row.schema_version, row.schema_hash, row.validator_version = fp.prompt_hash, fp.schema_version, fp.schema_hash, fp.validator_version
    row.status, row.output, row.error_text = "succeeded", output, None
    row.prompt_tokens, row.completion_tokens, row.total_tokens = usage.prompt_tokens, usage.completion_tokens, usage.total_tokens
    session.add(row)


def _usage_sum(items: list[LLMUsage]) -> LLMUsage:
    def add(name: str):
        values = [getattr(x, name) for x in items]
        return sum(x for x in values if x is not None) if any(x is not None for x in values) else None
    return LLMUsage(add("prompt_tokens"), add("completion_tokens"), add("total_tokens"))


def _validate_content_batch(
    batch: ContentEditorialBatch,
    inputs: list[ContentEditorialInput],
    policy: EditorialPolicy | None = None,
) -> None:
    if [x.content_ref for x in batch.items] != [x.content_ref for x in inputs]:
        raise ValueError("content refs do not exactly match input order")
    by_ref = {x.content_ref: x for x in inputs}
    allowed_tags = {tag.tag_key: tag.title for tag in policy.tag_catalog} if policy else None
    for item in batch.items:
        source = by_ref[item.content_ref]
        if item.input_content_hash != source.input_content_hash:
            raise ValueError(f"content hash mismatch for {item.content_ref}")
        evidence = {x.ref: x.text for x in source.evidence}
        expected_claims = [f"{item.content_ref}#summary:{i}" for i in range(1, len(item.summary_units) + 1)]
        if [x.claim_ref for x in item.summary_units] != expected_claims:
            raise ValueError(f"claim refs are not stable for {item.content_ref}")
        grounded = [(item.chinese_title, item.title_evidence_refs)] + [(x.text_zh, x.evidence_refs) for x in item.summary_units] + [(x.label_zh, x.evidence_refs) for x in item.tags]
        if allowed_tags is not None:
            for tag in item.tags:
                if allowed_tags.get(tag.tag_key) != tag.label_zh:
                    raise ValueError(f"tag is not in domain policy for {item.content_ref}: {tag.tag_key}")
        for text, refs in grounded:
            if not set(refs).issubset(evidence):
                raise ValueError(f"unknown evidence ref for {item.content_ref}")
            cited = " ".join(evidence[ref] for ref in refs)
            missing = {
                number
                for number in _numbers(text)
                if not _is_supported_number(number, cited, text)
            }
            if missing:
                article_evidence = " ".join(evidence.values())
                unsupported = {
                    number
                    for number in missing
                    if not _is_supported_number(number, article_evidence, text)
                }
                if unsupported:
                    raise ValueError(
                        f"unsupported numbers for {item.content_ref}: {sorted(unsupported)}"
                    )


def contents_missing_editorials(
    session: Session,
    articles: list[tuple[ContentItem, Source]],
) -> list[tuple[ContentItem, Source]]:
    ids = [content.id for content, _source in articles if content.id is not None]
    hashes: dict[int, str] = {}
    if ids:
        rows = session.scalars(
            select(LLMProcessingResult)
            .where(
                LLMProcessingResult.subject_type == "content_item",
                LLMProcessingResult.task_name == CONTENT_TASK_NAME,
                LLMProcessingResult.status == "succeeded",
                LLMProcessingResult.subject_key.in_([f"content:{item}" for item in ids]),
            )
            .order_by(LLMProcessingResult.id)
        )
        for row in rows:
            if (
                row.schema_version != CONTENT_SCHEMA_VERSION
                or row.validator_version != CONTENT_VALIDATOR_VERSION
            ):
                continue
            try:
                content_id = int(row.subject_key.split(":", 1)[1])
            except (ValueError, IndexError):
                continue
            output = row.output or {}
            if not isinstance(output, dict):
                continue
            title = str(output.get("chinese_title") or "").strip()
            units = output.get("summary_units") or []
            summary = str(output.get("chinese_summary") or "").strip()
            if not title or not (units or summary):
                continue
            hashes[content_id] = str(output.get("input_content_hash") or "")
    return [
        (content, source)
        for content, source in articles
        if hashes.get(content.id) != content.content_hash
    ]


def beijing_day_start_utc(now: datetime | None = None) -> datetime:
    current = now.astimezone(BEIJING_TZ) if now else datetime.now(BEIJING_TZ)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


def reader_editorials_used_today(session: Session, *, now: datetime | None = None) -> int:
    start = beijing_day_start_utc(now)
    count = session.scalar(
        select(func.count(LLMProcessingResult.id)).where(
            LLMProcessingResult.task_name == CONTENT_TASK_NAME,
            LLMProcessingResult.status == "succeeded",
            LLMProcessingResult.created_at >= start,
        )
    )
    return int(count or 0)


def ensure_reader_editorials(
    session: Session,
    articles: list[tuple[ContentItem, Source]],
    client: DeepSeekClient,
    *,
    limit: int = READER_CRAWL_LIMIT,
    batch_size: int = READER_CONTENT_BATCH_SIZE,
    daily_limit: int = READER_EDITORIAL_DAILY_LIMIT,
    refresh: bool = False,
) -> dict:
    used_today = reader_editorials_used_today(session)
    remaining = max(0, daily_limit - used_today)
    limit = min(max(0, limit), remaining)
    if limit == 0:
        return {
            "processed": 0,
            "missing": 0,
            "skipped": "daily_limit",
            "used_today": used_today,
            "daily_limit": daily_limit,
        }
    missing = contents_missing_editorials(session, articles)
    if refresh:
        have = [
            pair
            for pair in articles
            if pair[0].id not in {content.id for content, _source in missing}
        ]
        missing = [*missing, *have]
    missing = missing[:limit]
    usable: list[tuple[ContentItem, Source]] = []
    skipped_empty = 0
    for pair in missing:
        try:
            build_content_editorial_input(*pair)
        except ValueError:
            skipped_empty += 1
            continue
        usable.append(pair)
    if not usable:
        return {
            "processed": 0,
            "missing": 0,
            "skipped_empty": skipped_empty,
            "used_today": used_today,
            "daily_limit": daily_limit,
        }
    try:
        artifacts, cache_hit, usage = process_content_editorials(
            session,
            usable,
            client,
            policy=None,
            refresh=refresh,
            batch_size=batch_size,
            system_prompt=READER_CONTENT_SYSTEM_PROMPT,
        )
        return {
            "processed": len(artifacts),
            "missing": len(usable),
            "skipped_empty": skipped_empty,
            "cache_hit": cache_hit,
            "total_tokens": usage.total_tokens,
            "used_today": used_today + len(artifacts),
            "daily_limit": daily_limit,
        }
    except (ValidationError, ValueError, RuntimeError):
        logger.exception("reader editorial batch failed; retrying individually")
        processed = 0
        failed = 0
        for pair in usable:
            try:
                part, _hit, _usage = process_content_editorials(
                    session,
                    [pair],
                    client,
                    policy=None,
                    refresh=refresh,
                    batch_size=1,
                    system_prompt=READER_CONTENT_SYSTEM_PROMPT,
                )
                processed += len(part)
            except Exception:
                logger.exception("reader editorial failed content_id=%s", pair[0].id)
                failed += 1
        return {
            "processed": processed,
            "missing": len(usable),
            "skipped_empty": skipped_empty,
            "failed": failed,
            "partial": True,
            "used_today": used_today + processed,
            "daily_limit": daily_limit,
        }



def _coerce_reader_editorial_output(output: object) -> object:
    if not isinstance(output, dict):
        return output
    items = output.get("items")
    if not isinstance(items, list):
        return output
    coerced = []
    for item in items:
        if not isinstance(item, dict):
            coerced.append(item)
            continue
        tags = []
        for tag in item.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            try:
                parsed = EditorialTag.model_validate(tag)
            except ValidationError:
                continue
            tags.append(parsed.model_dump())
        updated = dict(item)
        updated["tags"] = tags
        coerced.append(updated)
    return {**output, "items": coerced}


def _parse_content_editorial_batch(
    output: object,
    inputs: list[ContentEditorialInput],
    policy: EditorialPolicy | None,
) -> ContentEditorialBatch:
    payload = _coerce_reader_editorial_output(output) if policy is None else output
    batch = ContentEditorialBatch.model_validate(payload)
    _validate_content_batch(batch, inputs, policy)
    return batch


def process_content_editorials(
    session: Session,
    articles: list[tuple[ContentItem, Source]],
    client: DeepSeekClient,
    *,
    policy: EditorialPolicy | None = None,
    refresh: bool = False,
    batch_size: int = DEFAULT_CONTENT_BATCH_SIZE,
    system_prompt: str = CONTENT_SYSTEM_PROMPT,
) -> tuple[dict[str, ContentEditorial], bool, LLMUsage]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    unique = {content.id: (content, source) for content, source in articles}
    inputs = [build_content_editorial_input(*pair) for pair in sorted(unique.values(), key=lambda x: x[0].id)]
    artifacts, missing, usages = {}, [], []
    policy_payload = policy.model_dump() if policy else None
    for source in inputs:
        fingerprint_input = {"domain_policy": policy_payload, "item": source.model_dump()}
        fp = _cache_fingerprint(CONTENT_TASK_NAME, CONTENT_SCHEMA_VERSION, CONTENT_VALIDATOR_VERSION, system_prompt, ContentEditorialBatch.model_json_schema(), fingerprint_input, client)
        row = None if refresh else _cached(session, fp.cache_key)
        if row:
            try:
                item = ContentEditorial.model_validate(row.output)
                _validate_content_batch(ContentEditorialBatch(schema_version=CONTENT_SCHEMA_VERSION, items=[item]), [source], policy)
            except (ValidationError, ValueError):
                row = None
            else:
                artifacts[item.content_ref] = item
        if row is None:
            missing.append((source, fp))
    for start in range(0, len(missing), batch_size):
        chunk = missing[start:start + batch_size]
        chunk_inputs = [x[0] for x in chunk]
        prompt = "请处理以下单篇证据并输出 JSON：\n" + json.dumps(
            {"domain_policy": policy_payload, "items": [x.model_dump() for x in chunk_inputs]},
            ensure_ascii=False,
        )
        response = client.generate_json(system_prompt=system_prompt, user_prompt=prompt)
        try:
            batch = _parse_content_editorial_batch(response.output, chunk_inputs, policy)
        except (ValidationError, ValueError) as exc:
            repair = client.generate_json(
                system_prompt=system_prompt,
                user_prompt=(
                    prompt
                    + f"\n\n上次输出未通过校验：{exc}。请严格修复并重新输出完整 JSON，不要解释。"
                    + "\n上次输出："
                    + json.dumps(response.output, ensure_ascii=False)
                ),
            )
            response = LLMResponse(
                repair.output,
                _usage_sum([response.usage, repair.usage]),
            )
            batch = _parse_content_editorial_batch(response.output, chunk_inputs, policy)
        usages.append(response.usage)
        for item, (_, fp) in zip(batch.items, chunk, strict=True):
            artifacts[item.content_ref] = item
            _store(session, "content_item", item.content_ref, CONTENT_TASK_NAME, fp, client, item.model_dump(), LLMUsage(None, None, None))
        session.commit()
    return artifacts, not missing, _usage_sum(usages)


def _story_key(story: ReportStory) -> str:
    return f"event:{story.event_id}" if story.event_id else f"content:{story.content_item_id}"


def _collect_report_articles(session: Session, data: DailyReportData) -> tuple[list[tuple[ContentItem, Source]], dict[str, list[str]]]:
    articles, members = {}, {}
    for story in data.stories:
        if story.event_id is not None:
            rows = list(session.execute(select(ContentItem, Source).join(EventMember, EventMember.content_item_id == ContentItem.id).join(Source, Source.id == ContentItem.source_id).where(EventMember.event_id == story.event_id, EventMember.is_active.is_(True)).order_by(ContentItem.published_at.asc(), ContentItem.id.asc())))
        else:
            rows = list(session.execute(select(ContentItem, Source).join(Source, Source.id == ContentItem.source_id).where(ContentItem.id == story.content_item_id)))
        members[_story_key(story)] = [f"content:{content.id}" for content, _ in rows]
        for content, source in rows:
            articles.setdefault(content.id, (content, source))
    return list(articles.values()), members


def build_daily_edition_input(
    session: Session,
    data: DailyReportData,
    artifacts: dict[str, ContentEditorial],
    members: dict[str, list[str]],
    policy: EditorialPolicy,
) -> dict:
    policy_payload = policy.model_dump()
    stories = []
    for story in data.stories:
        ref, member_refs = _story_key(story), members[_story_key(story)]
        stories.append({
            "story_ref": ref,
            "event_id": story.event_id,
            "representative_content_ref": f"content:{story.content_item_id}",
            "member_content_refs": member_refs,
            "metrics": {
                "source_count": story.source_count,
                "member_count": story.member_count,
                "body_chars": story.body_chars,
                "published_at": story.published_at.isoformat(),
            },
            "articles": [artifacts[x].model_dump() for x in member_refs],
        })
    return {
        "issue_date": data.issue_date.isoformat(),
        "coverage_date": data.report_date.isoformat(),
        "domain": {
            "key": data.domain_key,
            "name": data.domain_name,
            "policy": policy_payload,
            "policy_hash": _stable_hash(policy_payload),
        },
        "stories": stories,
    }


def _validate_daily_edition(
    edition: DailyEdition,
    source: dict,
    policy: EditorialPolicy | None = None,
) -> None:
    expected = [x["story_ref"] for x in source["stories"]]
    actual = [ref for section in edition.sections for ref in section.story_refs]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        missing = [ref for ref in expected if ref not in actual]
        unexpected = [ref for ref in actual if ref not in expected]
        duplicates = sorted({ref for ref in actual if actual.count(ref) > 1})
        raise ValueError(
            "every story must appear in sections exactly once; "
            f"missing={missing}; unexpected={unexpected}; duplicates={duplicates}"
        )
    if not set(edition.daily_lead.story_refs).issubset(expected):
        raise ValueError("daily lead contains unknown story refs")
    story_evidence = {
        story["story_ref"]: " ".join(
            text
            for article in story["articles"]
            for text in (
                article["chinese_title"],
                *(item["text_zh"] for item in article["summary_units"]),
                *(item["label_zh"] for item in article["tags"]),
            )
        )
        for story in source["stories"]
    }
    if policy is not None:
        allowed_sections = {item.section_key: item for item in policy.section_catalog}
        if len(edition.sections) > policy.layout_policy.max_sections:
            raise ValueError("daily edition exceeds max_sections")
        policy_order = {key: index for index, key in enumerate(policy.ranking_policy.section_order)}
        indexes = []
        for section in edition.sections:
            configured = allowed_sections.get(section.section_key)
            if configured is None or section.title != configured.title:
                raise ValueError(f"section is not in domain policy: {section.section_key}")
            indexes.append(policy_order[section.section_key])
        if indexes != sorted(indexes):
            raise ValueError("sections do not follow domain policy order")
    for section in edition.sections:
        if not set(section.intro_story_refs).issubset(section.story_refs):
            raise ValueError(f"section intro contains out-of-section refs: {section.section_key}")

    def validate_scoped_numbers(text: str, refs: list[str], field: str) -> None:
        evidence = " ".join(story_evidence[ref] for ref in refs)
        missing = {
            number
            for number in _numbers(text)
            if not _is_supported_number(number, evidence, text)
        }
        if missing:
            raise ValueError(f"unsupported numbers in {field}: {sorted(missing)}")

    validate_scoped_numbers(
        f"{edition.daily_lead.deck} {edition.daily_lead.text}",
        edition.daily_lead.story_refs,
        "daily lead",
    )
    for section in edition.sections:
        validate_scoped_numbers(section.intro, section.intro_story_refs, section.section_key)


def _complete_daily_edition(
    edition: DailyEdition,
    source: dict,
    policy: EditorialPolicy,
) -> DailyEdition:
    """Enforce lossless placement without inventing editorial copy.

    The LLM still chooses sections and ordering. If it omits an input reference, the
    contract layer appends it to an existing section whose configured tags match the
    article tags. The fallback is the configured general section when present, then
    the last section already chosen by the editor.
    """

    expected = [item["story_ref"] for item in source["stories"]]
    expected_set = set(expected)
    seen: set[str] = set()
    sections: list[EditionSection] = []
    for section in edition.sections:
        refs = []
        for ref in section.story_refs:
            if ref in expected_set and ref not in seen:
                refs.append(ref)
                seen.add(ref)
        intro_refs = [ref for ref in section.intro_story_refs if ref in refs]
        if refs and intro_refs:
            sections.append(
                section.model_copy(
                    update={"story_refs": refs, "intro_story_refs": intro_refs}
                )
            )

    if not sections:
        return edition

    configured = {item.section_key: item for item in policy.section_catalog}
    story_by_ref = {item["story_ref"]: item for item in source["stories"]}
    fallback_key = policy.layout_policy.fallback_section_key
    for ref in expected:
        if ref in seen:
            continue
        story_tags = {
            tag["tag_key"]
            for article in story_by_ref[ref]["articles"]
            for tag in article.get("tags", [])
        }
        target_index = next(
            (
                index
                for index, section in enumerate(sections)
                if story_tags.intersection(
                    configured.get(section.section_key).tag_keys
                    if configured.get(section.section_key)
                    else []
                )
            ),
            None,
        )
        if target_index is None:
            target_index = next(
                (
                    index
                    for index, section in enumerate(sections)
                    if section.section_key == fallback_key
                ),
                len(sections) - 1,
            )
        target = sections[target_index]
        sections[target_index] = target.model_copy(
            update={"story_refs": [*target.story_refs, ref]}
        )
        seen.add(ref)
    return edition.model_copy(update={"sections": sections})


def _bounded_copy(parts: list[str], limit: int) -> str:
    text = "；".join(part.strip().rstrip("。") for part in parts if part.strip())
    if len(text) <= limit:
        return text + ("。" if text and not text.endswith("。") else "")
    return text[: limit - 1].rstrip("，；、 ") + "…"


def _derive_subset_edition(
    prior: DailyEdition,
    source: dict,
    policy: EditorialPolicy,
) -> DailyEdition | None:
    """Reuse cached placements when a data correction only removes stories."""

    current_refs = {item["story_ref"] for item in source["stories"]}
    prior_refs = {
        story_ref for section in prior.sections for story_ref in section.story_refs
    }
    if not current_refs or not current_refs.issubset(prior_refs):
        return None
    source_by_ref = {item["story_ref"]: item for item in source["stories"]}

    def title_for(story_ref: str) -> str:
        story = source_by_ref[story_ref]
        representative = story["representative_content_ref"]
        article = next(
            (
                item
                for item in story["articles"]
                if item["content_ref"] == representative
            ),
            story["articles"][0],
        )
        return article["chinese_title"]

    def summary_for(story_ref: str) -> str:
        story = source_by_ref[story_ref]
        representative = story["representative_content_ref"]
        article = next(
            (
                item
                for item in story["articles"]
                if item["content_ref"] == representative
            ),
            story["articles"][0],
        )
        return article["summary_units"][0]["text_zh"]

    sections = []
    for prior_section in prior.sections:
        story_refs = [ref for ref in prior_section.story_refs if ref in current_refs]
        if not story_refs:
            continue
        sections.append(
            EditionSection(
                section_key=prior_section.section_key,
                title=prior_section.title,
                intro=_bounded_copy(
                    [summary_for(ref) for ref in story_refs],
                    180,
                ),
                intro_story_refs=story_refs,
                story_refs=story_refs,
            )
        )
    ordered_refs = [ref for section in sections for ref in section.story_refs]
    if set(ordered_refs) != current_refs:
        return None
    edition = DailyEdition(
        schema_version=EDITION_SCHEMA_VERSION,
        daily_lead=DailyLead(
            deck=_bounded_copy(
                [title_for(ref) for ref in ordered_refs], 100
            ),
            text=_bounded_copy(
                [title_for(ref) for ref in ordered_refs]
                + [summary_for(ref) for ref in ordered_refs],
                360,
            ),
            story_refs=ordered_refs,
        ),
        sections=sections,
    )
    _validate_daily_edition(edition, source, policy)
    return edition


def process_daily_edition(
    session: Session,
    data: DailyReportData,
    artifacts: dict[str, ContentEditorial],
    members: dict[str, list[str]],
    client: DeepSeekClient,
    *,
    policy: EditorialPolicy,
    refresh: bool = False,
) -> tuple[DailyEdition, bool, LLMUsage]:
    source = build_daily_edition_input(session, data, artifacts, members, policy)
    fp = _cache_fingerprint(EDITION_TASK_NAME, EDITION_SCHEMA_VERSION, EDITION_VALIDATOR_VERSION, EDITION_SYSTEM_PROMPT, DailyEdition.model_json_schema(), source, client)
    row = None if refresh else _cached(session, fp.cache_key)
    if row:
        try:
            edition = DailyEdition.model_validate(row.output)
            _validate_daily_edition(edition, source, policy)
        except (ValidationError, ValueError):
            row = None
        else:
            return edition, True, LLMUsage(row.prompt_tokens, row.completion_tokens, row.total_tokens)
    if not getattr(client, "api_key", ""):
        prior_rows = session.scalars(
            select(LLMProcessingResult)
            .where(
                LLMProcessingResult.subject_type == "daily_report",
                LLMProcessingResult.subject_key
                == f"{data.domain_key}:{data.report_date.isoformat()}",
                LLMProcessingResult.task_name == EDITION_TASK_NAME,
                LLMProcessingResult.status == "succeeded",
                LLMProcessingResult.provider == getattr(client, "provider", PROVIDER),
                LLMProcessingResult.model == client.model,
                LLMProcessingResult.prompt_hash == fp.prompt_hash,
                LLMProcessingResult.schema_hash == fp.schema_hash,
                LLMProcessingResult.validator_version == fp.validator_version,
            )
            .order_by(LLMProcessingResult.id.desc())
        )
        for prior_row in prior_rows:
            try:
                derived = _derive_subset_edition(
                    DailyEdition.model_validate(prior_row.output), source, policy
                )
            except (ValidationError, ValueError):
                continue
            if derived is not None:
                return derived, True, LLMUsage(None, None, None)
    expected_story_refs = [item["story_ref"] for item in source["stories"]]
    prompt_payload = {"expected_story_refs": expected_story_refs, **source}
    user_prompt = (
        "请统一比较和编排以下工件并输出 JSON。输出前逐项核对 "
        "expected_story_refs 与所有 section.story_refs 的扁平集合完全相同，且每项只出现一次：\n"
        + json.dumps(prompt_payload, ensure_ascii=False)
    )
    response = client.generate_json(
        system_prompt=EDITION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )
    try:
        edition = DailyEdition.model_validate(response.output)
        edition = _complete_daily_edition(edition, source, policy)
        _validate_daily_edition(edition, source, policy)
    except (ValidationError, ValueError) as exc:
        repair = client.generate_json(
            system_prompt=EDITION_SYSTEM_PROMPT,
            user_prompt=(
                user_prompt
                + f"\n\n上次输出未通过校验：{exc}。请严格修复并重新输出完整 JSON，不要解释。"
                + "\n上次输出："
                + json.dumps(response.output, ensure_ascii=False)
            ),
        )
        response = LLMResponse(repair.output, _usage_sum([response.usage, repair.usage]))
        edition = DailyEdition.model_validate(response.output)
        edition = _complete_daily_edition(edition, source, policy)
        _validate_daily_edition(edition, source, policy)
    _store(session, "daily_report", f"{data.domain_key}:{data.report_date.isoformat()}", EDITION_TASK_NAME, fp, client, edition.model_dump(), response.usage)
    session.commit()
    return edition, False, response.usage


def _render_payload(data: DailyReportData, artifacts: dict[str, ContentEditorial], edition: DailyEdition) -> dict:
    stories = []
    for story in data.stories:
        item = artifacts[f"content:{story.content_item_id}"]
        stories.append({"story_key": _story_key(story), "chinese_title": item.chinese_title, "chinese_summary": item.chinese_summary, "summary_units": [x.model_dump() for x in item.summary_units], "tags": [x.model_dump() for x in item.tags], "key_points": []})
    return {"schema_version": EDITION_SCHEMA_VERSION, "daily_lead": edition.daily_lead.model_dump(), "sections": [x.model_dump() for x in edition.sections], "stories": stories}


def enrich_daily_report(session: Session, data: DailyReportData, client: DeepSeekClient, *, refresh: bool = False) -> tuple[DailyReportData, bool, LLMUsage]:
    policy = load_editorial_policy(data.domain_key)
    articles, members = _collect_report_articles(session, data)
    artifacts, content_hit, content_usage = process_content_editorials(
        session, articles, client, policy=policy, refresh=refresh
    )
    edition, edition_hit, edition_usage = process_daily_edition(
        session, data, artifacts, members, client, policy=policy, refresh=refresh
    )
    return replace(data, editorial=_render_payload(data, artifacts, edition)), content_hit and edition_hit, _usage_sum([content_usage, edition_usage])
