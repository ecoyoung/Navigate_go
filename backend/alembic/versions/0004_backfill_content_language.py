"""Backfill language labels on content created before article.v1."""

from alembic import op

revision = "0004_backfill_content_language"
down_revision = "0003_source_access_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE content_items
        SET language = (
            SELECT sources.default_language
            FROM sources
            WHERE sources.id = content_items.source_id
        )
        WHERE language IS NULL
        """
    )


def downgrade() -> None:
    # The original null/non-null distinction cannot be reconstructed safely.
    pass
