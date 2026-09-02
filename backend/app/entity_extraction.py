from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    ContentItem,
    Entity,
    EntityAlias,
    EntityMention,
    EntityProcessingResult,
    EntityResolutionCandidate,
)

SCHEMA_VERSION = "entity-mentions.v1"
EXTRACTOR_NAME = "configured_aliases"
EXTRACTOR_VERSION = "configured-aliases.v1"
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "entities" / "beauty.v1.json"
)
ENTITY_TYPES = {
    "organization",
    "brand",
    "person",
    "product",
    "location",
    "substance",
    "regulation",
    "technology",
}


@dataclass(frozen=True)
class ExtractionStats:
    processed: int
    skipped: int
    candidates: int
    resolved: int
    unresolved: int


def normalize_entity_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return "".join(character for character in normalized if character.isalnum())


def _hash(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_entity_policy(path: Path | None = None) -> dict:
    policy = json.loads((path or DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    if policy.get("schema_version") != "entity-extraction-policy.v1":
        raise ValueError("unsupported entity extraction policy schema")
    keys: set[str] = set()
    for item in policy.get("entities", []):
        key = str(item.get("registry_key") or "").strip()
        entity_type = str(item.get("entity_type") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not key or key in keys:
            raise ValueError("entity registry_key must be present and unique")
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unsupported entity type: {entity_type}")
        if not canonical_name or not normalize_entity_name(canonical_name):
            raise ValueError(f"entity {key} has no usable canonical name")
        keys.add(key)
    return policy


def _alias_values(item: dict) -> list[tuple[str, str]]:
    default_language = str(item.get("language") or "und")
    values = [(str(item["canonical_name"]).strip(), default_language)]
    for alias in item.get("aliases", []):
        if isinstance(alias, str):
            values.append((alias.strip(), default_language))
        else:
            values.append(
                (str(alias.get("value") or "").strip(), str(alias.get("language") or "und"))
            )
    return [(value, language) for value, language in values if value]


def seed_entity_registry(session: Session, policy: dict) -> list[Entity]:
    existing = {
        entity.registry_key: entity
        for entity in session.scalars(
            select(Entity).where(Entity.registry_key.is_not(None))
        )
    }
    entities: list[Entity] = []
    for item in policy.get("entities", []):
        key = str(item["registry_key"])
        canonical_name = str(item["canonical_name"]).strip()
        entity = existing.get(key)
        if entity is None:
            entity = Entity(
                registry_key=key,
                entity_type=str(item["entity_type"]),
                canonical_name=canonical_name,
                normalized_name=normalize_entity_name(canonical_name),
                description=item.get("description"),
                attributes=item.get("attributes") or {},
            )
            session.add(entity)
            session.flush()
        else:
            entity.entity_type = str(item["entity_type"])
            entity.canonical_name = canonical_name
            entity.normalized_name = normalize_entity_name(canonical_name)
            entity.description = item.get("description")
            entity.attributes = item.get("attributes") or {}
            entity.status = "active"
        known_aliases = {
            (alias.normalized_alias, alias.language)
            for alias in session.scalars(
                select(EntityAlias).where(EntityAlias.entity_id == entity.id)
            )
        }
        for alias, language in _alias_values(item):
            normalized_alias = normalize_entity_name(alias)
            alias_key = (normalized_alias, language)
            if not normalized_alias or alias_key in known_aliases:
                continue
            session.add(
                EntityAlias(
                    entity_id=entity.id,
                    alias=alias,
                    normalized_alias=normalized_alias,
                    language=language,
                    alias_type="canonical" if alias == canonical_name else "configured",
                    source=str(policy.get("policy_key") or "policy"),
                    confidence=1.0,
                )
            )
            known_aliases.add(alias_key)
        entities.append(entity)
    session.flush()
    return entities


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    prefix = r"(?<![\w])" if alias[0].isascii() and alias[0].isalnum() else ""
    suffix = r"(?![\w])" if alias[-1].isascii() and alias[-1].isalnum() else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


def _evidence(text: str, start: int, end: int, context_chars: int = 60) -> str:
    return text[max(0, start - context_chars) : min(len(text), end + context_chars)].strip()


def _find_candidates(
    session: Session, content: ContentItem
) -> list[dict]:
    rows = list(
        session.execute(
            select(EntityAlias, Entity)
            .join(Entity, Entity.id == EntityAlias.entity_id)
            .where(Entity.status == "active")
            .order_by(EntityAlias.id)
        )
    )
    grouped: dict[tuple[str, int, int], dict] = {}
    fields = {
        "title": content.title or "",
        "excerpt": content.excerpt or "",
        "body": content.body or "",
    }
    for field, text in fields.items():
        for alias, entity in rows:
            for match in _alias_pattern(alias.alias).finditer(text):
                key = (field, match.start(), match.end())
                candidate = grouped.setdefault(
                    key,
                    {
                        "field": field,
                        "start_offset": match.start(),
                        "end_offset": match.end(),
                        "surface": match.group(0),
                        "normalized_surface": normalize_entity_name(match.group(0)),
                        "evidence_text": _evidence(text, match.start(), match.end()),
                        "entities": {},
                    },
                )
                candidate["entities"][entity.id] = entity
    return sorted(
        grouped.values(),
        key=lambda item: (item["field"], item["start_offset"], item["end_offset"]),
    )


def process_content_entities(
    session: Session,
    content: ContentItem,
    policy: dict,
    *,
    registry_seeded: bool = False,
) -> tuple[EntityProcessingResult, bool]:
    config_hash = _hash(policy)
    existing = session.scalar(
        select(EntityProcessingResult).where(
            EntityProcessingResult.content_item_id == content.id,
            EntityProcessingResult.extractor_name == EXTRACTOR_NAME,
            EntityProcessingResult.extractor_version == EXTRACTOR_VERSION,
            EntityProcessingResult.input_content_hash == content.content_hash,
            EntityProcessingResult.config_hash == config_hash,
            EntityProcessingResult.status == "succeeded",
        )
    )
    if existing is not None:
        return existing, False

    if not registry_seeded:
        seed_entity_registry(session, policy)
    candidates = _find_candidates(session, content)
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

    output_mentions = []
    resolved = unresolved = 0
    for candidate in candidates:
        entities = list(candidate.pop("entities").values())
        entity = entities[0] if len(entities) == 1 else None
        entity_type = entity.entity_type if entity is not None else "unknown"
        status = "resolved" if entity is not None else "ambiguous"
        mention = EntityMention(
            processing_result_id=result.id,
            content_item_id=content.id,
            entity_id=entity.id if entity is not None else None,
            entity_type=entity_type,
            confidence=1.0 if entity is not None else 0.0,
            resolution_status=status,
            extraction_method="configured_alias",
            **candidate,
        )
        session.add(mention)
        session.flush()
        if entity is not None:
            resolved += 1
        else:
            unresolved += 1
            for possible in entities:
                session.add(
                    EntityResolutionCandidate(
                        mention_id=mention.id,
                        candidate_entity_id=possible.id,
                        score=0.5,
                        signals={"reason": "ambiguous_configured_alias"},
                    )
                )
        output_mentions.append(
            {
                "mention_id": mention.id,
                "entity_id": mention.entity_id,
                "entity_type": mention.entity_type,
                "surface": mention.surface,
                "field": mention.field,
                "start_offset": mention.start_offset,
                "end_offset": mention.end_offset,
                "evidence_text": mention.evidence_text,
                "confidence": mention.confidence,
                "resolution_status": mention.resolution_status,
            }
        )
    result.candidate_count = len(candidates)
    result.resolved_count = resolved
    result.unresolved_count = unresolved
    result.output = {
        "schema_version": SCHEMA_VERSION,
        "content_ref": f"content:{content.id}",
        "input_content_hash": content.content_hash,
        "mentions": output_mentions,
    }
    session.flush()
    return result, True


def process_entities(
    session: Session,
    contents: list[ContentItem],
    policy: dict,
) -> ExtractionStats:
    processed = skipped = candidates = resolved = unresolved = 0
    seed_entity_registry(session, policy)
    for content in contents:
        result, created = process_content_entities(
            session, content, policy, registry_seeded=True
        )
        if not created:
            skipped += 1
            continue
        processed += 1
        candidates += result.candidate_count
        resolved += result.resolved_count
        unresolved += result.unresolved_count
    return ExtractionStats(processed, skipped, candidates, resolved, unresolved)
