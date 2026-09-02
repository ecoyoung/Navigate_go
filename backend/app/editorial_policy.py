from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_POLICY_DIR = Path(__file__).resolve().parents[1] / "config" / "editorial"
_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
_VERSION_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EditorialTag(PolicyModel):
    tag_key: str = Field(pattern=_IDENTIFIER_PATTERN)
    title: str = Field(min_length=1, max_length=80)
    definition: str = Field(min_length=4, max_length=300)
    aliases: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_aliases(self):
        normalized = [alias.strip().casefold() for alias in self.aliases]
        if any(not alias for alias in normalized):
            raise ValueError("tag aliases must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"duplicate aliases in tag {self.tag_key}")
        return self


class EditorialSection(PolicyModel):
    section_key: str = Field(pattern=_IDENTIFIER_PATTERN)
    title: str = Field(min_length=1, max_length=80)
    definition: str = Field(min_length=4, max_length=400)
    tag_keys: list[str] = Field(default_factory=list)
    is_fallback: bool = False

    @model_validator(mode="after")
    def validate_tag_keys(self):
        if len(self.tag_keys) != len(set(self.tag_keys)):
            raise ValueError(f"duplicate tag keys in section {self.section_key}")
        return self


class EditorialLayoutPolicy(PolicyModel):
    max_sections: int = Field(ge=1, le=20)
    preferred_min_stories_per_section: int = Field(ge=1, le=20)
    allow_single_story_section: bool
    fallback_section_key: str = Field(pattern=_IDENTIFIER_PATTERN)


TieBreaker = Literal[
    "cross_source_corroboration",
    "content_completeness",
    "published_at_desc",
    "story_key_asc",
]


class EditorialRankingPolicy(PolicyModel):
    section_order: list[str] = Field(min_length=1)
    story_priority_tags: list[str] = Field(default_factory=list)
    story_tie_breakers: list[TieBreaker] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ordering(self):
        if len(self.section_order) != len(set(self.section_order)):
            raise ValueError("section_order must not contain duplicates")
        if len(self.story_priority_tags) != len(set(self.story_priority_tags)):
            raise ValueError("story_priority_tags must not contain duplicates")
        if len(self.story_tie_breakers) != len(set(self.story_tie_breakers)):
            raise ValueError("story_tie_breakers must not contain duplicates")
        if self.story_tie_breakers[-1] != "story_key_asc":
            raise ValueError("story_tie_breakers must end with story_key_asc")
        return self


class EditorialPolicy(PolicyModel):
    schema_version: Literal["editorial-policy.v1"]
    domain_key: str = Field(pattern=_IDENTIFIER_PATTERN)
    taxonomy_version: str = Field(pattern=_VERSION_PATTERN)
    tag_catalog: list[EditorialTag] = Field(min_length=1)
    section_catalog: list[EditorialSection] = Field(min_length=1)
    layout_policy: EditorialLayoutPolicy
    ranking_policy: EditorialRankingPolicy

    @model_validator(mode="after")
    def validate_catalogs(self):
        tag_keys = [tag.tag_key for tag in self.tag_catalog]
        if len(tag_keys) != len(set(tag_keys)):
            raise ValueError("tag_catalog contains duplicate tag_key values")
        tag_titles = [tag.title.strip().casefold() for tag in self.tag_catalog]
        if len(tag_titles) != len(set(tag_titles)):
            raise ValueError("tag_catalog contains duplicate titles")

        alias_owner: dict[str, str] = {}
        for tag in self.tag_catalog:
            for value in (tag.tag_key, tag.title, *tag.aliases):
                normalized = value.strip().casefold()
                owner = alias_owner.get(normalized)
                if owner is not None and owner != tag.tag_key:
                    raise ValueError(
                        f"tag label {value!r} is shared by {owner!r} and {tag.tag_key!r}"
                    )
                alias_owner[normalized] = tag.tag_key

        section_keys = [section.section_key for section in self.section_catalog]
        if len(section_keys) != len(set(section_keys)):
            raise ValueError("section_catalog contains duplicate section_key values")
        section_titles = [section.title.strip().casefold() for section in self.section_catalog]
        if len(section_titles) != len(set(section_titles)):
            raise ValueError("section_catalog contains duplicate titles")

        known_tags = set(tag_keys)
        for section in self.section_catalog:
            unknown = set(section.tag_keys) - known_tags
            if unknown:
                raise ValueError(
                    f"section {section.section_key!r} references unknown tags: {sorted(unknown)}"
                )

        fallbacks = [section for section in self.section_catalog if section.is_fallback]
        if len(fallbacks) != 1:
            raise ValueError("section_catalog must contain exactly one fallback section")
        fallback = fallbacks[0]
        if fallback.section_key != "general":
            raise ValueError("the fallback section_key must be 'general'")
        if fallback.tag_keys:
            raise ValueError("the general fallback section must not bind tag_keys")
        if self.layout_policy.fallback_section_key != fallback.section_key:
            raise ValueError(
                "layout_policy.fallback_section_key must reference the fallback section"
            )

        if self.layout_policy.max_sections > len(self.section_catalog):
            raise ValueError("layout_policy.max_sections exceeds the section catalog size")

        if self.ranking_policy.section_order != section_keys:
            raise ValueError(
                "ranking_policy.section_order must list every section exactly once in catalog order"
            )
        if self.ranking_policy.section_order[-1] != fallback.section_key:
            raise ValueError("the fallback section must be last in section_order")

        unknown_priority_tags = set(self.ranking_policy.story_priority_tags) - known_tags
        if unknown_priority_tags:
            raise ValueError(
                f"ranking_policy references unknown priority tags: {sorted(unknown_priority_tags)}"
            )
        return self


def load_editorial_policy(
    domain_key: str,
    *,
    version: str = "v1",
    config_dir: Path | None = None,
) -> EditorialPolicy:
    if re.fullmatch(_IDENTIFIER_PATTERN, domain_key) is None:
        raise ValueError(f"Invalid editorial policy domain key: {domain_key!r}")
    if re.fullmatch(_VERSION_PATTERN, version) is None:
        raise ValueError(f"Invalid editorial policy version: {version!r}")

    policy_dir = config_dir or DEFAULT_POLICY_DIR
    path = policy_dir / f"{domain_key}.{version}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Editorial policy not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid editorial policy JSON at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Editorial policy must be a JSON object: {path}")

    policy = EditorialPolicy.model_validate(raw)
    if policy.domain_key != domain_key:
        raise ValueError(
            f"Editorial policy domain mismatch: requested {domain_key!r}, got {policy.domain_key!r}"
        )
    return policy
