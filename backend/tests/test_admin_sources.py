from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.auth import create_user
from app.models import ContentItem, CrawlRun, RawItem, Source
from app.source_probe import ProbeDocument, analyze_probe_document

PASSWORD = "Admin-password-2026"
FIXTURES = Path(__file__).parent / "fixtures" / "source_probe"


def login(client, email="operator@example.com", password=PASSWORD):
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200


def seed_admin(session_factory, *, member=False):
    with session_factory() as db:
        create_user(
            db,
            email="operator@example.com",
            display_name="管理员",
            password=PASSWORD,
            role="admin",
        )
        if member:
            create_user(
                db,
                email="member@example.com",
                display_name="读者",
                password=PASSWORD,
            )


def fixture_probe(name: str, content_type: str, url: str):
    return analyze_probe_document(
        ProbeDocument(
            requested_url=url,
            final_url=url,
            observed_at=datetime(2026, 9, 2, 10, tzinfo=UTC),
            status_code=200,
            content_type=content_type,
            body=(FIXTURES / name).read_text(encoding="utf-8"),
            robots_status="allowed",
        )
    )


def test_anonymous_cannot_manage_sources(client):
    assert client.get("/api/v1/sources").status_code == 401
    probe = client.post(
        "/api/v1/sources/probe",
        json={"start_url": "https://example.com/feed.xml"},
    )
    assert probe.status_code == 401


def test_member_cannot_manage_sources(client, session_factory):
    seed_admin(session_factory, member=True)
    login(client, "member@example.com")
    assert client.get("/api/v1/sources").status_code == 403
    probe = client.post(
        "/api/v1/sources/probe",
        json={"start_url": "https://example.com/feed.xml"},
    )
    assert probe.status_code == 403


def test_admin_lists_website_paths_and_excludes_topic_discovery(client, session_factory):
    seed_admin(session_factory)
    login(client)
    created = client.post(
        "/api/v1/sources",
        json={
            "name": "行业RSS",
            "channel_type": "rss",
            "start_url": "https://news.example.com/feed.xml",
            "parser_config": {"discovery_method": "feed"},
        },
    )
    assert created.status_code == 201
    assert created.json()["viable_paths"][0]["label"] == "RSS / Atom"
    with session_factory() as db:
        db.add(
            Source(
                catalog_id="discovered_host",
                name="主题发现站",
                channel_type="web",
                start_url="https://found.example.com/",
                normalized_start_url="https://found.example.com/",
                parser_config={"discovery_method": "user_topic", "provider": "firecrawl"},
                is_enabled=False,
            )
        )
        db.commit()
    listed = client.get("/api/v1/sources?family=website").json()
    assert [item["name"] for item in listed] == ["行业RSS"]
    assert listed[0]["execution_engine"] == "feed_direct"


def test_admin_probe_and_register_uses_detected_feed_path(client, session_factory, monkeypatch):
    seed_admin(session_factory)
    login(client)
    result = fixture_probe("rss-full.xml", "application/rss+xml", "https://news.example.com/feed.xml")

    async def fake_probe(url, *, observed_at):
        assert url == "https://news.example.com/feed.xml"
        assert observed_at.tzinfo is not None
        return result

    monkeypatch.setattr("app.main.probe_public_url", fake_probe)
    preview = client.post(
        "/api/v1/sources/probe",
        json={"start_url": "https://news.example.com/feed.xml"},
    )
    assert preview.status_code == 200
    assert preview.json()["recommended"]["label"] == "RSS / Atom"
    created = client.post(
        "/api/v1/sources",
        json={"start_url": "https://news.example.com/feed.xml", "probe": True},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["channel_type"] == "rss"
    assert body["name"] == "news.example.com"
    assert {item["key"] for item in body["viable_paths"]} == {"feed"}


def test_admin_can_crawl_selected_websites(client, session_factory):
    seed_admin(session_factory)
    login(client)
    first = client.post(
        "/api/v1/sources",
        json={"name": "站点甲", "start_url": "https://a.example.com/news"},
    ).json()
    second = client.post(
        "/api/v1/sources",
        json={"name": "站点乙", "start_url": "https://b.example.com/news"},
    ).json()
    with patch("app.main.crawl_source"):
        response = client.post(
            "/api/v1/sources/crawl-selected",
            json={"source_ids": [first["id"], second["id"], first["id"]]},
        )
    assert response.status_code == 202
    assert len(response.json()) == 2


def test_admin_source_list_includes_last_run_counts(client, session_factory):
    seed_admin(session_factory)
    login(client)
    created = client.post(
        "/api/v1/sources",
        json={"name": "站点丙", "start_url": "https://c.example.com/news"},
    ).json()
    with session_factory() as db:
        db.add(
            CrawlRun(
                source_id=created["id"],
                trigger="manual",
                status="succeeded",
                fetched_count=8,
                new_count=3,
                skipped_count=5,
                rejected_count=0,
            )
        )
        db.commit()
    listed = client.get("/api/v1/sources?family=website").json()[0]
    assert listed["last_run_status"] == "succeeded"
    assert listed["last_fetched_count"] == 8
    assert listed["last_new_count"] == 3
    assert listed["last_skipped_count"] == 5


def _add_content(db, source, *, title, body, published, identity):
    crawl = CrawlRun(source_id=source.id, status="succeeded")
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
        language="zh",
        content_hash=identity,
        published_at=published,
        quality={"body_complete": True, "metadata_only": False},
    )
    db.add(content)
    return content


def test_explore_lists_readable_website_content_and_hides_stubs(client, session_factory):
    seed_admin(session_factory, member=True)
    login(client)
    with session_factory() as db:
        site = Source(
            catalog_id="explore_site",
            name="探索站",
            channel_type="web",
            start_url="https://news.example.com/",
            normalized_start_url="https://news.example.com/",
            parser_config={"discovery_method": "html"},
        )
        stub = Source(
            catalog_id="explore_stub",
            name="主题发现站",
            channel_type="web",
            start_url="https://found.example.com/",
            normalized_start_url="https://found.example.com/",
            parser_config={"discovery_method": "user_topic", "provider": "firecrawl"},
        )
        db.add_all([site, stub])
        db.flush()
        kept = _add_content(
            db,
            site,
            title="目录站新稿",
            body="正文内容足够长，用于通过读者可读门槛。" * 8,
            published=datetime.now(UTC),
            identity="d" * 64,
        )
        _add_content(
            db,
            site,
            title="无日期旧稿",
            body="这篇没有发布日期，不应进入探索。" * 8,
            published=None,
            identity="e" * 64,
        )
        _add_content(
            db,
            stub,
            title="发现源不应出现",
            body="主题发现戳记源的内容不应进入探索。" * 8,
            published=datetime.now(UTC),
            identity="f" * 64,
        )
        db.commit()
        kept_id = kept.id
    listed = client.get("/api/v1/explore").json()
    assert [item["title"] for item in listed] == ["目录站新稿"]
    assert listed[0]["content_id"] == kept_id
    assert listed[0]["source_name"] == "探索站"
    login(client, "member@example.com")
    assert [item["title"] for item in client.get("/api/v1/explore").json()] == ["目录站新稿"]


def test_anonymous_cannot_read_explore(client):
    assert client.get("/api/v1/explore").status_code == 401


def test_admin_can_remove_uncrawlable_websites_from_catalog(client, session_factory):
    seed_admin(session_factory)
    login(client)
    unused = client.post(
        "/api/v1/sources",
        json={"name": "空站", "start_url": "https://empty.example.com/"},
    ).json()
    failed = client.post(
        "/api/v1/sources",
        json={"name": "失败站", "start_url": "https://fail.example.com/"},
    ).json()
    with session_factory() as db:
        db.add(CrawlRun(source_id=failed["id"], status="failed"))
        db.commit()
    removed = client.post(
        "/api/v1/sources/delete-selected",
        json={"source_ids": [unused["id"], failed["id"]]},
    )
    assert removed.status_code == 200
    assert removed.json() == {"deleted": 1, "hidden": 1}
    assert client.get("/api/v1/sources?family=website").json() == []
    with session_factory() as db:
        assert db.get(Source, unused["id"]) is None
        kept = db.get(Source, failed["id"])
        assert kept is not None
        assert kept.is_enabled is False
        assert kept.parser_config["removed_from_catalog"] is True
    restored = client.post(
        "/api/v1/sources",
        json={"name": "失败站", "start_url": "https://fail.example.com/"},
    )
    assert restored.status_code == 201
    listed = client.get("/api/v1/sources?family=website").json()
    assert [item["name"] for item in listed] == ["失败站"]
    assert listed[0]["is_enabled"] is True


def test_member_cannot_delete_sources(client, session_factory):
    seed_admin(session_factory, member=True)
    login(client)
    created = client.post(
        "/api/v1/sources",
        json={"name": "管理员站", "start_url": "https://keep.example.com/"},
    ).json()
    login(client, "member@example.com")
    assert client.delete(f"/api/v1/sources/{created['id']}").status_code == 403
    blocked = client.post(
        "/api/v1/sources/delete-selected",
        json={"source_ids": [created["id"]]},
    )
    assert blocked.status_code == 403
