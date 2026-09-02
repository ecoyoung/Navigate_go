import argparse

from app.database import SessionLocal
from app.domain_assignments import sync_processing_results_to_domain


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Project an existing processor into the generic multi-domain assignment layer."
    )
    parser.add_argument("--domain-key", required=True)
    parser.add_argument("--domain-name", required=True)
    parser.add_argument("--processor-name", required=True)
    parser.add_argument("--processor-version", required=True)
    parser.add_argument("--description")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with SessionLocal() as session:
        result = sync_processing_results_to_domain(
            session,
            domain_key=args.domain_key,
            domain_name=args.domain_name,
            description=args.description,
            processor_name=args.processor_name,
            processor_version=args.processor_version,
            activate_classifier=args.activate,
        )
        if args.apply:
            session.commit()
        else:
            session.rollback()
        mode = "applied" if args.apply else "dry-run"
        print(
            f"{mode}: domain_id={result.domain_id} created={result.created} "
            f"updated={result.updated} skipped={result.skipped}"
        )


if __name__ == "__main__":
    main()
