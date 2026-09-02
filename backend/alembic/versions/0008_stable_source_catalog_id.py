"""Add stable catalog identity for sources whose URLs can change."""

import sqlalchemy as sa
from alembic import op

revision = "0008_stable_source_catalog_id"
down_revision = "0007_normalize_content_form"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("catalog_id", sa.String(length=80), nullable=True))
    op.create_index("uq_sources_catalog_id", "sources", ["catalog_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_sources_catalog_id", table_name="sources")
    op.drop_column("sources", "catalog_id")
