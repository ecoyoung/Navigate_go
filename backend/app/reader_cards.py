from __future__ import annotations

import html
import re
import unicodedata

from zhconv import convert as zh_convert

from .content_quality import quality_tier
from .event_signature import clean_title, distinctive_tokens
from .models import ContentItem, Source

MAX_CARD_PARAGRAPHS = 3
MAX_CARD_CHARS = 480
MIN_SENTENCE_CHARS = 18
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=[.!?])\s+(?=[A-Z\"“‘\u3400-\u9fff])")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)|!\[[^\]]*\]\(<[^>]+>\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]+\)")
_BARE_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_TRACKING = re.compile(
    r"(?:ck|m|sha256|pac_uid|openid|uid)=[A-Za-z0-9_+\-/%.=]+",
    re.IGNORECASE,
)
_JSON_BLOB = re.compile(r"\{[^{}]{0,500}\}")
_HTML_TAG = re.compile(r"<[^>]+>")
_BYLINE_NAME = (
    r"(?:[\u3400-\u9fff]{2,4}(?:·[\u3400-\u9fff]{1,4})?|[A-Za-z][A-Za-z. '\-]{1,24})"
)
_BYLINE_ROLE = r"(?:作者|编辑|记者|责任编辑|撰文|采访)"
_BYLINE_GUARD = rf"(?=\s|{_BYLINE_ROLE}|[，,。．.;；]|$|获悉|近日|据|国家|本月|最新)"
_BYLINE_CHUNK = re.compile(
    r"(?:"
    r"图源\s*[/／:：]\s*\S{1,20}|"
    r"图片来源\s*[/／:：]\s*\S{1,20}|"
    r"本文(?:约|共)?\s*\d[\d,，.]*\s*字|"
    r"本文共字|"
    r"(?:建议)?阅读(?:时间)?\s*\d+\s*分钟|"
    r"预计阅读时间|"
    r"(?:^|(?<=\s))导语(?=\s|$)|"
    r"(?:文章来源|来源)\s*[:：]\s*\S{1,20}|"
    rf"{_BYLINE_ROLE}\s*[丨|/／|:：]\s*{_BYLINE_NAME}|"
    rf"(?:摄影|视觉|设计)\s*[丨|/／]\s*{_BYLINE_NAME}|"
    rf"(?:^|(?<=\s))文\s*[丨|/／|:：]\s*{_BYLINE_NAME}{_BYLINE_GUARD}"
    r")",
    re.I,
)
_HEADING = re.compile(r"^#{1,6}\s+")
_BREADCRUMB = re.compile(
    r"(?:广告\s*)?(?:首页|资讯|新闻|正文|话题)"
    r"(?:\s*[>／/]\s*[\w\u3400-\u9fff]+){1,8}\s*(?:正文\s*)?"
)
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?\s*")
_CUTOFF = re.compile(
    r"^(相关推荐|免责声明|举报|当前您处于未登录|下载安装|立即下载|Document)\b"
)
_CHROME_LINE = re.compile(
    r"^(首页|要闻|北京|科技|财经|更多|搜索|登录|游戏|网页设置|广告|关注|评论|分享|"
    r"复制链接|微信好友|QQ好友|手机看|正在浏览|内容更精彩|安装电脑版|刷新|反馈|"
    r"无障碍|广告设置|顶部|侵权投诉|提示|更多|问AI.*|蓝鲨硬科技|元宝.?新闻妹)$"
)
_CHROME_TEXT = re.compile(
    r"(首页|要闻|网页设置|安装电脑版|内容更精彩|复制链接|微信好友|QQ好友|"
    r"正在浏览|相关推荐|免责声明|未登录状态下|个性化广告|立即下载|问AI)"
)
_NAV_LINK_TEXT = {
    "首页",
    "要闻",
    "北京",
    "科技",
    "财经",
    "ai",
    "更多",
    "刷新",
    "反馈",
    "无障碍",
    "侵权投诉",
    "关注",
    "评论",
    "分享",
    "登录",
    "搜索",
    "账号设置",
    "我的关注",
    "我的收藏",
    "申请的项目",
    "退出登录",
}


def unwrap_editorial(artifact: dict | None, content_id: int | None = None) -> dict:
    if not artifact:
        return {}
    keys = ("chinese_title", "chinese_summary", "summary_units", "tags_zh")
    if any(artifact.get(key) for key in keys):
        return artifact
    items = artifact.get("items")
    if not isinstance(items, list):
        return artifact
    if content_id is not None:
        wanted = f"content:{content_id}"
        for item in items:
            if isinstance(item, dict) and item.get("content_ref") == wanted:
                return item
    if len(items) == 1 and isinstance(items[0], dict):
        return items[0]
    return artifact


def editorial_title(artifact: dict, fallback: str) -> str:
    title = artifact.get("chinese_title")
    if isinstance(title, str) and title.strip():
        return html.unescape(title.strip())
    return clean_title(fallback) or html.unescape(fallback)


def editorial_paragraphs(artifact: dict) -> list[str]:
    units = artifact.get("summary_units") or []
    texts = [
        html.unescape(str(unit.get("text_zh") or "").strip())
        for unit in units
        if isinstance(unit, dict) and str(unit.get("text_zh") or "").strip()
    ]
    if texts:
        return _cap_paragraphs(texts)
    summary = artifact.get("chinese_summary")
    if isinstance(summary, str) and summary.strip():
        return _cap_paragraphs([html.unescape(summary.strip())])
    return []


def editorial_tags(artifact: dict) -> list[str]:
    tags = artifact.get("tags_zh")
    if isinstance(tags, list):
        labels = [str(tag).strip() for tag in tags if isinstance(tag, str) and str(tag).strip()]
        if labels:
            return labels[:8]
        labels = [
            str(tag.get("label_zh") or tag.get("tag_key") or "").strip()
            for tag in tags
            if isinstance(tag, dict)
        ]
        return [item for item in labels if item][:8]
    tags = artifact.get("tags")
    if isinstance(tags, list):
        labels = [
            str(tag.get("label_zh") or tag.get("tag_key") or tag).strip()
            for tag in tags
            if tag
        ]
        return [item for item in labels if item][:8]
    return []


def _keep_link_text(text: str) -> str:
    cleaned = html.unescape(text or "").strip()
    if not cleaned or cleaned.casefold() in _NAV_LINK_TEXT:
        return ""
    if _BARE_URL.search(cleaned) or cleaned.startswith("http"):
        return ""
    return cleaned


def _clean_markup(value: str) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", value or ""))
    text = zh_convert(text, "zh-cn")
    text = _HTML_TAG.sub(" ", text)
    text = _MD_IMAGE.sub(" ", text)
    text = _MD_LINK.sub(lambda match: _keep_link_text(match.group(1)), text)
    text = _BARE_URL.sub(" ", text)
    text = _TRACKING.sub(" ", text)
    text = _JSON_BLOB.sub(" ", text)
    text = _BYLINE_CHUNK.sub(" ", text)
    text = text.replace("<Base64-Image-Removed>", " ")
    text = text.replace("Base64-Image-Removed", " ")
    return text


def _is_chrome_line(line: str) -> bool:
    compact = re.sub(r"\s+", "", line)
    if not compact:
        return True
    if _CHROME_LINE.match(line) or line.startswith("正在浏览"):
        return True
    if _CUTOFF.match(line):
        return True
    if re.fullmatch(r"\d{1,2}/\d{1,2}", compact):
        return True
    if re.fullmatch(r"[\d\W_]{1,12}", compact):
        return True
    if len(compact) <= 24 and _CHROME_TEXT.search(line):
        return True
    if any(token in line for token in ("可点击分享", "扫码分享", "微信扫一扫", "随时随地看")):
        return True
    if "发布于" in line and len(compact) <= 24:
        return True
    if "新闻妹" in line or ("元宝" in line and len(compact) <= 16):
        return True
    if "http" in line.casefold() or "ck=" in line or "sha256=" in line:
        return True
    if _looks_like_menu(line):
        return True
    return False


def _looks_like_menu(line: str) -> bool:
    compact = re.sub(r"\s+", "", line)
    if any(mark in line for mark in "。！？，、；;,.!?"):
        return False
    if len(compact) <= 16 and "，" not in line:
        return True
    tokens = re.findall(r"[\u3400-\u9fff]{2,8}|[A-Za-z]{3,}", line)
    return len(tokens) >= 3 and len(compact) <= 140


def _strip_byline_chunks(text: str) -> str:
    current = text or ""
    for _ in range(8):
        updated = _BYLINE_CHUNK.sub(" ", current)
        if updated == current:
            break
        current = updated
    current = re.sub(r"\s+", " ", current).strip()
    current = re.sub(r"^[\s,，、;；:：/／|丨。.\-•*]+", "", current)
    current = re.sub(r"[\s,，、;；:：/／|丨•*\-]+$", "", current)
    return current.strip()


def sanitize_article_text(value: str | None, *, title: str | None = None) -> str:
    raw = _clean_markup(value or "")
    kept: list[str] = []
    for original in raw.splitlines():
        line = _HEADING.sub("", original).strip(" \t-•*[]")
        line = _BREADCRUMB.sub(" ", line)
        line = _DATE_PREFIX.sub("", line)
        line = _strip_byline_chunks(line)
        line = re.sub(r"\s+", " ", line).strip()
        if _CUTOFF.match(line):
            break
        if _is_chrome_line(line):
            continue
        if title and line in {title, clean_title(title)}:
            continue
        kept.append(line)
    text = _strip_byline_chunks(" ".join(kept))
    text = re.sub(r"\s+", " ", text).strip()
    if title:
        cleaned_title = clean_title(title) or title
        if text.startswith(cleaned_title):
            text = text[len(cleaned_title) :].lstrip(" ：:，,")
        elif text.startswith(title):
            text = text[len(title) :].lstrip(" ：:，,")
    return _strip_byline_chunks(text)


def _is_chrome_sentence(sentence: str) -> bool:
    if _BREADCRUMB.search(sentence):
        return True
    if _CHROME_TEXT.search(sentence) and len(sentence) < 80:
        return True
    if _BARE_URL.search(sentence) or "ck=" in sentence or "sha256=" in sentence:
        return True
    if sentence.count("http") >= 1:
        return True
    if re.search(r"\]\(|!\[", sentence):
        return True
    residue = re.sub(r"[\s\W_]+", "", _strip_byline_chunks(sentence), flags=re.UNICODE)
    if not residue:
        return True
    return False


def _sentences(value: str) -> list[str]:
    value = _strip_byline_chunks(value)
    parts = [
        part.strip(" \n\t·-")
        for part in _SENTENCE_SPLIT.split(value)
        if part and part.strip()
    ]
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        part = _strip_byline_chunks(_DATE_PREFIX.sub("", part))
        if _is_chrome_sentence(part):
            continue
        if len(part) < MIN_SENTENCE_CHARS:
            continue
        if part in seen:
            continue
        seen.add(part)
        cleaned.append(part)
    return cleaned


def _cap_paragraphs(paragraphs: list[str]) -> list[str]:
    result: list[str] = []
    used = 0
    for paragraph in paragraphs:
        text = _strip_byline_chunks(re.sub(r"\s+", " ", paragraph))
        if not text or _is_chrome_sentence(text):
            continue
        remaining = MAX_CARD_CHARS - used
        if remaining <= 24:
            break
        if len(text) > remaining:
            text = text[: remaining - 1].rstrip() + "…"
        result.append(text)
        used += len(text)
        if len(result) >= MAX_CARD_PARAGRAPHS:
            break
    return result


def extractive_paragraphs(content: ContentItem) -> list[str]:
    title = clean_title(content.title) or sanitize_article_text(content.title)
    excerpt = sanitize_article_text(content.excerpt, title=title)
    body = sanitize_article_text(content.body, title=title)
    chosen: list[str] = []
    title_tokens = distinctive_tokens(title)

    def consider(sentences: list[str]) -> None:
        for sentence in sentences:
            if sentence in chosen or sentence == title:
                continue
            if title and sentence.startswith(title[:12]):
                continue
            chosen.append(sentence)
            if len(chosen) >= MAX_CARD_PARAGRAPHS:
                return

    consider(_sentences(excerpt))
    if len(chosen) >= MAX_CARD_PARAGRAPHS:
        return _cap_paragraphs(chosen)

    body_sentences = [
        item
        for item in _sentences(body)
        if item not in chosen and item != title
    ]
    ranked = sorted(
        body_sentences,
        key=lambda sentence: (
            len(distinctive_tokens(sentence) & title_tokens),
            min(len(sentence), 160),
        ),
        reverse=True,
    )
    consider([sentence for sentence in body_sentences if sentence in set(ranked[:6])])
    if not chosen and body:
        consider(_sentences(body)[:MAX_CARD_PARAGRAPHS] or [body[:MAX_CARD_CHARS]])
    return _cap_paragraphs(chosen)


def card_paragraphs(content: ContentItem, artifact: dict | None) -> list[str]:
    unwrapped = unwrap_editorial(artifact, content.id)
    paragraphs = editorial_paragraphs(unwrapped)
    if paragraphs:
        return paragraphs
    return extractive_paragraphs(content)


def build_reader_card(content: ContentItem, source: Source, artifact: dict | None) -> dict:
    unwrapped = unwrap_editorial(artifact, content.id)
    paragraphs = card_paragraphs(content, unwrapped)
    excerpt = " ".join(paragraphs) if paragraphs else content.excerpt
    return {
        "content_id": content.id,
        "title": editorial_title(unwrapped, content.title),
        "excerpt": excerpt,
        "paragraphs": paragraphs,
        "source_name": source.name,
        "url": content.canonical_url or content.original_url,
        "published_at": content.published_at,
        "discovered_at": content.discovered_at,
        "language": content.language,
        "tags": editorial_tags(unwrapped),
        "quality_tier": quality_tier(content),
        "reader_eligible": quality_tier(content) in {"verified_full", "partial"},
    }
