import argparse
import asyncio
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from app.catalog import load_catalog
from app.channel_adapters import crawl_source
from app.database import SessionLocal
from app.models import CrawlRun, Source
from app.normalization import normalize_url
from app.web_ingestion import create_crawl_run

CATALOG_PATH = Path(__file__).parents[1] / "config" / "sites.json"
OUTPUT_DIR = Path(__file__).parents[2] / "output"


async def run(
    source_ids: set[str] | None = None,
    *,
    provider: str | None = None,
    exclude_providers: set[str] | None = None,
    coverage_date: date | None = None,
    include_disabled: bool = False,
    skip_succeeded_coverage: bool = False,
) -> list[dict]:
    results: list[dict] = []
    catalog = load_catalog(CATALOG_PATH)
    if source_ids:
        known_ids = {item.id for item in catalog}
        unknown_ids = source_ids - known_ids
        if unknown_ids:
            raise ValueError(f"unknown source ids: {', '.join(sorted(unknown_ids))}")
        catalog = [item for item in catalog if item.id in source_ids]
    if provider:
        catalog = [
            item for item in catalog if str(item.parser_config.get("provider") or "") == provider
        ]
    if exclude_providers:
        catalog = [
            item
            for item in catalog
            if str(item.parser_config.get("provider") or "") not in exclude_providers
        ]
    for item in catalog:
        if not item.is_enabled and not include_disabled:
            results.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "region": item.source_region,
                    "language": item.default_language,
                    "status": "disabled",
                    "error_summary": "来源已停用，不发起采集请求。",
                }
            )
            print(f"[{len(results):02d}] {item.name}: disabled")
            continue
        if item.crawl_strategy in {"blocked", "unavailable"}:
            results.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "region": item.source_region,
                    "language": item.default_language,
                    "status": item.crawl_strategy,
                    "error_summary": item.skip_reason,
                }
            )
            print(f"[{len(results):02d}] {item.name}: {item.crawl_strategy}")
            continue
        with SessionLocal() as session:
            source = session.scalar(
                select(Source).where(Source.catalog_id == item.id)
            )
            if source is None:
                source = session.scalar(
                    select(Source)
                    .where(Source.normalized_start_url == normalize_url(str(item.start_url)))
                    .limit(1)
                )
            if not source:
                results.append({"id": item.id, "name": item.name, "status": "not_registered"})
                continue
            if skip_succeeded_coverage and coverage_date:
                succeeded = session.scalar(
                    select(CrawlRun.id)
                    .where(
                        CrawlRun.source_id == source.id,
                        CrawlRun.coverage_date == coverage_date,
                        CrawlRun.status.in_(("succeeded", "unchanged")),
                    )
                    .limit(1)
                )
                if succeeded:
                    results.append(
                        {
                            "id": item.id,
                            "name": item.name,
                            "status": "skipped_existing_coverage",
                            "coverage_date": coverage_date.isoformat(),
                        }
                    )
                    print(f"[{len(results):02d}] {item.name}: skipped_existing_coverage")
                    continue
            run_record, created = create_crawl_run(
                session, source, coverage_date=coverage_date
            )
            run_id, source_id = run_record.id, source.id
        if created:
            await crawl_source(SessionLocal, source_id, run_id)
        with SessionLocal() as session:
            completed = session.get(CrawlRun, run_id)
            result = {
                "id": item.id,
                "name": item.name,
                "region": item.source_region,
                "language": item.default_language,
                "status": completed.status,
                "coverage_date": (
                    completed.coverage_date.isoformat() if completed.coverage_date else None
                ),
                "fetched": completed.fetched_count,
                "new": completed.new_count,
                "updated": completed.updated_count,
                "skipped": completed.skipped_count,
                "rejected": completed.rejected_count,
                "errors": completed.error_count,
                "error_code": completed.error_code,
                "error_summary": completed.error_summary,
            }
            results.append(result)
            print(
                f"[{len(results):02d}] {item.name}: {completed.status} "
                f"new={completed.new_count} skipped={completed.skipped_count} "
                f"rejected={completed.rejected_count} errors={completed.error_count}"
            )
    return results


def latest_results() -> list[dict]:
    results: list[dict] = []
    with SessionLocal() as session:
        for item in load_catalog(CATALOG_PATH):
            if not item.is_enabled:
                results.append(
                    {
                        "id": item.id,
                        "name": item.name,
                        "region": item.source_region,
                        "language": item.default_language,
                        "status": "disabled",
                        "error_summary": "来源已停用，不发起采集请求。",
                    }
                )
                continue
            if item.crawl_strategy in {"blocked", "unavailable"}:
                results.append(
                    {
                        "id": item.id,
                        "name": item.name,
                        "region": item.source_region,
                        "language": item.default_language,
                        "status": item.crawl_strategy,
                        "error_summary": item.skip_reason,
                    }
                )
                continue
            source = session.scalar(
                select(Source).where(
                    Source.normalized_start_url == normalize_url(str(item.start_url))
                )
            )
            completed = (
                session.scalar(
                    select(CrawlRun)
                    .where(CrawlRun.source_id == source.id)
                    .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
                    .limit(1)
                )
                if source
                else None
            )
            if not source or not completed:
                results.append(
                    {
                        "id": item.id,
                        "name": item.name,
                        "region": item.source_region,
                        "language": item.default_language,
                        "status": "not_run",
                    }
                )
                continue
            results.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "region": item.source_region,
                    "language": item.default_language,
                    "status": completed.status,
                    "fetched": completed.fetched_count,
                    "new": completed.new_count,
                    "updated": completed.updated_count,
                    "skipped": completed.skipped_count,
                    "rejected": completed.rejected_count,
                    "errors": completed.error_count,
                    "error_code": completed.error_code,
                    "error_summary": completed.error_summary,
                }
            )
    return results


def write_report(results: list[dict]) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"site_crawl_report_{stamp}.json"
    md_path = OUTPUT_DIR / f"site_crawl_report_{stamp}.md"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 网站采集运行报告",
        "",
        (
            "| 来源 | 区域 | 语言 | 状态 | 抓取 | 新增 | 更新 | 重复跳过 | "
            "形式拒绝 | 错误 | 错误摘要 |"
        ),
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in results:
        error = (item.get("error_summary") or "-").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {item['name']} | {item.get('region', '-')} | {item.get('language', '-')} | "
            f"{item['status']} | {item.get('fetched', 0)} | {item.get('new', 0)} | "
            f"{item.get('updated', 0)} | {item.get('skipped', 0)} | "
            f"{item.get('rejected', 0)} | "
            f"{item.get('errors', 0)} | {error[:160]} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the configured website crawl catalog")
    parser.add_argument(
        "--source",
        action="append",
        dest="source_ids",
        help="Only crawl this catalog source id; repeat the flag for multiple sources.",
    )
    parser.add_argument("--provider", help="Only crawl sources for this configured provider.")
    parser.add_argument(
        "--exclude-provider",
        action="append",
        dest="exclude_providers",
        help="Exclude this provider before scheduling; repeat for multiple providers.",
    )
    parser.add_argument(
        "--coverage-date",
        type=date.fromisoformat,
        help="Explicit source-local coverage date for date-based providers.",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="One-shot crawl of disabled sources; requires --provider and --coverage-date.",
    )
    parser.add_argument(
        "--skip-succeeded-coverage",
        action="store_true",
        help="Do not repeat a successful source run for the same explicit coverage date.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Do not crawl; report the latest run for every catalog source.",
    )
    args = parser.parse_args()
    if args.report_only and args.source_ids:
        parser.error("--report-only cannot be combined with --source")
    if args.include_disabled and (not args.provider or not args.coverage_date):
        parser.error("--include-disabled requires --provider and --coverage-date")
    if args.provider and args.exclude_providers and args.provider in args.exclude_providers:
        parser.error("--provider cannot also be listed in --exclude-provider")
    results = (
        latest_results()
        if args.report_only
        else asyncio.run(
            run(
                set(args.source_ids) if args.source_ids else None,
                provider=args.provider,
                exclude_providers=set(args.exclude_providers or []),
                coverage_date=args.coverage_date,
                include_disabled=args.include_disabled,
                skip_succeeded_coverage=args.skip_succeeded_coverage,
            )
        )
    )
    paths = write_report(results)
    print(f"Reports written: {paths[0]} and {paths[1]}")


if __name__ == "__main__":
    main()
