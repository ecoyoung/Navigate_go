import argparse
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.catalog import load_redfox_wechat_accounts
from app.secrets import MissingSecretError, require_secret

ACCOUNT_PATH = Path(__file__).parents[1] / "config" / "wechat_accounts.json"
CACHE_PATH = Path(__file__).parents[1] / "data" / "redfox_account_resolution_cache.json"
SEARCH_URL = "https://redfox.hk/story/api/gzh/data/searchUser"


def normalized_name(value: object) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value or "").lower()).removesuffix(
        "公众号"
    )


def choose_exact_candidate(name: str, candidates: list[dict]) -> dict | None:
    literal = [
        item
        for item in candidates
        if isinstance(item, dict)
        and str(item.get("accountName") or "").strip().casefold() == name.strip().casefold()
    ]
    literal_identities = {
        (item.get("wxId"), item.get("bizInfo"), item.get("account")) for item in literal
    }
    if len(literal_identities) == 1 and literal:
        return literal[0]
    target = normalized_name(name)
    exact = [
        item
        for item in candidates
        if isinstance(item, dict) and normalized_name(item.get("accountName")) == target
    ]
    identities = {
        (item.get("wxId"), item.get("bizInfo"), item.get("account")) for item in exact
    }
    return exact[0] if len(identities) == 1 and exact else None


def selector_from_candidate(candidate: dict) -> tuple[str, str, str] | None:
    biz = str(candidate.get("bizInfo") or "").strip()
    for key in ("wxId", "bizInfo", "account"):
        value = str(candidate.get(key) or "").strip()
        if value and biz:
            return key, value, biz
    return None


def search_candidates(client: httpx.Client, name: str) -> list[dict]:
    response = client.post(SEARCH_URL, json={"keyword": name, "offset": 0})
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 2000:
        message = payload.get("msg") or payload.get("message")
        raise RuntimeError(
            f"redfox_search_error:{payload.get('code')}:{message}"
        )
    data = payload.get("data") or {}
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        rows = data.get("list") or data.get("accounts") or data.get("records") or []
        return [item for item in rows if isinstance(item, dict)]
    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve pending RedFox WeChat accounts with one paid search per name."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--delay-seconds", type=float, default=0.5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    accounts = load_redfox_wechat_accounts(ACCOUNT_PATH)
    pending_names = [item.name for item in accounts if item.status == "pending"][: args.limit]
    cache = (
        json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.is_file() else {}
    )
    uncached = [name for name in pending_names if name not in cache]
    print(
        f"preview: pending={len(pending_names)} cached={len(pending_names) - len(uncached)} "
        f"max_paid_calls={len(uncached)}"
    )
    if not args.apply:
        return
    try:
        api_key = require_secret("REDFOX_API_KEY")
    except MissingSecretError as exc:
        raise SystemExit(str(exc)) from exc
    headers = {
        "Content-Type": "application/json",
        "REDFOX_API_KEY": api_key,
        "X-API-KEY": api_key,
    }
    with httpx.Client(timeout=30, headers=headers) as client:
        for index, name in enumerate(uncached, 1):
            try:
                rows = search_candidates(client, name)
                cache[name] = {
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "candidates": rows,
                }
                print(f"[{index}/{len(uncached)}] {name}: candidates={len(rows)}")
            except Exception as exc:
                cache[name] = {
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "error": f"{type(exc).__name__}:{exc}"[:300],
                    "candidates": [],
                }
                print(f"[{index}/{len(uncached)}] {name}: error={type(exc).__name__}")
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            time.sleep(max(args.delay_seconds, 0.2))

    raw = json.loads(ACCOUNT_PATH.read_text(encoding="utf-8"))
    resolved = 0
    for item in raw["accounts"]:
        if item["status"] != "pending" or item["name"] not in pending_names:
            continue
        entry = cache.get(item["name"]) or {}
        candidate = choose_exact_candidate(item["name"], entry.get("candidates") or [])
        selector = selector_from_candidate(candidate) if candidate else None
        if not selector:
            continue
        selector_kind, selector_value, biz = selector
        item.update(
            {
                "status": "ready",
                "selector_kind": selector_kind,
                "selector_value": selector_value,
                "source_external_id": biz,
                "is_enabled": False,
            }
        )
        resolved += 1
    ACCOUNT_PATH.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    remaining = sum(item["status"] == "pending" for item in raw["accounts"])
    print(
        f"applied: searched={len(uncached)} resolved={resolved} remaining_pending={remaining}"
    )


if __name__ == "__main__":
    main()
