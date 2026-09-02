from sqlalchemy import func, select

from app.models import ContentItem, CrawlRun, Source
from app.strict_deduplication import rebuild_strict_duplicates
from app.web_ingestion import ingest_article


def add_source(session, name: str) -> tuple[Source, CrawlRun]:
    source = Source(
        name=name,
        channel_type="web",
        start_url=f"https://{name}.example.com/",
        normalized_start_url=f"https://{name}.example.com/",
        parser_config={},
    )
    session.add(source)
    session.flush()
    run = CrawlRun(source_id=source.id, status="running")
    session.add(run)
    session.flush()
    return source, run


def article(url: str, body: str) -> dict:
    return {
        "title": "Same release",
        "canonical_url": url,
        "original_url": url,
        "author": None,
        "published_at": None,
        "body": body,
        "description": "Same excerpt",
        "content_type": "article",
        "topics": ["Beauty"],
    }


def test_exact_duplicate_requires_different_sources_and_long_body(session_factory):
    body = "A" * 240
    with session_factory() as session:
        source_a, run_a = add_source(session, "a")
        source_b, run_b = add_source(session, "b")
        assert (
            ingest_article(session, source_a, run_a, article("https://a.example/1", body))
            == "new"
        )
        assert (
            ingest_article(session, source_b, run_b, article("https://b.example/2", body))
            == "new"
        )
        session.commit()

        contents = list(session.scalars(select(ContentItem).order_by(ContentItem.id)))
        assert contents[0].duplicate_of_id is None
        assert contents[1].duplicate_of_id == contents[0].id
        assert contents[1].duplicate_rule == "exact-content-v1"


def test_short_or_changed_content_is_not_marked_duplicate(session_factory):
    with session_factory() as session:
        source_a, run_a = add_source(session, "a")
        source_b, run_b = add_source(session, "b")
        ingest_article(session, source_a, run_a, article("https://a.example/1", "A" * 80))
        ingest_article(session, source_b, run_b, article("https://b.example/2", "A" * 80))
        ingest_article(session, source_b, run_b, article("https://b.example/3", "B" * 240))
        session.commit()

        assert session.scalar(
            select(func.count(ContentItem.id)).where(ContentItem.duplicate_of_id.is_not(None))
        ) == 0


def test_rebuild_is_dry_run_safe_and_apply_is_idempotent(session_factory):
    body = "A" * 240
    with session_factory() as session:
        source_a, run_a = add_source(session, "a")
        source_b, run_b = add_source(session, "b")
        ingest_article(session, source_a, run_a, article("https://a.example/1", body))
        ingest_article(session, source_b, run_b, article("https://b.example/2", body))
        for item in session.scalars(select(ContentItem)):
            item.content_hash = ""
            item.duplicate_of_id = None
            item.duplicate_rule = None
        session.commit()

        preview = rebuild_strict_duplicates(session, apply=False)
        assert preview.hashes_updated == 2
        assert all(item.content_hash == "" for item in session.scalars(select(ContentItem)))

        first = rebuild_strict_duplicates(session, apply=True)
        session.commit()
        second = rebuild_strict_duplicates(session, apply=True)
        session.commit()

        assert first.groups == 1 and first.duplicates == 1
        assert second.hashes_updated == 0
        assert second.groups == 1 and second.duplicates == 1
