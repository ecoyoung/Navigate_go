import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.llm_editorial import DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekClient
from app.models import ContentItem, InterestTopic, Source, TopicMatch
from app.secrets import MissingSecretError, require_secret
from app.topic_intelligence import run_topic_intelligence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile one topic and batch-edit its current linked content."
    )
    parser.add_argument("--topic-id", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=12, choices=range(1, 13))
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--llm-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with SessionLocal() as session:
        topic = session.get(InterestTopic, args.topic_id)
        if topic is None:
            raise SystemExit(f"topic not found: {args.topic_id}")
        rows = list(
            session.execute(
                select(ContentItem, Source, TopicMatch)
                .join(TopicMatch, TopicMatch.content_item_id == ContentItem.id)
                .join(Source, Source.id == ContentItem.source_id)
                .where(
                    TopicMatch.topic_id == topic.id,
                    TopicMatch.input_content_hash == ContentItem.content_hash,
                )
                .order_by(ContentItem.id)
            )
        )
        selected = [
            (content, source)
            for content, source, match in rows
            if match.decision == "include"
            or "llm_topic" in (match.matched_signals or {})
        ]
        articles = list(
            {content.id: (content, source) for content, source in selected}.values()
        )[: args.limit]
        if not articles:
            raise SystemExit(f"topic has no current linked content: {args.topic_id}")
        if not args.apply:
            print(
                f"preview: topic_id={topic.id} items={len(articles)} "
                "max_initial_calls=2 max_repair_calls=2"
            )
            return
        try:
            api_key = require_secret(args.api_key_env)
        except MissingSecretError as exc:
            raise SystemExit(str(exc)) from exc
        result = run_topic_intelligence(
            session,
            topic,
            articles,
            DeepSeekClient(
                api_key=api_key,
                model=args.llm_model,
                base_url=args.llm_base_url,
            ),
        )
        print(
            f"applied: processed={result.processed} included={result.included} "
            f"excluded={result.excluded} "
            f"intent_cache_hit={result.intent_cache_hit} "
            f"content_cache_hit={result.content_cache_hit} "
            f"prompt_tokens={result.usage.prompt_tokens} "
            f"completion_tokens={result.usage.completion_tokens} "
            f"total_tokens={result.usage.total_tokens}"
        )


if __name__ == "__main__":
    main()
