from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from zhconv import convert as _zh_convert

from .content_quality import is_reader_eligible
from .models import ContentItem, InterestTopic, RawItem, Source, TopicMatch

COMPILER_NAME = "local_topic_compiler"
COMPILER_VERSION = "topic-intent.v1"
MATCHER_VERSION = "topic-matcher.v1"
TOPIC_ADMIT_DAYS = 7
_SPLIT = re.compile(
    r"[，,。；;、/\n|]+|(?:以及|或者|或是)|(?:和|与)|(?:\s+and\s+|\s+or\s+)",
    re.I,
)
_PREFIX = re.compile(
    r"^(?:我想|请|持续|重点|please\s+)?(?:关注|追踪|跟踪|订阅|了解|follow|track|subscribe(?:\s+to)?|focus\s+on|monitor)\s*",
    re.I,
)
_EXCLUDE_SPLIT = re.compile(
    r"(?:排除|不要|不含)|(?:(?:,|;)\s*)?(?:please\s+)?(?:exclude|excluding)\b",
    re.I,
)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_PHRASE = re.compile(
    r"[A-Za-z][A-Za-z0-9+._/-]*(?:[ '\-][A-Za-z0-9+._/-]+){0,4}"
)
_LATIN_TERM = re.compile(r"[a-z0-9]")
_CJK_TERM = re.compile(r"[\u4e00-\u9fff]")
_POSITIVE_FIELDS = (
    "positive_keywords",
    "user_positive_keywords",
    "query_expansions",
)
_EXCLUSION_FIELDS = ("excluded_keywords", "user_excluded_keywords")
_GENERIC = {
    "资讯",
    "新闻",
    "全球",
    "关注",
    "动态",
    "报道",
    "内容",
    "相关",
    "最新",
    "信息",
    "文章",
    "行业",
    "进展",
    "发布",
    "突破",
    "趋势",
    "监测",
    "监控",
    "融资",
    "投资",
    "创业",
    "news",
    "global",
    "latest",
    "world",
    "daily",
    "update",
    "updates",
}
_UNCOUNTABLE = {"news", "series", "business", "cosmetics", "ai", "actives"}
_BILINGUAL_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("具身智能", ("embodied ai", "embodied intelligence")),
    ("人形机器人", ("humanoid robot",)),
    ("原料", ("ingredient", "actives", "raw material")),
    ("韩妆", ("k-beauty", "korean beauty")),
    ("防晒", ("sunscreen",)),
    ("人工智能", ("artificial intelligence",)),
    ("化妆品", ("cosmetics",)),
)


def _normalize_term(value: str) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", value or ""))
    text = _zh_convert(text, "zh-cn")
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[\s_]+", " ", text).strip().casefold()
    return text


def _normalize_text(value: str) -> str:
    return _normalize_term(value)


def _term_script(term: str) -> str:
    has_cjk = bool(_CJK_TERM.search(term))
    has_latin = bool(_LATIN_TERM.search(term))
    if has_cjk and has_latin:
        return "mixed"
    if has_cjk:
        return "cjk"
    if has_latin:
        return "latin"
    return "other"


def _unique(values: list[str], *, limit: int = 24) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = _normalize_term(value)
        if 2 <= len(clean) <= 40 and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result[:limit]


def _cjk_terms_from_run(run: str) -> list[str]:
    cleaned = re.sub(r"^[与和及或]", "", run).strip()
    if len(cleaned) < 2:
        return []
    terms = [cleaned]
    if len(cleaned) >= 6:
        terms.append(cleaned[:4])
        terms.append(cleaned[-4:])
    return terms


def _with_bilingual_aliases(terms: list[str], source_text: str) -> list[str]:
    blob = _normalize_text(" ".join([source_text, *terms]))
    extras: list[str] = []
    for cjk, english in _BILINGUAL_PAIRS:
        english_norm = [_normalize_term(item) for item in english]
        if _normalize_term(cjk) in blob or any(item and item in blob for item in english_norm):
            extras.append(cjk)
            extras.extend(english)
    return _unique([*terms, *extras])


def _extract_intent_terms(text: str) -> list[str]:
    parts = [item.strip(" .:-") for item in _SPLIT.split(text) if item and item.strip()]
    if not parts:
        parts = [text]
    terms = list(parts)
    latin: list[str] = []
    cjk: list[str] = []
    for part in parts:
        cleaned = _PREFIX.sub("", part).strip() or part
        if not cleaned:
            continue
        latin.extend(match.group(0) for match in _LATIN_PHRASE.finditer(cleaned))
        runs = [run for run in _CJK_RUN.findall(cleaned) if len(run) >= 2]
        cjk.extend(term for run in runs for term in _cjk_terms_from_run(run))
        if _CJK_TERM.search(cleaned) and _LATIN_TERM.search(_normalize_term(cleaned)):
            terms.append(cleaned)
    all_runs = [run for run in _CJK_RUN.findall(text) if len(run) >= 2]
    if len(all_runs) >= 2 and all(len(run) == 2 for run in all_runs):
        cjk = [item for item in cjk if len("".join(_CJK_RUN.findall(item))) > 2]
    terms.extend(latin)
    terms.extend(cjk)
    return terms


def compile_topic_intent(
    intent_text: str,
    *,
    keywords: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
) -> tuple[dict, str]:
    clean_intent = " ".join(intent_text.strip().split())
    if not clean_intent:
        raise ValueError("主题描述不能为空")
    pieces = _EXCLUDE_SPLIT.split(clean_intent, maxsplit=1)
    positive_text = pieces[0]
    excluded_text = pieces[1] if len(pieces) > 1 else ""
    stripped = _PREFIX.sub("", positive_text).strip() or positive_text.strip()
    positive = _with_bilingual_aliases(
        _unique([*(keywords or []), *_extract_intent_terms(stripped)]),
        stripped,
    )
    excluded = _unique([*(excluded_keywords or []), *_extract_intent_terms(excluded_text)])
    if not positive:
        positive = [_normalize_term(clean_intent)[:40]]
    scripts = {_term_script(item) for item in positive}
    if "cjk" in scripts and "latin" not in scripts and "mixed" not in scripts:
        original_language = "zh-CN"
    elif "latin" in scripts and "cjk" not in scripts and "mixed" not in scripts:
        original_language = "en"
    else:
        original_language = "mixed"
    compiled = {
        "schema_version": "topic-intent.v1",
        "positive_keywords": positive,
        "excluded_keywords": excluded,
        "original_language": original_language,
    }
    intent_hash = hashlib.sha256(
        (clean_intent + "\n" + "\n".join(positive) + "\n--\n" + "\n".join(excluded)).encode()
    ).hexdigest()
    return compiled, intent_hash


def suggested_topic_name(intent_text: str, compiled: dict) -> str:
    keywords = compiled.get("positive_keywords") or []
    if keywords:
        return str(keywords[0])[:24]
    return intent_text.strip()[:24]


def matching_terms(
    compiled: dict | None, *, fields: tuple[str, ...] = _POSITIVE_FIELDS
) -> list[str]:
    values: list[str] = []
    config = compiled or {}
    for field in fields:
        raw = config.get(field) or []
        if isinstance(raw, str):
            values.append(raw)
            continue
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if item)
    terms = _unique(values, limit=40)
    if len(terms) <= 1:
        return terms
    return [item for item in terms if item not in _GENERIC]


def _compiled_for_match(topic: InterestTopic) -> dict:
    compiled = dict(topic.compiled_intent or {})
    intent = (topic.intent_text or topic.name or "").strip()
    if not intent:
        return compiled
    local, _hash = compile_topic_intent(
        intent,
        keywords=list(compiled.get("user_positive_keywords") or []),
    )
    return {
        **compiled,
        "positive_keywords": [
            *local.get("positive_keywords", []),
            *(compiled.get("positive_keywords") or []),
        ],
        "excluded_keywords": list(local.get("excluded_keywords") or []),
        "user_excluded_keywords": [],
    }


def _plural_forms(word: str) -> list[str]:
    if len(word) < 4 or word in _UNCOUNTABLE:
        return []
    forms: list[str] = []
    if word.endswith("ies") and len(word) > 4:
        forms.append(word[:-3] + "y")
    elif word.endswith("es") and len(word) > 4:
        forms.append(word[:-2])
    elif word.endswith("s") and not word.endswith("ss"):
        forms.append(word[:-1])
    else:
        forms.append(word + "s")
        if word.endswith(("ch", "sh", "x", "z", "o")):
            forms.append(word + "es")
        if word.endswith("y") and word[-2] not in "aeiou":
            forms.append(word[:-1] + "ies")
    return forms


def _latin_variants(term: str) -> list[str]:
    variants = [term]
    for item in (term.replace("-", " "), term.replace(" ", "-")):
        if item not in variants:
            variants.append(item)
    compact = re.sub(r"[ \-]+", "", term)
    if len(compact) >= 4 and compact not in variants:
        variants.append(compact)
    inflected: list[str] = []
    for item in variants:
        tokens = item.split(" ")
        last = tokens[-1]
        for form in _plural_forms(last):
            inflected.append(" ".join((*tokens[:-1], form)) if len(tokens) > 1 else form)
    for item in inflected:
        if item not in variants:
            variants.append(item)
    return variants


def _term_in_text(term: str, text: str) -> bool:
    if not term or not text:
        return False
    if _term_script(term) == "latin":
        for variant in _latin_variants(term):
            pattern = rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])"
            if re.search(pattern, text):
                return True
        return False
    return term in text


def _keyword_hits(terms: list[str], text: str, *, allow_short_latin: bool = True) -> list[str]:
    hits: list[str] = []
    for term in terms:
        latin_core = re.sub(r"[^a-z0-9]", "", term)
        if not allow_short_latin and _term_script(term) == "latin" and len(latin_core) <= 2:
            continue
        if _term_in_text(term, text):
            hits.append(term)
    return hits


def content_script(content: ContentItem) -> str:
    language = (content.language or "").lower()
    if language.startswith("zh"):
        return "cjk"
    if language.startswith("en"):
        return "latin"
    blob = _normalize_text(
        f"{content.title or ''} {content.excerpt or ''} {(content.body or '')[:1200]}"
    )
    return _term_script(blob)


def has_cross_language_gap(positives: list[str], content: ContentItem) -> bool:
    scripts = {_term_script(term) for term in positives if term}
    body = content_script(content)
    if body == "latin" and "cjk" in scripts and "latin" not in scripts and "mixed" not in scripts:
        return True
    if body == "cjk" and "latin" in scripts and "cjk" not in scripts and "mixed" not in scripts:
        return True
    return False


@dataclass(frozen=True)
class MatchDecision:
    decision: str
    score: float
    reasons: list[str]
    signals: dict


def match_content(topic: InterestTopic, content: ContentItem) -> MatchDecision:
    compiled = _compiled_for_match(topic)
    positives = matching_terms(compiled)
    exclusions = matching_terms(compiled, fields=_EXCLUSION_FIELDS)
    title = _normalize_text(content.title or "")
    excerpt = _normalize_text(content.excerpt or "")
    body = _normalize_text(content.body or "")
    topic_text = _normalize_text(" ".join(content.topics or []))
    combined = f"{title}\n{excerpt}\n{body}\n{topic_text}"
    excluded_hits = _keyword_hits(exclusions, combined)
    if excluded_hits:
        return MatchDecision("exclude", 0.0, ["excluded_keyword"], {"excluded": excluded_hits})
    title_hits = _keyword_hits(positives, title)
    excerpt_hits = _keyword_hits(positives, excerpt, allow_short_latin=False)
    topic_hits = _keyword_hits(positives, topic_text, allow_short_latin=False)
    body_hits = _keyword_hits(positives, body, allow_short_latin=False)
    score = min(
        1.0,
        len(title_hits) * 0.42
        + len(excerpt_hits) * 0.24
        + len(topic_hits) * 0.28
        + len(body_hits) * 0.10,
    )
    if score >= 0.2:
        decision = "include"
    elif score >= 0.1:
        decision = "review"
    elif has_cross_language_gap(positives, content):
        decision = "review"
    else:
        decision = "exclude"
    reasons = [
        name
        for name, hits in (
            ("title_keyword", title_hits),
            ("excerpt_keyword", excerpt_hits),
            ("topic_keyword", topic_hits),
            ("body_keyword", body_hits),
        )
        if hits
    ]
    if not reasons:
        reasons = (
            ["cross_language_unresolved"]
            if decision == "review"
            else ["no_keyword_evidence"]
        )
    return MatchDecision(
        decision,
        score,
        reasons,
        {"title": title_hits, "excerpt": excerpt_hits, "topics": topic_hits, "body": body_hits},
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _collection_window(content: ContentItem, *, days: int = TOPIC_ADMIT_DAYS) -> dict | None:
    if content.published_at is None:
        return None
    published = _as_utc(content.published_at)
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return {
        "schema_version": "collection-window.v2",
        "mode": "shared_pool",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "published_at": published.isoformat(),
        "admitted": start <= published <= end,
    }


def _upsert_match(
    db: Session,
    topic: InterestTopic,
    content: ContentItem,
    decision: MatchDecision,
    *,
    existing: TopicMatch | None,
    collection_window: dict | None,
) -> None:
    admitted = True if collection_window is None else bool(collection_window.get("admitted"))
    final_decision = decision.decision
    score = decision.score
    reasons = list(decision.reasons)
    if not admitted and final_decision != "exclude":
        final_decision = "exclude"
        score = 0.0
        reasons = [*reasons, "outside_collection_window"]
    values = {
        "input_content_hash": content.content_hash,
        "decision": final_decision,
        "score": score,
        "reasons": reasons,
        "matched_signals": {
            **decision.signals,
            **({"collection_window": collection_window} if collection_window else {}),
        },
        "matched_at": datetime.now(UTC),
    }
    if existing is None:
        db.add(
            TopicMatch(
                topic_id=topic.id,
                content_item_id=content.id,
                matcher_version=MATCHER_VERSION,
                **values,
            )
        )
        return
    for field, value in values.items():
        setattr(existing, field, value)


def match_contents_to_topics(
    db: Session,
    contents: list[ContentItem],
    *,
    topics: list[InterestTopic] | None = None,
) -> dict:
    eligible = [item for item in contents if is_reader_eligible(item)]
    active = topics or list(
        db.scalars(select(InterestTopic).where(InterestTopic.status == "active"))
    )
    included = reviewed = excluded = 0
    for topic in active:
        existing = {
            item.content_item_id: item
            for item in db.scalars(
                select(TopicMatch).where(
                    TopicMatch.topic_id == topic.id,
                    TopicMatch.matcher_version == MATCHER_VERSION,
                    TopicMatch.content_item_id.in_([item.id for item in eligible] or [0]),
                )
            )
        }
        for content in eligible:
            decision = match_content(topic, content)
            prior = existing.get(content.id)
            window = _collection_window(content) or (
                (prior.matched_signals or {}).get("collection_window") if prior else None
            )
            if prior is None and not (isinstance(window, dict) and window.get("admitted")):
                continue
            _upsert_match(
                db,
                topic,
                content,
                decision,
                existing=prior,
                collection_window=window if isinstance(window, dict) else None,
            )
            included += int(decision.decision == "include")
            reviewed += int(decision.decision == "review")
            excluded += int(decision.decision == "exclude")
    db.flush()
    return {
        "contents": len(eligible),
        "topics": len(active),
        "included": included,
        "review": reviewed,
        "excluded": excluded,
    }


def contents_from_crawl_run(db: Session, run_id: int) -> list[ContentItem]:
    return list(
        db.scalars(
            select(ContentItem)
            .join(RawItem, RawItem.id == ContentItem.raw_item_id)
            .where(
                RawItem.crawl_run_id == run_id,
                ContentItem.duplicate_of_id.is_(None),
            )
        )
    )


def recent_reader_contents(db: Session, *, days: int = TOPIC_ADMIT_DAYS) -> list[ContentItem]:
    start = datetime.now(UTC) - timedelta(days=days)
    return list(
        db.scalars(
            select(ContentItem).where(
                ContentItem.duplicate_of_id.is_(None),
                ContentItem.published_at.is_not(None),
                ContentItem.published_at >= start,
            )
        )
    )


def refresh_topic_matches(
    db: Session,
    topic: InterestTopic,
    *,
    limit: int = 300,
    new_item_window_start: datetime | None = None,
    new_item_window_end: datetime | None = None,
) -> dict:
    rows = list(
        db.execute(
            select(ContentItem, Source)
            .join(Source, Source.id == ContentItem.source_id)
            .where(ContentItem.duplicate_of_id.is_(None))
            .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
            .limit(limit)
        )
    )
    existing = {
        item.content_item_id: item
        for item in db.scalars(
            select(TopicMatch).where(
                TopicMatch.topic_id == topic.id,
                TopicMatch.matcher_version == MATCHER_VERSION,
            )
        )
    }
    included = reviewed = excluded = 0
    for content, _source in rows:
        match = existing.get(content.id)
        if match is None and new_item_window_start is not None:
            published_at = content.published_at
            if published_at is None:
                continue
            published_utc = _as_utc(published_at)
            start_utc = _as_utc(new_item_window_start)
            end_utc = _as_utc(new_item_window_end or datetime.now(UTC))
            if not start_utc <= published_utc <= end_utc:
                continue
            collection_window = {
                "schema_version": "collection-window.v2",
                "mode": "shared_pool",
                "start_at": start_utc.isoformat(),
                "end_at": end_utc.isoformat(),
                "published_at": published_utc.isoformat(),
                "admitted": True,
            }
        else:
            collection_window = (
                (match.matched_signals or {}).get("collection_window") if match else None
            )
        decision = match_content(topic, content)
        included += int(decision.decision == "include")
        reviewed += int(decision.decision == "review")
        excluded += int(decision.decision == "exclude")
        _upsert_match(
            db,
            topic,
            content,
            decision,
            existing=match,
            collection_window=collection_window,
        )
    db.flush()
    return {"scanned": len(rows), "included": included, "review": reviewed, "excluded": excluded}
