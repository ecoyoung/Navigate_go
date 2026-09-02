import json

import pytest
from pydantic import ValidationError

from app.editorial_policy import EditorialPolicy, load_editorial_policy


def _valid_policy() -> dict:
    return {
        "schema_version": "editorial-policy.v1",
        "domain_key": "sample",
        "taxonomy_version": "sample-editorial.v1",
        "tag_catalog": [
            {
                "tag_key": "product",
                "title": "产品",
                "definition": "产品发布与产品变化。",
                "aliases": ["新品", "launch"],
            }
        ],
        "section_catalog": [
            {
                "section_key": "product_news",
                "title": "产品动态",
                "definition": "产品发布和产品线变化。",
                "tag_keys": ["product"],
                "is_fallback": False,
            },
            {
                "section_key": "general",
                "title": "行业动态",
                "definition": "无法可靠归入其他栏目的资讯。",
                "tag_keys": [],
                "is_fallback": True,
            },
        ],
        "layout_policy": {
            "max_sections": 2,
            "preferred_min_stories_per_section": 2,
            "allow_single_story_section": False,
            "fallback_section_key": "general",
        },
        "ranking_policy": {
            "section_order": ["product_news", "general"],
            "story_priority_tags": ["product"],
            "story_tie_breakers": ["published_at_desc", "story_key_asc"],
        },
    }


def test_load_beauty_editorial_policy():
    policy = load_editorial_policy("beauty")

    assert policy.taxonomy_version == "beauty-editorial.v1"
    assert policy.layout_policy.fallback_section_key == "general"
    assert policy.section_catalog[-1].section_key == "general"
    assert policy.section_catalog[-1].is_fallback is True
    assert policy.ranking_policy.section_order == [
        section.section_key for section in policy.section_catalog
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["tag_catalog"].append(value["tag_catalog"][0].copy()),
            "duplicate tag_key",
        ),
        (
            lambda value: value["section_catalog"][0]["tag_keys"].append("missing"),
            "unknown tags",
        ),
        (
            lambda value: value["section_catalog"][1].update(is_fallback=False),
            "exactly one fallback",
        ),
        (
            lambda value: value["section_catalog"][1].update(section_key="misc"),
            "fallback section_key must be 'general'",
        ),
        (
            lambda value: value["ranking_policy"].update(
                section_order=["general", "product_news"]
            ),
            "catalog order",
        ),
        (
            lambda value: value["ranking_policy"].update(
                story_tie_breakers=["story_key_asc", "published_at_desc"]
            ),
            "must end with story_key_asc",
        ),
    ],
)
def test_policy_rejects_invalid_cross_catalog_rules(mutation, message):
    raw = _valid_policy()
    mutation(raw)

    with pytest.raises(ValidationError, match=message):
        EditorialPolicy.model_validate(raw)


def test_policy_rejects_extra_fields():
    raw = _valid_policy()
    raw["unexpected"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EditorialPolicy.model_validate(raw)


def test_loader_rejects_domain_mismatch(tmp_path):
    raw = _valid_policy()
    (tmp_path / "other.v1.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="domain mismatch"):
        load_editorial_policy("other", config_dir=tmp_path)


@pytest.mark.parametrize("domain_key", ["../beauty", "Beauty", "beauty/v1"])
def test_loader_rejects_unsafe_domain_keys(domain_key):
    with pytest.raises(ValueError, match="Invalid editorial policy domain key"):
        load_editorial_policy(domain_key)
