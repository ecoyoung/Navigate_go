"""Add local accounts, hashed sessions, and domain subscriptions."""

import sqlalchemy as sa

from alembic import op

revision = "0021_accounts_subscriptions"
down_revision = "0020_content_value_scores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False, server_default="member"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_users_role"),
    )
    op.create_index("uq_users_email", "users", ["email"], unique=True)
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "uq_auth_sessions_token_hash", "auth_sessions", ["token_hash"], unique=True
    )
    op.create_index(
        "idx_auth_sessions_user_expires", "auth_sessions", ["user_id", "expires_at"]
    )
    op.create_table(
        "user_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "domain_id",
            sa.Integer(),
            sa.ForeignKey("domains.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "delivery_type",
            sa.String(length=40),
            nullable=False,
            server_default="daily_brief",
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "domain_id",
            "delivery_type",
            name="uq_user_subscription_delivery",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused')", name="ck_user_subscriptions_status"
        ),
    )
    op.create_index(
        "idx_user_subscriptions_user_status",
        "user_subscriptions",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_user_subscriptions_user_status", table_name="user_subscriptions"
    )
    op.drop_table("user_subscriptions")
    op.drop_index("idx_auth_sessions_user_expires", table_name="auth_sessions")
    op.drop_index("uq_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("uq_users_email", table_name="users")
    op.drop_table("users")
