import argparse

from app.content_repair import repair_redfox_publication_timezone
from app.database import SessionLocal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair naive RedFox publication timestamps without changing old Raw rows."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-count", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.apply and args.expected_count is None:
        raise SystemExit("--apply requires --expected-count")
    with SessionLocal() as session:
        result = repair_redfox_publication_timezone(
            session,
            apply=args.apply,
            expected_count=args.expected_count,
        )
        if args.apply:
            session.commit()
        else:
            session.rollback()
        print(
            f"candidates={result.candidates} sources={result.sources} "
            f"inserted_raw={result.inserted_raw} reused_raw={result.reused_raw} "
            f"updated_content={result.updated_content} applied={args.apply}"
        )


if __name__ == "__main__":
    main()
