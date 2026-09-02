"""Add personalized interest topics and provider request audit tables."""

import sqlalchemy as sa

from alembic import op

revision = "0022_interest_topics"
down_revision = "0021_accounts_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interest_topics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("intent_text", sa.Text(), nullable=False),
        sa.Column("compiled_intent", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "compiler_name",
            sa.String(length=80),
            nullable=False,
            server_default="local_topic_compiler",
        ),
        sa.Column(
            "compiler_version",
            sa.String(length=80),
            nullable=False,
            server_default="topic-intent.v1",
        ),
        sa.Column("intent_hash", sa.String(length=64), nullable=False),
        sa.Column("cadence", sa.String(length=24), nullable=False, server_default="daily"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("daily_credit_limit", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'draft')", name="ck_interest_topics_status"
        ),
        sa.CheckConstraint(
            "cadence IN ('realtime', 'daily', 'weekly')", name="ck_interest_topics_cadence"
        ),
        sa.CheckConstraint(
            "daily_credit_limit >= 0 AND daily_credit_limit <= 100",
            name="ck_interest_topics_credit_limit",
        ),
    )
    op.create_index("idx_interest_topics_user_status", "interest_topics", ["user_id", "status"])
    op.create_table(
        "topic_source_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "topic_id",
            sa.Integer(),
            sa.ForeignKey("interest_topics.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("discovery_method", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="SET NULL")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="candidate"),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("topic_id", "canonical_url", name="uq_topic_source_candidate_url"),
        sa.CheckConstraint(
            "status IN ('candidate', 'approved', 'rejected')",
            name="ck_topic_source_candidates_status",
        ),
    )
    op.create_index(
        "idx_topic_source_candidates_topic_status",
        "topic_source_candidates",
        ["topic_id", "status"],
    )
    op.create_table(
        "topic_matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "topic_id",
            sa.Integer(),
            sa.ForeignKey("interest_topics.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "content_item_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "matcher_version",
            sa.String(length=80),
            nullable=False,
            server_default="topic-matcher.v1",
        ),
        sa.Column("input_content_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("matched_signals", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "topic_id", "content_item_id", "matcher_version", name="uq_topic_match_version"
        ),
        sa.CheckConstraint(
            "decision IN ('include', 'exclude', 'review')", name="ck_topic_matches_decision"
        ),
        sa.CheckConstraint("score >= 0.0 AND score <= 1.0", name="ck_topic_matches_score"),
    )
    op.create_index(
        "idx_topic_matches_topic_decision_score", "topic_matches", ["topic_id", "decision", "score"]
    )
    op.create_table(
        "topic_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "topic_id",
            sa.Integer(),
            sa.ForeignKey("interest_topics.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("pool_candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("search_candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fetched_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("firecrawl_credits_reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("firecrawl_credits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_topic_runs_topic_started", "topic_runs", ["topic_id", "started_at"])
    op.create_table(
        "provider_request_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("credits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "operation", "request_hash", name="uq_provider_request"),
    )
    op.create_index(
        "idx_provider_request_expires",
        "provider_request_cache",
        ["provider", "operation", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_provider_request_expires", table_name="provider_request_cache")
    op.drop_table("provider_request_cache")
    op.drop_index("idx_topic_runs_topic_started", table_name="topic_runs")
    op.drop_table("topic_runs")
    op.drop_index("idx_topic_matches_topic_decision_score", table_name="topic_matches")
    op.drop_table("topic_matches")
    op.drop_index("idx_topic_source_candidates_topic_status", table_name="topic_source_candidates")
    op.drop_table("topic_source_candidates")
    op.drop_index("idx_interest_topics_user_status", table_name="interest_topics")
    op.drop_table("interest_topics")
