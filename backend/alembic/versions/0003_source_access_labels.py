"""Add source type and access labels."""

from alembic import op
import sqlalchemy as sa

revision = "0003_source_access_labels"
down_revision = "0002_multilingual_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("source_type", sa.String(40), nullable=False, server_default="trade_media"),
    )
    op.add_column(
        "content_items",
        sa.Column("source_type", sa.String(40), nullable=False, server_default="trade_media"),
    )
    op.add_column(
        "content_items",
        sa.Column("access_level", sa.String(24), nullable=False, server_default="public"),
    )
    op.add_column(
        "content_items",
        sa.Column("is_sponsored", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "content_items",
        sa.Column("is_roundup", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("content_items", "is_roundup")
    op.drop_column("content_items", "is_sponsored")
    op.drop_column("content_items", "access_level")
    op.drop_column("content_items", "source_type")
    op.drop_column("sources", "source_type")
