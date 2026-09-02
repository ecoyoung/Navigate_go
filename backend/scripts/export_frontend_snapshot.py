import argparse
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import distinct, func, select

from app.database import SessionLocal
from app.models import (
    ContentItem,
    ContentValueScore,
    ContentValueScoreRun,
    Domain,
    Event,
    EventMember,
    LLMProcessingResult,
    Source,
)

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "frontend/public/data/home.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a read-only frontend data snapshot.")
    parser.add_argument("--domain", default="beauty")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _editorial_map(session) -> dict[int, dict]:
    results: dict[int, dict] = {}
    rows = session.scalars(
        select(LLMProcessingResult)
        .where(
            LLMProcessingResult.subject_type == "content_item",
            LLMProcessingResult.task_name == "content_editorial_zh",
            LLMProcessingResult.status == "succeeded",
        )
        .order_by(LLMProcessingResult.id.desc())
    )
    for row in rows:
        try:
            content_id = int(row.subject_key.removeprefix("content:"))
        except ValueError:
            continue
        results.setdefault(content_id, row.output or {})
    return results


def _has_chinese(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _display_copy(item: ContentItem, editorial: dict) -> tuple[str, str] | None:
    summary_units = editorial.get("summary_units") or []
    editorial_summary = "".join(
        str(unit.get("text_zh") or "") for unit in summary_units[:2]
    ).strip()
    editorial_title = str(editorial.get("chinese_title") or "").strip()
    current_editorial = editorial.get("input_content_hash") == item.content_hash
    if current_editorial and _has_chinese(editorial_title) and _has_chinese(editorial_summary):
        return editorial_title, editorial_summary
    if (item.language or "").lower().startswith("zh"):
        title = (item.title or "").strip()
        summary = (item.excerpt or item.body or "").strip()[:240]
        if _has_chinese(title) and _has_chinese(summary):
            return title, summary
    return None


def _daily_lead(session, domain_key: str) -> dict:
    row = session.scalar(
        select(LLMProcessingResult)
        .where(
            LLMProcessingResult.subject_type == "daily_report",
            LLMProcessingResult.subject_key.like(f"{domain_key}:%"),
            LLMProcessingResult.task_name == "daily_edition_zh",
            LLMProcessingResult.status == "succeeded",
        )
        .order_by(LLMProcessingResult.id.desc())
        .limit(1)
    )
    if row is None:
        return {}
    lead = dict((row.output or {}).get("daily_lead", {}))
    coverage_date = date.fromisoformat(row.subject_key.rsplit(":", 1)[-1])
    lead["coverage_date"] = coverage_date.isoformat()
    lead["publication_date"] = (coverage_date + timedelta(days=1)).isoformat()
    return lead


def main() -> None:
    args = parse_args()
    with SessionLocal() as session:
        domain = session.scalar(select(Domain).where(Domain.key == args.domain))
        if domain is None:
            raise SystemExit(f"unknown domain: {args.domain}")
        score_run = session.scalar(
            select(ContentValueScoreRun)
            .where(
                ContentValueScoreRun.domain_id == domain.id,
                ContentValueScoreRun.status == "succeeded",
            )
            .order_by(ContentValueScoreRun.as_of.desc(), ContentValueScoreRun.id.desc())
            .limit(1)
        )
        if score_run is None:
            raise SystemExit(f"no successful value score run for domain: {args.domain}")

        current_event = dict(
            session.execute(
                select(EventMember.content_item_id, EventMember.event_id).where(
                    EventMember.is_active.is_(True)
                )
            ).all()
        )
        event_stats = {
            event_id: {"member_count": member_count, "source_count": source_count}
            for event_id, member_count, source_count in session.execute(
                select(
                    EventMember.event_id,
                    func.count(distinct(EventMember.content_item_id)),
                    func.count(distinct(ContentItem.source_id)),
                )
                .join(ContentItem, ContentItem.id == EventMember.content_item_id)
                .where(EventMember.is_active.is_(True))
                .group_by(EventMember.event_id)
            )
        }
        editorial = _editorial_map(session)
        score_rows = session.execute(
            select(ContentValueScore, ContentItem, Source)
            .join(ContentItem, ContentItem.id == ContentValueScore.content_item_id)
            .join(Source, Source.id == ContentItem.source_id)
            .where(
                ContentValueScore.run_id == score_run.id,
                ContentValueScore.decision == "selected",
                ContentValueScore.input_content_hash == ContentItem.content_hash,
            )
            .order_by(ContentValueScore.total_score.desc(), ContentValueScore.id)
        )
        stories = []
        omitted_untranslated = []
        for score, item, source in score_rows:
            zh = editorial.get(item.id, {})
            display_copy = _display_copy(item, zh)
            if display_copy is None:
                omitted_untranslated.append(item.id)
                continue
            title_zh, summary_zh = display_copy
            event_id = current_event.get(item.id)
            stats = event_stats.get(event_id, {"member_count": 1, "source_count": 1})
            tags = [
                str(tag.get("label_zh"))
                for tag in (zh.get("tags") or [])
                if tag.get("label_zh")
            ]
            if not tags:
                tags = [str(topic) for topic in (item.topics or [])[:3]]
            stories.append(
                {
                    "id": item.id,
                    "event_id": event_id,
                    "title_zh": title_zh,
                    "summary_zh": summary_zh,
                    "original_language": item.language or "und",
                    "source_name": source.name,
                    "source_type": item.source_type,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                    "ranking_score": score.total_score,
                    "tags": tags[:3],
                    "url": item.original_url or item.canonical_url,
                    **stats,
                }
            )

        multi_events = []
        multi_ids = [
            event_id
            for event_id, stats in event_stats.items()
            if stats["source_count"] > 1
        ]
        if multi_ids:
            events = session.scalars(select(Event).where(Event.id.in_(multi_ids))).all()
            for event in events:
                members = session.execute(
                    select(ContentItem, Source, EventMember)
                    .join(EventMember, EventMember.content_item_id == ContentItem.id)
                    .join(Source, Source.id == ContentItem.source_id)
                    .where(EventMember.event_id == event.id, EventMember.is_active.is_(True))
                    .order_by(ContentItem.published_at.desc())
                )
                multi_events.append(
                    {
                        "id": event.id,
                        "title": event.canonical_title,
                        "source_count": event_stats[event.id]["source_count"],
                        "member_count": event_stats[event.id]["member_count"],
                        "members": [
                            {
                                "content_id": item.id,
                                "title_zh": display[0],
                                "source_name": source.name,
                                "url": item.original_url or item.canonical_url,
                                "decision_source": member.decision_source,
                            }
                            for item, source, member in members
                            if (display := _display_copy(item, editorial.get(item.id, {})))
                        ],
                    }
                )

        payload = {
            "schema_version": "navigate-home.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "domain": {"key": domain.key, "name": domain.name},
            "edition": {
                "as_of": score_run.as_of.isoformat(),
                "lead": _daily_lead(session, args.domain),
            },
            "counts": {
                "selected": score_run.selected_count,
                "full_pool": score_run.input_count,
                "active_events": len(event_stats),
                "multi_source_events": len(multi_events),
                "displayed": len(stories),
                "omitted_untranslated": len(omitted_untranslated),
            },
            "stories": stories,
            "multi_source_events": multi_events,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"exported: output={args.output} stories={len(stories)} "
        f"multi_source_events={len(multi_events)} "
        f"omitted_untranslated={len(omitted_untranslated)}"
    )


if __name__ == "__main__":
    main()
