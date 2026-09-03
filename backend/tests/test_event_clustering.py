from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.domain_assignments import sync_processing_results_to_domain
from app.event_clustering import apply_cluster_plan, build_cluster_plan, load_clustering_config
from app.models import (
    ContentDomainAssignment,
    ContentItem,
    ContentProcessingResult,
    CrawlRun,
    Domain,
    Event,
    EventClusterConstraint,
    EventClusterRun,
    EventMember,
    Source,
)
from app.web_ingestion import ingest_article


def add_source(session, name: str) -> tuple[Source, CrawlRun]:
    slug = name.lower().replace(" ", "-")
    source = Source(
        name=name,
        channel_type="web",
        start_url=f"https://{slug}.example.com/",
        normalized_start_url=f"https://{slug}.example.com/",
        parser_config={},
        processing_config={"scope_mode": "dedicated"},
    )
    session.add(source)
    session.flush()
    run = CrawlRun(source_id=source.id, status="running")
    session.add(run)
    session.flush()
    return source, run


def ingest(
    session,
    source: Source,
    run: CrawlRun,
    *,
    suffix: str,
    title: str,
    body: str,
    published_at: datetime,
) -> ContentItem:
    result = ingest_article(
        session,
        source,
        run,
        {
            "title": title,
            "canonical_url": f"{source.start_url}{suffix}",
            "original_url": f"{source.start_url}{suffix}",
            "author": None,
            "published_at": published_at,
            "body": body,
            "description": body[:160],
            "content_type": "article",
            "topics": [],
        },
    )
    assert result == "new"
    return session.scalar(
        select(ContentItem).where(
            ContentItem.source_id == source.id,
            ContentItem.canonical_url == f"{source.start_url}{suffix}",
        )
    )


def test_domain_neutral_clustering_is_explainable_and_idempotent(session_factory):
    with session_factory() as session:
        first_source, first_run = add_source(session, "World Wire")
        second_source, second_run = add_source(session, "Market Desk")
        now = datetime(2026, 8, 27, 8, tzinfo=UTC)
        first = ingest(
            session,
            first_source,
            first_run,
            suffix="deal-a",
            title="Acme acquires Nova in two billion dollar deal",
            body="Acme signed an agreement to acquire Nova in a two billion dollar deal. " * 8,
            published_at=now,
        )
        second = ingest(
            session,
            second_source,
            second_run,
            suffix="deal-b",
            title="Acme acquires Nova in two billion dollar acquisition deal",
            body="Acme signed an agreement to acquire Nova in a two billion dollar deal. "
            * 8
            + "Second source confirmation.",
            published_at=now + timedelta(hours=2),
        )
        unrelated = ingest(
            session,
            first_source,
            first_run,
            suffix="launch",
            title="Acme launches a new home audio device",
            body="Acme introduced a home audio device with new speakers and retail availability. "
            * 8,
            published_at=now + timedelta(hours=3),
        )
        processing = session.scalar(
            select(ContentProcessingResult).where(
                ContentProcessingResult.content_item_id == second.id
            )
        )
        assert processing is None
        session.add(
            ContentProcessingResult(
                content_item_id=second.id,
                processor_name="test-domain-classifier",
                processor_version="test.v1",
                input_content_hash=second.content_hash,
                is_relevant=False,
                matched_topics=[],
                matched_events=[],
                reason="outside_test_domain",
            )
        )
        session.commit()

        config = load_clustering_config()
        plan = build_cluster_plan(session, config)
        assert session.scalar(select(func.count(Event.id))) == 0
        assert sorted(len(cluster.items) for cluster in plan.clusters) == [1, 2]
        paired = next(cluster for cluster in plan.clusters if len(cluster.items) == 2)
        assert {item.id for item in paired.items} == {first.id, second.id}
        assert unrelated.id not in {item.id for item in paired.items}

        applied = apply_cluster_plan(session, plan)
        session.commit()
        repeated_plan = build_cluster_plan(session, config)
        repeated = apply_cluster_plan(session, repeated_plan)
        session.commit()

        assert applied.created_event_count == 2
        assert repeated.reused_run is True
        assert session.scalar(select(func.count(EventClusterRun.id))) == 1
        assert session.scalar(select(func.count(Event.id)).where(Event.status == "active")) == 2
        assert session.scalar(
            select(func.count(EventMember.id)).where(EventMember.is_active.is_(True))
        ) == 3
        evidence = session.scalar(
            select(EventMember).where(EventMember.content_item_id == second.id)
        )
        assert "score_to_representative" in evidence.reasons


def test_exact_duplicate_is_forced_into_one_event(session_factory):
    with session_factory() as session:
        source_a, run_a = add_source(session, "Source A")
        source_b, run_b = add_source(session, "Source B")
        now = datetime(2026, 8, 27, tzinfo=UTC)
        body = "Identical full article evidence. " * 20
        first = ingest(
            session,
            source_a,
            run_a,
            suffix="one",
            title="Original headline",
            body=body,
            published_at=now,
        )
        second = ingest(
            session,
            source_b,
            run_b,
            suffix="two",
            title="Original headline",
            body=body,
            published_at=now + timedelta(days=20),
        )
        session.commit()

        assert second.duplicate_of_id == first.id
        plan = build_cluster_plan(session)
        assert len(plan.clusters) == 1
        assert {item.id for item in plan.clusters[0].items} == {first.id, second.id}


def test_manual_must_link_is_auditable_on_both_event_members(session_factory):
    with session_factory() as session:
        source_a, run_a = add_source(session, "Source A")
        source_b, run_b = add_source(session, "Source B")
        first = ingest(
            session,
            source_a,
            run_a,
            suffix="earnings-one",
            title="Company reports first-half earnings",
            body="Revenue and profit evidence from the financial report. " * 12,
            published_at=datetime(2026, 8, 25, 1, tzinfo=UTC),
        )
        second = ingest(
            session,
            source_b,
            run_b,
            suffix="earnings-two",
            title="New growth curve emerges from subsidiary brands",
            body="A differently written account with the same confirmed filing. " * 12,
            published_at=datetime(2026, 8, 25, 2, tzinfo=UTC),
        )
        constraint = EventClusterConstraint(
            left_content_id=first.id,
            right_content_id=second.id,
            relation="must_link",
            reason="Same filing and matching revenue, profit, and reporting period.",
            created_by="test-reviewer",
        )
        session.add(constraint)
        session.commit()

        plan = build_cluster_plan(session)
        assert len(plan.clusters) == 1
        assert {item.id for item in plan.clusters[0].items} == {first.id, second.id}
        apply_cluster_plan(session, plan)
        session.commit()

        members = list(
            session.scalars(
                select(EventMember).order_by(EventMember.content_item_id)
            )
        )
        assert len(members) == 2
        assert all(member.decision_source == "manual" for member in members)
        assert all(member.confidence == 1 for member in members)
        assert all(
            member.reasons["manual_constraints"][0]["constraint_id"] == constraint.id
            for member in members
        )


def test_legacy_processor_can_be_projected_into_named_domain(session_factory):
    with session_factory() as session:
        source, run = add_source(session, "Vertical Desk")
        content = ingest(
            session,
            source,
            run,
            suffix="story",
            title="A vertical market story",
            body="A domain candidate article. " * 20,
            published_at=datetime(2026, 8, 27, tzinfo=UTC),
        )
        processing = ContentProcessingResult(
            content_item_id=content.id,
            processor_name="sample-domain-classifier",
            processor_version="sample.v1",
            input_content_hash=content.content_hash,
            is_relevant=True,
            matched_topics=["sample"],
            matched_events=[],
            reason="sample_match",
        )
        session.add(processing)
        session.commit()

        result = sync_processing_results_to_domain(
            session,
            domain_key="sample-domain",
            domain_name="Sample Domain",
            processor_name=processing.processor_name,
            processor_version=processing.processor_version,
        )
        session.commit()
        repeated = sync_processing_results_to_domain(
            session,
            domain_key="sample-domain",
            domain_name="Sample Domain",
            processor_name=processing.processor_name,
            processor_version=processing.processor_version,
        )
        session.commit()

        domain = session.scalar(select(Domain).where(Domain.key == "sample-domain"))
        assignment = session.scalar(select(ContentDomainAssignment))
        assert result.created == 1
        assert repeated.skipped == 1
        assert assignment.domain_id == domain.id
        assert assignment.input_content_hash == content.content_hash
        assert assignment.decision == "include"


def test_bilingual_same_story_merges_and_different_event_stays_out(session_factory):
    with session_factory() as session:
        zh_source, zh_run = add_source(session, "China Wire")
        en_source, en_run = add_source(session, "Global Desk")
        now = datetime(2026, 9, 2, 8, tzinfo=UTC)
        zh = ingest(
            session,
            zh_source,
            zh_run,
            suffix="acme-zh",
            title="Acme 以 20 亿美元收购 Nova",
            body="Acme 签署协议，以 20 亿美元收购 Nova，交易将于年内完成。" * 8,
            published_at=now,
        )
        en = ingest(
            session,
            en_source,
            en_run,
            suffix="acme-en",
            title="Acme acquires Nova in two billion dollar deal",
            body="Acme signed an agreement to acquire Nova in a two billion dollar deal. " * 8,
            published_at=now + timedelta(hours=3),
        )
        unrelated = ingest(
            session,
            zh_source,
            zh_run,
            suffix="acme-launch",
            title="Acme 发布新款家用音频设备",
            body="Acme 推出一款新的家用音频设备，并开始零售铺货。" * 8,
            published_at=now + timedelta(hours=4),
        )
        session.commit()

        plan = build_cluster_plan(session)
        paired = next(cluster for cluster in plan.clusters if len(cluster.items) == 2)
        assert {item.id for item in paired.items} == {zh.id, en.id}
        unrelated_cluster = next(
            cluster
            for cluster in plan.clusters
            if unrelated.id in {item.id for item in cluster.items}
        )
        assert unrelated_cluster.items == [unrelated]
        apply_cluster_plan(session, plan)
        session.commit()
        members = list(session.scalars(select(EventMember).where(EventMember.is_active.is_(True))))
        grouped = {}
        for member in members:
            grouped.setdefault(member.event_id, set()).add(member.content_item_id)
        assert {zh.id, en.id} in grouped.values()


def test_same_story_with_source_suffix_and_traditional_chinese_merges(session_factory):
    with session_factory() as session:
        first_source, first_run = add_source(session, "Office News")
        second_source, second_run = add_source(session, "Overseas Desk")
        now = datetime(2026, 9, 1, 2, tzinfo=UTC)
        first = ingest(
            session,
            first_source,
            first_run,
            suffix="summit-a",
            title=(
                "副總統為全球AI高峰會錄影致詞　盼攜手打造安全、可信賴且以人為本的AI"
                "-總統府新聞｜中華民國總統府"
            ),
            body="副总统以录影方式为全球AI高峰会致词，强调主权人工智慧与机器人。" * 8,
            published_at=now,
        )
        second = ingest(
            session,
            second_source,
            second_run,
            suffix="summit-b",
            title="副總統為全球AI高峰會錄影致詞　盼攜手打造安全、可信賴且以人為本的AI|新創臺灣",
            body="副总统以录影方式为全球AI高峰会致词，强调主权人工智慧与机器人。" * 8,
            published_at=now + timedelta(hours=1),
        )
        session.commit()
        plan = build_cluster_plan(session)
        assert len(plan.clusters) == 1
        assert {item.id for item in plan.clusters[0].items} == {first.id, second.id}
