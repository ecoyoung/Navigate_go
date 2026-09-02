"""Add committed per-source synchronization state."""

import sqlalchemy as sa

from alembic import op

revision = "0016_source_sync_states"
down_revision = "0015_llm_cache_fingerprints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_sync_states",
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("sync_version", sa.String(length=40), nullable=False),
        sa.Column("etag", sa.Text()),
        sa.Column("last_modified", sa.Text()),
        sa.Column("recent_entries", sa.JSON(), nullable=False),
        sa.Column("published_watermark", sa.DateTime(timezone=True)),
        sa.Column("updated_watermark", sa.DateTime(timezone=True)),
        sa.Column("last_committed_run_id", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_committed_run_id"], ["crawl_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("source_id"),
    )


def downgrade() -> None:
    op.drop_table("source_sync_states")
