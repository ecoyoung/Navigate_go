import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.entity_reviews import decide_entity_candidate, sync_entity_candidate_reviews
from app.models import EntityCandidateReview


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review unresolved entity candidates.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync")
    listing = subparsers.add_parser("list")
    listing.add_argument(
        "--status", default="pending", choices=["pending", "confirmed", "rejected"]
    )
    listing.add_argument("--limit", type=int, default=50)
    decision = subparsers.add_parser("decide")
    decision.add_argument("--review-id", type=int, required=True)
    decision.add_argument("--action", choices=["create", "link", "reject"], required=True)
    decision.add_argument("--entity-id", type=int)
    decision.add_argument("--canonical-name")
    decision.add_argument("--decided-by", required=True)
    decision.add_argument("--reason", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with SessionLocal() as session:
        if args.command == "sync":
            result = sync_entity_candidate_reviews(session)
            session.commit()
            print(
                f"synced: created={result.created} updated={result.updated} "
                f"applied_decisions={result.applied_decisions} skipped={result.skipped}"
            )
            return
        if args.command == "list":
            rows = session.scalars(
                select(EntityCandidateReview)
                .where(EntityCandidateReview.status == args.status)
                .order_by(EntityCandidateReview.id)
                .limit(max(1, min(args.limit, 100)))
            )
            for item in rows:
                print(
                    f"{item.id}\t{item.status}\t{item.entity_type}\t"
                    f"{item.proposed_name}\tmentions={item.mention_count}"
                )
            return
        result = decide_entity_candidate(
            session,
            args.review_id,
            action=args.action,
            entity_id=args.entity_id,
            canonical_name=args.canonical_name,
            decided_by=args.decided_by,
            reason=args.reason,
        )
        session.commit()
        print(
            f"decided: review_id={result.review_id} action={result.action} "
            f"entity_id={result.entity_id} affected_mentions={result.affected_mentions} "
            f"reused={result.reused}"
        )


if __name__ == "__main__":
    main()
