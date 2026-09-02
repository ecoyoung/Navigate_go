from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    ContentItem,
    Event,
    EventClusterCandidate,
    EventClusterConstraint,
    EventClusterRun,
    EventMember,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/event_clustering.v1.json"


@dataclass(frozen=True)
class PairScore:
    score: float
    signals: dict


@dataclass
class PlannedCluster:
    items: list[ContentItem]
    representative: ContentItem
    membership_hash: str
    member_evidence: dict[int, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewCandidate:
    left_content_id: int
    right_content_id: int
    score: float
    signals: dict


@dataclass
class ClusterPlan:
    algorithm_version: str
    config: dict
    input_hash: str
    input_count: int
    candidate_pair_count: int
    clusters: list[PlannedCluster]
    review_candidates: list[ReviewCandidate]
    protected_event_count: int

    @property
    def multi_item_event_count(self) -> int:
        return sum(len(cluster.items) > 1 for cluster in self.clusters)


@dataclass(frozen=True)
class ClusterApplyResult:
    run_id: int
    input_count: int
    event_count: int
    multi_item_event_count: int
    created_event_count: int
    reused_event_count: int
    review_candidate_count: int
    reused_run: bool


class _DisjointSet:
    def __init__(self, ids: list[int]):
        self.parent = {item_id: item_id for item_id in ids}

    def find(self, item_id: int) -> int:
        parent = self.parent[item_id]
        if parent != item_id:
            self.parent[item_id] = self.find(parent)
        return self.parent[item_id]

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def load_clustering_config(path: Path | None = None) -> dict:
    return json.loads((path or DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _effective_at(item: ContentItem) -> datetime:
    return _utc(item.published_at or item.discovered_at)


def _normalized_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


def _ngrams(value: str | None, size: int) -> set[str]:
    compact = _normalized_text(value)
    if not compact:
        return set()
    if len(compact) <= size:
        return {compact}
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float | None:
    union = left | right
    if not union:
        return None
    return len(left & right) / len(union)


def _numbers(value: str | None) -> set[str]:
    return set(re.findall(r"(?<![\w])\d+(?:[.,]\d+)?%?", value or ""))


def _lead_text(item: ContentItem, chars: int) -> str:
    return (item.excerpt or item.body or "")[:chars]


def pair_score(left: ContentItem, right: ContentItem, config: dict) -> PairScore:
    if left.id == right.duplicate_of_id or right.id == left.duplicate_of_id:
        return PairScore(1.0, {"exact_duplicate": True})
    ngram_size = int(config["title_ngram_size"])
    lead_chars = int(config["lead_text_chars"])
    title = _jaccard(_ngrams(left.title, ngram_size), _ngrams(right.title, ngram_size)) or 0.0
    lead = _jaccard(
        _ngrams(_lead_text(left, lead_chars), ngram_size),
        _ngrams(_lead_text(right, lead_chars), ngram_size),
    )
    topics = _jaccard(
        {_normalized_text(str(item)) for item in left.topics if _normalized_text(str(item))},
        {_normalized_text(str(item)) for item in right.topics if _normalized_text(str(item))},
    )
    left_numbers = _numbers(f"{left.title} {_lead_text(left, lead_chars)}")
    right_numbers = _numbers(f"{right.title} {_lead_text(right, lead_chars)}")
    number_score = _jaccard(left_numbers, right_numbers)
    number_conflict = bool(left_numbers and right_numbers and not left_numbers & right_numbers)
    hours = abs((_effective_at(left) - _effective_at(right)).total_seconds()) / 3600
    window = float(config["candidate_window_hours"])
    time_score = max(0.0, 1.0 - hours / window)
    raw_signals = {
        "title": title,
        "lead_text": lead,
        "topics": topics,
        "numbers": number_score,
        "time": time_score,
    }
    weights = config["weights"]
    available = {
        key: value for key, value in raw_signals.items() if value is not None and key in weights
    }
    weight_total = sum(float(weights[key]) for key in available)
    score = sum(float(weights[key]) * value for key, value in available.items()) / weight_total
    if number_conflict:
        score = min(score, float(config["auto_match_threshold"]) - 0.01)
    signals = {
        **{
            key: round(value, 4) if value is not None else None
            for key, value in raw_signals.items()
        },
        "hours_apart": round(hours, 2),
        "number_conflict": number_conflict,
    }
    return PairScore(round(max(0.0, min(1.0, score)), 6), signals)


def _membership_hash(items: list[ContentItem]) -> str:
    payload = [
        {"content_item_id": item.id, "content_hash": item.content_hash}
        for item in sorted(items, key=lambda candidate: candidate.id)
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _representative(items: list[ContentItem], config: dict) -> ContentItem:
    if len(items) == 1:
        return items[0]
    scored: list[tuple[float, int, int, ContentItem]] = []
    for candidate in items:
        peers = [item for item in items if item.id != candidate.id]
        average = sum(pair_score(candidate, peer, config).score for peer in peers) / len(peers)
        body_length = len(candidate.body or "")
        scored.append((average, body_length, -candidate.id, candidate))
    return max(scored, key=lambda value: value[:3])[3]


def _cluster_evidence(
    cluster: list[ContentItem],
    representative: ContentItem,
    config: dict,
    constraints: list[EventClusterConstraint],
) -> dict:
    cluster_ids = {item.id for item in cluster}
    manual_by_item: dict[int, list[dict]] = {}
    for constraint in constraints:
        if constraint.relation != "must_link":
            continue
        if {constraint.left_content_id, constraint.right_content_id} <= cluster_ids:
            detail = {
                "constraint_id": constraint.id,
                "relation": constraint.relation,
                "left_content_id": constraint.left_content_id,
                "right_content_id": constraint.right_content_id,
                "reason": constraint.reason,
                "created_by": constraint.created_by,
            }
            manual_by_item.setdefault(constraint.left_content_id, []).append(detail)
            manual_by_item.setdefault(constraint.right_content_id, []).append(detail)
    evidence = {}
    for item in cluster:
        if item.id == representative.id:
            evidence[item.id] = {"score_to_representative": 1.0, "representative": True}
        else:
            result = pair_score(item, representative, config)
            evidence[item.id] = {
                "score_to_representative": result.score,
                "signals": result.signals,
            }
        if item.id in manual_by_item:
            evidence[item.id]["manual_constraints"] = manual_by_item[item.id]
    return evidence


def _input_hash(
    items: list[ContentItem],
    config: dict,
    constraints: list[EventClusterConstraint],
    protected_memberships: dict[int, list[int]],
) -> str:
    payload = {
        "config": config,
        "contents": [
            {
                "id": item.id,
                "content_hash": item.content_hash,
                "effective_at": _effective_at(item).isoformat(),
                "duplicate_of_id": item.duplicate_of_id,
            }
            for item in sorted(items, key=lambda candidate: candidate.id)
        ],
        "constraints": [
            {
                "left": item.left_content_id,
                "right": item.right_content_id,
                "relation": item.relation,
            }
            for item in sorted(constraints, key=lambda value: value.id)
        ],
        "protected_events": [
            {"event_id": event_id, "content_item_ids": sorted(content_ids)}
            for event_id, content_ids in sorted(protected_memberships.items())
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def build_cluster_plan(session: Session, config: dict | None = None) -> ClusterPlan:
    config = config or load_clustering_config()
    all_items = list(session.scalars(select(ContentItem).order_by(ContentItem.id)))
    constraints = list(
        session.scalars(select(EventClusterConstraint).order_by(EventClusterConstraint.id))
    )
    protected_events = list(
        session.scalars(select(Event).where(Event.status == "active", Event.manual_lock.is_(True)))
    )
    protected_ids = {event.id for event in protected_events}
    protected_memberships: dict[int, list[int]] = {event_id: [] for event_id in protected_ids}
    if protected_ids:
        for event_id, content_item_id in session.execute(
            select(EventMember.event_id, EventMember.content_item_id).where(
                EventMember.event_id.in_(protected_ids), EventMember.is_active.is_(True)
            )
        ):
            protected_memberships[event_id].append(content_item_id)
    protected_content_ids = {
        content_id for content_ids in protected_memberships.values() for content_id in content_ids
    }
    items = [item for item in all_items if item.id not in protected_content_ids]
    by_id = {item.id: item for item in items}
    dsu = _DisjointSet(list(by_id))
    for item in items:
        if item.duplicate_of_id in by_id:
            dsu.union(item.id, item.duplicate_of_id)
    cannot_links: set[tuple[int, int]] = set()
    for constraint in constraints:
        pair = (constraint.left_content_id, constraint.right_content_id)
        if pair[0] not in by_id or pair[1] not in by_id:
            continue
        if constraint.relation == "must_link":
            dsu.union(*pair)
        elif constraint.relation == "cannot_link":
            cannot_links.add(pair)

    components: dict[int, list[ContentItem]] = {}
    for item in items:
        components.setdefault(dsu.find(item.id), []).append(item)
    ordered_components = sorted(
        components.values(),
        key=lambda group: (
            max(_effective_at(item) for item in group),
            max(item.id for item in group),
        ),
        reverse=True,
    )
    clusters: list[list[ContentItem]] = []
    review_by_pair: dict[tuple[int, int], ReviewCandidate] = {}
    candidate_pair_count = 0
    threshold = float(config["auto_match_threshold"])
    review_threshold = float(config["review_threshold"])
    minimum_member_score = float(config["minimum_member_score"])
    window_hours = float(config["candidate_window_hours"])

    for component in ordered_components:
        best_index = None
        best_average = -math.inf
        for index, cluster in enumerate(clusters):
            newest_component = max(_effective_at(item) for item in component)
            newest_cluster = max(_effective_at(item) for item in cluster)
            if abs((newest_component - newest_cluster).total_seconds()) / 3600 > window_hours:
                continue
            pairs = [(left, right) for left in component for right in cluster]
            if any(tuple(sorted((left.id, right.id))) in cannot_links for left, right in pairs):
                continue
            results = [(left, right, pair_score(left, right, config)) for left, right in pairs]
            candidate_pair_count += len(results)
            scores = [result.score for _, _, result in results]
            average = sum(scores) / len(scores)
            minimum = min(scores)
            for left, right, result in results:
                if review_threshold <= result.score < threshold:
                    pair = tuple(sorted((left.id, right.id)))
                    review_by_pair[pair] = ReviewCandidate(
                        pair[0], pair[1], result.score, result.signals
                    )
            if average >= threshold and minimum >= minimum_member_score and average > best_average:
                best_index = index
                best_average = average
        if best_index is None:
            clusters.append(list(component))
        else:
            clusters[best_index].extend(component)

    planned = []
    for cluster in clusters:
        representative = _representative(cluster, config)
        planned.append(
            PlannedCluster(
                items=sorted(cluster, key=lambda item: item.id),
                representative=representative,
                membership_hash=_membership_hash(cluster),
                member_evidence=_cluster_evidence(
                    cluster, representative, config, constraints
                ),
            )
        )
    return ClusterPlan(
        algorithm_version=str(config["algorithm_version"]),
        config=config,
        input_hash=_input_hash(all_items, config, constraints, protected_memberships),
        input_count=len(all_items),
        candidate_pair_count=candidate_pair_count,
        clusters=planned,
        review_candidates=sorted(
            review_by_pair.values(), key=lambda item: (-item.score, item.left_content_id)
        ),
        protected_event_count=len(protected_events),
    )


def apply_cluster_plan(session: Session, plan: ClusterPlan) -> ClusterApplyResult:
    prior_run = session.scalar(
        select(EventClusterRun)
        .where(
            EventClusterRun.algorithm_version == plan.algorithm_version,
            EventClusterRun.input_hash == plan.input_hash,
            EventClusterRun.status == "succeeded",
        )
        .order_by(EventClusterRun.id.desc())
    )
    if prior_run:
        review_count = session.scalar(
            select(func.count(EventClusterCandidate.id)).where(
                EventClusterCandidate.cluster_run_id == prior_run.id
            )
        )
        return ClusterApplyResult(
            prior_run.id,
            prior_run.input_count,
            prior_run.event_count,
            prior_run.multi_item_event_count,
            prior_run.created_event_count,
            prior_run.reused_event_count,
            int(review_count or 0),
            True,
        )

    run = EventClusterRun(
        algorithm_version=plan.algorithm_version,
        config=plan.config,
        input_hash=plan.input_hash,
        status="running",
        input_count=plan.input_count,
        candidate_pair_count=plan.candidate_pair_count,
    )
    session.add(run)
    session.flush()

    active_auto_events = list(
        session.scalars(select(Event).where(Event.status == "active", Event.manual_lock.is_(False)))
    )
    reusable = {event.membership_hash: event for event in active_auto_events}
    planned_hashes = {cluster.membership_hash for cluster in plan.clusters}
    for event in active_auto_events:
        if event.membership_hash not in planned_hashes:
            event.status = "superseded"
            for member in event.members:
                if member.is_active:
                    member.is_active = False

    session.flush()
    created = reused = 0
    for cluster in plan.clusters:
        event = reusable.get(cluster.membership_hash)
        first_at = min(_effective_at(item) for item in cluster.items)
        last_at = max(_effective_at(item) for item in cluster.items)
        if event is None:
            event = Event(
                representative_content_id=cluster.representative.id,
                canonical_title=cluster.representative.title,
                first_published_at=first_at,
                last_published_at=last_at,
                membership_hash=cluster.membership_hash,
                status="active",
                cluster_version=plan.algorithm_version,
            )
            session.add(event)
            session.flush()
            created += 1
            for item in cluster.items:
                evidence = cluster.member_evidence[item.id]
                is_manual = bool(evidence.get("manual_constraints"))
                session.add(
                    EventMember(
                        event_id=event.id,
                        content_item_id=item.id,
                        confidence=(
                            1.0
                            if is_manual
                            else float(evidence["score_to_representative"])
                        ),
                        reasons=evidence,
                        decision_source="manual" if is_manual else "automatic",
                        algorithm_version=plan.algorithm_version,
                    )
                )
        else:
            event.representative_content_id = cluster.representative.id
            event.canonical_title = cluster.representative.title
            event.first_published_at = first_at
            event.last_published_at = last_at
            event.cluster_version = plan.algorithm_version
            reused += 1

    for candidate in plan.review_candidates:
        session.add(
            EventClusterCandidate(
                cluster_run_id=run.id,
                left_content_id=candidate.left_content_id,
                right_content_id=candidate.right_content_id,
                score=candidate.score,
                signals=candidate.signals,
                status="pending",
            )
        )
    run.status = "succeeded"
    run.event_count = len(plan.clusters) + plan.protected_event_count
    run.multi_item_event_count = plan.multi_item_event_count
    run.created_event_count = created
    run.reused_event_count = reused
    run.finished_at = datetime.now(UTC)
    session.flush()
    return ClusterApplyResult(
        run.id,
        run.input_count,
        run.event_count,
        run.multi_item_event_count,
        created,
        reused,
        len(plan.review_candidates),
        False,
    )
