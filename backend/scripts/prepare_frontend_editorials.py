import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.editorial_policy import load_editorial_policy
from app.llm_editorial import (
    CONTENT_SYSTEM_PROMPT,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DeepSeekClient,
    process_content_editorials,
)
from app.models import (
    ContentItem,
    ContentValueScore,
    ContentValueScoreRun,
    Domain,
    LLMProcessingResult,
    Source,
)
from app.secrets import MissingSecretError, require_secret


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare Chinese editorial artifacts for selected frontend stories."
    )
    parser.add_argument("--domain", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--content-id", action="append", type=int, default=[])
    parser.add_argument("--no-numbers", action="store_true")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--llm-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with SessionLocal() as session:
        domain = session.scalar(select(Domain).where(Domain.key == args.domain))
        if domain is None:
            raise SystemExit(f"unknown domain: {args.domain}")
        score_run = session.scalar(
            select(ContentValueScoreRun)
            .where(
                ContentValueScoreRun.domain_id == domain.id,
                ContentValueScoreRun.status == "succeeded",
            )
            .order_by(ContentValueScoreRun.as_of.desc(), ContentValueScoreRun.id.desc())
            .limit(1)
        )
        if score_run is None:
            raise SystemExit(f"no successful score run for domain: {args.domain}")
        rows = list(
            session.execute(
                select(ContentItem, Source)
                .join(ContentValueScore, ContentValueScore.content_item_id == ContentItem.id)
                .join(Source, Source.id == ContentItem.source_id)
                .where(
                    ContentValueScore.run_id == score_run.id,
                    ContentValueScore.decision == "selected",
                    ContentValueScore.input_content_hash == ContentItem.content_hash,
                    ~ContentItem.language.startswith("zh"),
                )
                .order_by(ContentItem.id)
            )
        )
        cached_hashes = {
            int(row.subject_key.removeprefix("content:")): str(
                (row.output or {}).get("input_content_hash") or ""
            )
            for row in session.scalars(
                select(LLMProcessingResult).where(
                    LLMProcessingResult.subject_type == "content_item",
                    LLMProcessingResult.task_name == "content_editorial_zh",
                    LLMProcessingResult.status == "succeeded",
                )
            )
            if row.subject_key.startswith("content:")
        }
        missing_rows = [
            (content, source)
            for content, source in rows
            if cached_hashes.get(content.id) != content.content_hash
            and (not args.content_id or content.id in args.content_id)
        ]
        missing_ids = [content.id for content, _ in missing_rows]
        if not args.apply:
            print(
                f"preview: selected_non_zh={len(rows)} cached={len(rows) - len(missing_ids)} "
                f"missing={len(missing_ids)} content_ids={','.join(map(str, missing_ids)) or '-'}"
            )
            return
        try:
            api_key = require_secret(args.api_key_env)
        except MissingSecretError as exc:
            raise SystemExit(str(exc)) from exc
        client = DeepSeekClient(
            api_key=api_key,
            model=args.llm_model,
            base_url=args.llm_base_url,
        )
        _, cache_hit, usage = process_content_editorials(
            session,
            missing_rows,
            client,
            policy=load_editorial_policy(args.domain),
            batch_size=args.batch_size,
            system_prompt=(
                CONTENT_SYSTEM_PROMPT
                + " 本批次中文标题和摘要绝对禁止出现任何阿拉伯数字或数字换算。"
                if args.no_numbers
                else CONTENT_SYSTEM_PROMPT
            ),
        )
        print(
            f"applied: selected_non_zh={len(rows)} cache_hit={cache_hit} "
            f"prompt_tokens={usage.prompt_tokens} completion_tokens={usage.completion_tokens} "
            f"total_tokens={usage.total_tokens}"
        )


if __name__ == "__main__":
    main()
