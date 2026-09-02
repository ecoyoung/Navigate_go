from scripts.resolve_redfox_accounts import (
    choose_exact_candidate,
    normalized_name,
    selector_from_candidate,
)


def test_normalized_name_allows_spacing_case_and_public_account_suffix():
    assert normalized_name("花西子 Florasis公众号") == normalized_name("花西子Florasis")


def test_choose_exact_candidate_rejects_near_match_and_ambiguous_identity():
    candidates = [
        {"accountName": "青眼情报", "account": "other"},
        {"accountName": "青眼", "account": "first", "bizInfo": "biz-1"},
        {"accountName": "青眼", "account": "second", "bizInfo": "biz-2"},
    ]

    assert choose_exact_candidate("青眼", candidates) is None


def test_choose_exact_candidate_prefers_literal_name_over_punctuation_variant():
    candidates = [
        {"accountName": "36氪", "account": "official", "bizInfo": "biz-1"},
        {"accountName": "36氪）", "account": "unrelated", "bizInfo": "biz-2"},
    ]

    assert choose_exact_candidate("36氪", candidates)["account"] == "official"


def test_selector_prefers_wxid_and_requires_bizinfo():
    selected = selector_from_candidate(
        {"account": "alias", "wxId": "gh_original", "bizInfo": "biz-value"}
    )

    assert selected == ("wxId", "gh_original", "biz-value")
    assert selector_from_candidate({"account": "alias"}) is None
