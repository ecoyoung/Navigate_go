"""Run a tightly bounded Firecrawl Search/Scrape connectivity check."""

from urllib.parse import urlparse

from app.database import SessionLocal
from app.firecrawl import FirecrawlClient, cached_search, search_results

QUERY = "site:docs.firecrawl.dev Firecrawl scrape documentation"
ALLOWED_HOSTS = {"docs.firecrawl.dev", "www.firecrawl.dev", "firecrawl.dev"}


def main() -> None:
    """Spend at most three credits and print only non-sensitive metadata."""
    client = FirecrawlClient.from_environment()
    with SessionLocal() as db:
        payload, cache_hit, search_credits = cached_search(
            db,
            client,
            query=QUERY,
            limit=3,
        )
        results = search_results(payload)
        db.commit()

    hosts = [urlparse(item["url"]).hostname or "" for item in results]
    official = next(
        (item for item in results if (urlparse(item["url"]).hostname or "") in ALLOWED_HOSTS),
        None,
    )
    scrape_ok = False
    scrape_host = "none"
    scrape_credits = 0
    if official is not None:
        scraped = client.scrape(official["url"])
        scrape_ok = bool(scraped.get("success", True) and scraped.get("data"))
        scrape_host = urlparse(official["url"]).hostname or "unknown"
        scrape_credits = 1

    print("provider=firecrawl")
    print(f"search_ok={bool(results)} cache_hit={cache_hit} result_count={len(results)}")
    print(f"result_hosts={','.join(hosts)}")
    print(
        f"scrape_attempted={official is not None} "
        f"scrape_ok={scrape_ok} scrape_host={scrape_host}"
    )
    print(f"credits_used_max={search_credits + scrape_credits}")


if __name__ == "__main__":
    main()
