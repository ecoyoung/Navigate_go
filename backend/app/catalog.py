import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from pydantic import BaseModel, Field, HttpUrl


class CatalogSource(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    name: str
    start_url: HttpUrl
    source_region: str
    source_type: str
    default_language: str
    source_tags: list[str]
    source_external_id: str | None = None
    channel_type: Literal["web", "rss", "api", "third_party_feed"] | None = None
    crawl_strategy: str = "html"
    skip_reason: str | None = None
    parser_config: dict = Field(default_factory=dict)
    processing_config: dict = Field(default_factory=dict)
    fetch_interval_seconds: int = Field(default=21600, gt=0)
    is_enabled: bool = True

    @property
    def resolved_channel_type(self) -> str:
        if self.channel_type:
            return self.channel_type
        method = self.parser_config.get("discovery_method")
        if method == "feed":
            return "rss"
        if method == "json":
            return "api"
        return "web"


class RedFoxWechatAccount(BaseModel):
    catalog_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    status: Literal["ready", "pending"]
    selector_kind: Literal["account", "wxId", "bizInfo"] | None = None
    selector_value: str | None = None
    source_external_id: str | None = None
    is_enabled: bool = False

    def to_catalog_source(self) -> CatalogSource | None:
        if self.status == "pending":
            if self.is_enabled:
                raise ValueError(f"pending RedFox account cannot be enabled: {self.name}")
            return None
        if not self.selector_kind or not self.selector_value or not self.source_external_id:
            raise ValueError(f"ready RedFox account lacks selector evidence: {self.name}")
        query = urlencode({"action": "home", "__biz": self.source_external_id})
        return CatalogSource(
            id=self.catalog_id,
            name=f"{self.name}公众号",
            start_url=f"https://mp.weixin.qq.com/mp/profile_ext?{query}",
            source_region="CN",
            source_type="trade_media",
            default_language="zh-CN",
            source_tags=["美妆", "品牌", "渠道", "产业"],
            source_external_id=self.source_external_id,
            channel_type="third_party_feed",
            crawl_strategy="json",
            parser_config={
                "channel_type": "third_party_feed",
                "provider": "redfox",
                "discovery_method": "json",
                "discovery_http_method": "POST",
                "discovery_url": "https://redfox.hk/story/api/gzh/data/queryWorkList",
                "discovery_json": {
                    self.selector_kind: self.selector_value,
                    "offset": 0,
                    "sortType": "2",
                },
                "detail_url": "https://redfox.hk/story/api/gzh/data/workDetail",
                "publication_date_mode": "previous_day",
                "publication_timezone": "Asia/Shanghai",
                "exclude_explicit_pinned": True,
                "exclude_explicit_advertising": True,
                "max_listing_pages": 10,
                "max_articles_per_day": 100,
                "min_content_chars": 80,
                "request_delay_seconds": 0.5,
                "access_level": "public",
                "request_headers_env": {
                    "REDFOX_API_KEY": "REDFOX_API_KEY",
                    "X-API-KEY": "REDFOX_API_KEY",
                },
            },
            processing_config={"scope_mode": "dedicated"},
            fetch_interval_seconds=86400,
            is_enabled=self.is_enabled,
        )


def load_redfox_wechat_accounts(path: Path) -> list[RedFoxWechatAccount]:
    data = json.loads(path.read_text(encoding="utf-8"))
    accounts = [RedFoxWechatAccount.model_validate(item) for item in data["accounts"]]
    ids = [item.catalog_id for item in accounts]
    names = [name for item in accounts for name in (item.name, *item.aliases)]
    if len(ids) != len(set(ids)):
        raise ValueError("RedFox WeChat catalog contains duplicate ids")
    if len(names) != len(set(names)):
        raise ValueError("RedFox WeChat catalog contains duplicate names or aliases")
    if any(not alias.strip() for item in accounts for alias in item.aliases):
        raise ValueError("RedFox WeChat catalog contains blank aliases")
    return accounts


def load_catalog(path: Path) -> list[CatalogSource]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = [CatalogSource.model_validate(item) for item in data["sources"]]
    account_path = path.with_name("wechat_accounts.json")
    if path.name == "sites.json" and account_path.is_file():
        sources.extend(
            source
            for account in load_redfox_wechat_accounts(account_path)
            if (source := account.to_catalog_source()) is not None
        )
    ids = [source.id for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("catalog contains duplicate ids")
    return sources
