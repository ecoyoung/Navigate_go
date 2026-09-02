"""Add versioned, explainable content value scores."""

import sqlalchemy as sa

from alembic import op

revision = "0020_content_value_scores"
down_revision = "0019_entity_candidate_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_value_score_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "domain_id",
            sa.Integer(),
            sa.ForeignKey("domains.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("algorithm_version", sa.String(length=80), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(length=80),
            nullable=False,
            server_default="content-value-score.v1",
        ),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("input_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_summary", sa.Text()),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_value_score_runs_status",
        ),
    )
    op.create_index(
        "uq_value_score_runs_input_hash",
        "content_value_score_runs",
        ["input_hash"],
        unique=True,
    )
    op.create_index(
        "idx_value_score_runs_domain_as_of",
        "content_value_score_runs",
        ["domain_id", "as_of"],
    )
    op.create_table(
        "content_value_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("content_value_score_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "content_item_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_content_hash", sa.String(length=64), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("component_scores", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("penalties", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("gates", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "content_item_id", name="uq_value_score_run_content"),
        sa.CheckConstraint(
            "total_score >= 0.0 AND total_score <= 100.0",
            name="ck_value_scores_total",
        ),
        sa.CheckConstraint(
            "decision IN ('selected', 'full_pool')",
            name="ck_value_scores_decision",
        ),
    )
    op.create_index(
        "idx_value_scores_run_decision_score",
        "content_value_scores",
        ["run_id", "decision", "total_score"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_value_scores_run_decision_score", table_name="content_value_scores"
    )
    op.drop_table("content_value_scores")
    op.drop_index(
        "idx_value_score_runs_domain_as_of", table_name="content_value_score_runs"
    )
    op.drop_index(
        "uq_value_score_runs_input_hash", table_name="content_value_score_runs"
    )
    op.drop_table("content_value_score_runs")
