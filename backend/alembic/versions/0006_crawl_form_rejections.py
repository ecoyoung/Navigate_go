"""Replace crawl-time business filtering with content-form rejection count."""

import sqlalchemy as sa
from alembic import op

revision = "0006_crawl_form_rejections"
down_revision = "0005_midstream_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crawl_runs",
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_column("crawl_runs", "filtered_count")


def downgrade() -> None:
    op.add_column(
        "crawl_runs",
        sa.Column("filtered_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_column("crawl_runs", "rejected_count")
