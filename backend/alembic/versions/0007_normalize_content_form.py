"""Normalize legacy business-specific content type to a neutral form."""

from alembic import op

revision = "0007_normalize_content_form"
down_revision = "0006_crawl_form_rejections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE content_items SET content_type = 'article' WHERE content_type = 'industry_article'"
    )


def downgrade() -> None:
    # The original legacy labels cannot be distinguished from corrected rows.
    pass
