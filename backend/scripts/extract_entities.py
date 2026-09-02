import argparse
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.entity_extraction import DEFAULT_POLICY_PATH, load_entity_policy, process_entities
from app.models import ContentDomainAssignment, ContentItem, Domain


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract and resolve configured entities.")
    parser.add_argument("--config", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--domain-key")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    policy = load_entity_policy(args.config)
    with SessionLocal() as session:
        statement = select(ContentItem).order_by(ContentItem.id)
        if args.domain_key:
            statement = (
                statement.join(
                    ContentDomainAssignment,
                    (ContentDomainAssignment.content_item_id == ContentItem.id)
                    & (
                        ContentDomainAssignment.input_content_hash
                        == ContentItem.content_hash
                    )
                    & (ContentDomainAssignment.decision == "include"),
                )
                .join(Domain, Domain.id == ContentDomainAssignment.domain_id)
                .where(Domain.key == args.domain_key)
            )
        result = process_entities(session, list(session.scalars(statement)), policy)
        if args.apply:
            session.commit()
        else:
            session.rollback()
        mode = "applied" if args.apply else "dry-run"
        print(
            f"{mode}: processed={result.processed} skipped={result.skipped} "
            f"candidates={result.candidates} resolved={result.resolved} "
            f"unresolved={result.unresolved}"
        )


if __name__ == "__main__":
    main()
