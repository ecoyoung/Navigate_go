"""Freeze publication coverage on crawl runs."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from alembic import op

revision = "0017_crawl_run_coverage"
down_revision = "0016_source_sync_states"
branch_labels = None
depends_on = None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def upgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.add_column(sa.Column("coverage_date", sa.Date()))
        batch_op.add_column(sa.Column("publication_timezone", sa.String(length=64)))
        batch_op.add_column(sa.Column("retry_of_run_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_crawl_runs_retry_of", "crawl_runs", ["retry_of_run_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_index(
            "idx_crawl_runs_source_coverage",
            ["source_id", "coverage_date", "started_at"],
        )

    connection = op.get_bind()
    sources = sa.table(
        "sources",
        sa.column("id", sa.Integer()),
        sa.column("parser_config", sa.JSON()),
    )
    runs = sa.table(
        "crawl_runs",
        sa.column("source_id", sa.Integer()),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("coverage_date", sa.Date()),
        sa.column("publication_timezone", sa.String(length=64)),
    )
    for source_id, parser_config in connection.execute(
        sa.select(sources.c.id, sources.c.parser_config)
    ):
        config = parser_config if isinstance(parser_config, dict) else {}
        if (
            config.get("provider") != "redfox"
            or config.get("publication_date_mode") != "previous_day"
        ):
            continue
        timezone_name = str(config.get("publication_timezone") or "Asia/Shanghai")
        timezone = ZoneInfo(timezone_name)
        source_runs = connection.execute(
            sa.select(runs.c.started_at).where(runs.c.source_id == source_id)
        )
        for (started_at,) in source_runs:
            coverage_date = (
                _as_utc(started_at).astimezone(timezone).date() - timedelta(days=1)
            )
            connection.execute(
                runs.update()
                .where(
                    runs.c.source_id == source_id,
                    runs.c.started_at == started_at,
                )
                .values(
                    coverage_date=coverage_date,
                    publication_timezone=timezone_name,
                )
            )

    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.create_check_constraint(
            "ck_runs_coverage_timezone",
            "coverage_date IS NULL OR publication_timezone IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_constraint("ck_runs_coverage_timezone", type_="check")
        batch_op.drop_index("idx_crawl_runs_source_coverage")
        batch_op.drop_constraint("fk_crawl_runs_retry_of", type_="foreignkey")
        batch_op.drop_column("retry_of_run_id")
        batch_op.drop_column("publication_timezone")
        batch_op.drop_column("coverage_date")
