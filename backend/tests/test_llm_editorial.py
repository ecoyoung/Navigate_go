import json
from datetime import UTC, date, datetime

from sqlalchemy import func, select

from app.daily_report import collect_daily_report, render_daily_report
from app.domain_assignments import sync_processing_results_to_domain
from app.editorial_policy import load_editorial_policy
from app.event_clustering import apply_cluster_plan, build_cluster_plan
from app.llm_editorial import (
    CONTENT_SCHEMA_VERSION,
    EDITION_SCHEMA_VERSION,
    DailyEdition,
    DeepSeekClient,
    LLMResponse,
    LLMUsage,
    _complete_daily_edition,
    _derive_subset_edition,
    _is_supported_number,
    _validate_daily_edition,
    build_content_editorial_input,
    enrich_daily_report,
)
from app.models import (
    ContentItem,
    ContentProcessingResult,
    CrawlRun,
    LLMProcessingResult,
    Source,
)
from app.web_ingestion import ingest_article


class FakeEditorialClient:
    model = "deepseek-v4-flash"
    provider = "deepseek"

    def __init__(self):
        self.calls = 0

    def generate_json(self, *, system_prompt: str, user_prompt: str) -> LLMResponse:
        assert "JSON" in system_prompt
        self.calls += 1
        source = json.loads(user_prompt.split("\n", 1)[1])
        if CONTENT_SCHEMA_VERSION in system_prompt:
            items = []
            for item in source["items"]:
                ref = item["evidence"][0]["ref"]
                content_ref = item["content_ref"]
                items.append(
                    {
                        "content_ref": content_ref,
                        "input_content_hash": item["input_content_hash"],
                        "chinese_title": "国际品牌发布香水新品",
                        "title_evidence_refs": [ref],
                        "summary_units": [
                            {
                                "claim_ref": f"{content_ref}#summary:1",
                                "text_zh": "该品牌发布香水，并介绍相关产品策略。",
                                "evidence_refs": [ref],
                            }
                        ],
                        "tags": [
                            {
                                "tag_key": "product_launch",
                                "label_zh": "产品发布",
                                "kind": "event",
                                "confidence": 0.9,
                                "evidence_refs": [ref],
                            }
                        ],
                    }
                )
            output = {"schema_version": CONTENT_SCHEMA_VERSION, "items": items}
        else:
            story_refs = [item["story_ref"] for item in source["stories"]]
            output = {
                "schema_version": EDITION_SCHEMA_VERSION,
                "daily_lead": {
                    "deck": "香水新品 · 品牌动态",
                    "text": "本期关注国际品牌发布香水新品及其产品策略。",
                    "story_refs": story_refs,
                },
                "sections": [
                    {
                        "section_key": "product_innovation",
                        "title": "产品与创新",
                        "intro": "本栏目汇总品牌发布信息。",
                        "intro_story_refs": story_refs,
                        "story_refs": story_refs,
                    }
                ],
            }
        return LLMResponse(output, LLMUsage(120, 80, 200))


def prepare_report(session):
    source = Source(
        name="International Desk",
        channel_type="web",
        start_url="https://international.example.com/",
        normalized_start_url="https://international.example.com/",
        parser_config={},
    )
    session.add(source)
    session.flush()
    run = CrawlRun(
        source_id=source.id,
        status="succeeded",
        started_at=datetime(2026, 8, 27, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
    )
    session.add(run)
    session.flush()
    ingest_article(
        session,
        source,
        run,
        {
            "title": "A global brand launches a fragrance",
            "canonical_url": "https://international.example.com/story",
            "original_url": "https://international.example.com/story",
            "author": None,
            "published_at": datetime(2026, 8, 27, 0, 20, tzinfo=UTC),
            "body": "The brand introduced a fragrance and described its product strategy. " * 20,
            "description": "The brand introduced a fragrance.",
            "content_type": "article",
            "topics": [],
        },
    )
    content = session.scalar(select(ContentItem))
    session.add(
        ContentProcessingResult(
            content_item_id=content.id,
            processor_name="editorial_test",
            processor_version="editorial.v1",
            input_content_hash=content.content_hash,
            is_relevant=True,
            matched_topics=[],
            matched_events=[],
            reason="test_match",
        )
    )
    session.flush()
    sync_processing_results_to_domain(
        session,
        domain_key="beauty",
        domain_name="美妆",
        processor_name="editorial_test",
        processor_version="editorial.v1",
    )
    apply_cluster_plan(session, build_cluster_plan(session))
    session.commit()
    return collect_daily_report(
        session,
        domain_key="beauty",
        issue_date=date(2026, 8, 28),
    )


def test_editorial_generation_is_cached_and_rendered_as_chinese(session_factory):
    with session_factory() as session:
        data = prepare_report(session)
        client = FakeEditorialClient()
        enriched, cache_hit, usage = enrich_daily_report(session, data, client)
        repeated, repeated_hit, _ = enrich_daily_report(session, data, client)

        assert cache_hit is False and repeated_hit is True
        assert client.calls == 2
        assert usage.total_tokens == 400
        assert repeated.editorial == enriched.editorial
        assert session.scalar(select(func.count(LLMProcessingResult.id))) == 2
        rendered = render_daily_report(enriched)
        assert "国际品牌发布香水新品" in rendered
        assert "香水新品 · 品牌动态" in rendered
        assert "该品牌发布香水" in rendered
        assert "产品与创新" in rendered
        assert "产品发布" in rendered
        assert "今日重点" not in rendered
        assert "快讯" not in rendered
        assert "未调用 LLM" not in rendered
        assert "算法版本" not in rendered


def test_evidence_refs_are_stable_and_include_positions(session_factory):
    with session_factory() as session:
        prepare_report(session)
        content = session.scalar(select(ContentItem))
        source = session.scalar(select(Source))
        first = build_content_editorial_input(content, source)
        second = build_content_editorial_input(content, source)
        assert first.evidence == second.evidence
        assert all(f"content:{content.id}@" in item.ref for item in first.evidence)
        assert all(item.end_char > item.start_char for item in first.evidence)


def test_daily_edition_rejects_duplicate_story_placement():
    edition = DailyEdition.model_validate(
        {
            "schema_version": EDITION_SCHEMA_VERSION,
            "daily_lead": {
                "deck": "今日品牌动态",
                "text": "本期汇总两条品牌资讯，并按主题进行统一编排。",
                "story_refs": ["content:1"],
            },
            "sections": [
                {
                    "section_key": "one",
                    "title": "动态",
                    "intro": "第一组资讯。",
                    "intro_story_refs": ["content:1"],
                    "story_refs": ["content:1"],
                },
                {
                    "section_key": "two",
                    "title": "其他",
                    "intro": "第二组资讯。",
                    "intro_story_refs": ["content:1", "content:2"],
                    "story_refs": ["content:1", "content:2"],
                },
            ],
        }
    )
    source = {"stories": [{"story_ref": "content:1"}, {"story_ref": "content:2"}]}
    try:
        _validate_daily_edition(edition, source)
    except ValueError as exc:
        assert "exactly once" in str(exc)
    else:
        raise AssertionError("duplicate story placement was accepted")


def test_daily_edition_contract_appends_omitted_story_to_matching_section():
    edition = DailyEdition.model_validate(
        {
            "schema_version": EDITION_SCHEMA_VERSION,
            "daily_lead": {
                "deck": "今日品牌动态",
                "text": "本期关注品牌建设与经营动作。",
                "story_refs": ["content:1"],
            },
            "sections": [
                {
                    "section_key": "company_strategy",
                    "title": "公司与品牌",
                    "intro": "本栏目收录品牌经营资讯。",
                    "intro_story_refs": ["content:1"],
                    "story_refs": ["content:1"],
                }
            ],
        }
    )
    source = {
        "stories": [
            {
                "story_ref": "content:1",
                "articles": [
                    {
                        "chinese_title": "企业推进品牌战略",
                        "summary_units": [{"text_zh": "企业推进品牌战略。"}],
                        "tags": [
                            {"tag_key": "company_strategy", "label_zh": "公司战略"}
                        ],
                    }
                ],
            },
            {
                "story_ref": "content:2",
                "articles": [
                    {
                        "chinese_title": "品牌更新经营定位",
                        "summary_units": [{"text_zh": "品牌更新经营定位。"}],
                        "tags": [
                            {"tag_key": "brand_development", "label_zh": "品牌发展"}
                        ],
                    }
                ],
            },
        ]
    }

    completed = _complete_daily_edition(
        edition, source, load_editorial_policy("beauty")
    )

    assert completed.sections[0].story_refs == ["content:1", "content:2"]
    _validate_daily_edition(completed, source, load_editorial_policy("beauty"))


def test_cached_edition_can_be_pruned_after_a_story_is_removed():
    prior = DailyEdition.model_validate(
        {
            "schema_version": EDITION_SCHEMA_VERSION,
            "daily_lead": {
                "deck": "保留故事与待删除故事",
                "text": "本期同时关注保留故事与待删除故事。",
                "story_refs": ["event:1", "event:2"],
            },
            "sections": [
                {
                    "section_key": "product_innovation",
                    "title": "产品与创新",
                    "intro": "保留故事与待删除故事均位于本栏目。",
                    "intro_story_refs": ["event:1", "event:2"],
                    "story_refs": ["event:1", "event:2"],
                }
            ],
        }
    )
    source = {
        "stories": [
            {
                "story_ref": "event:1",
                "representative_content_ref": "content:1",
                "articles": [
                    {
                        "content_ref": "content:1",
                        "chinese_title": "品牌发布护肤新品",
                        "summary_units": [{"text_zh": "品牌发布一款护肤新品。"}],
                        "tags": [{"label_zh": "产品发布"}],
                    }
                ],
            }
        ]
    }

    derived = _derive_subset_edition(
        prior, source, load_editorial_policy("beauty")
    )

    assert derived is not None
    assert derived.daily_lead.story_refs == ["event:1"]
    assert derived.sections[0].story_refs == ["event:1"]
    assert "待删除" not in derived.daily_lead.text
    assert "待删除" not in derived.sections[0].intro


def test_daily_edition_rejects_section_outside_domain_policy():
    edition = DailyEdition.model_validate(
        {
            "schema_version": EDITION_SCHEMA_VERSION,
            "daily_lead": {
                "deck": "品牌与产品动态",
                "text": "本期收录一条经过核验的品牌产品资讯。",
                "story_refs": ["content:1"],
            },
            "sections": [
                {
                    "section_key": "invented_section",
                    "title": "模型自创栏目",
                    "intro": "本栏目收录一条品牌产品资讯。",
                    "intro_story_refs": ["content:1"],
                    "story_refs": ["content:1"],
                }
            ],
        }
    )
    source = {
        "stories": [
            {
                "story_ref": "content:1",
                "articles": [
                    {
                        "chinese_title": "品牌发布产品",
                        "summary_units": [{"text_zh": "品牌发布一款产品。"}],
                        "tags": [{"label_zh": "产品发布"}],
                    }
                ],
            }
        ]
    }
    try:
        _validate_daily_edition(edition, source, load_editorial_policy("beauty"))
    except ValueError as exc:
        assert "domain policy" in str(exc)
    else:
        raise AssertionError("unknown section was accepted")


def test_preferred_section_size_does_not_force_cross_topic_grouping():
    edition = DailyEdition.model_validate(
        {
            "schema_version": EDITION_SCHEMA_VERSION,
            "daily_lead": {
                "deck": "资本与产品动态",
                "text": "本期分别收录资本活动与产品发布资讯。",
                "story_refs": ["content:1", "content:2"],
            },
            "sections": [
                {
                    "section_key": "capital_policy",
                    "title": "资本与监管",
                    "intro": "本栏目收录资本活动资讯。",
                    "intro_story_refs": ["content:1"],
                    "story_refs": ["content:1"],
                },
                {
                    "section_key": "product_innovation",
                    "title": "产品与创新",
                    "intro": "本栏目收录产品发布资讯。",
                    "intro_story_refs": ["content:2"],
                    "story_refs": ["content:2"],
                },
            ],
        }
    )
    source = {
        "stories": [
            {
                "story_ref": "content:1",
                "articles": [
                    {
                        "chinese_title": "企业披露资本活动",
                        "summary_units": [{"text_zh": "企业披露资本活动。"}],
                        "tags": [{"label_zh": "资本活动"}],
                    }
                ],
            },
            {
                "story_ref": "content:2",
                "articles": [
                    {
                        "chinese_title": "品牌发布产品",
                        "summary_units": [{"text_zh": "品牌发布一款产品。"}],
                        "tags": [{"label_zh": "产品发布"}],
                    }
                ],
            },
        ]
    }
    _validate_daily_edition(edition, source, load_editorial_policy("beauty"))


def test_deepseek_request_omits_max_tokens(monkeypatch):
    captured = {}
    response_body = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": json.dumps({"ok": True})},
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return response_body

    class FakeHTTPClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("app.llm_editorial.httpx.Client", FakeHTTPClient)
    client = DeepSeekClient(api_key="test-secret")
    result = client.generate_json(system_prompt="Return JSON", user_prompt="JSON please")

    assert result.output == {"ok": True}
    assert captured["payload"]["model"] == "deepseek-v4-flash"
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert "max_tokens" not in captured["payload"]
    assert captured["headers"]["Authorization"] == "Bearer test-secret"


def test_billion_to_yi_is_the_only_supported_numeric_localization():
    evidence = "At a $10.1 Billion valuation, the company introduced a product."
    assert _is_supported_number("101", evidence) is True
    assert _is_supported_number("102", evidence) is False
    revenue_evidence = "The company expects annual revenue from $2 million to $5 million."
    assert _is_supported_number("200", revenue_evidence) is True
    assert _is_supported_number("500", revenue_evidence) is True
    assert _is_supported_number("600", revenue_evidence) is False
    abbreviated_revenue = "2026 Full Year Projected Revenue: $2M - $5M."
    assert _is_supported_number("200", abbreviated_revenue) is True
    assert _is_supported_number("500", abbreviated_revenue) is True
    assert _is_supported_number("600", abbreviated_revenue) is False
    assert _is_supported_number("1", "The product is scheduled for January 2026") is True
    assert _is_supported_number("1.91", "Spa visits reached 191 million", "达到1.91亿次")
    assert not _is_supported_number("1.91", "Spa visits reached 191 million")
