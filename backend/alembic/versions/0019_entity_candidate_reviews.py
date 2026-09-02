"""Add auditable entity candidate review decisions."""

import sqlalchemy as sa

from alembic import op

revision = "0019_entity_candidate_reviews"
down_revision = "0018_generic_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_candidate_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_key", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("proposed_name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column(
            "resolved_entity_id",
            sa.Integer(),
            sa.ForeignKey("entities.id", ondelete="RESTRICT"),
        ),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mention_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("decision_action", sa.String(length=24)),
        sa.Column("decision_reason", sa.Text()),
        sa.Column("decided_by", sa.String(length=80)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_entity_candidate_review_status",
        ),
    )
    op.create_index(
        "uq_entity_candidate_review_key",
        "entity_candidate_reviews",
        ["candidate_key"],
        unique=True,
    )
    op.create_index(
        "idx_entity_candidate_review_status",
        "entity_candidate_reviews",
        ["status", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_entity_candidate_review_status", table_name="entity_candidate_reviews"
    )
    op.drop_index(
        "uq_entity_candidate_review_key", table_name="entity_candidate_reviews"
    )
    op.drop_table("entity_candidate_reviews")
