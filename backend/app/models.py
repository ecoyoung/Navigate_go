from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("fetch_interval_seconds > 0", name="ck_sources_interval_positive"),
        UniqueConstraint("channel_type", "normalized_start_url", name="uq_sources_channel_url"),
        Index("uq_sources_catalog_id", "catalog_id", unique=True),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_id: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    channel_type: Mapped[str] = mapped_column(String(24), default="web")
    start_url: Mapped[str] = mapped_column(Text)
    normalized_start_url: Mapped[str] = mapped_column(Text)
    fetch_interval_seconds: Mapped[int] = mapped_column(default=3600)
    parser_config: Mapped[dict] = mapped_column(JSON, default=dict)
    processing_config: Mapped[dict] = mapped_column(JSON, default=dict)
    source_region: Mapped[str] = mapped_column(String(24), default="GLOBAL")
    source_type: Mapped[str] = mapped_column(String(40), default="trade_media")
    default_language: Mapped[str] = mapped_column(String(24), default="en")
    source_tags: Mapped[list] = mapped_column(JSON, default=list)
    source_external_id: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    runs: Mapped[list["CrawlRun"]] = relationship(back_populates="source")


class SourceSyncState(Base):
    __tablename__ = "source_sync_states"
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    sync_version: Mapped[str] = mapped_column(String(40), default="feed-sync.v1")
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    recent_entries: Mapped[list] = mapped_column(JSON, default=list)
    published_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_committed_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="ck_runs_time_order"
        ),
        CheckConstraint(
            "coverage_date IS NULL OR publication_timezone IS NOT NULL",
            name="ck_runs_coverage_timezone",
        ),
        Index("idx_crawl_runs_source_started", "source_id", "started_at"),
        Index(
            "idx_crawl_runs_source_coverage",
            "source_id",
            "coverage_date",
            "started_at",
        ),
        Index(
            "uq_crawl_runs_active_source",
            "source_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    trigger: Mapped[str] = mapped_column(String(24), default="manual")
    coverage_date: Mapped[date | None] = mapped_column(Date)
    publication_timezone: Mapped[str | None] = mapped_column(String(64))
    retry_of_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_count: Mapped[int] = mapped_column(default=0)
    new_count: Mapped[int] = mapped_column(default=0)
    updated_count: Mapped[int] = mapped_column(default=0)
    skipped_count: Mapped[int] = mapped_column(default=0)
    rejected_count: Mapped[int] = mapped_column(default=0)
    error_count: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[Source] = relationship(back_populates="runs")


class PageSnapshot(Base):
    __tablename__ = "page_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="RESTRICT"))
    url: Mapped[str] = mapped_column(Text)
    page_type: Mapped[str] = mapped_column(String(24))
    request_method: Mapped[str] = mapped_column(String(12), default="GET")
    http_status: Mapped[int]
    content_type: Mapped[str | None] = mapped_column(String(200))
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    error_text: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    body_sha256: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawItem(Base):
    __tablename__ = "raw_items"
    __table_args__ = (
        UniqueConstraint("source_id", "identity_key", "payload_sha256", name="uq_raw_item_version"),
        Index("idx_raw_items_source_fetched", "source_id", "fetched_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="RESTRICT"))
    page_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("page_snapshots.id", ondelete="RESTRICT")
    )
    external_id: Mapped[str | None] = mapped_column(Text)
    identity_key: Mapped[str] = mapped_column(String(64))
    original_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContentItem(Base):
    __tablename__ = "content_items"
    __table_args__ = (
        UniqueConstraint("source_id", "identity_key", name="uq_content_identity"),
        Index("idx_content_published_id", "published_at", "id"),
        Index("idx_content_source_id", "source_id", "id"),
        Index("uq_content_source_external", "source_id", "external_id", unique=True),
        Index("idx_content_content_hash", "content_hash"),
        Index("idx_content_duplicate_of", "duplicate_of_id"),
        CheckConstraint(
            "duplicate_of_id IS NULL OR duplicate_of_id != id",
            name="ck_no_self_duplicate",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id", ondelete="RESTRICT"))
    identity_key: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(24))
    source_region: Mapped[str] = mapped_column(String(24), default="GLOBAL")
    source_type: Mapped[str] = mapped_column(String(40), default="trade_media")
    access_level: Mapped[str] = mapped_column(String(24), default="public")
    content_type: Mapped[str] = mapped_column(String(40), default="article")
    topics: Mapped[list] = mapped_column(JSON, default=list)
    is_sponsored: Mapped[bool] = mapped_column(Boolean, default=False)
    is_roundup: Mapped[bool] = mapped_column(Boolean, default=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    media: Mapped[list] = mapped_column(JSON, default=list)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    schema_version: Mapped[str] = mapped_column(String(32), default="article.v1.1")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    normalizer_version: Mapped[str] = mapped_column(String(40), default="unified-v1.1")
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    duplicate_rule: Mapped[str | None] = mapped_column(String(40))
    processing_results: Mapped[list["ContentProcessingResult"]] = relationship(
        back_populates="content_item"
    )


class ContentProcessingResult(Base):
    __tablename__ = "content_processing_results"
    __table_args__ = (
        UniqueConstraint(
            "content_item_id",
            "processor_name",
            "processor_version",
            name="uq_content_processing_version",
        ),
        Index("idx_processing_relevance", "processor_name", "is_relevant", "content_item_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    processor_name: Mapped[str] = mapped_column(String(40), default="industry_rules")
    processor_version: Mapped[str] = mapped_column(String(40))
    input_content_hash: Mapped[str | None] = mapped_column(String(64))
    is_relevant: Mapped[bool] = mapped_column(Boolean)
    matched_topics: Mapped[list] = mapped_column(JSON, default=list)
    matched_events: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(120))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content_item: Mapped[ContentItem] = relationship(back_populates="processing_results")


class Domain(Base):
    __tablename__ = "domains"
    __table_args__ = (Index("uq_domains_key", "key", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ContentDomainAssignment(Base):
    __tablename__ = "content_domain_assignments"
    __table_args__ = (
        UniqueConstraint(
            "content_item_id",
            "domain_id",
            "classifier_name",
            "classifier_version",
            name="uq_content_domain_classifier_version",
        ),
        Index("idx_domain_assignments_domain_decision", "domain_id", "decision"),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_domain_assignment_confidence",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="RESTRICT"))
    classifier_name: Mapped[str] = mapped_column(String(80))
    classifier_version: Mapped[str] = mapped_column(String(80))
    input_content_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(24))
    confidence: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventClusterRun(Base):
    __tablename__ = "event_cluster_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    algorithm_version: Mapped[str] = mapped_column(String(80))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="running")
    input_count: Mapped[int] = mapped_column(default=0)
    candidate_pair_count: Mapped[int] = mapped_column(default=0)
    event_count: Mapped[int] = mapped_column(default=0)
    multi_item_event_count: Mapped[int] = mapped_column(default=0)
    created_event_count: Mapped[int] = mapped_column(default=0)
    reused_event_count: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("idx_events_status_last_published", "status", "last_published_at"),
        Index("idx_events_membership_hash", "membership_hash"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    representative_content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    canonical_title: Mapped[str] = mapped_column(Text)
    first_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    membership_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="active")
    cluster_version: Mapped[str] = mapped_column(String(80))
    manual_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    members: Mapped[list["EventMember"]] = relationship(back_populates="event")


class EventMember(Base):
    __tablename__ = "event_members"
    __table_args__ = (
        UniqueConstraint("event_id", "content_item_id", name="uq_event_member"),
        Index("idx_event_members_content_active", "content_item_id", "is_active"),
        Index(
            "uq_event_members_active_content",
            "content_item_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_event_member_confidence",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    confidence: Mapped[float] = mapped_column(Float)
    reasons: Mapped[dict] = mapped_column(JSON, default=dict)
    decision_source: Mapped[str] = mapped_column(String(24), default="automatic")
    algorithm_version: Mapped[str] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    event: Mapped[Event] = relationship(back_populates="members")


class EventClusterCandidate(Base):
    __tablename__ = "event_cluster_candidates"
    __table_args__ = (
        UniqueConstraint(
            "cluster_run_id",
            "left_content_id",
            "right_content_id",
            name="uq_event_candidate_run_pair",
        ),
        CheckConstraint("left_content_id < right_content_id", name="ck_event_candidate_order"),
        CheckConstraint("score >= 0.0 AND score <= 1.0", name="ck_event_candidate_score"),
        Index("idx_event_candidates_status_score", "status", "score"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_run_id: Mapped[int] = mapped_column(
        ForeignKey("event_cluster_runs.id", ondelete="RESTRICT")
    )
    left_content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    right_content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    score: Mapped[float] = mapped_column(Float)
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventClusterConstraint(Base):
    __tablename__ = "event_cluster_constraints"
    __table_args__ = (
        UniqueConstraint(
            "left_content_id",
            "right_content_id",
            name="uq_event_constraint_pair",
        ),
        CheckConstraint("left_content_id < right_content_id", name="ck_event_constraint_order"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    left_content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    right_content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    relation: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(80), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        Index("uq_entities_registry_key", "registry_key", unique=True),
        Index("idx_entities_type_name", "entity_type", "normalized_name"),
        CheckConstraint(
            "entity_type IN ('organization', 'brand', 'person', 'product', "
            "'location', 'substance', 'regulation', 'technology')",
            name="ck_entities_type",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    registry_key: Mapped[str | None] = mapped_column(String(160))
    entity_type: Mapped[str] = mapped_column(String(32))
    canonical_name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EntityAlias(Base):
    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "normalized_alias", "language", name="uq_entity_alias_language"
        ),
        Index("idx_entity_aliases_normalized", "normalized_alias", "language"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="RESTRICT"))
    alias: Mapped[str] = mapped_column(Text)
    normalized_alias: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(24), default="und")
    alias_type: Mapped[str] = mapped_column(String(24), default="configured")
    source: Mapped[str] = mapped_column(String(80), default="policy")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EntityProcessingResult(Base):
    __tablename__ = "entity_processing_results"
    __table_args__ = (
        UniqueConstraint(
            "content_item_id",
            "extractor_name",
            "extractor_version",
            "input_content_hash",
            "config_hash",
            name="uq_entity_processing_input",
        ),
        Index("idx_entity_processing_content", "content_item_id", "status", "id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    extractor_name: Mapped[str] = mapped_column(String(80))
    extractor_version: Mapped[str] = mapped_column(String(80))
    input_content_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(80), default="entity-mentions.v1")
    status: Mapped[str] = mapped_column(String(24), default="succeeded")
    candidate_count: Mapped[int] = mapped_column(default=0)
    resolved_count: Mapped[int] = mapped_column(default=0)
    unresolved_count: Mapped[int] = mapped_column(default=0)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EntityMention(Base):
    __tablename__ = "entity_mentions"
    __table_args__ = (
        UniqueConstraint(
            "processing_result_id",
            "field",
            "start_offset",
            "end_offset",
            "entity_type",
            name="uq_entity_mention_span",
        ),
        Index("idx_entity_mentions_entity", "entity_id", "content_item_id"),
        Index("idx_entity_mentions_content", "content_item_id", "processing_result_id"),
        CheckConstraint("start_offset >= 0", name="ck_entity_mention_start"),
        CheckConstraint("end_offset > start_offset", name="ck_entity_mention_end"),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_entity_mention_confidence",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    processing_result_id: Mapped[int] = mapped_column(
        ForeignKey("entity_processing_results.id", ondelete="RESTRICT")
    )
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT")
    )
    entity_type: Mapped[str] = mapped_column(String(32))
    surface: Mapped[str] = mapped_column(Text)
    normalized_surface: Mapped[str] = mapped_column(Text)
    field: Mapped[str] = mapped_column(String(24))
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    evidence_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float]
    resolution_status: Mapped[str] = mapped_column(String(24))
    extraction_method: Mapped[str] = mapped_column(String(32), default="configured_alias")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EntityResolutionCandidate(Base):
    __tablename__ = "entity_resolution_candidates"
    __table_args__ = (
        UniqueConstraint(
            "mention_id", "candidate_entity_id", name="uq_entity_resolution_candidate"
        ),
        Index("idx_entity_resolution_status", "status", "score"),
        CheckConstraint(
            "score >= 0.0 AND score <= 1.0", name="ck_entity_resolution_score"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    mention_id: Mapped[int] = mapped_column(
        ForeignKey("entity_mentions.id", ondelete="RESTRICT")
    )
    candidate_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT")
    )
    score: Mapped[float]
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EntityCandidateReview(Base):
    __tablename__ = "entity_candidate_reviews"
    __table_args__ = (
        Index("uq_entity_candidate_review_key", "candidate_key", unique=True),
        Index("idx_entity_candidate_review_status", "status", "id"),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_entity_candidate_review_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_key: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    proposed_name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    resolved_entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT")
    )
    mention_count: Mapped[int] = mapped_column(default=0)
    mention_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    decision_action: Mapped[str | None] = mapped_column(String(24))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(80))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ContentValueScoreRun(Base):
    __tablename__ = "content_value_score_runs"
    __table_args__ = (
        Index("uq_value_score_runs_input_hash", "input_hash", unique=True),
        Index("idx_value_score_runs_domain_as_of", "domain_id", "as_of"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_value_score_runs_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="RESTRICT"))
    algorithm_version: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[str] = mapped_column(String(80), default="content-value-score.v1")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="running")
    input_count: Mapped[int] = mapped_column(default=0)
    selected_count: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)


class ContentValueScore(Base):
    __tablename__ = "content_value_scores"
    __table_args__ = (
        UniqueConstraint("run_id", "content_item_id", name="uq_value_score_run_content"),
        Index("idx_value_scores_run_decision_score", "run_id", "decision", "total_score"),
        CheckConstraint(
            "total_score >= 0.0 AND total_score <= 100.0",
            name="ck_value_scores_total",
        ),
        CheckConstraint(
            "decision IN ('selected', 'full_pool')",
            name="ck_value_scores_decision",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("content_value_score_runs.id", ondelete="RESTRICT")
    )
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    input_content_hash: Mapped[str] = mapped_column(String(64))
    total_score: Mapped[float] = mapped_column(Float)
    component_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    penalties: Mapped[list] = mapped_column(JSON, default=list)
    gates: Mapped[list] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(24))
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LLMProcessingResult(Base):
    __tablename__ = "llm_processing_results"
    __table_args__ = (
        UniqueConstraint(
            "cache_key",
            name="uq_llm_processing_cache_key",
        ),
        Index("idx_llm_processing_subject", "subject_type", "subject_key", "task_name"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(String(160))
    task_name: Mapped[str] = mapped_column(String(80))
    task_version: Mapped[str] = mapped_column(String(80))
    input_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(80))
    cache_key: Mapped[str | None] = mapped_column(String(64))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    schema_version: Mapped[str | None] = mapped_column(String(80))
    schema_hash: Mapped[str | None] = mapped_column(String(64))
    validator_version: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="succeeded")
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    total_tokens: Mapped[int | None]
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_email", "email", unique=True),
        CheckConstraint("role IN ('admin', 'member')", name="ck_users_role"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(24), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("uq_auth_sessions_token_hash", "token_hash", unique=True),
        Index("idx_auth_sessions_user_expires", "user_id", "expires_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "domain_id",
            "delivery_type",
            name="uq_user_subscription_delivery",
        ),
        Index("idx_user_subscriptions_user_status", "user_id", "status"),
        CheckConstraint(
            "status IN ('active', 'paused')", name="ck_user_subscriptions_status"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="RESTRICT")
    )
    delivery_type: Mapped[str] = mapped_column(String(40), default="daily_brief")
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class InterestTopic(Base):
    __tablename__ = "interest_topics"
    __table_args__ = (
        Index("idx_interest_topics_user_status", "user_id", "status"),
        CheckConstraint(
            "status IN ('active', 'paused', 'draft')", name="ck_interest_topics_status"
        ),
        CheckConstraint(
            "cadence IN ('realtime', 'daily', 'weekly')", name="ck_interest_topics_cadence"
        ),
        CheckConstraint(
            "daily_credit_limit >= 0 AND daily_credit_limit <= 100",
            name="ck_interest_topics_credit_limit",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    intent_text: Mapped[str] = mapped_column(Text)
    compiled_intent: Mapped[dict] = mapped_column(JSON, default=dict)
    compiler_name: Mapped[str] = mapped_column(String(80), default="local_topic_compiler")
    compiler_version: Mapped[str] = mapped_column(String(80), default="topic-intent.v1")
    intent_hash: Mapped[str] = mapped_column(String(64))
    cadence: Mapped[str] = mapped_column(String(24), default="daily")
    status: Mapped[str] = mapped_column(String(24), default="active")
    daily_credit_limit: Mapped[int] = mapped_column(default=4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TopicSourceCandidate(Base):
    __tablename__ = "topic_source_candidates"
    __table_args__ = (
        UniqueConstraint("topic_id", "canonical_url", name="uq_topic_source_candidate_url"),
        Index("idx_topic_source_candidates_topic_status", "topic_id", "status"),
        CheckConstraint(
            "status IN ('candidate', 'approved', 'rejected')",
            name="ck_topic_source_candidates_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("interest_topics.id", ondelete="CASCADE")
    )
    canonical_url: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    discovery_method: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL")
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(24), default="candidate")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TopicMatch(Base):
    __tablename__ = "topic_matches"
    __table_args__ = (
        UniqueConstraint(
            "topic_id", "content_item_id", "matcher_version", name="uq_topic_match_version"
        ),
        Index("idx_topic_matches_topic_decision_score", "topic_id", "decision", "score"),
        CheckConstraint(
            "decision IN ('include', 'exclude', 'review')", name="ck_topic_matches_decision"
        ),
        CheckConstraint("score >= 0.0 AND score <= 1.0", name="ck_topic_matches_score"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("interest_topics.id", ondelete="CASCADE")
    )
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE")
    )
    matcher_version: Mapped[str] = mapped_column(String(80), default="topic-matcher.v1")
    input_content_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(24))
    score: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    matched_signals: Mapped[dict] = mapped_column(JSON, default=dict)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TopicRun(Base):
    __tablename__ = "topic_runs"
    __table_args__ = (Index("idx_topic_runs_topic_started", "topic_id", "started_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("interest_topics.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="running")
    pool_candidates: Mapped[int] = mapped_column(default=0)
    search_candidates: Mapped[int] = mapped_column(default=0)
    fetched_pages: Mapped[int] = mapped_column(default=0)
    matched_items: Mapped[int] = mapped_column(default=0)
    firecrawl_credits_reserved: Mapped[int] = mapped_column(default=0)
    firecrawl_credits_used: Mapped[int] = mapped_column(default=0)
    llm_tokens_used: Mapped[int] = mapped_column(default=0)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderRequestCache(Base):
    __tablename__ = "provider_request_cache"
    __table_args__ = (
        UniqueConstraint("provider", "operation", "request_hash", name="uq_provider_request"),
        Index("idx_provider_request_expires", "provider", "operation", "expires_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    operation: Mapped[str] = mapped_column(String(40))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    credits_used: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
