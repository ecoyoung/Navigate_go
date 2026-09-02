import argparse

from app.database import SessionLocal
from app.strict_deduplication import rebuild_strict_duplicates


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild local exact-content duplicate relationships without AI"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write hashes and duplicate links. Without this flag, only preview the result.",
    )
    args = parser.parse_args()
    with SessionLocal() as session:
        summary = rebuild_strict_duplicates(session, apply=args.apply)
        if args.apply:
            session.commit()
        else:
            session.rollback()
    print(
        f"scanned={summary.scanned} hashes_updated={summary.hashes_updated} "
        f"groups={summary.groups} duplicates={summary.duplicates} "
        f"mode={'apply' if args.apply else 'dry-run'}"
    )


if __name__ == "__main__":
    main()
