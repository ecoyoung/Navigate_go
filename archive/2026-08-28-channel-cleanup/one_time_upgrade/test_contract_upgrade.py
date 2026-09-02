from sqlalchemy import func, select

from app.contract_upgrade import upgrade_current_contracts
from app.contracts import article_content_hash
from app.models import ContentItem, CrawlRun, PageSnapshot, RawItem, Source
from app.normalization import identity_key, normalize_url


def test_contract_upgrade_appends_v1_1_raw_and_preserves_legacy_identity(session_factory):
    url = "https://mp.weixin.qq.com/s?__biz=biz-1==&mid=7#rd"
    title = "Legacy article"
    body = "这是用于验证旧契约升级的一段完整本地正文。"
    with session_factory() as session:
        source = Source(
            name="Legacy account",
            channel_type="third_party_feed",
            start_url="https://redfox.hk/",
            normalized_start_url="https://redfox.hk/",
            parser_config={"provider": "redfox_archive", "account_fakeid": "biz-1"},
            default_language="zh-CN",
        )
        session.add(source)
        session.flush()
        run = CrawlRun(source_id=source.id, trigger="offline_import", status="succeeded")
        session.add(run)
        session.flush()
        snapshot = PageSnapshot(
            crawl_run_id=run.id,
            url=url,
            page_type="article",
            http_status=200,
            body="legacy evidence",
            body_sha256="a" * 64,
        )
        session.add(snapshot)
        session.flush()
        legacy_identity = identity_key(None, url, title, "")
        legacy_payload = {"schema_version": "article.v1", "title": title, "body_text": body}
        raw = RawItem(
            source_id=source.id,
            crawl_run_id=run.id,
            page_snapshot_id=None,
            external_id=None,
            identity_key=legacy_identity,
            original_url=url,
            canonical_url=url,
            payload=legacy_payload,
            payload_sha256="b" * 64,
        )
        session.add(raw)
        session.flush()
        content = ContentItem(
            source_id=source.id,
            raw_item_id=raw.id,
            identity_key=legacy_identity,
            title=title,
            original_url=url,
            canonical_url=url,
            body=body,
            language="zh-CN",
            excerpt=body,
            content_hash=article_content_hash(title, body, body, []),
            schema_version="article.v1",
            normalizer_version="web-v1",
        )
        session.add(content)
        session.commit()
        old_raw_id = raw.id

        summary = upgrade_current_contracts(
            session, archive_external_ids={normalize_url(url): "work-7"}
        )
        session.commit()

        assert summary.upgraded == 1
        assert summary.external_ids_bound == 1
        assert summary.snapshots_bound == 1
        assert session.scalar(select(func.count(RawItem.id))) == 2
        old_raw = session.get(RawItem, old_raw_id)
        current = session.get(ContentItem, content.id)
        current_raw = session.get(RawItem, current.raw_item_id)
        assert old_raw.payload == legacy_payload
        assert current.identity_key == legacy_identity
        assert current.external_id == "work-7"
        assert current.schema_version == "article.v1.1"
        assert current_raw.page_snapshot_id == snapshot.id
        assert current_raw.payload["external_item_id"] == "work-7"
        assert current_raw.payload["source_external_id"] == "biz-1"

        repeated = upgrade_current_contracts(
            session, archive_external_ids={normalize_url(url): "work-7"}
        )
        session.commit()
        assert repeated.skipped == 1
        assert session.scalar(select(func.count(RawItem.id))) == 2
