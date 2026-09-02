"""Add article.v1.1 source identity, media, quality, and update metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0012_article_contract_v1_1"
down_revision = "0011_strict_cross_source_duplicates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("source_external_id", sa.Text(), nullable=True))
    with op.batch_alter_table("content_items") as batch:
        batch.add_column(sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("media", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("quality", sa.JSON(), nullable=False, server_default="{}"))
    op.create_index(
        "uq_content_source_external",
        "content_items",
        ["source_id", "external_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_content_source_external", table_name="content_items")
    with op.batch_alter_table("content_items") as batch:
        batch.drop_column("quality")
        batch.drop_column("media")
        batch.drop_column("source_updated_at")
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("source_external_id")
