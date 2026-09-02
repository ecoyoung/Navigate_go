import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CrawlRun, Source, utcnow
from .normalization import normalize_url
from .web_ingestion import ingest_article


@dataclass(frozen=True)
class ArchivedWechatArticle:
    account: dict
    title: str
    url: str
    published_at: datetime | None
    author: str | None
    body: str
    work_uuid: str | None

    def extracted(self) -> dict:
        canonical_url = normalize_url(self.url)
        return {
            "title": self.title,
            "canonical_url": canonical_url,
            "original_url": self.url,
            "author": self.author,
            "published_at": self.published_at,
            "updated_at": None,
            "external_item_id": self.work_uuid,
            "body": self.body,
            "description": self.body[:500],
            "content_type": "article",
            "topics": [],
            "media": [],
            "content_completeness": "unknown",
            "validation_warnings": ["media_not_archived", "offline_archive"],
        }


@dataclass(frozen=True)
class ArchiveImportSummary:
    rows: int = 0
    sources_created: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def load_archive_articles(
    archive_path: Path, account_path: Path
) -> list[ArchivedWechatArticle]:
    account_payload = _read_json(account_path)
    if not isinstance(account_payload, dict) or not isinstance(
        account_payload.get("accounts"), list
    ):
        raise ValueError("account file must contain an accounts list")

    accounts: dict[str, dict] = {}
    for account in account_payload["accounts"]:
        if not isinstance(account, dict):
            raise ValueError("account entries must be objects")
        name = str(account.get("name") or "").strip()
        fakeid = str(account.get("fakeid") or "").strip()
        if not name or not fakeid:
            raise ValueError("each account requires name and fakeid")
        if name in accounts:
            raise ValueError(f"duplicate account name: {name}")
        accounts[name] = account

    articles: list[ArchivedWechatArticle] = []
    seen_names: set[str] = set()
    for line_number, line in enumerate(archive_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") != "ok":
            continue
        name = str(row.get("name") or "").strip()
        if name not in accounts:
            raise ValueError(f"line {line_number}: unknown account: {name}")
        if name in seen_names:
            raise ValueError(f"line {line_number}: duplicate ok account: {name}")
        required = {
            "title": str(row.get("title") or "").strip(),
            "url": str(row.get("url") or "").strip(),
            "body": str(row.get("body") or "").strip(),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError(f"line {line_number}: missing {', '.join(missing)}")
        published_raw = str(row.get("published_at") or "").strip()
        published_at = date_parser.parse(published_raw) if published_raw else None
        articles.append(
            ArchivedWechatArticle(
                account=accounts[name],
                title=required["title"],
                url=required["url"],
                published_at=published_at,
                author=str(row.get("author") or "").strip() or None,
                body=required["body"],
                work_uuid=str(row.get("workUuid") or "").strip() or None,
            )
        )
        seen_names.add(name)
    return articles


def archive_catalog_id(account: dict) -> str:
    configured = str(account.get("catalog_id") or "").strip()
    if configured:
        return configured
    fakeid = str(account["fakeid"]).strip()
    digest = hashlib.sha256(fakeid.encode()).hexdigest()[:16]
    return f"wechat_mp_{digest}"


def _account_start_url(account: dict) -> str:
    query = urlencode({"action": "home", "__biz": str(account["fakeid"]).strip()})
    return f"https://mp.weixin.qq.com/mp/profile_ext?{query}"


def ensure_archive_source(session: Session, account: dict) -> tuple[Source, bool]:
    catalog_id = archive_catalog_id(account)
    source = session.scalar(select(Source).where(Source.catalog_id == catalog_id))
    if source:
        source.is_enabled = False
        source.source_external_id = str(account["fakeid"]).strip()
        return source, False

    name = str(account["name"]).strip()
    start_url = _account_start_url(account)
    source = Source(
        catalog_id=catalog_id,
        name=f"{name}公众号",
        channel_type="third_party_feed",
        start_url=start_url,
        normalized_start_url=normalize_url(start_url),
        fetch_interval_seconds=86400,
        parser_config={
            "provider": "redfox_archive",
            "discovery_method": "json",
            "discovery_url": "https://redfox.hk/",
            "crawl_strategy": "offline_import",
            "account_fakeid": str(account["fakeid"]).strip(),
            "account_alias": str(account.get("alias") or "").strip() or None,
            "access_level": "public",
        },
        processing_config={"scope_mode": "dedicated"},
        source_region="CN",
        source_type="wechat_official_account",
        default_language="zh-CN",
        source_tags=["公众号"],
        source_external_id=str(account["fakeid"]).strip(),
        is_enabled=False,
    )
    session.add(source)
    session.flush()
    return source, True


def import_archive_articles(
    session: Session, articles: list[ArchivedWechatArticle]
) -> ArchiveImportSummary:
    sources_created = new = updated = skipped = 0
    for article in articles:
        source, was_created = ensure_archive_source(session, article.account)
        sources_created += int(was_created)
        run = CrawlRun(
            source_id=source.id,
            trigger="offline_import",
            status="running",
            fetched_count=1,
        )
        session.add(run)
        session.flush()
        result = ingest_article(session, source, run, article.extracted())
        new += int(result == "new")
        updated += int(result == "updated")
        skipped += int(result == "skipped")
        run.new_count = int(result == "new")
        run.updated_count = int(result == "updated")
        run.skipped_count = int(result == "skipped")
        run.status = "succeeded"
        run.finished_at = utcnow()
    return ArchiveImportSummary(
        rows=len(articles),
        sources_created=sources_created,
        new=new,
        updated=updated,
        skipped=skipped,
    )
