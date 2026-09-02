import argparse
from datetime import datetime
from pathlib import Path

from app.database import SessionLocal
from app.value_scoring import (
    apply_value_score_plan,
    build_value_score_plan,
    load_value_scoring_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an explainable content value score run.")
    parser.add_argument("--domain", required=True)
    parser.add_argument(
        "--as-of",
        required=True,
        help="Frozen ISO-8601 scoring time with timezone, for example 2026-08-30T00:00:00+08:00",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--show", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        raise SystemExit("--as-of must include a UTC offset")
    config = load_value_scoring_config(args.config)
    with SessionLocal() as session:
        plan = build_value_score_plan(
            session,
            domain_key=args.domain,
            as_of=as_of,
            config=config,
        )
        selected = [score for score in plan.scores if score.decision == "selected"]
        print(
            f"preview: domain={plan.domain_key} as_of={plan.as_of.isoformat()} "
            f"input={len(plan.scores)} selected={len(selected)} "
            f"input_hash={plan.input_hash}"
        )
        for score in plan.scores[: max(0, args.show)]:
            print(
                f"content_id={score.content_item_id} score={score.total_score:.2f} "
                f"decision={score.decision} gates={','.join(score.gates) or '-'}"
            )
        if args.apply:
            result = apply_value_score_plan(session, plan)
            session.commit()
            print(
                f"applied: run_id={result.run_id} input={result.input_count} "
                f"selected={result.selected_count} reused={str(result.reused_run).lower()}"
            )
        else:
            session.rollback()


if __name__ == "__main__":
    main()
