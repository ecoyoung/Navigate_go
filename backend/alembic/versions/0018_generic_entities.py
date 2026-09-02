"""Add generic entity registry, aliases, mentions, and resolution candidates."""

import sqlalchemy as sa

from alembic import op

revision = "0018_generic_entities"
down_revision = "0017_crawl_run_coverage"
branch_labels = None
depends_on = None


ENTITY_TYPES = (
    "organization",
    "brand",
    "person",
    "product",
    "location",
    "substance",
    "regulation",
    "technology",
)


def upgrade() -> None:
    entity_type_check = "entity_type IN ({})".format(
        ", ".join(repr(value) for value in ENTITY_TYPES)
    )
    op.create_table(
        "entities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("registry_key", sa.String(length=160)),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("attributes", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(entity_type_check, name="ck_entities_type"),
    )
    op.create_index("uq_entities_registry_key", "entities", ["registry_key"], unique=True)
    op.create_index(
        "idx_entities_type_name", "entities", ["entity_type", "normalized_name"]
    )

    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("entities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("normalized_alias", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=24), nullable=False, server_default="und"),
        sa.Column(
            "alias_type", sa.String(length=24), nullable=False, server_default="configured"
        ),
        sa.Column("source", sa.String(length=80), nullable=False, server_default="policy"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "entity_id", "normalized_alias", "language", name="uq_entity_alias_language"
        ),
    )
    op.create_index(
        "idx_entity_aliases_normalized",
        "entity_aliases",
        ["normalized_alias", "language"],
    )

    op.create_table(
        "entity_processing_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "content_item_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("extractor_name", sa.String(length=80), nullable=False),
        sa.Column("extractor_version", sa.String(length=80), nullable=False),
        sa.Column("input_content_hash", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(length=80),
            nullable=False,
            server_default="entity-mentions.v1",
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="succeeded"),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "content_item_id",
            "extractor_name",
            "extractor_version",
            "input_content_hash",
            "config_hash",
            name="uq_entity_processing_input",
        ),
    )
    op.create_index(
        "idx_entity_processing_content",
        "entity_processing_results",
        ["content_item_id", "status", "id"],
    )

    op.create_table(
        "entity_mentions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "processing_result_id",
            sa.Integer(),
            sa.ForeignKey("entity_processing_results.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "content_item_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("entities.id", ondelete="RESTRICT"),
        ),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("normalized_surface", sa.Text(), nullable=False),
        sa.Column("field", sa.String(length=24), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("resolution_status", sa.String(length=24), nullable=False),
        sa.Column(
            "extraction_method",
            sa.String(length=32),
            nullable=False,
            server_default="configured_alias",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("start_offset >= 0", name="ck_entity_mention_start"),
        sa.CheckConstraint("end_offset > start_offset", name="ck_entity_mention_end"),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_entity_mention_confidence",
        ),
        sa.UniqueConstraint(
            "processing_result_id",
            "field",
            "start_offset",
            "end_offset",
            "entity_type",
            name="uq_entity_mention_span",
        ),
    )
    op.create_index(
        "idx_entity_mentions_entity", "entity_mentions", ["entity_id", "content_item_id"]
    )
    op.create_index(
        "idx_entity_mentions_content",
        "entity_mentions",
        ["content_item_id", "processing_result_id"],
    )

    op.create_table(
        "entity_resolution_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "mention_id",
            sa.Integer(),
            sa.ForeignKey("entity_mentions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "candidate_entity_id",
            sa.Integer(),
            sa.ForeignKey("entities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("signals", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "score >= 0.0 AND score <= 1.0", name="ck_entity_resolution_score"
        ),
        sa.UniqueConstraint(
            "mention_id", "candidate_entity_id", name="uq_entity_resolution_candidate"
        ),
    )
    op.create_index(
        "idx_entity_resolution_status",
        "entity_resolution_candidates",
        ["status", "score"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_entity_resolution_status", table_name="entity_resolution_candidates"
    )
    op.drop_table("entity_resolution_candidates")
    op.drop_index("idx_entity_mentions_content", table_name="entity_mentions")
    op.drop_index("idx_entity_mentions_entity", table_name="entity_mentions")
    op.drop_table("entity_mentions")
    op.drop_index("idx_entity_processing_content", table_name="entity_processing_results")
    op.drop_table("entity_processing_results")
    op.drop_index("idx_entity_aliases_normalized", table_name="entity_aliases")
    op.drop_table("entity_aliases")
    op.drop_index("idx_entities_type_name", table_name="entities")
    op.drop_index("uq_entities_registry_key", table_name="entities")
    op.drop_table("entities")
