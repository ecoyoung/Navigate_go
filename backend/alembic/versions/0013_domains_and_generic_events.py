"""Add versioned domains and domain-neutral event clustering tables."""

import sqlalchemy as sa
from alembic import op

revision = "0013_domains_and_generic_events"
down_revision = "0012_article_contract_v1_1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domains",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_domains_key", "domains", ["key"], unique=True)

    op.create_table(
        "content_domain_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "content_item_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "domain_id",
            sa.Integer(),
            sa.ForeignKey("domains.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("classifier_name", sa.String(length=80), nullable=False),
        sa.Column("classifier_version", sa.String(length=80), nullable=False),
        sa.Column("input_content_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_domain_assignment_confidence",
        ),
        sa.UniqueConstraint(
            "content_item_id",
            "domain_id",
            "classifier_name",
            "classifier_version",
            name="uq_content_domain_classifier_version",
        ),
    )
    op.create_index(
        "idx_domain_assignments_domain_decision",
        "content_domain_assignments",
        ["domain_id", "decision"],
    )

    op.create_table(
        "event_cluster_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("algorithm_version", sa.String(length=80), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_pair_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("multi_item_event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reused_event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "representative_content_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("canonical_title", sa.Text(), nullable=False),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("membership_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("cluster_version", sa.String(length=80), nullable=False),
        sa.Column("manual_lock", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "merged_into_id",
            sa.Integer(),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_events_status_last_published", "events", ["status", "last_published_at"]
    )
    op.create_index("idx_events_membership_hash", "events", ["membership_hash"])

    op.create_table(
        "event_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "content_item_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("decision_source", sa.String(length=24), nullable=False),
        sa.Column("algorithm_version", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0", name="ck_event_member_confidence"
        ),
        sa.UniqueConstraint("event_id", "content_item_id", name="uq_event_member"),
    )
    op.create_index(
        "idx_event_members_content_active", "event_members", ["content_item_id", "is_active"]
    )
    op.create_index(
        "uq_event_members_active_content",
        "event_members",
        ["content_item_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "event_cluster_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cluster_run_id",
            sa.Integer(),
            sa.ForeignKey("event_cluster_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "left_content_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "right_content_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("signals", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("left_content_id < right_content_id", name="ck_event_candidate_order"),
        sa.CheckConstraint("score >= 0.0 AND score <= 1.0", name="ck_event_candidate_score"),
        sa.UniqueConstraint(
            "cluster_run_id",
            "left_content_id",
            "right_content_id",
            name="uq_event_candidate_run_pair",
        ),
    )
    op.create_index(
        "idx_event_candidates_status_score",
        "event_cluster_candidates",
        ["status", "score"],
    )

    op.create_table(
        "event_cluster_constraints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "left_content_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "right_content_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("left_content_id < right_content_id", name="ck_event_constraint_order"),
        sa.UniqueConstraint(
            "left_content_id", "right_content_id", name="uq_event_constraint_pair"
        ),
    )


def downgrade() -> None:
    op.drop_table("event_cluster_constraints")
    op.drop_index("idx_event_candidates_status_score", table_name="event_cluster_candidates")
    op.drop_table("event_cluster_candidates")
    op.drop_index("uq_event_members_active_content", table_name="event_members")
    op.drop_index("idx_event_members_content_active", table_name="event_members")
    op.drop_table("event_members")
    op.drop_index("idx_events_membership_hash", table_name="events")
    op.drop_index("idx_events_status_last_published", table_name="events")
    op.drop_table("events")
    op.drop_table("event_cluster_runs")
    op.drop_index(
        "idx_domain_assignments_domain_decision", table_name="content_domain_assignments"
    )
    op.drop_table("content_domain_assignments")
    op.drop_index("uq_domains_key", table_name="domains")
    op.drop_table("domains")
