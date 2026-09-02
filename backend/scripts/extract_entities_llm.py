import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.llm_editorial import DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekClient
from app.llm_entity_extraction import MAX_BATCH_SIZE, build_entity_input, process_llm_entity_batch
from app.models import ContentItem
from app.secrets import MissingSecretError, require_secret


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded LLM entity extraction canary.")
    parser.add_argument("--content-id", type=int, action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--llm-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    content_ids = list(dict.fromkeys(args.content_id))
    if not 1 <= len(content_ids) <= MAX_BATCH_SIZE:
        raise SystemExit(f"provide 1-{MAX_BATCH_SIZE} unique --content-id values")
    with SessionLocal() as session:
        contents = list(
            session.scalars(
                select(ContentItem)
                .where(ContentItem.id.in_(content_ids))
                .order_by(ContentItem.id)
            )
        )
        if {item.id for item in contents} != set(content_ids):
            missing = sorted(set(content_ids) - {item.id for item in contents})
            raise SystemExit(f"unknown content ids: {missing}")
        inputs = [build_entity_input(content) for content in contents]
        evidence_chars = sum(
            len(span["text"]) for item in inputs for span in item["evidence"]
        )
        print(
            f"preview: contents={content_ids} evidence_chars={evidence_chars} "
            f"max_batch={MAX_BATCH_SIZE}"
        )
        if not args.apply:
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
        result = process_llm_entity_batch(session, contents, client)
        print(
            f"applied: processed={result.processed} skipped={result.skipped} "
            f"mentions={result.mentions} resolved={result.resolved} "
            f"unresolved={result.unresolved} cache_hit={result.cache_hit} "
            f"prompt_tokens={result.usage.prompt_tokens} "
            f"completion_tokens={result.usage.completion_tokens} "
            f"total_tokens={result.usage.total_tokens}"
        )


if __name__ == "__main__":
    main()
