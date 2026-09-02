from datetime import UTC, datetime

from app.event_clustering import apply_cluster_plan, build_cluster_plan
from app.models import CrawlRun, Source
from app.web_ingestion import ingest_article


def test_event_list_and_detail_are_domain_neutral(client, session_factory):
    with session_factory() as session:
        source = Source(
            name="General News",
            channel_type="web",
            start_url="https://general.example.com/",
            normalized_start_url="https://general.example.com/",
            parser_config={},
            processing_config={"scope_mode": "dedicated"},
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
                "title": "A general news event",
                "canonical_url": "https://general.example.com/story",
                "original_url": "https://general.example.com/story",
                "author": None,
                "published_at": datetime(2026, 8, 27, tzinfo=UTC),
                "body": "General public-interest reporting. " * 20,
                "description": "General public-interest reporting.",
                "content_type": "article",
                "topics": [],
            },
        )
        result = apply_cluster_plan(session, build_cluster_plan(session))
        session.commit()
        event_id = result.run_id

    listed = client.get("/api/v1/events")
    assert listed.status_code == 200
    assert listed.json()[0]["member_count"] == 1
    assert "industry" not in listed.text.lower()
    actual_event_id = listed.json()[0]["id"]
    detailed = client.get(f"/api/v1/events/{actual_event_id}")
    assert detailed.status_code == 200
    assert detailed.json()["members"][0]["source_name"] == "General News"
    assert client.get(f"/api/v1/events/{event_id + 999}").status_code == 404
