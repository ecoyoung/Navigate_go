from sqlalchemy import select

from app.database import SessionLocal
from app.models import Source
from app.normalization import normalize_url

START_URL = "https://beautymatter.com/articles"
CONFIG = {
    "link_selector": ".articles article a[href]",
    "card_selector": "article",
    "exclude_card_selector": ".premium-bar",
    "article_url_pattern": r"^https://beautymatter\.com/articles/(?!tag:)[^?]+$",
    "title_selector": "h1",
    "body_selector": ".paywall .module.rich-text .container.boxed.text",
    "author_selector": ".author-list .author",
    "date_selector": ".date.desktop-only",
    "max_articles": 10,
    "request_delay_seconds": 2,
    "min_content_chars": 200,
}


def main() -> None:
    with SessionLocal() as session:
        normalized = normalize_url(START_URL)
        source = session.scalar(select(Source).where(Source.normalized_start_url == normalized))
        if source:
            print(f"BeautyMatter already registered as source {source.id}")
            return
        source = Source(
            name="BeautyMatter",
            channel_type="web",
            start_url=START_URL,
            normalized_start_url=normalized,
            fetch_interval_seconds=21600,
            parser_config=CONFIG,
            source_region="GLOBAL",
            source_type="trade_media",
            default_language="en",
            source_tags=["beauty", "brands", "retail", "industry"],
        )
        session.add(source)
        session.commit()
        print(f"BeautyMatter registered as source {source.id}")


if __name__ == "__main__":
    main()
