import argparse
from pathlib import Path

from app.contract_upgrade import load_archive_external_ids, upgrade_current_contracts
from app.database import SessionLocal

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = BACKEND_ROOT / "data" / "wechat_redfox_second.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append immutable article.v1.1 versions for current content without network"
    )
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write v1.1 versions. Without this flag all changes are rolled back.",
    )
    args = parser.parse_args()
    external_ids = load_archive_external_ids(args.archive)
    with SessionLocal() as session:
        summary = upgrade_current_contracts(session, archive_external_ids=external_ids)
        if args.apply:
            session.commit()
        else:
            session.rollback()
    print(
        f"scanned={summary.scanned} upgraded={summary.upgraded} skipped={summary.skipped} "
        f"external_ids={summary.external_ids_bound} snapshots={summary.snapshots_bound} "
        f"mode={'apply' if args.apply else 'dry-run'} network_requests=0"
    )


if __name__ == "__main__":
    main()
