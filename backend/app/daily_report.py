# ruff: noqa: E501
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .content_quality import is_reader_eligible
from .domain_assignments import active_domain_classifier
from .models import (
    ContentDomainAssignment,
    ContentItem,
    CrawlRun,
    Domain,
    Event,
    EventMember,
    InterestTopic,
    Source,
    TopicMatch,
)

REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ReportStory:
    event_id: int | None
    content_item_id: int
    title: str
    summary: str
    source_name: str
    source_count: int
    member_count: int
    published_at: datetime
    canonical_url: str | None
    body_chars: int
    language: str | None


@dataclass(frozen=True)
class DailyReportData:
    issue_date: date
    report_date: date
    domain_key: str
    domain_name: str
    stories: list[ReportStory]
    seven_day_story_count: int
    seven_day_source_count: int
    total_content_count: int
    content_without_media_count: int
    enabled_source_count: int
    succeeded_source_count: int
    failed_source_count: int
    never_run_source_count: int
    data_cutoff: datetime | None
    cluster_version: str | None
    editorial: dict | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _local(value: datetime) -> datetime:
    return _as_utc(value).astimezone(REPORT_TIMEZONE)


def _summary(content: ContentItem, limit: int = 260) -> str:
    value = " ".join((content.excerpt or content.body or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip("，。；;,. ") + "…"


def _source_health(session: Session) -> tuple[int, int, int, int]:
    sources = list(session.scalars(select(Source).where(Source.is_enabled.is_(True))))
    succeeded = failed = never = 0
    for source in sources:
        latest = session.scalar(
            select(CrawlRun.status)
            .where(CrawlRun.source_id == source.id)
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
            .limit(1)
        )
        if latest is None:
            never += 1
        elif latest in {"succeeded", "unchanged"}:
            succeeded += 1
        else:
            failed += 1
    return len(sources), succeeded, failed, never


def _published_domain_rows(
    session: Session, domain: Domain
) -> list[tuple[ContentItem, Source, Event | None]]:
    """Reader-ready domain content, grouped from the active domain classifier."""
    active_classifier = active_domain_classifier(domain)
    assignment_join = [
        ContentDomainAssignment.content_item_id == ContentItem.id,
        ContentDomainAssignment.domain_id == domain.id,
        ContentDomainAssignment.input_content_hash == ContentItem.content_hash,
        ContentDomainAssignment.decision == "include",
    ]
    if active_classifier is not None:
        assignment_join.extend(
            [
                ContentDomainAssignment.classifier_name == active_classifier[0],
                ContentDomainAssignment.classifier_version == active_classifier[1],
            ]
        )
    rows = list(
        session.execute(
            select(ContentItem, Source, Event)
            .join(Source, Source.id == ContentItem.source_id)
            .join(ContentDomainAssignment, and_(*assignment_join))
            .outerjoin(
                EventMember,
                (EventMember.content_item_id == ContentItem.id)
                & (EventMember.is_active.is_(True)),
            )
            .outerjoin(Event, (Event.id == EventMember.event_id) & (Event.status == "active"))
            .where(ContentItem.published_at.is_not(None))
            .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
        )
    )
    return [row for row in rows if is_reader_eligible(row[0])]


def available_report_dates(
    session: Session, *, domain_key: str
) -> list[tuple[date, int]]:
    """Coverage dates with reader-ready content; dates derive solely from publication time."""
    domain = session.scalar(select(Domain).where(Domain.key == domain_key))
    if domain is None:
        raise ValueError(f"Unknown domain: {domain_key}")
    counts: dict[date, int] = {}
    for content, _, _ in _published_domain_rows(session, domain):
        local_date = _local(content.published_at).date()
        counts[local_date] = counts.get(local_date, 0) + 1
    return sorted(counts.items(), reverse=True)


def _topic_published_rows(
    session: Session, topic: InterestTopic
) -> list[tuple[ContentItem, Source, TopicMatch]]:
    rows = list(
        session.execute(
            select(ContentItem, Source, TopicMatch)
            .join(Source, Source.id == ContentItem.source_id)
            .join(TopicMatch, TopicMatch.content_item_id == ContentItem.id)
            .where(
                TopicMatch.topic_id == topic.id,
                TopicMatch.decision == "include",
                TopicMatch.input_content_hash == ContentItem.content_hash,
                ContentItem.published_at.is_not(None),
            )
            .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
        )
    )
    return [
        row
        for row in rows
        if is_reader_eligible(row[0])
        and bool(((row[2].matched_signals or {}).get("collection_window") or {}).get("admitted"))
    ]


def available_topic_report_dates(
    session: Session, *, topic: InterestTopic
) -> list[tuple[date, int]]:
    counts: dict[date, int] = {}
    for content, _, _ in _topic_published_rows(session, topic):
        local_date = _local(content.published_at).date()
        counts[local_date] = counts.get(local_date, 0) + 1
    return sorted(counts.items(), reverse=True)


def collect_topic_daily_report(
    session: Session,
    *,
    topic: InterestTopic,
    coverage_date: date,
) -> DailyReportData:
    rows = [
        row
        for row in _topic_published_rows(session, topic)
        if _local(row[0].published_at).date() == coverage_date
    ]
    if not rows:
        raise ValueError(f"No published content for topic {topic.id} on coverage date {coverage_date}")
    stories = [
        ReportStory(
            event_id=None,
            content_item_id=content.id,
            title=content.title,
            summary=_summary(content),
            source_name=source.name,
            source_count=1,
            member_count=1,
            published_at=_local(content.published_at),
            canonical_url=content.canonical_url or content.original_url,
            body_chars=len(content.body or ""),
            language=content.language,
        )
        for content, source, _ in rows
    ]
    stories.sort(key=lambda item: (item.published_at, item.content_item_id), reverse=True)
    return DailyReportData(
        issue_date=coverage_date + timedelta(days=1),
        report_date=coverage_date,
        domain_key=f"topic-{topic.id}",
        domain_name=topic.name,
        stories=stories,
        seven_day_story_count=0,
        seven_day_source_count=0,
        total_content_count=0,
        content_without_media_count=0,
        enabled_source_count=0,
        succeeded_source_count=0,
        failed_source_count=0,
        never_run_source_count=0,
        data_cutoff=None,
        cluster_version=None,
    )


def collect_daily_report(
    session: Session,
    *,
    domain_key: str,
    coverage_date: date | None = None,
    latest: bool = False,
    issue_date: date | None = None,
) -> DailyReportData:
    if coverage_date is not None and latest:
        raise ValueError("coverage_date and latest are mutually exclusive")
    domain = session.scalar(select(Domain).where(Domain.key == domain_key))
    if domain is None:
        raise ValueError(f"Unknown domain: {domain_key}")
    rows = _published_domain_rows(session, domain)
    if not rows:
        raise ValueError(f"No published content available for domain: {domain_key}")
    effective_issue_date = issue_date or (
        coverage_date + timedelta(days=1)
        if coverage_date is not None
        else datetime.now(REPORT_TIMEZONE).date()
    )
    available_dates = [_local(content.published_at).date() for content, _, _ in rows]
    selected_date = (
        max(available_dates)
        if latest
        else coverage_date or effective_issue_date - timedelta(days=1)
    )
    window_start = datetime.combine(selected_date, time.min, tzinfo=REPORT_TIMEZONE)
    window_end = window_start + timedelta(days=1)
    selected_rows = [
        row
        for row in rows
        if window_start <= _local(row[0].published_at) < window_end
    ]
    if not selected_rows:
        raise ValueError(
            f"No published content for domain {domain_key} on coverage date {selected_date}"
        )

    grouped: dict[tuple[str, int], list[tuple[ContentItem, Source, Event | None]]] = {}
    for content, source, event in selected_rows:
        group_key = ("event", event.id) if event else ("content", content.id)
        grouped.setdefault(group_key, []).append((content, source, event))

    stories = []
    for group in grouped.values():
        event = group[0][2]
        representative = next(
            (
                row
                for row in group
                if event is not None and row[0].id == event.representative_content_id
            ),
            max(group, key=lambda row: (len(row[0].body or ""), -row[0].id)),
        )
        content, source, _ = representative
        if event is not None:
            member_rows = list(
                session.execute(
                    select(EventMember, ContentItem)
                    .join(ContentItem, ContentItem.id == EventMember.content_item_id)
                    .where(EventMember.event_id == event.id, EventMember.is_active.is_(True))
                )
            )
            member_count = len(member_rows)
            source_count = len({item.source_id for _, item in member_rows})
            title = event.canonical_title
        else:
            member_count = source_count = 1
            title = content.title
        stories.append(
            ReportStory(
                event_id=event.id if event else None,
                content_item_id=content.id,
                title=title,
                summary=_summary(content),
                source_name=source.name,
                source_count=source_count,
                member_count=member_count,
                published_at=_local(content.published_at),
                canonical_url=content.canonical_url or content.original_url,
                body_chars=len(content.body or ""),
                language=content.language,
            )
        )
    stories.sort(key=lambda item: (item.published_at, item.content_item_id), reverse=True)

    seven_day_start = selected_date - timedelta(days=6)
    seven_day_rows = [
        row
        for row in rows
        if seven_day_start <= _local(row[0].published_at).date() <= selected_date
    ]
    seven_day_groups = {
        ("event", event.id) if event else ("content", content.id)
        for content, _, event in seven_day_rows
    }
    enabled, succeeded, failed, never = _source_health(session)
    total_content = session.scalar(select(func.count(ContentItem.id))) or 0
    without_media = sum(not bool(item.media) for item in session.scalars(select(ContentItem)))
    cutoff = session.scalar(select(func.max(CrawlRun.finished_at)))
    cluster_version = session.scalar(
        select(Event.cluster_version)
        .where(Event.status == "active")
        .order_by(Event.updated_at.desc(), Event.id.desc())
        .limit(1)
    )
    return DailyReportData(
        issue_date=effective_issue_date,
        report_date=selected_date,
        domain_key=domain.key,
        domain_name=domain.name,
        stories=stories,
        seven_day_story_count=len(seven_day_groups),
        seven_day_source_count=len({source.id for _, source, _ in seven_day_rows}),
        total_content_count=total_content,
        content_without_media_count=without_media,
        enabled_source_count=enabled,
        succeeded_source_count=succeeded,
        failed_source_count=failed,
        never_run_source_count=never,
        data_cutoff=_local(cutoff) if cutoff else None,
        cluster_version=cluster_version,
    )


def _story_html(story: ReportStory, number: int, editorial: dict | None = None) -> str:
    url = html.escape(story.canonical_url or "#", quote=True)
    source = html.escape(story.source_name)
    title = html.escape(str((editorial or {}).get("chinese_title") or story.title))
    summary = html.escape(
        str((editorial or {}).get("chinese_summary") or story.summary or "来源信息有限。")
    )
    points = (editorial or {}).get("key_points") or []
    point_items = "".join(
        f"<li>{html.escape(str(point.get('text') or ''))}</li>"
        for point in points
        if point.get("text")
    )
    points_html = f'<ul class="story-points">{point_items}</ul>' if point_items else ""
    tags = (editorial or {}).get("tags") or []
    tag_items = "".join(
        f'<span>{html.escape(str(tag.get("label_zh") or ""))}</span>'
        for tag in tags
        if tag.get("label_zh")
    )
    tags_html = f'<div class="story-tags">{tag_items}</div>' if tag_items else ""
    evidence = f"{story.source_count} 个来源" if story.source_count > 1 else ""
    evidence_html = (
        f'<div class="story-meta"><span>{html.escape(evidence)}</span></div>'
        if evidence
        else ""
    )
    return f"""
      <article class="story" data-story="{number}">
        <div class="story-index">{number:02d}</div>
        <div class="story-copy">
          <h3><a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a></h3>
          <p>{summary}</p>
          {points_html}
          {tags_html}
          {evidence_html}
        </div>
        <div class="story-source">
          <cite>{source}</cite>
          <a href="{url}" target="_blank" rel="noopener noreferrer">原文 ↗</a>
        </div>
      </article>"""


def _section_html(
    title: str,
    intro: str,
    stories: list[ReportStory],
    start_number: int,
    editorial_by_story: dict[str, dict],
) -> str:
    if not stories:
        return ""
    items = "".join(
        _story_html(
            story,
            start_number + index,
            editorial_by_story.get(
                f"event:{story.event_id}" if story.event_id else f"content:{story.content_item_id}"
            ),
        )
        for index, story in enumerate(stories)
    )
    return f"""
    <section class="report-section">
      <div class="section-rule"><span></span><h2>{html.escape(title)}</h2><span></span></div>
      {f'<p class="section-intro">{html.escape(intro)}</p>' if intro else ''}
      <div class="stories">{items}</div>
    </section>"""


_CENSUS_LEAD = re.compile(
    r"本期(整理|收录).{0,48}(条|个).{0,24}(相关|已发布|独立来源)"
)


def _reader_kicker(value: object, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text or _CENSUS_LEAD.search(text):
        return fallback
    return text


def _primary_tag(story: dict) -> str:
    tags = story.get("tags") or []
    if not tags:
        return ""
    first = tags[0]
    if isinstance(first, dict):
        return str(first.get("label_zh") or "").strip()
    return str(first).strip()


def compose_topic_edition(topic_name: str, editorial: dict, ordered_refs: list[str]) -> dict:
    """Build a paper edition from topic story copy, without census leads."""
    stories = list(editorial.get("stories") or [])
    by_key = {str(item.get("story_key") or ""): item for item in stories if item.get("story_key")}
    ordered = [by_key[ref] for ref in ordered_refs if ref in by_key]
    if not ordered:
        return {
            **editorial,
            "daily_lead": {"deck": topic_name, "text": "", "story_refs": []},
            "sections": [],
            "stories": stories,
        }
    top = ordered[0]
    groups: dict[str, list[str]] = {}
    section_order: list[str] = []
    tagged = False
    for item in ordered:
        label = _primary_tag(item)
        tagged = tagged or bool(label)
        label = label or "要闻"
        if label not in groups:
            groups[label] = []
            section_order.append(label)
        groups[label].append(str(item["story_key"]))
    if not tagged and len(ordered) > 1:
        groups = {
            "要闻": [str(ordered[0]["story_key"])],
            "续闻": [str(item["story_key"]) for item in ordered[1:]],
        }
        section_order = ["要闻", "续闻"]
    return {
        **editorial,
        "daily_lead": {
            "deck": _reader_kicker(top.get("chinese_title"), topic_name),
            "text": _reader_kicker(top.get("chinese_summary")),
            "story_refs": [str(top["story_key"])],
        },
        "sections": [
            {"title": title, "intro": "", "story_refs": groups[title]}
            for title in section_order
        ],
        "stories": stories,
    }


def render_daily_report(data: DailyReportData) -> str:
    masthead_date = data.report_date.strftime("%Y.%m.%d")
    editorial = data.editorial or {}
    editorial_stories = {
        item.get("story_key"): item for item in editorial.get("stories", []) if item.get("story_key")
    }
    daily_lead = editorial.get("daily_lead") or {}
    deck = _reader_kicker(daily_lead.get("deck"), data.domain_name)
    lead = _reader_kicker(daily_lead.get("text"))
    story_by_key = {
        (f"event:{story.event_id}" if story.event_id else f"content:{story.content_item_id}"): story
        for story in data.stories
    }
    section_contract = editorial.get("sections") or []
    if section_contract:
        ordered_refs = [
            ref for section in section_contract for ref in (section.get("story_refs") or [])
        ]
        if len(ordered_refs) != len(set(ordered_refs)) or set(ordered_refs) != set(story_by_key):
            raise ValueError("Editorial sections must contain every report story exactly once")
        section_parts = []
        next_number = 1
        for index, section in enumerate(section_contract, start=1):
            section_stories = [story_by_key[ref] for ref in section.get("story_refs") or []]
            numeral = "一二三四五六七八九十"
            prefix = numeral[index - 1] if index <= len(numeral) else str(index)
            section_parts.append(
                _section_html(
                    f"{prefix}、{section.get('title') or '要闻'}",
                    str(section.get("intro") or ""),
                    section_stories,
                    next_number,
                    editorial_stories,
                )
            )
            next_number += len(section_stories)
        sections = "".join(section_parts)
    else:
        sections = _section_html("一、要闻", "", data.stories, 1, editorial_stories)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Navigate · {html.escape(data.domain_name)} · {masthead_date}</title>
  <style>
    :root {{
      --paper: #f7f2e8;
      --paper-deep: #eee6d8;
      --ink: #24201d;
      --muted: #817972;
      --accent: #722f37;
      --rule: #d8cdbb;
      --serif: "Songti SC", "STSong", "Noto Serif CJK SC", Georgia, serif;
      --sans: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: #e6dfd4; }}
    body {{ margin: 0; color: var(--ink); font-family: var(--serif); background:
      radial-gradient(circle at 1px 1px, rgba(69,52,42,.055) .7px, transparent .8px) 0 0/7px 7px,
      #e6dfd4; }}
    a {{ color: inherit; text-decoration: none; }}
    a:focus-visible, button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 4px; }}
    .toolbar {{ position: fixed; z-index: 5; top: 18px; right: 22px; display: flex; gap: 8px; }}
    .toolbar button {{ border: 1px solid rgba(36,32,29,.18); background: rgba(247,242,232,.92);
      color: var(--ink); padding: 9px 13px; font: 600 12px var(--sans); letter-spacing: .08em;
      cursor: pointer; backdrop-filter: blur(8px); transition: transform .18s ease, border-color .18s ease; }}
    .toolbar button:hover {{ transform: translateY(-1px); border-color: var(--accent); }}
    .sheet {{ width: min(1040px, calc(100% - 36px)); margin: 30px auto 64px; padding: 58px 62px 44px;
      background: var(--paper); box-shadow: 0 20px 70px rgba(69,52,42,.16); }}
    .masthead {{ text-align: center; border-top: 4px double var(--accent);
      border-bottom: 4px double var(--accent); padding: 28px 16px 24px; }}
    .masthead .mark {{ display: block; width: 36px; height: 36px; margin: 0 auto 12px; }}
    .eyebrow {{ margin: 0 0 12px; color: var(--accent); font: 700 12px var(--serif);
      letter-spacing: .22em; }}
    h1 {{ margin: 0; font-size: clamp(36px, 6vw, 64px); line-height: 1.08; letter-spacing: .06em;
      font-weight: 800; text-wrap: balance; }}
    .deck {{ margin: 14px 0 0; color: #625b55; font-size: clamp(15px, 2vw, 20px);
      font-style: italic; letter-spacing: .08em; }}
    .edition {{ margin-top: 16px; display: flex; justify-content: center; flex-wrap: wrap; gap: 8px 24px;
      color: var(--muted); font: 12px var(--sans); letter-spacing: .06em; }}
    .lead {{ padding: 28px clamp(4px, 5vw, 64px); border-bottom: 2px solid var(--accent); text-align: center; }}
    .lead p {{ margin: 0; font-size: clamp(16px, 2vw, 21px); line-height: 1.9; font-style: italic;
      color: #5e5751; text-wrap: pretty; }}
    .report-section {{ margin-top: 8px; }}
    .section-rule {{ display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 18px;
      padding: 18px 0 5px; }}
    .section-rule span {{ height: 1px; background: var(--rule); }}
    .section-rule h2 {{ margin: 0; color: var(--accent); font-size: 22px; letter-spacing: .12em; }}
    .story {{ display: grid; grid-template-columns: 42px minmax(0,1fr) 150px; gap: 16px;
      padding: 23px 10px 20px; border-bottom: 1px dashed var(--rule); }}
    .story-index {{ color: var(--accent); font-weight: 800; padding-top: 4px; letter-spacing: .08em; }}
    .story h3 {{ margin: 0; font-size: 19px; line-height: 1.55; text-wrap: pretty; }}
    .story h3 a {{ background: linear-gradient(var(--accent),var(--accent)) 0 100%/0 1px no-repeat;
      transition: background-size .2s ease; }}
    .story h3 a:hover {{ color: var(--accent); background-size: 100% 1px; }}
    .story p {{ margin: 7px 0 0; color: #6d655e; font-size: 14px; line-height: 1.75; text-wrap: pretty; }}
    .story-points {{ margin: 9px 0 0; padding: 0; list-style: none; color: #554d47;
      font-size: 12px; line-height: 1.7; }}
    .story-points li {{ position: relative; padding-left: 14px; }}
    .story-points li::before {{ content: ""; position: absolute; left: 1px; top: .78em;
      width: 5px; height: 1px; background: var(--accent); }}
    .story-tags {{ margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }}
    .story-tags span {{ padding: 3px 7px; border: 1px solid rgba(114,47,55,.26); color: var(--accent);
      font: 10px var(--sans); letter-spacing: .04em; }}
    .section-intro {{ max-width: 760px; margin: 8px auto 0; color: var(--muted); text-align: center;
      font-size: 13px; line-height: 1.7; font-style: italic; }}
    .story-meta {{ margin-top: 9px; display: flex; flex-wrap: wrap; gap: 7px 14px; color: #9a9087;
      font: 10px var(--sans); letter-spacing: .05em; }}
    .story-source {{ text-align: right; padding-top: 4px; }}
    .story-source cite {{ display: block; color: var(--muted); font-size: 12px; line-height: 1.5; }}
    .story-source a {{ display: inline-block; margin-top: 7px; color: var(--accent); font-size: 12px; }}
    footer {{ margin-top: 26px; padding-top: 18px; border-top: 1px solid var(--rule); color: #92877e;
      font: 10px/1.7 var(--sans); text-align: center; letter-spacing: .04em; }}
    @media (max-width: 720px) {{
      .sheet {{ width: 100%; margin: 0; padding: 38px 20px 28px; box-shadow: none; }}
      .toolbar {{ position: sticky; top: 0; justify-content: flex-end; padding: 10px; background: #e6dfd4; }}
      .story {{ grid-template-columns: 32px minmax(0,1fr); gap: 10px; padding-inline: 0; }}
      .story-source {{ grid-column: 2; text-align: left; display: flex; align-items: baseline; gap: 10px; }}
      .story-source cite {{ display: inline; }}
    }}
    @media print {{
      @page {{ size: A4; margin: 13mm; }}
      html, body {{ background: var(--paper); }}
      .toolbar {{ display: none !important; }}
      .sheet {{ width: auto; margin: 0; padding: 0; box-shadow: none; }}
      .story {{ break-inside: avoid; }}
      .report-section {{ break-inside: auto; }}
      a {{ color: inherit; }}
    }}
    @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
  </style>
</head>
<body>
  <div class="toolbar" aria-label="报告操作">
    <button type="button" onclick="window.print()">打印 / 存为 PDF</button>
  </div>
  <main class="sheet">
    <header class="masthead">
      <svg class="mark" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" aria-hidden="true">
        <path fill="#722F37" d="M4.8 2.2h4.2v10.4L15.8 2.2H20.2v19.6h-4.2V11.4L9 21.8H4.8V2.2z"/>
      </svg>
      <p class="eyebrow">Navigate · 每日简报</p>
      <h1>{html.escape(data.domain_name)}</h1>
      {f'<p class="deck">{html.escape(deck)}</p>' if deck else ''}
      <div class="edition"><span>{data.issue_date.strftime('%Y 年 %m 月 %d 日')}出版</span></div>
    </header>
    {f'<section class="lead"><p>{html.escape(lead)}</p></section>' if lead else ''}
    {sections}
    <footer>原文版权归各来源所有</footer>
  </main>
</body>
</html>"""


def write_daily_report(data: DailyReportData, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(render_daily_report(data), encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path
