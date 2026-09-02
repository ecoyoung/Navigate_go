import argparse
from pathlib import Path

from app.database import SessionLocal
from app.event_clustering import apply_cluster_plan, build_cluster_plan, load_clustering_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build domain-neutral local event clusters.")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_clustering_config(args.config)
    with SessionLocal() as session:
        plan = build_cluster_plan(session, config)
        event_count = len(plan.clusters) + plan.protected_event_count
        print(
            f"plan: inputs={plan.input_count} events={event_count} "
            f"multi_item={plan.multi_item_event_count} compared_pairs={plan.candidate_pair_count} "
            f"review_candidates={len(plan.review_candidates)} input_hash={plan.input_hash}"
        )
        if not args.apply:
            session.rollback()
            print("dry-run: database unchanged")
            return
        result = apply_cluster_plan(session, plan)
        session.commit()
        print(
            f"applied: run_id={result.run_id} created={result.created_event_count} "
            f"reused={result.reused_event_count} reused_run={result.reused_run}"
        )


if __name__ == "__main__":
    main()
