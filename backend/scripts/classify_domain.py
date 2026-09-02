import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.domain_relevance import load_domain_relevance_policy, process_domain_relevance
from app.models import ContentItem, Source


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a versioned domain relevance policy.")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    policy = load_domain_relevance_policy(args.domain)

    processed = relevant = irrelevant = 0
    with SessionLocal() as session:
        rows = session.execute(
            select(ContentItem, Source).join(Source, Source.id == ContentItem.source_id)
        )
        for content, source in rows:
            result, changed = process_domain_relevance(session, content, source, policy)
            if not changed:
                continue
            processed += 1
            relevant += int(result.is_relevant)
            irrelevant += int(not result.is_relevant)
        if args.apply:
            session.commit()
        else:
            session.rollback()
    mode = "applied" if args.apply else "dry-run"
    print(
        f"{mode}: domain={args.domain} processed={processed} "
        f"relevant={relevant} irrelevant={irrelevant} "
        f"classifier={policy['classifier_name']}:{policy['classifier_version']}"
    )


if __name__ == "__main__":
    main()
