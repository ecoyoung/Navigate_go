"""Move semantic filtering into versioned midstream processing."""

import sqlalchemy as sa
from alembic import op

revision = "0005_midstream_processing"
down_revision = "0004_backfill_content_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("processing_config", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "content_processing_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("content_item_id", sa.Integer(), nullable=False),
        sa.Column(
            "processor_name",
            sa.String(length=40),
            nullable=False,
            server_default="industry_rules",
        ),
        sa.Column("processor_version", sa.String(length=40), nullable=False),
        sa.Column("is_relevant", sa.Boolean(), nullable=False),
        sa.Column("matched_topics", sa.JSON(), nullable=False),
        sa.Column("matched_events", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=120), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["content_item_id"], ["content_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_item_id",
            "processor_name",
            "processor_version",
            name="uq_content_processing_version",
        ),
    )
    op.create_index(
        "idx_processing_relevance",
        "content_processing_results",
        ["processor_name", "is_relevant", "content_item_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_processing_relevance", table_name="content_processing_results")
    op.drop_table("content_processing_results")
    op.drop_column("sources", "processing_config")
