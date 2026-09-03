from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass

from zhconv import convert as zh_convert

from .models import ContentItem

_SOURCE_TAIL = re.compile(
    r"(?:[\s\u3000]*[-–—_|｜/][\s\u3000]*)+(?:[^|-–—_|｜/]{1,24}(?:新闻|新聞|活动|活動|首发|原創|原创|知乎|搜狐|腾讯|时间线|报道|報導))?$"
)
_SITE_SPLIT = re.compile(r"\s*[|｜]\s*")
_DASH_SITE = re.compile(
    r"[\s\u3000]*[-–—_]+\s*[\w\u3400-\u9fff]{1,20}(?:新闻|新聞|活动|活動|首发|知乎|搜狐|时间线)?\s*$"
)
_HTML_TAG = re.compile(r"<[^>]+>")
_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_UNITS = {
    "亿": 100_000_000,
    "億": 100_000_000,
    "万": 10_000,
    "萬": 10_000,
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "b": 1_000_000_000,
    "million": 1_000_000,
    "mm": 1_000_000,
    "m": 1_000_000,
}
_AMOUNT_RE = re.compile(
    r"(?:us\$|usd|\$|€|£)?\s*(?P<word>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+(?:\.\d+)?)\s*(?P<unit>亿美元|亿美金|亿元|亿|億|万元|万|萬|billion|million|bn|mm|[bm])(?=$|[^a-z0-9])",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_LATIN_WORD = re.compile(r"[a-z][a-z0-9-]{2,}")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]{2,}")
_QUOTED_NAME = re.compile(r"[「“\"']([^」”\"']{2,20})[」”\"']")
_GENERIC_TOKENS = {
    "ai",
    "news",
    "global",
    "latest",
    "world",
    "daily",
    "update",
    "updates",
    "market",
    "company",
    "deal",
    "dollar",
    "dollars",
    "round",
    "new",
    "year",
    "month",
    "today",
    "report",
    "reports",
    "says",
    "after",
    "into",
    "with",
    "from",
    "over",
    "under",
    "more",
    "bigger",
    "piece",
    "win",
    "wants",
    "leans",
    "beauty",
    "economy",
    "industry",
    "startup",
    "startups",
    "robot",
    "robots",
    "intelligence",
    "embodied",
    "humanoid",
    "ingredient",
    "ingredients",
    "actives",
    "cosmetics",
    "files",
    "file",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "融资",
    "投资",
    "收购",
    "并购",
    "发布",
    "推出",
    "上市",
    "营收",
    "净利润",
    "具身智能",
    "人形机器人",
    "人工智能",
    "机器人",
    "原料",
    "美妆",
    "化妆品",
    "亿美元",
    "万元",
    "数千万元",
    "公司",
    "企业",
    "市场",
    "行业",
    "报道",
    "新闻",
    "观察",
    "盘点",
    "赛道",
    "场景",
    "数据",
    "趋势",
    "考量",
    "商业化",
    "产业化",
    "长期主义",
    "细分赛道",
    "国货品牌",
    "开放平台",
    "personal",
    "care",
    "club",
    "expand",
    "expands",
    "reach",
}
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "its",
    "are",
    "was",
    "were",
    "has",
    "have",
    "had",
    "will",
    "into",
    "onto",
    "over",
    "under",
    "after",
    "before",
    "about",
    "against",
    "between",
    "through",
    "during",
    "without",
    "within",
    "in",
    "on",
    "at",
    "to",
    "of",
    "by",
    "a",
    "an",
    "is",
    "be",
    "as",
    "or",
    "的",
    "了",
    "在",
    "是",
    "与",
    "和",
    "及",
    "以",
    "将",
    "获",
    "称",
    "为",
    "对",
    "中",
    "并",
    "等",
    "就",
    "也",
    "而",
    "其",
    "被",
    "由",
}
_ACTION_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("acquisition", ("收购", "并购", "acquire", "acquires", "acquired", "acquisition", "buyout")),
    ("funding", ("融资", "获投", "funding", "financing", "raises", "raised", "raise", "series")),
    ("launch", ("发布", "推出", "上线", "launch", "launches", "launched", "unveil", "debut")),
    ("earnings", ("营收", "财报", "净利润", "earnings", "revenue", "profit")),
    ("listing", ("上市", "ipo", "lists", "listed")),
)
_ALIAS_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("具身智能", ("embodied ai", "embodied intelligence")),
    ("人形机器人", ("humanoid robot", "humanoid robots")),
    ("人工智能", ("artificial intelligence",)),
)


@dataclass(frozen=True)
class EventSignature:
    title: str
    tokens: frozenset[str]
    distinctive: frozenset[str]
    amounts: frozenset[int]
    actions: frozenset[str]


def normalize_signature_text(value: str | None) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", value or ""))
    text = _HTML_TAG.sub(" ", text)
    text = zh_convert(text, "zh-cn")
    text = text.replace("\u3000", " ").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", text).strip()


def clean_title(value: str | None) -> str:
    text = normalize_signature_text(value)
    if not text:
        return ""
    if _SITE_SPLIT.search(text):
        left = _SITE_SPLIT.split(text, maxsplit=1)[0].strip()
        if len(left) >= 8:
            text = left
    text = _SOURCE_TAIL.sub("", text).strip()
    text = _DASH_SITE.sub("", text).strip()
    return text or normalize_signature_text(value)


def _canonical_phrases(text: str) -> str:
    lowered = text.casefold()
    replacements: list[tuple[int, int, str]] = []
    for canonical, english in _ALIAS_PAIRS:
        variants = (canonical, *english)
        for variant in variants:
            needle = variant.casefold()
            start = 0
            while True:
                index = lowered.find(needle, start)
                if index < 0:
                    break
                replacements.append((index, index + len(needle), canonical))
                start = index + max(len(needle), 1)
    if not replacements:
        return lowered
    replacements.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    merged: list[tuple[int, int, str]] = []
    for start, end, token in replacements:
        if merged and start < merged[-1][1]:
            continue
        merged.append((start, end, token))
    parts: list[str] = []
    cursor = 0
    for start, end, token in merged:
        parts.append(lowered[cursor:start])
        parts.append(f" {token} ")
        cursor = end
    parts.append(lowered[cursor:])
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def extract_amounts(value: str | None) -> set[int]:
    text = normalize_signature_text(value)
    if not text:
        return set()
    amounts: set[int] = set()
    for match in _AMOUNT_RE.finditer(text):
        raw_number = match.group("word").casefold()
        number = float(_WORD_NUMBERS.get(raw_number, raw_number))
        unit = match.group("unit").casefold()
        if unit.startswith("亿") or unit.startswith("億"):
            unit = "亿"
        elif unit.startswith("万") or unit.startswith("萬"):
            unit = "万"
        magnitude = _UNITS.get(unit)
        if magnitude is None:
            continue
        amount = int(round(number * magnitude))
        if 1_000 <= amount <= 10_000_000_000_000:
            amounts.add(amount)
    return amounts


def _actions(text: str) -> set[str]:
    blob = text.casefold()
    found = set()
    for name, terms in _ACTION_CLASSES:
        if any(term.casefold() in blob for term in terms):
            found.add(name)
    return found


def _strip_generics(text: str) -> str:
    stripped = text
    for token in sorted(_GENERIC_TOKENS, key=len, reverse=True):
        if len(token) >= 2:
            stripped = stripped.replace(token, " ")
    return re.sub(r"\s+", " ", stripped).strip()


def _cjk_name_tokens(run: str) -> set[str]:
    if run in _GENERIC_TOKENS or run in _STOPWORDS:
        return set()
    if not (3 <= len(run) <= 12):
        return set()
    tokens = {run}
    if len(run) >= 6:
        tokens.add(run[:4])
        tokens.add(run[-4:])
    return {token for token in tokens if token not in _GENERIC_TOKENS}


def distinctive_tokens(value: str | None) -> set[str]:
    text = _canonical_phrases(clean_title(value) or normalize_signature_text(value))
    tokens: set[str] = set()
    for quoted in _QUOTED_NAME.findall(text):
        cleaned = quoted.casefold().strip()
        if cleaned and cleaned not in _GENERIC_TOKENS:
            tokens.add(cleaned)
    for word in _LATIN_WORD.findall(text.casefold()):
        if word not in _STOPWORDS and word not in _GENERIC_TOKENS:
            tokens.add(word)
    remainder = _strip_generics(text)
    for run in _CJK_RUN.findall(remainder):
        tokens.update(_cjk_name_tokens(run))
    return tokens


def all_signature_tokens(value: str | None) -> set[str]:
    text = _canonical_phrases(normalize_signature_text(value))
    tokens = distinctive_tokens(text)
    for word in _LATIN_WORD.findall(text.casefold()):
        tokens.add(word)
    for run in _CJK_RUN.findall(text):
        if len(run) >= 2:
            tokens.add(run)
    return {token for token in tokens if token not in _STOPWORDS}


def lead_text(item: ContentItem, chars: int) -> str:
    return normalize_signature_text((item.excerpt or item.body or "")[:chars])


def item_signature(item: ContentItem, *, lead_chars: int = 420) -> EventSignature:
    title = clean_title(item.title)
    lead = lead_text(item, lead_chars)
    blob = f"{title} {lead}"
    return EventSignature(
        title=title,
        tokens=frozenset(all_signature_tokens(blob)),
        distinctive=frozenset(distinctive_tokens(title)),
        amounts=frozenset(extract_amounts(blob)),
        actions=frozenset(_actions(blob)),
    )


def amount_conflict(left: set[int] | frozenset[int], right: set[int] | frozenset[int]) -> bool:
    if not left or not right:
        return False
    for first in left:
        for second in right:
            bigger = max(first, second)
            if bigger == 0:
                continue
            if abs(first - second) / bigger <= 0.08:
                return False
    return True


def signature_jaccard(left: EventSignature, right: EventSignature) -> float:
    union = left.tokens | right.tokens
    if not union:
        return 0.0
    return len(left.tokens & right.tokens) / len(union)


def signature_core_match(left: EventSignature, right: EventSignature) -> bool:
    if amount_conflict(left.amounts, right.amounts):
        return False
    if left.actions and right.actions and left.actions.isdisjoint(right.actions):
        return False
    shared = left.distinctive & right.distinctive
    if len(shared) >= 2:
        return True
    if len(shared) == 1:
        token = next(iter(shared))
        overlapping = left.actions & right.actions if left.actions and right.actions else set()
        same_action = bool(overlapping)
        if len(token) >= 4 and same_action and signature_jaccard(left, right) >= 0.28:
            return True
    return False
