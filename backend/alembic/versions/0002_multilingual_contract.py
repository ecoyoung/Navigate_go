"""Add multilingual source labels and article contract fields."""

from alembic import op
import sqlalchemy as sa

revision = "0002_multilingual_contract"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("source_region", sa.String(24), nullable=False, server_default="GLOBAL"),
    )
    op.add_column(
        "sources", sa.Column("default_language", sa.String(24), nullable=False, server_default="en")
    )
    op.add_column(
        "sources", sa.Column("source_tags", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column(
        "crawl_runs", sa.Column("filtered_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "content_items",
        sa.Column("source_region", sa.String(24), nullable=False, server_default="GLOBAL"),
    )
    op.add_column(
        "content_items",
        sa.Column("content_type", sa.String(40), nullable=False, server_default="industry_article"),
    )
    op.add_column(
        "content_items", sa.Column("topics", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column("content_items", sa.Column("excerpt", sa.Text(), nullable=True))
    op.add_column(
        "content_items", sa.Column("content_hash", sa.String(64), nullable=False, server_default="")
    )
    op.add_column(
        "content_items",
        sa.Column("schema_version", sa.String(32), nullable=False, server_default="article.v1"),
    )


def downgrade() -> None:
    op.drop_column("content_items", "schema_version")
    op.drop_column("content_items", "content_hash")
    op.drop_column("content_items", "excerpt")
    op.drop_column("content_items", "topics")
    op.drop_column("content_items", "content_type")
    op.drop_column("content_items", "source_region")
    op.drop_column("crawl_runs", "filtered_count")
    op.drop_column("sources", "source_tags")
    op.drop_column("sources", "default_language")
    op.drop_column("sources", "source_region")
