from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.auth import create_user
from app.models import ContentItem, CrawlRun, InterestTopic, RawItem, Source, TopicMatch
from app.topic_matching import compile_topic_intent, match_content, match_contents_to_topics

PASSWORD = "Admin-password-2026"


def _topic(db, user_id, intent, *, keywords=None, status="active"):
    compiled, intent_hash = compile_topic_intent(intent, keywords=keywords)
    topic = InterestTopic(
        user_id=user_id,
        name=compiled["positive_keywords"][0][:24],
        intent_text=intent,
        compiled_intent=compiled,
        intent_hash=intent_hash,
        status=status,
    )
    db.add(topic)
    db.flush()
    return topic


def _content(db, source, *, title, body, language, published, identity):
    crawl = CrawlRun(source_id=source.id, status="succeeded", trigger="manual")
    db.add(crawl)
    db.flush()
    raw = RawItem(
        source_id=source.id,
        crawl_run_id=crawl.id,
        identity_key=identity,
        original_url=f"https://example.com/{identity[:8]}",
        canonical_url=f"https://example.com/{identity[:8]}",
        payload={"title": title},
        payload_sha256=identity,
    )
    db.add(raw)
    db.flush()
    content = ContentItem(
        source_id=source.id,
        raw_item_id=raw.id,
        identity_key=identity,
        title=title,
        canonical_url=raw.canonical_url,
        excerpt=title,
        body=body,
        language=language,
        content_hash=identity,
        published_at=published,
        quality={"body_complete": True, "metadata_only": False},
    )
    db.add(content)
    db.flush()
    return content, crawl


def test_compile_keeps_chinese_and_english_phrases():
    compiled, _hash = compile_topic_intent("关注 embodied AI 与具身智能融资，排除促销")
    assert "embodied ai" in compiled["positive_keywords"]
    assert "具身智能" in compiled["positive_keywords"]
    assert "具身智能融资" in compiled["positive_keywords"]
    assert "促销" in compiled["excluded_keywords"]
    assert not any(item.startswith("与") for item in compiled["positive_keywords"])
    mixed, _ = compile_topic_intent("K-beauty 韩妆")
    assert "k-beauty" in mixed["positive_keywords"]
    assert "韩妆" in mixed["positive_keywords"]
    compound, _ = compile_topic_intent("具身智能创业融资")
    assert "具身智能" in compound["positive_keywords"]
    assert "创业融资" in compound["positive_keywords"]
    assert "embodied ai" in compound["positive_keywords"]
    ingredients, _ = compile_topic_intent("美妆原料监控")
    assert "ingredient" in ingredients["positive_keywords"]
    assert "actives" in ingredients["positive_keywords"]
    assert "raw material" in ingredients["positive_keywords"]
    global_ai, _ = compile_topic_intent("持续关注全球AI资讯")
    assert "ai" in global_ai["positive_keywords"]
    assert "全球ai资讯" in global_ai["positive_keywords"]
    assert "全球" not in global_ai["positive_keywords"]
    assert "资讯" not in global_ai["positive_keywords"]
    excluded, _ = compile_topic_intent("sunscreen launches, exclude promotions")
    assert "sunscreen launches" in excluded["positive_keywords"]
    assert "promotions" in excluded["excluded_keywords"]


def test_english_word_boundary_does_not_match_inside_words(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="a@example.com", display_name="A", password=PASSWORD
        )
        topic = _topic(db, user.id, "care")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        inside, _ = _content(
            db,
            source,
            title="Medicare pricing update",
            body="Healthcare policy notes for hospitals.",
            language="en",
            published=datetime.now(UTC),
            identity="a" * 64,
        )
        exact, _ = _content(
            db,
            source,
            title="Skin care routine",
            body="A new skin care line launches this week.",
            language="en",
            published=datetime.now(UTC),
            identity="b" * 64,
        )
        assert match_content(topic, inside).decision == "exclude"
        assert match_content(topic, exact).decision == "include"


def test_mixed_intent_matches_english_k_beauty_and_chinese_copy(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="b@example.com", display_name="B", password=PASSWORD
        )
        topic = _topic(db, user.id, "K-beauty 韩妆")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        english, _ = _content(
            db,
            source,
            title="Amazon Builds Its K-Beauty Advantage",
            body="Retailers are expanding Korean beauty shelves in the US.",
            language="en",
            published=datetime.now(UTC),
            identity="c" * 64,
        )
        chinese, _ = _content(
            db,
            source,
            title="韩妆品牌加速出海",
            body="多家韩妆公司公布东南亚渠道计划。",
            language="zh-CN",
            published=datetime.now(UTC),
            identity="d" * 64,
        )
        assert match_content(topic, english).decision == "include"
        assert match_content(topic, chinese).decision == "include"


def test_chinese_only_topic_matches_english_alias(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="c@example.com", display_name="C", password=PASSWORD
        )
        topic = _topic(db, user.id, "具身智能创业融资")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        english, _ = _content(
            db,
            source,
            title="Embodied AI startup raises Series A",
            body="The robotics company closed a financing round in Silicon Valley.",
            language="en",
            published=datetime.now(UTC),
            identity="e" * 64,
        )
        unrelated, _ = _content(
            db,
            source,
            title="Cloud vendor reports quarterly earnings",
            body="The software company posted higher subscription revenue.",
            language="en",
            published=datetime.now(UTC),
            identity="e1" * 32,
        )
        assert match_content(topic, english).decision == "include"
        assert match_content(topic, unrelated).decision == "exclude"


def test_query_expansions_let_english_article_match_chinese_topic(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="d@example.com", display_name="D", password=PASSWORD
        )
        topic = _topic(db, user.id, "具身智能创业融资")
        topic.compiled_intent = {
            **topic.compiled_intent,
            "query_expansions": ["embodied AI", "robotics startup financing"],
        }
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        english, _ = _content(
            db,
            source,
            title="Embodied AI startup raises Series A",
            body="The robotics company closed a financing round in Silicon Valley.",
            language="en",
            published=datetime.now(UTC),
            identity="f" * 64,
        )
        assert match_content(topic, english).decision == "include"


def test_exclusion_works_in_english_and_chinese(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="e@example.com", display_name="E", password=PASSWORD
        )
        topic = _topic(db, user.id, "防晒新品，排除促销")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        hit, _ = _content(
            db,
            source,
            title="防晒新品发布会促销专场",
            body="全场促销折扣。",
            language="zh-CN",
            published=datetime.now(UTC),
            identity="g" * 64,
        )
        assert match_content(topic, hit).decision == "exclude"


def test_crawl_distribution_assigns_readable_items_to_all_active_topics(session_factory):
    with session_factory() as db:
        user = create_user(
            db,
            email="f@example.com",
            display_name="F",
            password="Admin-password-2026",
            role="admin",
        )
        active = _topic(db, user.id, "K-beauty 韩妆")
        paused = _topic(db, user.id, "K-beauty 韩妆", status="paused")
        source = Source(
            name="BeautyMatter",
            channel_type="web",
            start_url="https://beautymatter.com",
            normalized_start_url="https://beautymatter.com/",
        )
        db.add(source)
        db.flush()
        content, crawl = _content(
            db,
            source,
            title="Amazon Builds Its K-Beauty Advantage",
            body="Retailers are expanding Korean beauty shelves in the US." * 4,
            language="en",
            published=datetime.now(UTC),
            identity="h" * 64,
        )
        stale, _ = _content(
            db,
            source,
            title="Old K-Beauty note",
            body="This article is from last year and should stay out of new matches." * 3,
            language="en",
            published=datetime.now(UTC) - timedelta(days=30),
            identity="i" * 64,
        )
        stats = match_contents_to_topics(db, [content, stale])
        db.commit()
        matches = list(db.scalars(select(TopicMatch)))
        by_topic = {item.topic_id: item for item in matches if item.content_item_id == content.id}
        assert active.id in by_topic
        assert paused.id not in by_topic
        assert by_topic[active.id].decision == "include"
        assert not any(item.content_item_id == stale.id for item in matches)
        assert stats["included"] >= 1


def test_chinese_compound_intent_matches_partial_title(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="g@example.com", display_name="G", password=PASSWORD
        )
        topic = _topic(db, user.id, "具身智能创业融资")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        content, _ = _content(
            db,
            source,
            title="00后清华博士闯入具身智能赛道，5个月融资超5亿元",
            body="该公司完成新一轮创业融资。",
            language="zh-CN",
            published=datetime.now(UTC),
            identity="j" * 64,
        )
        assert match_content(topic, content).decision == "include"


def test_global_ai_topic_does_not_match_generic_chinese_news(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="h@example.com", display_name="H", password=PASSWORD
        )
        topic = _topic(db, user.id, "持续关注全球AI资讯")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        generic, _ = _content(
            db,
            source,
            title="經濟部國際貿易署 ::: 全球商機資訊",
            body="这是一则国际贸易商机通报，没有提到人工智能。",
            language="zh-CN",
            published=datetime.now(UTC),
            identity="k" * 64,
        )
        ai_hit, _ = _content(
            db,
            source,
            title="全球AI产业链加速整合",
            body="多家公司公布人工智能模型进展。",
            language="zh-CN",
            published=datetime.now(UTC),
            identity="l" * 64,
        )
        assert match_content(topic, generic).decision == "exclude"
        assert match_content(topic, ai_hit).decision == "include"


def test_traditional_chinese_and_english_plurals_match(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="i@example.com", display_name="I", password=PASSWORD
        )
        beauty = _topic(db, user.id, "K-beauty 韩妆")
        startup = _topic(db, user.id, "robotics startup")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        traditional, _ = _content(
            db,
            source,
            title="韓妝品牌加速出海",
            body="多家韓妝公司公布东南亚渠道计划。",
            language="zh-TW",
            published=datetime.now(UTC),
            identity="m" * 64,
        )
        plural, _ = _content(
            db,
            source,
            title="Robotics startups raise Series A",
            body="Two robotics startups closed financing rounds this week.",
            language="en",
            published=datetime.now(UTC),
            identity="n" * 64,
        )
        assert match_content(beauty, traditional).decision == "include"
        assert match_content(startup, plural).decision == "include"


def test_english_exclude_keyword_is_honored(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="j@example.com", display_name="J", password=PASSWORD
        )
        topic = _topic(db, user.id, "sunscreen launches, exclude promotions")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        hit, _ = _content(
            db,
            source,
            title="Sunscreen launches with big promotions",
            body="Retailers are running promotions on new sunscreen launches.",
            language="en",
            published=datetime.now(UTC),
            identity="o" * 64,
        )
        assert match_content(topic, hit).decision == "exclude"


def test_short_latin_token_only_counts_in_title(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="k@example.com", display_name="K", password=PASSWORD
        )
        topic = _topic(db, user.id, "持续关注全球AI资讯")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        excerpt_only, _ = _content(
            db,
            source,
            title="Dyson Takes on Oral Care with CameraJet Toothbrush",
            body="The device combines AI, water flossing, and years of R&D.",
            language="en",
            published=datetime.now(UTC),
            identity="p" * 64,
        )
        excerpt_only.excerpt = "The device combines AI, water flossing, and years of R&D."
        title_hit, _ = _content(
            db,
            source,
            title="AI News & Artificial Intelligence",
            body="Daily coverage of machine learning labs.",
            language="en",
            published=datetime.now(UTC),
            identity="q" * 64,
        )
        assert match_content(topic, excerpt_only).decision == "exclude"
        assert match_content(topic, title_hit).decision == "include"


def test_event_types_do_not_include_unrelated_articles(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="n@example.com", display_name="N", password=PASSWORD
        )
        topic = _topic(db, user.id, "具身智能创业融资")
        topic.compiled_intent = {
            **topic.compiled_intent,
            "event_types": ["融资", "投资", "创业"],
            "query_expansions": ["embodied AI funding"],
        }
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        bank, _ = _content(
            db,
            source,
            title="银行融资新规发布",
            body="监管部门公布创业投资和融资政策。",
            language="zh-CN",
            published=datetime.now(UTC),
            identity="r" * 64,
        )
        english, _ = _content(
            db,
            source,
            title="Embodied AI funding round closes",
            body="The humanoid robot startup announced embodied AI funding.",
            language="en",
            published=datetime.now(UTC),
            identity="s" * 64,
        )
        assert match_content(topic, bank).decision == "exclude"
        assert match_content(topic, english).decision == "include"


def test_llm_exclusions_and_products_do_not_overreach(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="p@example.com", display_name="P", password=PASSWORD
        )
        topic = _topic(db, user.id, "具身智能创业融资")
        topic.compiled_intent = {
            **topic.compiled_intent,
            "excluded_keywords": ["招聘", "jobs"],
            "user_excluded_keywords": ["招聘", "jobs"],
            "products": ["人形机器人", "智能机器人"],
            "query_expansions": ["embodied AI funding"],
        }
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        jobs_footer, _ = _content(
            db,
            source,
            title="985教授们集体创业做具身智能，今年已拿下超百亿元融资",
            body="文章正文。 Careers and jobs at the publisher.",
            language="zh-CN",
            published=datetime.now(UTC),
            identity="t" * 64,
        )
        appliance, _ = _content(
            db,
            source,
            title="美的集团一周动向",
            body="美的继续推进人形机器人和智能家居业务。",
            language="zh-CN",
            published=datetime.now(UTC),
            identity="u" * 64,
        )
        assert match_content(topic, jobs_footer).decision == "include"
        assert match_content(topic, appliance).decision == "exclude"


def test_ingredient_aliases_match_actives_not_retail_beauty(session_factory):
    with session_factory() as db:
        user = create_user(
            db, email="q@example.com", display_name="Q", password=PASSWORD
        )
        topic = _topic(db, user.id, "美妆原料监控")
        source = Source(
            name="S",
            channel_type="web",
            start_url="https://example.com",
            normalized_start_url="https://example.com/",
        )
        db.add(source)
        db.flush()
        actives, _ = _content(
            db,
            source,
            title="BASF Reimagines Floral Actives Through Epigenetics",
            body="Epigenetic science is beginning to shape new skincare ingredients.",
            language="en",
            published=datetime.now(UTC),
            identity="v" * 64,
        )
        tariffs, _ = _content(
            db,
            source,
            title="Trump's Tariff War with Canada Is Coming for Beauty",
            body="New tariffs raise costs for ingredients, manufacturing, and distribution.",
            language="en",
            published=datetime.now(UTC),
            identity="w" * 64,
        )
        tariffs.excerpt = (
            "New 50% US-Canada tariffs threaten beauty supply chains, "
            "raising costs for ingredients, manufacturing, and distribution."
        )
        retail, _ = _content(
            db,
            source,
            title="How Far Can the K-beauty Craze Go?",
            body="Korean skincare sales surge across US retail partnerships.",
            language="en",
            published=datetime.now(UTC),
            identity="x" * 64,
        )
        assert match_content(topic, actives).decision == "include"
        assert match_content(topic, tariffs).decision == "include"
        assert match_content(topic, retail).decision == "exclude"
