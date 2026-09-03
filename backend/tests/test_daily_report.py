from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.auth import create_user
from app.daily_report import (
    available_report_dates,
    available_topic_report_dates,
    collect_daily_report,
    collect_topic_daily_report,
    compose_topic_edition,
    render_daily_report,
    topic_editorial_from_cards,
)
from app.domain_assignments import sync_processing_results_to_domain
from app.event_clustering import apply_cluster_plan, build_cluster_plan
from app.models import (
    ContentItem,
    ContentProcessingResult,
    CrawlRun,
    InterestTopic,
    LLMProcessingResult,
    Source,
    TopicMatch,
    User,
)
from app.web_ingestion import ingest_article


def test_daily_report_uses_shanghai_date_and_escapes_content(session_factory):
    with session_factory() as session:
        source = Source(
            name="News & Research",
            channel_type="web",
            start_url="https://report.example.com/",
            normalized_start_url="https://report.example.com/",
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
                "title": "Story <One>",
                "canonical_url": "https://report.example.com/story",
                "original_url": "https://report.example.com/story",
                "author": None,
                "published_at": datetime(2026, 8, 26, 18, 20, tzinfo=UTC),
                "body": "A report body with factual source text. " * 30,
                "description": "A report & its evidence.",
                "content_type": "article",
                "topics": [],
            },
        )
        content = session.scalar(select(ContentProcessingResult))
        assert content is None
        from app.models import ContentItem

        item = session.scalar(select(ContentItem))
        session.add(
            ContentProcessingResult(
                content_item_id=item.id,
                processor_name="sample_classifier",
                processor_version="sample.v1",
                input_content_hash=item.content_hash,
                is_relevant=True,
                matched_topics=[],
                matched_events=[],
                reason="sample",
            )
        )
        session.flush()
        ingest_article(
            session,
            source,
            run,
            {
                "title": "Story Two",
                "canonical_url": "https://report.example.com/story-two",
                "original_url": "https://report.example.com/story-two",
                "author": None,
                "published_at": datetime(2026, 8, 27, 16, 0, tzinfo=UTC),
                "body": "A second report body. " * 30,
                "description": "The next Shanghai calendar day.",
                "content_type": "article",
                "topics": [],
            },
        )
        second_item = session.scalar(select(ContentItem).where(ContentItem.title == "Story Two"))
        session.add(
            ContentProcessingResult(
                content_item_id=second_item.id,
                processor_name="sample_classifier",
                processor_version="sample.v1",
                input_content_hash=second_item.content_hash,
                is_relevant=True,
                matched_topics=[],
                matched_events=[],
                reason="sample",
            )
        )
        session.flush()
        sync_processing_results_to_domain(
            session,
            domain_key="all-news",
            domain_name="全行业",
            processor_name="sample_classifier",
            processor_version="sample.v1",
        )
        apply_cluster_plan(session, build_cluster_plan(session))
        session.commit()

        data = collect_daily_report(
            session,
            domain_key="all-news",
            issue_date=date(2026, 8, 28),
        )
        rendered = render_daily_report(data)

        assert data.issue_date.isoformat() == "2026-08-28"
        assert data.report_date.isoformat() == "2026-08-27"
        assert len(data.stories) == 1
        assert "Story &lt;One&gt;" in rendered
        assert "A report &amp; its evidence." in rendered
        assert "https://report.example.com/story" in rendered
        assert "window.print()" in rendered
        assert "2026 年 08 月 28 日出版" in rendered
        assert "覆盖" not in rendered
        assert "本期收录" not in rendered
        assert "本期整理" not in rendered
        assert "本期数据概览" not in rendered
        assert "近 7 日故事" not in rendered
        assert "CST" not in rendered
        assert 'href="http://' not in rendered
        assert 'src="http://' not in rendered
        assert "--accent: #722f37;" in rendered
        assert "Navigate · 每日简报" in rendered
        assert "M4.8 2.2h4.2v10.4" in rendered
        assert "<h1>全行业</h1>" in rendered
        assert "一、要闻" in rendered
        assert "本期资讯" not in rendered

        explicit = collect_daily_report(
            session,
            domain_key="all-news",
            coverage_date=date(2026, 8, 27),
        )
        assert explicit.issue_date.isoformat() == "2026-08-28"

        latest = collect_daily_report(
            session,
            domain_key="all-news",
            latest=True,
            issue_date=date(2026, 9, 3),
        )
        assert latest.issue_date.isoformat() == "2026-09-03"
        assert latest.report_date.isoformat() == "2026-08-28"
        assert latest.stories[0].title == "Story Two"

        assert available_report_dates(session, domain_key="all-news") == [
            (date(2026, 8, 28), 1),
            (date(2026, 8, 27), 1),
        ]

        with pytest.raises(ValueError, match="coverage date 2026-08-29"):
            collect_daily_report(
                session,
                domain_key="all-news",
                coverage_date=date(2026, 8, 29),
                issue_date=date(2026, 8, 30),
            )

        with pytest.raises(ValueError, match="mutually exclusive"):
            collect_daily_report(
                session,
                domain_key="all-news",
                coverage_date=date(2026, 8, 27),
                latest=True,
            )


def test_topic_daily_report_uses_only_topic_matches_and_publication_date(session_factory):
    with session_factory() as session:
        source = Source(
            name="English source",
            channel_type="web",
            start_url="https://example.com/",
            normalized_start_url="https://example.com/",
            parser_config={},
        )
        user = User(email="reader@example.com", display_name="Reader", password_hash="hash")
        session.add_all([source, user])
        session.flush()
        run = CrawlRun(source_id=source.id, status="succeeded")
        session.add(run)
        session.flush()
        articles = (
            ("matched", datetime(2026, 8, 30, 6, tzinfo=UTC)),
            ("other", datetime(2026, 8, 29, 6, tzinfo=UTC)),
        )
        for suffix, published_at in articles:
            ingest_article(
                session,
                source,
                run,
                {
                    "title": f"English {suffix}",
                    "canonical_url": f"https://example.com/{suffix}",
                    "original_url": f"https://example.com/{suffix}",
                    "published_at": published_at,
                    "body": "Reader-ready article body. " * 20,
                    "description": "A source summary.",
                    "content_type": "article",
                    "topics": [],
                    "validation_warnings": ["collection_window:v1"],
                },
            )
        session.flush()
        topic = InterestTopic(
            user_id=user.id,
            name="海外品牌",
            intent_text="海外品牌动态",
            compiled_intent={},
            intent_hash="a" * 64,
        )
        session.add(topic)
        session.flush()
        matched = session.scalar(select(ContentItem).where(ContentItem.title == "English matched"))
        session.add(
            TopicMatch(
                topic_id=topic.id,
                content_item_id=matched.id,
                matcher_version="test.v1",
                input_content_hash=matched.content_hash,
                decision="include",
                score=0.9,
                matched_signals={"collection_window": {"admitted": True}},
            )
        )
        session.commit()

        assert available_topic_report_dates(session, topic=topic) == [(date(2026, 8, 30), 1)]
        report = collect_topic_daily_report(session, topic=topic, coverage_date=date(2026, 8, 30))
        assert report.domain_name == "海外品牌"
        assert [story.title for story in report.stories] == ["English matched"]


def test_topic_daily_report_endpoint_renders_chinese_html(client, session_factory, monkeypatch):
    with session_factory() as session:
        user = create_user(
            session,
            email="daily-reader@example.com",
            display_name="日报读者",
            password="Daily-reader-2026!",
            role="admin",
        )
        source = Source(
            name="Topic source",
            channel_type="web",
            start_url="https://example.com/",
            normalized_start_url="https://example.com/",
            parser_config={},
        )
        session.add(source)
        session.flush()
        run = CrawlRun(source_id=source.id, status="succeeded")
        session.add(run)
        session.flush()
        ingest_article(
            session,
            source,
            run,
            {
                "title": "English source story",
                "canonical_url": "https://example.com/story",
                "original_url": "https://example.com/story",
                "published_at": datetime(2026, 8, 30, 6, tzinfo=UTC),
                "body": "A reader-ready article body. " * 20,
                "description": "A source summary.",
                "content_type": "article",
                "topics": [],
                "validation_warnings": ["collection_window:v1"],
            },
        )
        content = session.scalar(select(ContentItem))
        topic = InterestTopic(
            user_id=user.id,
            name="主题日报",
            intent_text="主题日报",
            compiled_intent={},
            intent_hash="b" * 64,
        )
        session.add(topic)
        session.flush()
        session.add(
            TopicMatch(
                topic_id=topic.id,
                content_item_id=content.id,
                matcher_version="test.v1",
                input_content_hash=content.content_hash,
                decision="include",
                score=0.9,
                matched_signals={"collection_window": {"admitted": True}},
            )
        )
        session.add(
            LLMProcessingResult(
                subject_type="content_item",
                subject_key=f"content:{content.id}",
                task_name="content_editorial_zh",
                task_version="content_editorial.zh.v2",
                schema_version="content_editorial.zh.v2",
                validator_version="content-editorial-validator.v6",
                input_hash=content.content_hash,
                provider="test",
                model="test",
                cache_key="topic-daily-card",
                status="succeeded",
                output={
                    "chinese_title": "中文主题标题",
                    "chinese_summary": "中文主题摘要，来自原始文章证据。",
                    "summary_units": [
                        {
                            "claim_ref": f"content:{content.id}#summary:1",
                            "text_zh": "中文主题摘要，来自原始文章证据。",
                            "evidence_refs": ["ref-1"],
                        }
                    ],
                    "tags": [
                        {
                            "tag_key": "theme",
                            "label_zh": "主题",
                            "kind": "topic",
                            "confidence": 0.9,
                            "evidence_refs": ["ref-1"],
                        }
                    ],
                },
            )
        )
        session.commit()
        topic_id = topic.id

    def _boom(*_args, **_kwargs):
        raise AssertionError("daily report must reuse content cards")

    monkeypatch.setattr("app.topic_intelligence.process_topic_contents", _boom)
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "daily-reader@example.com", "password": "Daily-reader-2026!"},
    ).status_code == 200
    response = client.get(f"/api/v1/topics/{topic_id}/daily-reports/2026-08-30")
    assert response.status_code == 200
    assert "<h1>主题日报</h1>" in response.text
    assert "中文主题标题" in response.text
    assert "中文主题摘要，来自原始文章证据。" in response.text
    assert "一、要闻" in response.text
    assert "本期资讯" not in response.text
    assert "本期整理" not in response.text
    assert "已发布资讯" not in response.text
    assert "--accent: #722f37;" in response.text


def test_compose_topic_edition_uses_top_story_and_tag_sections():
    editorial = {
        "stories": [
            {
                "story_key": "content:1",
                "chinese_title": "融资落地",
                "chinese_summary": "某公司完成新一轮融资。",
                "tags": [{"label_zh": "融资"}],
            },
            {
                "story_key": "content:2",
                "chinese_title": "监管发文",
                "chinese_summary": "监管部门发布新规。",
                "tags": [{"label_zh": "监管"}],
            },
        ]
    }
    edition = compose_topic_edition("具身智能", editorial, ["content:1", "content:2"])
    assert edition["daily_lead"]["deck"] == "融资落地；监管发文"
    assert edition["daily_lead"]["text"] == "某公司完成新一轮融资。监管部门发布新规。"
    assert edition["daily_lead"]["story_refs"] == ["content:1", "content:2"]
    assert [section["title"] for section in edition["sections"]] == ["融资", "监管"]
    assert "本期资讯" not in str(edition)


def test_compose_topic_edition_uses_first_sentence_as_lead():
    editorial = {
        "stories": [
            {
                "story_key": "content:1",
                "chinese_title": "孙鹏加入星尘智能",
                "chinese_summary": (
                    "9月2日，孙鹏博士正式加入星尘智能。"
                    "他毕业于清华大学，曾负责强化学习基础设施。"
                    "星尘智能将强化机器人后训练布局。"
                ),
                "tags": [{"label_zh": "星尘智能"}],
            }
        ]
    }
    edition = compose_topic_edition("具身智能创业融资", editorial, ["content:1"])
    assert edition["daily_lead"]["text"] == "9月2日，孙鹏博士正式加入星尘智能。"
    assert "清华大学" not in edition["daily_lead"]["text"]
    assert "清华大学" in edition["stories"][0]["chinese_summary"]
    assert [section["title"] for section in edition["sections"]] == ["人事"]


def test_compose_topic_edition_ignores_entity_tags_as_sections():
    editorial = {
        "stories": [
            {
                "story_key": "content:41",
                "chinese_title": "孙鹏加入星尘智能",
                "chinese_summary": "孙鹏博士正式加入星尘智能。",
                "tags": [{"label_zh": "星尘智能"}, {"label_zh": "机器人"}],
            },
            {
                "story_key": "content:3",
                "chinese_title": "机器人赛道，全是汽车人",
                "chinese_summary": "机器人赛道出现多笔融资。",
                "tags": [{"label_zh": "机器人"}, {"label_zh": "融资"}],
            },
        ]
    }
    edition = compose_topic_edition("具身智能创业融资", editorial, ["content:41", "content:3"])
    assert [section["title"] for section in edition["sections"]] == ["人事", "融资"]
    assert "星尘智能" not in [section["title"] for section in edition["sections"]]
    assert "机器人" not in [section["title"] for section in edition["sections"]]
    assert "孙鹏博士正式加入星尘智能。" in edition["daily_lead"]["text"]
    assert "机器人赛道出现多笔融资。" in edition["daily_lead"]["text"]


def test_compose_topic_edition_splits_untagged_stories():
    editorial = {
        "stories": [
            {
                "story_key": "content:1",
                "chinese_title": "头条事实",
                "chinese_summary": "这是当天最重要的一条进展。",
                "tags": [],
            },
            {
                "story_key": "content:2",
                "chinese_title": "后续事实",
                "chinese_summary": "另一条相关进展。",
                "tags": [],
            },
        ]
    }
    edition = compose_topic_edition("主题", editorial, ["content:1", "content:2"])
    assert [section["title"] for section in edition["sections"]] == ["要闻"]
    assert edition["sections"][0]["story_refs"] == ["content:1", "content:2"]
    assert edition["daily_lead"]["deck"] == "头条事实；后续事实"
    assert "这是当天最重要的一条进展。" in edition["daily_lead"]["text"]
    assert "另一条相关进展。" in edition["daily_lead"]["text"]



def test_topic_editorial_from_cards_uses_content_cards(session_factory):
    with session_factory() as session:
        source = Source(
            name="Card Desk",
            channel_type="web",
            start_url="https://cards.example.com/",
            normalized_start_url="https://cards.example.com/",
            parser_config={},
        )
        session.add(source)
        session.flush()
        run = CrawlRun(source_id=source.id, status="succeeded")
        session.add(run)
        session.flush()
        ingest_article(
            session,
            source,
            run,
            {
                "title": "English source story",
                "canonical_url": "https://cards.example.com/story",
                "original_url": "https://cards.example.com/story",
                "published_at": datetime(2026, 8, 30, 6, tzinfo=UTC),
                "body": "A reader-ready article body. " * 20,
                "description": "A source summary.",
                "content_type": "article",
                "topics": [],
            },
        )
        content = session.scalar(select(ContentItem))
        session.add(
            LLMProcessingResult(
                subject_type="content_item",
                subject_key=f"content:{content.id}",
                task_name="content_editorial_zh",
                task_version="content_editorial.zh.v2",
                schema_version="content_editorial.zh.v2",
                validator_version="content-editorial-validator.v6",
                input_hash=content.content_hash,
                provider="test",
                model="test",
                cache_key="card-reuse",
                status="succeeded",
                output={
                    "chinese_title": "卡片中文标题",
                    "summary_units": [
                        {
                            "claim_ref": f"content:{content.id}#summary:1",
                            "text_zh": "卡片中文摘要。",
                            "evidence_refs": ["ref-1"],
                        }
                    ],
                    "tags": [
                        {
                            "tag_key": "financing",
                            "label_zh": "融资",
                            "kind": "event",
                            "confidence": 0.9,
                            "evidence_refs": ["ref-1"],
                        }
                    ],
                },
            )
        )
        session.commit()
        editorial = topic_editorial_from_cards(session, [(content, source)])
        assert editorial["stories"] == [
            {
                "story_key": f"content:{content.id}",
                "chinese_title": "卡片中文标题",
                "chinese_summary": "卡片中文摘要。",
                "tags": [{"label_zh": "融资"}],
            }
        ]
