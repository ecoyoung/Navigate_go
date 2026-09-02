import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import article_content_hash
from .models import ContentItem

STRICT_DUPLICATE_RULE = "exact-content-v1"
MIN_STRICT_BODY_CHARS = 200


@dataclass(frozen=True)
class StrictDuplicateSummary:
    scanned: int
    hashes_updated: int
    groups: int
    duplicates: int


def _body_length(content: ContentItem) -> int:
    return len(re.sub(r"\s+", " ", content.body or "").strip())


def _computed_hash(content: ContentItem) -> str:
    return article_content_hash(
        content.title,
        content.body or "",
        content.excerpt,
        content.topics or [],
    )


def _desired_duplicates(
    contents: list[ContentItem], hashes: dict[int, str], min_body_chars: int
) -> tuple[dict[int, int], int]:
    grouped: dict[str, list[ContentItem]] = defaultdict(list)
    for content in contents:
        content_hash = hashes[content.id]
        if content_hash and _body_length(content) >= min_body_chars:
            grouped[content_hash].append(content)

    desired: dict[int, int] = {}
    groups = 0
    for items in grouped.values():
        if len({item.source_id for item in items}) < 2:
            continue
        ordered = sorted(items, key=lambda item: item.id)
        canonical = ordered[0]
        for duplicate in ordered[1:]:
            desired[duplicate.id] = canonical.id
        groups += 1
    return desired, groups


def rebuild_strict_duplicates(
    session: Session, *, apply: bool, min_body_chars: int = MIN_STRICT_BODY_CHARS
) -> StrictDuplicateSummary:
    contents = list(session.scalars(select(ContentItem).order_by(ContentItem.id)))
    hashes = {content.id: _computed_hash(content) for content in contents}
    hashes_updated = sum(content.content_hash != hashes[content.id] for content in contents)
    desired, groups = _desired_duplicates(contents, hashes, min_body_chars)
    if apply:
        for content in contents:
            content.content_hash = hashes[content.id]
            canonical_id = desired.get(content.id)
            content.duplicate_of_id = canonical_id
            content.duplicate_rule = STRICT_DUPLICATE_RULE if canonical_id is not None else None
    return StrictDuplicateSummary(
        scanned=len(contents),
        hashes_updated=hashes_updated,
        groups=groups,
        duplicates=len(desired),
    )


def refresh_strict_hash_groups(session: Session, content_hashes: set[str]) -> None:
    hashes = {value for value in content_hashes if value}
    if not hashes:
        return
    contents = list(
        session.scalars(
            select(ContentItem)
            .where(ContentItem.content_hash.in_(hashes))
            .order_by(ContentItem.id)
        )
    )
    known_hashes = {content.id: content.content_hash for content in contents}
    desired, _groups = _desired_duplicates(contents, known_hashes, MIN_STRICT_BODY_CHARS)
    for content in contents:
        canonical_id = desired.get(content.id)
        content.duplicate_of_id = canonical_id
        content.duplicate_rule = STRICT_DUPLICATE_RULE if canonical_id is not None else None
