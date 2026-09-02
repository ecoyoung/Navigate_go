from types import SimpleNamespace

from app.domain_relevance import evaluate_domain_relevance, load_domain_relevance_policy
from app.llm_domain_relevance import LLM_REVIEW_REASONS


def _content(title: str, body: str = "", topics: list[str] | None = None):
    return SimpleNamespace(title=title, excerpt=None, body=body, topics=topics or [])


def _source(tags: list[str]):
    return SimpleNamespace(source_tags=tags)


def test_dedicated_domain_source_is_included_from_configured_source_tag():
    policy = load_domain_relevance_policy("beauty")
    decision = evaluate_domain_relevance(
        _content("公司公布季度业绩"),
        _source(["beauty", "industry"]),
        policy,
    )
    assert decision.is_relevant
    assert decision.reason == "dedicated_domain_source"
    assert decision.matched_source_tags == ["beauty"]


def test_general_source_requires_article_level_domain_evidence():
    policy = load_domain_relevance_policy("beauty")
    relevant = evaluate_domain_relevance(
        _content("品牌发布新品", "这是一款面向敏感肌的护肤产品。"),
        _source(["公众号"]),
        policy,
    )
    sports = evaluate_domain_relevance(
        _content("体育迷期待更流畅的一体化数字体验"),
        _source(["公众号"]),
        policy,
    )
    food = evaluate_domain_relevance(
        _content("抖音2026年7月肠类制品榜单"),
        _source(["公众号"]),
        policy,
    )
    assert not relevant.is_relevant and relevant.reason == "needs_llm_domain_review"
    assert "护肤" in relevant.matched_content_keywords
    assert not sports.is_relevant and sports.reason == "no_domain_evidence"
    assert not food.is_relevant and food.reason == "no_domain_evidence"


def test_ascii_keywords_use_word_boundaries():
    policy = load_domain_relevance_policy("beauty")
    decision = evaluate_domain_relevance(
        _content("Space technology market update"),
        _source(["research"]),
        policy,
    )
    assert not decision.is_relevant


def test_both_ambiguous_source_modes_are_reserved_for_llm_review():
    assert LLM_REVIEW_REASONS == {
        "needs_llm_domain_review",
        "dedicated_domain_source",
    }
