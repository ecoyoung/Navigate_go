"""Initial website ingestion schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("channel_type", sa.String(24), nullable=False),
        sa.Column("start_url", sa.Text(), nullable=False),
        sa.Column("normalized_start_url", sa.Text(), nullable=False),
        sa.Column("fetch_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("parser_config", sa.JSON(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("fetch_interval_seconds > 0", name="ck_sources_interval_positive"),
        sa.UniqueConstraint("channel_type", "normalized_start_url", name="uq_sources_channel_url"),
    )
    op.create_table(
        "crawl_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_summary", sa.Text()),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="ck_runs_time_order"
        ),
    )
    op.create_index("idx_crawl_runs_source_started", "crawl_runs", ["source_id", "started_at"])
    op.create_index(
        "uq_crawl_runs_active_source",
        "crawl_runs",
        ["source_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_table(
        "page_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "crawl_run_id",
            sa.Integer(),
            sa.ForeignKey("crawl_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("page_type", sa.String(24), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(200)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "raw_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "crawl_run_id",
            sa.Integer(),
            sa.ForeignKey("crawl_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text()),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("original_url", sa.Text()),
        sa.Column("canonical_url", sa.Text()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_id", "identity_key", "payload_sha256", name="uq_raw_item_version"
        ),
    )
    op.create_index("idx_raw_items_source_fetched", "raw_items", ["source_id", "fetched_at"])
    op.create_table(
        "content_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "raw_item_id",
            sa.Integer(),
            sa.ForeignKey("raw_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("external_id", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text()),
        sa.Column("canonical_url", sa.Text()),
        sa.Column("author", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("language", sa.String(24)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("normalizer_version", sa.String(40), nullable=False),
        sa.UniqueConstraint("source_id", "identity_key", name="uq_content_identity"),
    )
    op.create_index("idx_content_published_id", "content_items", ["published_at", "id"])
    op.create_index("idx_content_source_id", "content_items", ["source_id", "id"])


def downgrade() -> None:
    op.drop_table("content_items")
    op.drop_table("raw_items")
    op.drop_table("page_snapshots")
    op.drop_table("crawl_runs")
    op.drop_table("sources")
