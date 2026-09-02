"""Fetch recent WeChat MP articles via the official-backend list API.

Uses a locally cached mp.weixin.qq.com session (token + cookies).
Does not crawl article HTML. Secrets stay in backend/data/wx_cookies.json.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
COOKIE_FILE = Path(__file__).resolve().parents[1] / "data" / "wx_cookies.json"
ACCOUNT_FILE = Path(__file__).resolve().parents[1] / "config" / "wechat_mp_accounts.json"
OUT_DIR = Path.home() / "Desktop" / "newsletter"
CN_TZ = timezone(timedelta(hours=8))
LIST_URL = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"
PAGE_SIZE = 5
MAX_PAGES = 3
REQUEST_GAP_SECONDS = 8


def _cn_now() -> datetime:
    return datetime.now(CN_TZ)


def keep_dates(now: datetime) -> set[str]:
    dates = {now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()}
    if now.weekday() == 0:
        dates.add((now.date() - timedelta(days=2)).isoformat())
    return dates


def load_session() -> tuple[str, str]:
    data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    token = str(data.get("token") or "").strip()
    cookie = str(data.get("cookie") or "").strip()
    if not token or not cookie:
        raise SystemExit(f"missing token/cookie in {COOKIE_FILE}")
    return token, cookie


def parse_publish_page(payload: dict) -> list[dict]:
    raw = payload.get("publish_page")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return []
    articles: list[dict] = []
    for item in raw.get("publish_list") or []:
        info = item.get("publish_info")
        if isinstance(info, str):
            info = json.loads(info)
        if not isinstance(info, dict):
            continue
        for art in info.get("appmsgex") or []:
            ts = int(art.get("update_time") or art.get("create_time") or 0)
            published = datetime.fromtimestamp(ts, CN_TZ) if ts else None
            articles.append(
                {
                    "aid": art.get("aid"),
                    "title": art.get("title") or "",
                    "link": art.get("link") or "",
                    "digest": art.get("digest") or "",
                    "published_at": published.isoformat(sep=" ", timespec="seconds") if published else "",
                    "published_date": published.date().isoformat() if published else "",
                }
            )
    return articles


def fetch_account(client: httpx.Client, token: str, name: str, fakeid: str) -> tuple[list[dict], str | None]:
    collected: list[dict] = []
    error = None
    for page in range(MAX_PAGES):
        response = client.get(
            LIST_URL,
            params={
                "sub": "list",
                "sub_action": "list_ex",
                "begin": page * PAGE_SIZE,
                "count": PAGE_SIZE,
                "fakeid": fakeid,
                "token": token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
            },
        )
        payload = response.json()
        status = (payload.get("base_resp") or {}).get("ret")
        message = (payload.get("base_resp") or {}).get("err_msg")
        if status == 200013:
            if page == 0:
                print(f"{name}: freq control, wait 90s and retry once")
                time.sleep(90)
                response = client.get(
                    LIST_URL,
                    params={
                        "sub": "list",
                        "sub_action": "list_ex",
                        "begin": 0,
                        "count": PAGE_SIZE,
                        "fakeid": fakeid,
                        "token": token,
                        "lang": "zh_CN",
                        "f": "json",
                        "ajax": "1",
                    },
                )
                payload = response.json()
                status = (payload.get("base_resp") or {}).get("ret")
                message = (payload.get("base_resp") or {}).get("err_msg")
                if status == 0:
                    batch = parse_publish_page(payload)
                    collected.extend(batch)
                    break
            error = f"freq control ({status} {message})"
            break
        if status == 200003:
            error = "invalid session"
            break
        if status != 0:
            error = f"{status} {message}"
            break
        batch = parse_publish_page(payload)
        collected.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(REQUEST_GAP_SECONDS)
    return collected, error


def write_outputs(rows: list[dict], dates: set[str]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _cn_now().strftime("%Y%m%d")
    csv_path = OUT_DIR / f"wechat_daily_{stamp}.csv"
    md_path = OUT_DIR / f"wechat_daily_{stamp}.md"
    fields = ["account", "title", "published_at", "link", "digest"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        f"# 公众号日报 {stamp}",
        "",
        f"> 保留日期：{', '.join(sorted(dates))}；共 {len(rows)} 条",
        "",
        "| 公众号 | 时间 | 标题 | 链接 |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        title = (row.get("title") or "").replace("|", "\\|")
        lines.append(
            f"| {row['account']} | {row.get('published_at','')} | {title} | {row.get('link','')} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path


def main() -> None:
    token, cookie = load_session()
    accounts = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))["accounts"]
    now = _cn_now()
    dates = keep_dates(now)
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    kept: list[dict] = []
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        for index, account in enumerate(accounts):
            name = account["name"]
            fakeid = account["fakeid"]
            articles, error = fetch_account(client, token, name, fakeid)
            matched = [item for item in articles if item.get("published_date") in dates]
            if not matched and articles:
                latest = max(item["published_date"] for item in articles if item.get("published_date"))
                matched = [item for item in articles if item.get("published_date") == latest]
                print(f"{name}: 今昨无新稿，改留最近一天 {latest}，{len(matched)} 条")
            elif error:
                print(f"{name}: {error}，已解析 {len(articles)} 条")
            else:
                print(f"{name}: 今昨 {len(matched)} 条 / 列表 {len(articles)} 条")
            for item in matched:
                kept.append({"account": name, **item})
            if index + 1 < len(accounts):
                time.sleep(REQUEST_GAP_SECONDS)
    csv_path, md_path = write_outputs(kept, dates)
    print(f"wrote {len(kept)} rows")
    print(csv_path)
    print(md_path)


if __name__ == "__main__":
    main()
