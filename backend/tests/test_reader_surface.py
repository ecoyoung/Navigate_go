import json
from datetime import UTC, datetime, timedelta

from app.auth import create_user
from app.models import (
    ContentItem,
    ContentValueScore,
    ContentValueScoreRun,
    CrawlRun,
    Domain,
    Event,
    EventMember,
    LLMProcessingResult,
    RawItem,
    Source,
)
from app.topic_distribution import distribute_crawl_run
from app.web_ingestion import ingest_article

PASSWORD = "Reader-Surface-2026!"
NOW = datetime.now(UTC)


def seed_user(session_factory, email="reader@example.com"):
    with session_factory() as db:
        user = create_user(
            db,
            email=email,
            display_name="读者",
            password=PASSWORD,
            role="admin",
        )
        db.commit()
        return user.id


def login(client, email="reader@example.com"):
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def add_source(db, suffix: str) -> Source:
    source = Source(
        catalog_id=f"src_{suffix}",
        name=f"来源{suffix}",
        channel_type="web",
        start_url=f"https://{suffix}.example.com/",
        normalized_start_url=f"https://{suffix}.example.com/",
        parser_config={"discovery_method": "html"},
    )
    db.add(source)
    db.flush()
    return source


def add_content(
    db,
    source: Source,
    *,
    title: str,
    body: str,
    published,
    identity: str,
    language: str = "zh",
    excerpt: str | None = None,
    path: str = "story",
) -> ContentItem:
    crawl = CrawlRun(source_id=source.id, status="succeeded")
    db.add(crawl)
    db.flush()
    raw = RawItem(
        source_id=source.id,
        crawl_run_id=crawl.id,
        identity_key=identity,
        original_url=f"{source.start_url}{path}",
        canonical_url=f"{source.start_url}{path}",
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
        original_url=raw.original_url,
        excerpt=excerpt or title,
        body=body,
        language=language,
        content_hash=identity,
        published_at=published,
        quality={"body_complete": True, "metadata_only": False},
    )
    db.add(content)
    db.flush()
    return content


def add_event(db, contents: list[ContentItem], *, title: str, digest: str) -> Event:
    first_at = min(item.published_at for item in contents)
    last_at = max(item.published_at for item in contents)
    event = Event(
        representative_content_id=contents[0].id,
        canonical_title=title,
        first_published_at=first_at,
        last_published_at=last_at,
        membership_hash=digest,
        status="active",
        cluster_version="test",
    )
    db.add(event)
    db.flush()
    for item in contents:
        db.add(
            EventMember(
                event_id=event.id,
                content_item_id=item.id,
                confidence=1.0,
                reasons={},
                decision_source="automatic",
                algorithm_version="test",
            )
        )
    return event


def test_topic_events_only_include_recent_topic_members(client, session_factory):
    seed_user(session_factory)
    login(client)
    with session_factory() as db:
        source_a = add_source(db, "alpha")
        source_b = add_source(db, "beta")
        source_c = add_source(db, "gamma")
        fresh = add_content(
            db,
            source_a,
            title="具身智能创业融资落地",
            body="具身智能创业融资的最新进展。" * 12,
            published=NOW,
            identity="a" * 64,
            path="fresh",
        )
        corroborating = add_content(
            db,
            source_b,
            title="Embodied AI startup financing round",
            body="Embodied AI startup financing details. " * 12,
            published=NOW - timedelta(hours=3),
            identity="b" * 64,
            language="en",
            path="fresh-en",
        )
        stale = add_content(
            db,
            source_c,
            title="去年的具身智能融资",
            body="这是一年前的具身智能创业融资报道。" * 12,
            published=NOW - timedelta(days=20),
            identity="c" * 64,
            path="stale",
        )
        lone = add_content(
            db,
            source_c,
            title="具身智能创业融资短讯",
            body="具身智能创业融资的单独短讯。" * 12,
            published=NOW - timedelta(hours=2),
            identity="d" * 64,
            path="lone",
        )
        visible = add_event(db, [fresh, corroborating], title="具身智能融资", digest="1" * 64)
        stale_event = add_event(db, [stale], title="过期事件", digest="2" * 64)
        lone_event = add_event(db, [lone], title="单源事件", digest="3" * 64)
        db.commit()
        visible_id = visible.id
        stale_event_id = stale_event.id
        lone_event_id = lone_event.id
    created = client.post(
        "/api/v1/topics",
        json={
            "name": "具身智能创业融资",
            "intent_text": "具身智能创业融资",
            "keywords": ["具身智能", "embodied AI"],
        },
    )
    assert created.status_code == 201
    topic_id = created.json()["topic"]["id"]
    listed = client.get(f"/api/v1/topics/{topic_id}/events").json()
    ids = [item["id"] for item in listed]
    assert visible_id in ids
    assert stale_event_id not in ids
    assert lone_event_id not in ids
    assert listed[0]["source_count"] >= 2
    assert {source["source_name"] for source in listed[0]["sources"]} >= {"来源alpha", "来源beta"}
    detailed = client.get(f"/api/v1/topics/{topic_id}/events/{visible_id}").json()
    assert len(detailed["members"]) >= 2
    assert client.get("/api/v1/feed/events").json()[0]["id"] == visible_id


def test_curated_feed_keeps_unscored_and_drops_full_pool(client, session_factory):
    seed_user(session_factory)
    login(client)
    with session_factory() as db:
        source = add_source(db, "pool")
        selected = add_content(
            db,
            source,
            title="具身智能创业融资获新一轮",
            body="具身智能创业融资获新一轮支持。" * 12,
            published=NOW,
            identity="s" * 64,
            path="selected",
        )
        full_pool = add_content(
            db,
            source,
            title="具身智能创业融资评论汇编",
            body="具身智能创业融资评论汇编内容。" * 12,
            published=NOW - timedelta(hours=1),
            identity="f" * 64,
            path="full",
        )
        unscored = add_content(
            db,
            source,
            title="具身智能创业融资后续",
            body="具身智能创业融资后续观察。" * 12,
            published=NOW - timedelta(hours=2),
            identity="u" * 64,
            path="unscored",
        )
        domain = Domain(key="sample", name="Sample")
        db.add(domain)
        db.flush()
        run = ContentValueScoreRun(
            domain_id=domain.id,
            algorithm_version="test",
            schema_version="content-value-score.v1",
            config={},
            config_hash="d" * 64,
            as_of=NOW,
            input_hash="e" * 64,
            status="succeeded",
            input_count=2,
            selected_count=1,
            finished_at=NOW,
        )
        db.add(run)
        db.flush()
        db.add_all(
            [
                ContentValueScore(
                    run_id=run.id,
                    content_item_id=selected.id,
                    input_content_hash=selected.content_hash,
                    total_score=88,
                    component_scores={},
                    penalties=[],
                    gates=[],
                    decision="selected",
                    reasons=[],
                ),
                ContentValueScore(
                    run_id=run.id,
                    content_item_id=full_pool.id,
                    input_content_hash=full_pool.content_hash,
                    total_score=12,
                    component_scores={},
                    penalties=[],
                    gates=[],
                    decision="full_pool",
                    reasons=[],
                ),
            ]
        )
        db.commit()
        selected_id = selected.id
        full_id = full_pool.id
        unscored_id = unscored.id
    created = client.post(
        "/api/v1/topics",
        json={"name": "具身智能创业融资", "intent_text": "具身智能创业融资"},
    )
    assert created.status_code == 201
    topic_id = created.json()["topic"]["id"]
    feed_ids = [item["content_id"] for item in client.get(f"/api/v1/topics/{topic_id}/feed").json()]
    assert selected_id in feed_ids
    assert unscored_id in feed_ids
    assert full_id not in feed_ids
    explore_ids = [item["content_id"] for item in client.get("/api/v1/explore").json()]
    assert full_id in explore_ids


def test_bilingual_search_and_topic_rss(client, session_factory):
    seed_user(session_factory)
    login(client)
    with session_factory() as db:
        source = add_source(db, "search")
        zh = add_content(
            db,
            source,
            title="具身智能创业融资落地",
            body="国内具身智能创业融资的最新一轮。" * 12,
            published=NOW,
            identity="z" * 64,
            path="zh",
        )
        en = add_content(
            db,
            source,
            title="Embodied AI startup raises new round",
            body="An embodied AI startup closed financing this week. " * 12,
            published=NOW,
            identity="y" * 64,
            language="en",
            path="en",
        )
        other = add_content(
            db,
            source,
            title="防晒新品原料备案",
            body="防晒新品完成原料备案，和融资无关。" * 12,
            published=NOW,
            identity="x" * 64,
            path="other",
        )
        db.commit()
        zh_id = zh.id
        en_id = en.id
        other_id = other.id
    created = client.post(
        "/api/v1/topics",
        json={
            "name": "具身智能创业融资",
            "intent_text": "具身智能创业融资",
            "keywords": ["具身智能", "embodied AI"],
        },
    )
    assert created.status_code == 201
    topic_id = created.json()["topic"]["id"]
    zh_hits = client.get(f"/api/v1/topics/{topic_id}/search", params={"q": "具身智能"}).json()
    en_hits = client.get(f"/api/v1/topics/{topic_id}/search", params={"q": "embodied"}).json()
    assert zh_id in [item["content_id"] for item in zh_hits]
    assert en_id in [item["content_id"] for item in en_hits]
    assert other_id not in [item["content_id"] for item in zh_hits + en_hits]
    explore_zh = client.get("/api/v1/explore/search", params={"q": "防晒"}).json()
    assert other_id in [item["content_id"] for item in explore_zh]
    rss = client.get(f"/api/v1/topics/{topic_id}/rss.xml")
    assert rss.status_code == 200
    assert rss.headers["content-type"].startswith("application/rss+xml")
    assert "具身智能创业融资落地" in rss.text
    assert "https://search.example.com/zh" in rss.text
    rss_text = rss.text.lower()
    assert "embodied ai startup raises new round" in rss_text or "embodied ai" in rss_text


def test_distribute_crawl_run_builds_events(session_factory, monkeypatch):
    monkeypatch.setattr("app.topic_distribution._llm_client", lambda: None)
    with session_factory() as session:
        source = Source(
            name="Cluster News",
            channel_type="web",
            start_url="https://cluster.example.com/",
            normalized_start_url="https://cluster.example.com/",
            parser_config={"discovery_method": "html"},
        )
        session.add(source)
        session.flush()
        run = CrawlRun(source_id=source.id, status="running")
        session.add(run)
        session.flush()
        ingest_article(
            session,
            source,
            run,
            {
                "title": "A clustered public event",
                "canonical_url": "https://cluster.example.com/story",
                "original_url": "https://cluster.example.com/story",
                "author": None,
                "published_at": NOW,
                "body": "General public-interest reporting. " * 20,
                "description": "General public-interest reporting.",
                "content_type": "article",
                "topics": [],
            },
        )
        run.status = "succeeded"
        session.commit()
        run_id = run.id
        stats = distribute_crawl_run(session, run_id)
        assert stats.get("events", {}).get("event_count", 0) >= 1
        from sqlalchemy import select

        assert session.scalar(select(Event.id)) is not None


def test_reader_cards_use_editorial_paragraphs_not_outbound_only(client, session_factory):
    seed_user(session_factory)
    login(client)
    with session_factory() as db:
        source = add_source(db, "cards")
        english = add_content(
            db,
            source,
            title="Acme acquires Nova in two billion dollar deal",
            body=("Acme signed an agreement to acquire Nova in a two billion dollar deal. "
                "The companies will keep both brands. ") * 8,
            published=NOW,
            identity="c" * 64,
            language="en",
            excerpt="Wire headline only.",
            path="deal",
        )
        chinese = add_content(
            db,
            source,
            title="石头科技上半年营收破百亿 洗地机业务进入兑现期",
            body=(
                "广告 首页 > 资讯 > 正文 石头科技上半年营收破百亿。"
                "据石头科技2026年半年报显示，当期公司实现营收100.84亿元，同比增长27.6%。"
                "洗地机业务成为核心增长支撑。公司继续推进全球化布局。"
            ),
            published=NOW,
            identity="d" * 64,
            excerpt="石头科技2026年上半年营收破百亿，洗地机业务高速增长成为核心支撑。",
            path="roborock",
        )
        db.add(
            LLMProcessingResult(
                subject_type="content_item",
                subject_key=f"content:{english.id}",
                task_name="content_editorial_zh",
                task_version="content_editorial.zh.v2",
                input_hash=english.content_hash,
                provider="test",
                model="test",
                cache_key=f"card-{english.id}",
                status="succeeded",
                output={
                    "chinese_title": "Acme 以二十亿美元收购 Nova",
                    "summary_units": [
                        {
                            "claim_ref": f"content:{english.id}#summary:1",
                            "text_zh": "Acme 签署协议收购 Nova，双方将保留原有品牌。",
                        }
                    ],
                    "tags_zh": ["并购"],
                },
            )
        )
        db.commit()
        english_id = english.id
        chinese_id = chinese.id
    created = client.post(
        "/api/v1/topics",
        json={
            "name": "全球并购",
            "intent_text": "Acme Nova 收购 石头科技 营收",
            "keywords": ["Acme", "Nova", "石头科技"],
        },
    )
    assert created.status_code == 201
    topic_id = created.json()["topic"]["id"]
    feed = client.get(f"/api/v1/topics/{topic_id}/feed").json()
    by_id = {item["content_id"]: item for item in feed}
    editorial = by_id[english_id]
    assert editorial["title"] == "Acme 以二十亿美元收购 Nova"
    assert editorial["paragraphs"] == ["Acme 签署协议收购 Nova，双方将保留原有品牌。"]
    assert "twenty" not in (editorial["excerpt"] or "").lower()
    assert editorial["url"] == "https://cards.example.com/deal"
    extractive = by_id[chinese_id]
    assert extractive["paragraphs"]
    assert extractive["title"] == "石头科技上半年营收破百亿 洗地机业务进入兑现期"
    assert all("首页 >" not in paragraph for paragraph in extractive["paragraphs"])
    assert len("".join(extractive["paragraphs"])) < 180
    card = client.get(f"/api/v1/contents/{english_id}", params={"topic_id": topic_id})
    assert card.status_code == 200
    assert card.json()["paragraphs"] == editorial["paragraphs"]
    rss = client.get(f"/api/v1/topics/{topic_id}/rss.xml")
    assert "Acme 签署协议收购 Nova，双方将保留原有品牌。" in rss.text



class FakeReaderEditorialClient:
    model = "deepseek-v4-flash"
    provider = "deepseek"

    def generate_json(self, *, system_prompt: str, user_prompt: str):
        from app.llm_editorial import LLMResponse, LLMUsage

        source = json.loads(user_prompt.split("\n", 1)[1])
        items = []
        for item in source["items"]:
            ref = item["evidence"][0]["ref"]
            content_ref = item["content_ref"]
            items.append(
                {
                    "content_ref": content_ref,
                    "input_content_hash": item["input_content_hash"],
                    "chinese_title": "Acme 收购 Nova",
                    "title_evidence_refs": [ref],
                    "summary_units": [
                        {
                            "claim_ref": f"{content_ref}#summary:1",
                            "text_zh": "Acme 签署协议收购 Nova，双方将保留原有品牌。",
                            "evidence_refs": [ref],
                        }
                    ],
                    "tags": [],
                }
            )
        return LLMResponse(
            {"schema_version": "content_editorial.zh.v2", "items": items},
            LLMUsage(80, 40, 120),
        )


def test_distribute_crawl_run_writes_reader_editorial(session_factory, monkeypatch):
    monkeypatch.setattr(
        "app.topic_distribution._llm_client", lambda: FakeReaderEditorialClient()
    )
    with session_factory() as session:
        source = Source(
            name="Wire",
            channel_type="web",
            start_url="https://wire.example.com/",
            normalized_start_url="https://wire.example.com/",
            parser_config={"discovery_method": "html"},
        )
        session.add(source)
        session.flush()
        run = CrawlRun(source_id=source.id, status="running", trigger="scheduled")
        session.add(run)
        session.flush()
        ingest_article(
            session,
            source,
            run,
            {
                "title": "Acme acquires Nova in two billion dollar deal",
                "canonical_url": "https://wire.example.com/deal",
                "original_url": "https://wire.example.com/deal",
                "author": None,
                "published_at": NOW,
                "body": (
                    "Acme signed an agreement to acquire Nova in a two billion dollar deal. "
                    "The companies will keep both brands. "
                )
                * 8,
                "description": "Acme signed an agreement to acquire Nova.",
                "content_type": "article",
                "topics": [],
            },
        )
        run.status = "succeeded"
        session.commit()
        stats = distribute_crawl_run(session, run.id)
        assert stats.get("editorials", {}).get("processed") == 1
        from sqlalchemy import select

        from app.llm_editorial import CONTENT_TASK_NAME
        from app.reader_cards import build_reader_card

        row = session.scalar(
            select(LLMProcessingResult).where(
                LLMProcessingResult.task_name == CONTENT_TASK_NAME,
                LLMProcessingResult.status == "succeeded",
            )
        )
        assert row is not None
        content = session.scalar(select(ContentItem))
        card = build_reader_card(content, source, row.output)
        assert card["title"] == "Acme 收购 Nova"
        assert card["paragraphs"] == ["Acme 签署协议收购 Nova，双方将保留原有品牌。"]
        assert "two billion" not in " ".join(card["paragraphs"])
