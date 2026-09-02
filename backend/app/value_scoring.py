from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from .domain_assignments import active_domain_classifier
from .models import (
    ContentDomainAssignment,
    ContentItem,
    ContentValueScore,
    ContentValueScoreRun,
    Domain,
    EntityMention,
    EntityProcessingResult,
    EventMember,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/value_scoring.v1.json"
SCHEMA_VERSION = "content-value-score.v1"


@dataclass(frozen=True)
class PlannedValueScore:
    content_item_id: int
    input_content_hash: str
    total_score: float
    component_scores: dict
    penalties: list[dict]
    gates: list[str]
    decision: str
    reasons: list[dict]


@dataclass(frozen=True)
class ValueScorePlan:
    domain_id: int
    domain_key: str
    algorithm_version: str
    config: dict
    config_hash: str
    as_of: datetime
    input_hash: str
    scores: list[PlannedValueScore]

    @property
    def selected_count(self) -> int:
        return sum(score.decision == "selected" for score in self.scores)


@dataclass(frozen=True)
class ValueScoreApplyResult:
    run_id: int
    input_count: int
    selected_count: int
    reused_run: bool


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def load_value_scoring_config(path: Path | None = None) -> dict:
    config = json.loads((path or DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    weights = config.get("weights", {})
    expected = {
        "recency",
        "source_quality",
        "completeness",
        "corroboration",
        "confirmed_entities",
        "domain_confidence",
    }
    if set(weights) != expected or abs(sum(float(value) for value in weights.values()) - 1) > 1e-9:
        raise ValueError("value scoring weights must contain the six v1 components and sum to 1")
    if not 0 <= float(config["selection_threshold"]) <= 100:
        raise ValueError("selection_threshold must be between 0 and 100")
    return config


def _recency_score(item: ContentItem, as_of: datetime, config: dict) -> tuple[float, float | None]:
    if item.published_at is None:
        return 0.0, None
    age_hours = max(0.0, (_utc(as_of) - _utc(item.published_at)).total_seconds() / 3600)
    full = float(config["full_score_hours"])
    zero = float(config["zero_score_hours"])
    if age_hours <= full:
        return 1.0, age_hours
    if age_hours >= zero:
        return 0.0, age_hours
    return (zero - age_hours) / (zero - full), age_hours


def _completeness_score(item: ContentItem, config: dict) -> tuple[float, dict]:
    quality = item.quality or {}
    complete = quality.get("body_complete")
    status = "complete" if complete is True else "partial" if complete is False else "unknown"
    status_score = float(config["body_status_scores"][status])
    body_chars = len((item.body or "").strip())
    presence_score = min(1.0, body_chars / float(config["full_body_chars"]))
    access_score = float(
        config["access_scores"].get(item.access_level, config["default_access_score"])
    )
    weights = config["weights"]
    score = (
        status_score * float(weights["body_status"])
        + presence_score * float(weights["body_presence"])
        + access_score * float(weights["access"])
    )
    return score, {
        "body_status": status,
        "body_chars": body_chars,
        "access_level": item.access_level,
    }


def _corroboration_score(source_count: int, config: dict) -> float:
    if source_count < 2:
        return 0.0
    full = int(config["full_score_sources"])
    if source_count >= full:
        return 1.0
    return float(config["two_sources"])


def _input_maps(session: Session) -> tuple[dict[int, tuple[int, int]], dict[int, int]]:
    event_stats: dict[int, tuple[int, int]] = {}
    peer_members = EventMember.__table__.alias("peer_members")
    event_rows = session.execute(
        select(
            EventMember.content_item_id,
            func.count(distinct(peer_members.c.content_item_id)),
            func.count(distinct(ContentItem.source_id)),
        )
        .join(
            peer_members,
            peer_members.c.event_id == EventMember.event_id,
        )
        .join(
            ContentItem,
            ContentItem.id == peer_members.c.content_item_id,
        )
        .where(EventMember.is_active.is_(True), peer_members.c.is_active)
        .group_by(EventMember.content_item_id)
    )
    for content_id, member_count, source_count in event_rows:
        event_stats[content_id] = (member_count, source_count)

    entity_counts = dict(
        session.execute(
            select(EntityMention.content_item_id, func.count(distinct(EntityMention.entity_id)))
            .join(
                EntityProcessingResult,
                EntityProcessingResult.id == EntityMention.processing_result_id,
            )
            .join(ContentItem, ContentItem.id == EntityMention.content_item_id)
            .where(
                EntityMention.entity_id.is_not(None),
                EntityProcessingResult.status == "succeeded",
                EntityProcessingResult.input_content_hash == ContentItem.content_hash,
            )
            .group_by(EntityMention.content_item_id)
        ).all()
    )
    return event_stats, entity_counts


def build_value_score_plan(
    session: Session,
    *,
    domain_key: str,
    as_of: datetime,
    config: dict | None = None,
) -> ValueScorePlan:
    config = config or load_value_scoring_config()
    as_of = _utc(as_of)
    domain = session.scalar(select(Domain).where(Domain.key == domain_key))
    if domain is None:
        raise ValueError(f"unknown domain: {domain_key}")
    active_classifier = active_domain_classifier(domain)
    assignment_filters = [
        ContentDomainAssignment.domain_id == domain.id,
        ContentDomainAssignment.decision == "include",
        ContentDomainAssignment.input_content_hash == ContentItem.content_hash,
    ]
    if active_classifier is not None:
        assignment_filters.extend(
            [
                ContentDomainAssignment.classifier_name == active_classifier[0],
                ContentDomainAssignment.classifier_version == active_classifier[1],
            ]
        )
    rows = list(
        session.execute(
            select(ContentItem, ContentDomainAssignment)
            .join(
                ContentDomainAssignment,
                ContentDomainAssignment.content_item_id == ContentItem.id,
            )
            .where(
                *assignment_filters,
                (ContentItem.published_at.is_(None) | (ContentItem.published_at <= as_of)),
            )
            .order_by(ContentItem.id, ContentDomainAssignment.id.desc())
        )
    )
    current: dict[int, tuple[ContentItem, ContentDomainAssignment]] = {}
    for item, assignment in rows:
        current.setdefault(item.id, (item, assignment))
    event_stats, entity_counts = _input_maps(session)
    weights = config["weights"]
    planned: list[PlannedValueScore] = []
    input_evidence: list[dict] = []
    for item, assignment in current.values():
        recency, age_hours = _recency_score(item, as_of, config["recency"])
        source_quality = float(
            config["source_quality"]["by_source_type"].get(
                item.source_type, config["source_quality"]["default"]
            )
        )
        completeness, completeness_evidence = _completeness_score(
            item, config["completeness"]
        )
        event_member_count, event_source_count = event_stats.get(item.id, (1, 1))
        corroboration = _corroboration_score(event_source_count, config["corroboration"])
        entity_count = entity_counts.get(item.id, 0)
        entity_score = min(
            1.0,
            entity_count / float(config["confirmed_entities"]["full_score_count"]),
        )
        domain_confidence = max(0.0, min(1.0, float(assignment.confidence)))
        raw_components = {
            "recency": recency,
            "source_quality": source_quality,
            "completeness": completeness,
            "corroboration": corroboration,
            "confirmed_entities": entity_score,
            "domain_confidence": domain_confidence,
        }
        component_scores = {
            key: {
                "normalized": round(value, 6),
                "weight": float(weights[key]),
                "points": round(value * float(weights[key]) * 100, 4),
            }
            for key, value in raw_components.items()
        }
        penalties: list[dict] = []
        if item.is_sponsored:
            penalties.append(
                {"code": "sponsored", "points": float(config["penalties"]["sponsored"])}
            )
        gates: list[str] = []
        if item.published_at is None:
            gates.append("missing_published_at")
        if item.duplicate_of_id is not None:
            gates.append("duplicate")
        base = sum(value["points"] for value in component_scores.values())
        total = round(max(0.0, min(100.0, base - sum(p["points"] for p in penalties))), 2)
        decision = (
            "selected"
            if total >= float(config["selection_threshold"]) and not gates
            else "full_pool"
        )
        reasons = [
            {"code": "age_hours", "value": round(age_hours, 2) if age_hours is not None else None},
            {"code": "source_type", "value": item.source_type},
            {"code": "completeness", **completeness_evidence},
            {
                "code": "event_corroboration",
                "member_count": event_member_count,
                "source_count": event_source_count,
            },
            {"code": "confirmed_entity_count", "value": entity_count},
            {"code": "domain_confidence", "value": round(domain_confidence, 6)},
        ]
        planned.append(
            PlannedValueScore(
                content_item_id=item.id,
                input_content_hash=item.content_hash,
                total_score=total,
                component_scores=component_scores,
                penalties=penalties,
                gates=gates,
                decision=decision,
                reasons=reasons,
            )
        )
        input_evidence.append(
            {
                "content_item_id": item.id,
                "content_hash": item.content_hash,
                "assignment_id": assignment.id,
                "assignment_confidence": assignment.confidence,
                "event_member_count": event_member_count,
                "event_source_count": event_source_count,
                "confirmed_entity_count": entity_count,
                "published_at": _utc(item.published_at).isoformat() if item.published_at else None,
                "source_type": item.source_type,
                "access_level": item.access_level,
                "quality": item.quality,
                "is_sponsored": item.is_sponsored,
                "duplicate_of_id": item.duplicate_of_id,
            }
        )
    config_hash = _canonical_hash(config)
    input_hash = _canonical_hash(
        {
            "domain_id": domain.id,
            "as_of": as_of.isoformat(),
            "config_hash": config_hash,
            "inputs": sorted(input_evidence, key=lambda value: value["content_item_id"]),
        }
    )
    return ValueScorePlan(
        domain_id=domain.id,
        domain_key=domain.key,
        algorithm_version=config["algorithm_version"],
        config=config,
        config_hash=config_hash,
        as_of=as_of,
        input_hash=input_hash,
        scores=sorted(planned, key=lambda value: (-value.total_score, value.content_item_id)),
    )


def apply_value_score_plan(session: Session, plan: ValueScorePlan) -> ValueScoreApplyResult:
    existing = session.scalar(
        select(ContentValueScoreRun).where(ContentValueScoreRun.input_hash == plan.input_hash)
    )
    if existing is not None:
        if existing.status != "succeeded":
            raise ValueError(f"matching score run {existing.id} is not succeeded")
        return ValueScoreApplyResult(
            run_id=existing.id,
            input_count=existing.input_count,
            selected_count=existing.selected_count,
            reused_run=True,
        )
    now = datetime.now(UTC)
    run = ContentValueScoreRun(
        domain_id=plan.domain_id,
        algorithm_version=plan.algorithm_version,
        schema_version=SCHEMA_VERSION,
        config=plan.config,
        config_hash=plan.config_hash,
        as_of=plan.as_of,
        input_hash=plan.input_hash,
        status="succeeded",
        input_count=len(plan.scores),
        selected_count=plan.selected_count,
        started_at=now,
        finished_at=now,
    )
    session.add(run)
    session.flush()
    session.add_all(
        [
            ContentValueScore(
                run_id=run.id,
                content_item_id=score.content_item_id,
                input_content_hash=score.input_content_hash,
                total_score=score.total_score,
                component_scores=score.component_scores,
                penalties=score.penalties,
                gates=score.gates,
                decision=score.decision,
                reasons=score.reasons,
                scored_at=now,
            )
            for score in plan.scores
        ]
    )
    return ValueScoreApplyResult(
        run_id=run.id,
        input_count=len(plan.scores),
        selected_count=plan.selected_count,
        reused_run=False,
    )
