#!/usr/bin/env python3
"""Fetch one RedFox article per WeChat account: 2nd non-pinned, non-ad item."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from app.redfox_wechat import detail_to_extracted, parse_list_payload, pick_articles

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
ACCOUNT_FILE = ROOT / "config" / "wechat_mp_accounts.json"
KEY_FILE = PROJECT / "公众号文章抓取_skill" / "config" / "redfox.json"
PROGRESS = ROOT / "data" / "wechat_redfox_second.jsonl"
SUMMARY = PROJECT / "output" / "wechat_redfox_second.md"
LIST_URL = "https://redfox.hk/story/api/gzh/data/queryWorkList"
DETAIL_URL = "https://redfox.hk/story/api/gzh/data/workDetail"
GAP = 0.4
PICK_CONFIG = {
    "skip_ad_titles": True,
    "skip_pinned": True,
    "skip_first_article": True,
    "max_articles": 1,
}


def load_done() -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not PROGRESS.exists():
        return done
    for line in PROGRESS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            done[row["name"]] = row
    return done


def append_row(row: dict) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_summary(rows: list[dict]) -> None:
    ok = [row for row in rows if row.get("status") in {"ok", "meta_only"}]
    lines = [
        "# 红狐公众号：非置顶非广告第 2 条",
        "",
        f"> 成功 {len(ok)} / {len(rows)}",
        "",
        "| 公众号 | 状态 | 发布时间 | 标题 | 字数 |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for row in rows:
        title = (row.get("title") or "").replace("|", "\\|")[:40]
        lines.append(
            f"| {row['name']} | {row.get('status')} | {row.get('published_at') or ''} "
            f"| {title} | {row.get('body_len') or 0} |"
        )
    SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")


def list_body(account: dict) -> dict:
    alias = str(account.get("alias") or "").strip()
    if alias:
        return {"account": alias, "offset": 0, "sortType": "2"}
    return {"bizInfo": account["fakeid"], "offset": 0, "sortType": "2"}


def main() -> None:
    key = json.loads(KEY_FILE.read_text(encoding="utf-8")).get("api_key") or ""
    if not key:
        raise SystemExit("missing RedFox api_key")
    accounts = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))["accounts"]
    done = load_done()
    pending = [item for item in accounts if item["name"] not in done]
    print(
        f"total={len(accounts)} have={len(done)} pending={len(pending)} "
        f"ok={sum(1 for r in done.values() if r.get('status')=='ok')}"
    )
    headers = {
        "Content-Type": "application/json",
        "REDFOX_API_KEY": key,
        "X-API-KEY": key,
    }
    with httpx.Client(timeout=30, headers=headers) as client:
        for index, account in enumerate(pending, 1):
            name = account["name"]
            print(f"[{index}/{len(pending)}] {name}")
            try:
                listed = client.post(LIST_URL, json=list_body(account))
                payload = listed.json()
                code = payload.get("code")
                if code not in (200, 2000):
                    row = {
                        "name": name,
                        "status": f"list_{code}",
                        "title": "",
                        "published_at": "",
                        "body_len": 0,
                        "message": str(payload.get("msg") or payload.get("message") or "")[:160],
                    }
                    if "余额不足" in row["message"] or code in (3106, 3107):
                        append_row(row)
                        print("  stop:", row["message"])
                        break
                    append_row(row)
                    done[name] = row
                    print(" ", row["status"], row["message"])
                    time.sleep(GAP)
                    continue
                items = parse_list_payload(listed.text)
                picked = pick_articles(items, PICK_CONFIG)
                if not picked:
                    row = {"name": name, "status": "no_article", "title": "", "body_len": 0}
                    append_row(row)
                    done[name] = row
                    print("  no_article")
                    time.sleep(GAP)
                    continue
                item = picked[0]
                time.sleep(GAP)
                detail = client.post(DETAIL_URL, json={"workUuid": item["workUuid"]})
                try:
                    extracted = detail_to_extracted(detail.text, item, min_content_chars=40)
                    published = extracted.get("published_at")
                    row = {
                        "name": name,
                        "status": "ok",
                        "title": extracted["title"],
                        "url": extracted["canonical_url"],
                        "published_at": (
                            published.isoformat(sep=" ", timespec="seconds") if published else ""
                        ),
                        "author": extracted.get("author") or "",
                        "body": extracted["body"],
                        "body_len": len(extracted["body"]),
                        "workUuid": item.get("workUuid"),
                    }
                except ValueError as exc:
                    if "content_too_short" not in str(exc):
                        raise
                    row = {
                        "name": name,
                        "status": "meta_only",
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("workUrl") or ""),
                        "published_at": str(item.get("publishTime") or ""),
                        "author": str(item.get("author") or ""),
                        "body": str(item.get("summary") or ""),
                        "body_len": len(str(item.get("summary") or "")),
                        "workUuid": item.get("workUuid"),
                        "message": str(exc),
                    }
            except Exception as exc:
                row = {
                    "name": name,
                    "status": "error",
                    "title": "",
                    "body_len": 0,
                    "message": f"{type(exc).__name__}: {exc}"[:200],
                }
            append_row(row)
            done[name] = row
            print(
                f"  {row['status']} date={row.get('published_at') or '-'} "
                f"len={row.get('body_len', 0)} {(row.get('title') or '')[:36]}"
            )
            time.sleep(GAP)
    write_summary(list(done.values()))
    ok = sum(1 for row in done.values() if row.get("status") == "ok")
    print(f"done ok={ok}/{len(done)} -> {SUMMARY}")


if __name__ == "__main__":
    main()
