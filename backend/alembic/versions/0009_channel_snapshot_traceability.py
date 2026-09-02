"""Add response evidence and raw item traceability for channel ingestion."""

import sqlalchemy as sa

from alembic import op

revision = "0009_channel_snapshot_traceability"
down_revision = "0008_stable_source_catalog_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("page_snapshots") as batch:
        batch.add_column(
            sa.Column("request_method", sa.String(length=12), nullable=False, server_default="GET")
        )
        batch.add_column(
            sa.Column("response_headers", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(sa.Column("error_text", sa.Text(), nullable=True))
    with op.batch_alter_table("raw_items") as batch:
        batch.add_column(sa.Column("page_snapshot_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_raw_items_page_snapshot_id",
            "page_snapshots",
            ["page_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("raw_items") as batch:
        batch.drop_constraint("fk_raw_items_page_snapshot_id", type_="foreignkey")
        batch.drop_column("page_snapshot_id")
    with op.batch_alter_table("page_snapshots") as batch:
        batch.drop_column("error_text")
        batch.drop_column("response_headers")
        batch.drop_column("request_method")
