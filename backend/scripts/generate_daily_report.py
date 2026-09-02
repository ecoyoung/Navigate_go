import argparse
from datetime import date
from pathlib import Path

from app.daily_report import collect_daily_report, write_daily_report
from app.database import SessionLocal
from app.llm_editorial import DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekClient, enrich_daily_report
from app.secrets import MissingSecretError, require_secret


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a self-contained HTML daily report.")
    parser.add_argument("--domain", required=True)
    date_mode = parser.add_mutually_exclusive_group()
    date_mode.add_argument(
        "--date",
        type=date.fromisoformat,
        help="Coverage date in Asia/Shanghai (defaults to yesterday).",
    )
    date_mode.add_argument(
        "--latest",
        action="store_true",
        help="Preview the latest available coverage date instead of yesterday.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--llm-cache-only", action="store_true")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--llm-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--refresh-llm", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with SessionLocal() as session:
        data = collect_daily_report(
            session,
            domain_key=args.domain,
            coverage_date=args.date,
            latest=args.latest,
        )
        if args.llm or args.llm_cache_only:
            api_key = ""
            if not args.llm_cache_only:
                try:
                    api_key = require_secret(args.api_key_env)
                except MissingSecretError as exc:
                    raise SystemExit(str(exc)) from exc
            client = DeepSeekClient(
                api_key=api_key,
                model=args.llm_model,
                base_url=args.llm_base_url,
            )
            data, cache_hit, usage = enrich_daily_report(
                session,
                data,
                client,
                refresh=args.refresh_llm,
            )
            print(
                f"editorial: model={client.model} cache_hit={cache_hit} "
                f"prompt_tokens={usage.prompt_tokens} "
                f"completion_tokens={usage.completion_tokens} total_tokens={usage.total_tokens}"
            )
    output = args.output or Path("../output/daily") / (
        f"{data.report_date.isoformat()}-{data.domain_key}.html"
    )
    path = write_daily_report(data, output.resolve())
    print(
        f"generated: {path} stories={len(data.stories)} "
        f"issue_date={data.issue_date} coverage_date={data.report_date} "
        f"domain={data.domain_key}"
    )


if __name__ == "__main__":
    main()
