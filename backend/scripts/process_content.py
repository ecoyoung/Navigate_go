import argparse

from sqlalchemy import select

from app.content_processing import PROCESSOR_VERSION, process_content_item
from app.database import SessionLocal
from app.models import ContentItem, Source


def main() -> None:
    parser = argparse.ArgumentParser(description="Run midstream content relevance processing")
    parser.add_argument("--version", default=PROCESSOR_VERSION)
    args = parser.parse_args()

    created = relevant = irrelevant = 0
    with SessionLocal() as session:
        rows = session.execute(
            select(ContentItem, Source).join(Source, Source.id == ContentItem.source_id)
        )
        for content, source in rows:
            result, was_processed = process_content_item(
                session, content, source, processor_version=args.version
            )
            if not was_processed:
                continue
            created += 1
            relevant += int(result.is_relevant)
            irrelevant += int(not result.is_relevant)
        session.commit()
    print(
        f"Processed {created} content items with {args.version}: "
        f"relevant={relevant}, irrelevant={irrelevant}"
    )


if __name__ == "__main__":
    main()
