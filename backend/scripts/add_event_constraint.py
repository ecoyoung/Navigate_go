import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ContentItem, EventClusterConstraint


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add an auditable event pair constraint.")
    parser.add_argument("--left", type=int, required=True)
    parser.add_argument("--right", type=int, required=True)
    parser.add_argument("--relation", choices=("must_link", "cannot_link"), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--created-by", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.left == args.right:
        raise SystemExit("event constraint requires two different content IDs")
    left_id, right_id = sorted((args.left, args.right))
    with SessionLocal() as session:
        found_ids = set(
            session.scalars(
                select(ContentItem.id).where(ContentItem.id.in_((left_id, right_id)))
            )
        )
        if found_ids != {left_id, right_id}:
            raise SystemExit("one or both content IDs do not exist")
        existing = session.scalar(
            select(EventClusterConstraint).where(
                EventClusterConstraint.left_content_id == left_id,
                EventClusterConstraint.right_content_id == right_id,
            )
        )
        reused = existing is not None
        if existing is not None and existing.relation != args.relation:
            raise SystemExit(
                f"pair already has conflicting relation {existing.relation}"
            )
        if existing is None:
            existing = EventClusterConstraint(
                left_content_id=left_id,
                right_content_id=right_id,
                relation=args.relation,
                reason=args.reason.strip(),
                created_by=args.created_by.strip(),
            )
            session.add(existing)
            session.flush()
        if args.apply:
            session.commit()
        else:
            session.rollback()
        print(
            f"{'applied' if args.apply else 'dry-run'}: constraint_id={existing.id} "
            f"pair={left_id},{right_id} relation={args.relation} reused={str(reused).lower()}"
        )


if __name__ == "__main__":
    main()
