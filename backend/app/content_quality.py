from __future__ import annotations

from typing import Literal

from .models import ContentItem

QualityTier = Literal["verified_full", "partial", "needs_enrichment"]
MIN_PARTIAL_BODY_CHARS = 200


def quality_tier(content: ContentItem) -> QualityTier:
    quality = content.quality or {}
    if (
        bool(quality.get("metadata_only"))
        or content.published_at is None
        or not (content.body or "").strip()
    ):
        return "needs_enrichment"
    if quality.get("body_complete") is True:
        return "verified_full"
    if len(content.body or "") >= MIN_PARTIAL_BODY_CHARS:
        return "partial"
    return "needs_enrichment"


def is_reader_eligible(content: ContentItem) -> bool:
    """Only dated full or substantial partial content may reach reader outputs."""
    return quality_tier(content) in {"verified_full", "partial"}
