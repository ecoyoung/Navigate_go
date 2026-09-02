#!/usr/bin/env python3
"""Crawl the latest WeChat article for every catalog account in one Chrome session.

No RedFox. Resume from backend/data/wechat_206_latest.jsonl.
If captcha appears, click 去验证 in the opened window.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_FILE = ROOT / "config" / "wechat_mp_accounts.json"
PROGRESS = ROOT / "data" / "wechat_206_latest.jsonl"
SUMMARY = ROOT.parent / "output" / "wechat_206_latest.md"
WAIT = 35
GAP = 4


def load_done() -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not PROGRESS.exists():
        return done
    for line in PROGRESS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done[row["name"]] = row
    return done


def append_row(row: dict) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_summary(rows: list[dict]) -> None:
    ok = [row for row in rows if row.get("status") == "ok"]
    lines = [
        "# 公众号浏览器最新文章",
        "",
        f"> 成功 {len(ok)} / {len(rows)}；更新时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| 公众号 | 状态 | 发布日期 | 标题 | 字数 |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for row in rows:
        title = (row.get("title") or "").replace("|", "\\|")[:40]
        lines.append(
            f"| {row['name']} | {row.get('status')} | {row.get('published_at') or ''} "
            f"| {title} | {row.get('body_len') or 0} |"
        )
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_publish_time(driver) -> str:
    for selector in ("#publish_time", "#js_publish_time", "em#publish_time", "#meta_content em"):
        nodes = driver.find_elements(By.CSS_SELECTOR, selector)
        text = nodes[0].text.strip() if nodes else ""
        if text and any(ch.isdigit() for ch in text):
            return text
    html = driver.page_source
    for pattern in (
        r'id="publish_time"[^>]*>\s*([^<]+)',
        r'var\s+ct\s*=\s*"(\d+)"',
        r'"create_time"\s*:\s*"?(\d{10})"?',
        r'property="og:updated_time"\s+content="([^"]+)"',
        r'property="article:published_time"\s+content="([^"]+)"',
    ):
        match = re.search(pattern, html)
        if not match:
            continue
        value = match.group(1).strip()
        if value.isdigit() and len(value) >= 10:
            return datetime.fromtimestamp(int(value[:10])).strftime("%Y-%m-%d %H:%M:%S")
        return value
    return ""


def extract_current(driver, wait: int) -> dict:
    WebDriverWait(driver, wait).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#js_content"))
    )
    title = ""
    for selector in ("#activity-name", "h1.rich_media_title", "h1"):
        nodes = driver.find_elements(By.CSS_SELECTOR, selector)
        if nodes and nodes[0].text.strip():
            title = nodes[0].text.strip()
            break
    body = re.sub(r"\s+", " ", driver.find_element(By.CSS_SELECTOR, "#js_content").text).strip()
    return {
        "title": title,
        "url": driver.current_url,
        "published_at": parse_publish_time(driver),
        "body": body,
        "body_len": len(body),
    }


def main() -> None:
    accounts = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))["accounts"]
    done = load_done()
    pending = [item for item in accounts if done.get(item["name"], {}).get("status") != "ok"]
    print(f"total={len(accounts)} done={len(done)} pending={len(pending)}")
    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=options)
    try:
        for index, item in enumerate(pending, 1):
            name = item["name"]
            fakeid = item["fakeid"]
            url = f"https://mp.weixin.qq.com/s?__biz={fakeid}"
            print(f"[{index}/{len(pending)}] {name}")
            try:
                driver.get(url)
                article = extract_current(driver, WAIT)
                row = {"name": name, "fakeid": fakeid, "status": "ok", **article}
            except TimeoutException:
                row = {
                    "name": name,
                    "fakeid": fakeid,
                    "status": "captcha_or_empty",
                    "title": driver.title,
                    "url": driver.current_url,
                    "published_at": "",
                    "body": "",
                    "body_len": 0,
                }
            except (NoSuchWindowException, WebDriverException) as exc:
                print("browser closed:", type(exc).__name__)
                break
            append_row(row)
            done[name] = row
            print(
                f"  {row['status']} date={row.get('published_at') or '-'} "
                f"len={row.get('body_len', 0)} {row.get('title','')[:40]}"
            )
            time.sleep(GAP)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    write_summary(list(done.values()))
    ok = sum(1 for row in done.values() if row.get("status") == "ok")
    print(f"summary ok={ok}/{len(done)} -> {SUMMARY}")


if __name__ == "__main__":
    main()
