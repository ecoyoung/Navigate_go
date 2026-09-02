import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.domain_relevance import load_domain_relevance_policy
from app.llm_domain_relevance import process_domain_candidates, project_deterministic_baseline
from app.llm_editorial import DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekClient
from app.models import ContentItem, ContentProcessingResult, Source
from app.secrets import MissingSecretError, require_secret


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use a cached LLM review for ambiguous domain-rule candidates."
    )
    parser.add_argument("--domain", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument(
        "--candidate-reason",
        choices=["needs_llm_domain_review", "dedicated_domain_source"],
        default="needs_llm_domain_review",
    )
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--llm-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    policy = load_domain_relevance_policy(args.domain)
    with SessionLocal() as session:
        rows = list(
            session.execute(
                select(ContentItem, Source, ContentProcessingResult)
                .join(Source, Source.id == ContentItem.source_id)
                .join(
                    ContentProcessingResult,
                    ContentProcessingResult.content_item_id == ContentItem.id,
                )
                .where(
                    ContentProcessingResult.processor_name == policy["classifier_name"],
                    ContentProcessingResult.processor_version
                    == policy["classifier_version"],
                    ContentProcessingResult.input_content_hash == ContentItem.content_hash,
                )
                .order_by(ContentItem.id)
            )
        )
        candidates = [
            (content, source)
            for content, source, result in rows
            if result.reason == args.candidate_reason
        ]
        current_hash_by_id = {content.id: content.content_hash for content, _, _ in rows}
        current_llm_ids = {
            result.content_item_id
            for result in session.scalars(
                select(ContentProcessingResult).where(
                    ContentProcessingResult.processor_name == policy["llm_classifier_name"],
                    ContentProcessingResult.processor_version
                    == policy["llm_classifier_version"],
                    ContentProcessingResult.reason.like("llm_domain_%"),
                )
            )
            if result.input_content_hash == current_hash_by_id.get(result.content_item_id)
        }
        candidates = [
            (content, source)
            for content, source in candidates
            if content.id not in current_llm_ids
        ]
        estimated_calls = (len(candidates) + args.batch_size - 1) // args.batch_size
        if not args.apply:
            print(
                f"preview: domain={args.domain} candidates={len(candidates)} "
                f"max_calls={estimated_calls} batch_size={args.batch_size}"
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
        result = process_domain_candidates(
            session,
            candidates,
            client,
            policy=policy,
            batch_size=args.batch_size,
        )
        deterministic = (
            project_deterministic_baseline(
                session,
                [(content, result) for content, _, result in rows],
                policy=policy,
            )
            if args.candidate_reason == "needs_llm_domain_review"
            else 0
        )
        print(
            f"applied: processed={result.processed} included={result.included} "
            f"excluded={result.excluded} cache_hits={result.cache_hits} "
            f"deterministic={deterministic} "
            f"prompt_tokens={result.usage.prompt_tokens} "
            f"completion_tokens={result.usage.completion_tokens} "
            f"total_tokens={result.usage.total_tokens}"
        )


if __name__ == "__main__":
    main()
