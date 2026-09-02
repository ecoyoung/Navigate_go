import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from app.source_probe import (
    ProbeDocument,
    analyze_probe_document,
)
from app.source_probe_fetch import ProbeFetchError, probe_public_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only source structure probe. It never registers a source or writes the database."
        )
    )
    parser.add_argument("url", help="Public HTTP(S) source entry URL")
    parser.add_argument(
        "--input-file",
        type=Path,
        help="Analyze a local response fixture instead of accessing the network",
    )
    parser.add_argument("--content-type", help="Fixture Content-Type")
    parser.add_argument("--status-code", type=int, default=200)
    parser.add_argument(
        "--robots-status",
        choices=("allowed", "disallowed", "unavailable", "not_checked"),
        default="not_checked",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    observed_at = datetime.now(UTC)
    if args.input_file:
        result = analyze_probe_document(
            ProbeDocument(
                requested_url=args.url,
                final_url=args.url,
                observed_at=observed_at,
                status_code=args.status_code,
                content_type=args.content_type,
                body=args.input_file.read_text(encoding="utf-8"),
                robots_status=args.robots_status,
            )
        )
    else:
        try:
            result = await probe_public_url(args.url, observed_at=observed_at)
        except (ProbeFetchError, ValueError) as exc:
            print(json.dumps({"outcome": "unreachable", "error_code": str(exc)}))
            return 2
    print(result.model_dump_json(indent=2))
    return 0 if result.outcome in {"success", "partial"} else 2


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
