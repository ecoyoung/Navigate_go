"""Track the content hash used by midstream processing."""

import sqlalchemy as sa
from alembic import op

revision = "0010_processing_input_hash"
down_revision = "0009_channel_snapshot_traceability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("content_processing_results") as batch:
        batch.add_column(sa.Column("input_content_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("content_processing_results") as batch:
        batch.drop_column("input_content_hash")
