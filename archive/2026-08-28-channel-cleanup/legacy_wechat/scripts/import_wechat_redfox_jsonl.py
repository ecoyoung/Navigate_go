import argparse
from pathlib import Path

from app.database import SessionLocal
from app.wechat_archive import import_archive_articles, load_archive_articles

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = BACKEND_ROOT / "data" / "wechat_redfox_second.jsonl"
DEFAULT_ACCOUNTS = BACKEND_ROOT / "config" / "wechat_mp_accounts.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import already-downloaded RedFox WeChat articles without network requests"
    )
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to the database. Without this flag the command only validates and previews.",
    )
    args = parser.parse_args()

    articles = load_archive_articles(args.archive, args.accounts)
    print(f"Validated offline archive: {len(articles)} ok articles; network requests: 0")
    if not args.apply:
        print("Dry run only. Re-run with --apply to write the validated rows.")
        return

    with SessionLocal() as session:
        try:
            summary = import_archive_articles(session, articles)
            session.commit()
        except Exception:
            session.rollback()
            raise
    print(
        f"Imported rows={summary.rows} sources_created={summary.sources_created} "
        f"new={summary.new} updated={summary.updated} skipped={summary.skipped}; "
        "network requests: 0"
    )


if __name__ == "__main__":
    main()
