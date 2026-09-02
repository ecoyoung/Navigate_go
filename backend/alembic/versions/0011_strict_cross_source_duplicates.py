"""Add explainable strict cross-source duplicate relationships."""

import sqlalchemy as sa
from alembic import op

revision = "0011_strict_cross_source_duplicates"
down_revision = "0010_processing_input_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("content_items") as batch:
        batch.add_column(sa.Column("duplicate_of_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("duplicate_rule", sa.String(length=40), nullable=True))
        batch.create_foreign_key(
            "fk_content_items_duplicate_of_id",
            "content_items",
            ["duplicate_of_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_no_self_duplicate", "duplicate_of_id IS NULL OR duplicate_of_id != id"
        )
    op.create_index("idx_content_content_hash", "content_items", ["content_hash"])
    op.create_index("idx_content_duplicate_of", "content_items", ["duplicate_of_id"])


def downgrade() -> None:
    op.drop_index("idx_content_duplicate_of", table_name="content_items")
    op.drop_index("idx_content_content_hash", table_name="content_items")
    with op.batch_alter_table("content_items") as batch:
        batch.drop_constraint("ck_no_self_duplicate", type_="check")
        batch.drop_constraint("fk_content_items_duplicate_of_id", type_="foreignkey")
        batch.drop_column("duplicate_rule")
        batch.drop_column("duplicate_of_id")
