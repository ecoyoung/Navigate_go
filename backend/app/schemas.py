from datetime import date, datetime
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    model_validator,
)

from .channel_adapters import ChannelConfigurationError, validate_channel_config

ChannelType = Literal["web", "rss", "api", "third_party_feed"]


def _reject_secret_bearing_api_config(parser_config: dict | None) -> None:
    config = parser_config or {}
    forbidden = {
        "request_headers",
        "request_headers_env",
        "authorization",
        "cookies",
    }
    present = sorted(key for key in forbidden if key in config)
    if present:
        raise ValueError(
            "API source registration cannot configure secret-bearing request fields: "
            + ", ".join(present)
        )


class SourceCreate(BaseModel):
    catalog_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    channel_type: ChannelType = "web"
    start_url: HttpUrl
    fetch_interval_seconds: int = Field(default=3600, gt=0, le=604800)
    parser_config: dict = Field(default_factory=dict)
    processing_config: dict = Field(default_factory=dict)
    source_region: str = Field(default="GLOBAL", min_length=2, max_length=24)
    source_type: str = Field(default="trade_media", min_length=2, max_length=40)
    default_language: str = Field(default="en", min_length=2, max_length=24)
    source_tags: list[str] = Field(default_factory=list)
    source_external_id: str | None = None

    @model_validator(mode="after")
    def validate_channel_rules(self):
        _reject_secret_bearing_api_config(self.parser_config)
        try:
            validate_channel_config(self.channel_type, self.parser_config)
        except ChannelConfigurationError as exc:
            raise ValueError(str(exc)) from exc
        return self


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    channel_type: ChannelType | None = None
    start_url: HttpUrl | None = None
    is_enabled: bool | None = None
    fetch_interval_seconds: int | None = Field(default=None, gt=0, le=604800)
    parser_config: dict | None = None
    processing_config: dict | None = None
    source_external_id: str | None = None

    @model_validator(mode="after")
    def reject_secret_bearing_config(self):
        _reject_secret_bearing_api_config(self.parser_config)
        return self


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    catalog_id: str | None
    name: str
    channel_type: str
    start_url: str
    fetch_interval_seconds: int
    parser_config: dict
    processing_config: dict
    source_region: str
    source_type: str
    default_language: str
    source_tags: list[str]
    source_external_id: str | None
    is_enabled: bool
    created_at: datetime
    updated_at: datetime
    last_run_status: str | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    circuit_open: bool = False
    last_finished_at: datetime | None = None


class CrawlRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_id: int
    trigger: str
    coverage_date: date | None
    publication_timezone: str | None
    retry_of_run_id: int | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    fetched_count: int
    new_count: int
    updated_count: int
    skipped_count: int
    rejected_count: int
    error_count: int
    error_code: str | None
    error_summary: str | None

    @computed_field
    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class ContentItemRead(BaseModel):
    id: int
    source_id: int
    source_name: str
    title: str
    original_url: str | None
    canonical_url: str | None
    author: str | None
    body: str | None
    language: str | None
    source_region: str
    source_type: str
    source_external_id: str | None
    external_item_id: str | None
    channel_type: str
    provider: str
    access_level: str
    content_type: str
    topics: list[str]
    is_sponsored: bool
    is_roundup: bool
    excerpt: str | None
    content_url: str | None
    discovery_url: str | None
    crawl_run_id: int | None
    page_snapshot_id: int | None
    updated_at: datetime | None
    word_count: int
    media: list[dict]
    quality: dict
    quality_tier: Literal["verified_full", "partial", "needs_enrichment"]
    content_hash: str
    schema_version: str
    published_at: datetime | None
    discovered_at: datetime
    duplicate_of_id: int | None = None
    duplicate_rule: str | None = None
    is_relevant: bool | None = None
    relevance_reason: str | None = None
    matched_topics: list[str] = Field(default_factory=list)
    matched_events: list[str] = Field(default_factory=list)


class RawItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_id: int
    crawl_run_id: int
    page_snapshot_id: int | None
    external_id: str | None
    identity_key: str
    original_url: str | None
    canonical_url: str | None
    payload: dict
    payload_sha256: str
    fetched_at: datetime


class PageSnapshotRead(BaseModel):
    id: int
    crawl_run_id: int
    url: str
    page_type: str
    request_method: str
    http_status: int
    content_type: str | None
    response_headers: dict
    error_text: str | None
    body_sha256: str
    fetched_at: datetime
    body: str | None = None


class CrawlAccepted(BaseModel):
    run_id: int
    status: str
    coverage_date: date | None = None
    publication_timezone: str | None = None


class DomainRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    name: str
    description: str | None
    config: dict
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class EntityAliasRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    alias: str
    normalized_alias: str
    language: str
    alias_type: str
    source: str
    confidence: float


class EntityMentionRead(BaseModel):
    id: int
    content_item_id: int
    content_title: str
    entity_type: str
    surface: str
    field: str
    start_offset: int
    end_offset: int
    evidence_text: str
    confidence: float
    resolution_status: str
    extraction_method: str


class EntityRead(BaseModel):
    id: int
    registry_key: str | None
    entity_type: str
    canonical_name: str
    normalized_name: str
    description: str | None
    attributes: dict
    status: str
    mention_count: int


class EntityDetailRead(EntityRead):
    aliases: list[EntityAliasRead]
    mentions: list[EntityMentionRead]


class EntityCandidateReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    candidate_key: str
    entity_type: str
    proposed_name: str
    normalized_name: str
    status: str
    resolved_entity_id: int | None
    mention_count: int
    mention_ids: list[int]
    evidence: list[dict]
    decision_action: str | None
    decision_reason: str | None
    decided_by: str | None
    decided_at: datetime | None


class EntityCandidateDecision(BaseModel):
    action: Literal["create", "link", "reject"]
    entity_id: int | None = None
    canonical_name: str | None = Field(default=None, min_length=1, max_length=200)
    decided_by: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)


class EventMemberRead(BaseModel):
    content_item_id: int
    title: str
    source_id: int
    source_name: str
    canonical_url: str | None
    published_at: datetime | None
    confidence: float
    reasons: dict
    decision_source: str


class EventRead(BaseModel):
    id: int
    canonical_title: str
    representative_content_id: int
    first_published_at: datetime
    last_published_at: datetime
    member_count: int
    source_count: int
    membership_hash: str
    cluster_version: str
    manual_lock: bool


class EventDetailRead(EventRead):
    members: list[EventMemberRead]


class ContentValueScoreRead(BaseModel):
    id: int
    run_id: int
    content_item_id: int
    title: str
    source_id: int
    source_name: str
    published_at: datetime | None
    total_score: float
    component_scores: dict
    penalties: list[dict]
    gates: list[str]
    decision: Literal["selected", "full_pool"]
    reasons: list[dict]
    scored_at: datetime
    algorithm_version: str
    schema_version: str
    as_of: datetime


class RegisterRequest(BaseModel):
    email: str = Field(
        min_length=3,
        max_length=64,
        validation_alias=AliasChoices("account", "email"),
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(
        min_length=3,
        max_length=64,
        validation_alias=AliasChoices("account", "email"),
    )
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class AdminUserCreate(BaseModel):
    email: str = Field(
        min_length=3,
        max_length=64,
        validation_alias=AliasChoices("account", "email"),
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    temporary_password: str = Field(min_length=12, max_length=128)
    role: Literal["admin", "member"] = "member"


class AdminUserUpdate(BaseModel):
    is_active: bool | None = None
    temporary_password: str | None = Field(default=None, min_length=12, max_length=128)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    display_name: str
    role: Literal["admin", "member"]
    is_active: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None


class AuthResponse(BaseModel):
    user: UserRead


class SubscriptionUpdate(BaseModel):
    status: Literal["active", "paused"]
    delivery_type: str = Field(default="daily_brief", pattern=r"^[a-z][a-z0-9_]{1,39}$")


class SubscriptionRead(BaseModel):
    id: int
    domain_key: str
    domain_name: str
    delivery_type: str
    status: Literal["active", "paused"]
    created_at: datetime
    updated_at: datetime


class TopicCreate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    intent_text: str = Field(min_length=2, max_length=2000)
    keywords: list[str] = Field(default_factory=list, max_length=16)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=16)
    cadence: Literal["realtime", "daily", "weekly"] = "daily"
    daily_credit_limit: int = Field(default=50, ge=0, le=100)


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    intent_text: str | None = Field(default=None, min_length=2, max_length=2000)
    keywords: list[str] | None = Field(default=None, max_length=16)
    excluded_keywords: list[str] | None = Field(default=None, max_length=16)
    cadence: Literal["realtime", "daily", "weekly"] | None = None
    status: Literal["active", "paused", "draft"] | None = None
    daily_credit_limit: int | None = Field(default=None, ge=0, le=100)


class TopicRead(BaseModel):
    id: int
    name: str
    intent_text: str
    compiled_intent: dict
    cadence: Literal["realtime", "daily", "weekly"]
    status: Literal["active", "paused", "draft"]
    daily_credit_limit: int
    match_count: int = 0
    candidate_source_count: int = 0
    created_at: datetime
    updated_at: datetime


class TopicFeedItem(BaseModel):
    content_id: int
    title: str
    excerpt: str | None
    source_name: str
    url: str | None
    published_at: datetime | None
    discovered_at: datetime
    language: str | None
    topic_ids: list[int]
    topic_names: list[str]
    tags: list[str] = Field(default_factory=list)
    match_score: float
    quality_tier: Literal["verified_full", "partial", "needs_enrichment"]
    reader_eligible: bool


class DailyReportHistoryItem(BaseModel):
    coverage_date: date
    available_content_count: int


class TopicPreview(BaseModel):
    topic: TopicRead
    items: list[TopicFeedItem]


class TopicDiscoverRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=50)


class TopicSourceCandidateRead(BaseModel):
    id: int
    topic_id: int
    canonical_url: str
    host: str
    title: str | None
    description: str | None
    discovery_method: str
    status: str
    confidence: float


class TopicDiscoverResponse(BaseModel):
    topic_id: int
    cache_hit: bool
    credits_used: int
    daily_credit_limit: int
    fetched_pages: int
    ingested_count: int
    metadata_only_count: int
    candidates: list[TopicSourceCandidateRead]
    items: list[TopicFeedItem]
