"""Add complete cache fingerprints to generic LLM results."""

import sqlalchemy as sa
from alembic import op

revision = "0015_llm_cache_fingerprints"
down_revision = "0014_llm_processing_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("llm_processing_results") as batch_op:
        batch_op.add_column(sa.Column("cache_key", sa.String(64)))
        batch_op.add_column(sa.Column("prompt_hash", sa.String(64)))
        batch_op.add_column(sa.Column("schema_version", sa.String(80)))
        batch_op.add_column(sa.Column("schema_hash", sa.String(64)))
        batch_op.add_column(sa.Column("validator_version", sa.String(80)))
        batch_op.drop_constraint("uq_llm_processing_input", type_="unique")
        batch_op.create_unique_constraint("uq_llm_processing_cache_key", ["cache_key"])


def downgrade() -> None:
    with op.batch_alter_table("llm_processing_results") as batch_op:
        batch_op.drop_constraint("uq_llm_processing_cache_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_llm_processing_input",
            [
                "subject_type",
                "subject_key",
                "task_name",
                "task_version",
                "input_hash",
                "provider",
                "model",
            ],
        )
        batch_op.drop_column("validator_version")
        batch_op.drop_column("schema_hash")
        batch_op.drop_column("schema_version")
        batch_op.drop_column("prompt_hash")
        batch_op.drop_column("cache_key")
