"""Add generic cached LLM processing results."""

import sqlalchemy as sa
from alembic import op

revision = "0014_llm_processing_results"
down_revision = "0013_domains_and_generic_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_processing_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_key", sa.String(length=160), nullable=False),
        sa.Column("task_name", sa.String(length=80), nullable=False),
        sa.Column("task_version", sa.String(length=80), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "subject_type",
            "subject_key",
            "task_name",
            "task_version",
            "input_hash",
            "provider",
            "model",
            name="uq_llm_processing_input",
        ),
    )
    op.create_index(
        "idx_llm_processing_subject",
        "llm_processing_results",
        ["subject_type", "subject_key", "task_name"],
    )


def downgrade() -> None:
    op.drop_index("idx_llm_processing_subject", table_name="llm_processing_results")
    op.drop_table("llm_processing_results")
