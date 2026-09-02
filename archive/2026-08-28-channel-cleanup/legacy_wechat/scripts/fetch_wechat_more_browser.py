#!/usr/bin/env python3
"""Fetch a few WeChat articles in one Chrome session. No RedFox.

You may need to click 去验证 when captcha appears.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
COOKIE_FILE = ROOT / "data" / "wx_cookies.json"
ACCOUNT_FILE = ROOT / "config" / "wechat_mp_accounts.json"
OUT_FILE = ROOT.parent / "output" / "wechat_browser_try.md"
TARGETS = ("化妆品报", "青眼情报", "美妆产品观")
WAIT = 75


def load_accounts() -> dict[str, str]:
    payload = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    return {item["name"]: item["fakeid"] for item in payload["accounts"]}


def inject_cookies(driver, cookie_header: str) -> None:
    driver.get("https://mp.weixin.qq.com/")
    time.sleep(1)
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if not name or not value:
            continue
        for domain in (".mp.weixin.qq.com", "mp.weixin.qq.com"):
            try:
                driver.add_cookie({"name": name, "value": value, "domain": domain, "path": "/"})
            except Exception:
                continue
    driver.get("https://mp.weixin.qq.com/")


def article_links(driver) -> list[str]:
    hrefs: list[str] = []
    for node in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
        href = node.get_attribute("href") or ""
        if "mp.weixin.qq.com/s" in href and href not in hrefs:
            hrefs.append(href)
    html = driver.page_source
    for match in re.findall(r"https://mp\.weixin\.qq\.com/s[^\s\"'<>]+", html):
        if match not in hrefs:
            hrefs.append(match)
    return hrefs


def wait_body(driver, seconds: int) -> str:
    WebDriverWait(driver, seconds).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#js_content"))
    )
    return re.sub(r"\s+", " ", driver.find_element(By.CSS_SELECTOR, "#js_content").text).strip()


def main() -> None:
    session = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    token = session["token"]
    cookie = session["cookie"]
    fakeids = load_accounts()
    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=options)
    results: list[dict] = []
    try:
        for name in TARGETS:
            fakeid = fakeids[name]
            print(f"\n==== {name}")
            profile = (
                f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={fakeid}&scene=124"
            )
            driver.get(profile)
            print("waiting captcha/list", driver.title[:40])
            deadline = time.time() + WAIT
            links: list[str] = []
            while time.time() < deadline:
                links = article_links(driver)
                if len(links) >= 1 or driver.find_elements(By.CSS_SELECTOR, "#js_content"):
                    break
                time.sleep(2)
            print("profile_links", len(links), "url", driver.current_url[:140], "title", driver.title[:40])
            if driver.find_elements(By.CSS_SELECTOR, "#js_content") and len(links) < 2:
                url = driver.current_url
            elif links:
                url = links[1] if len(links) >= 2 else links[0]
            else:
                results.append({"name": name, "status": "no_list", "title": "", "body_len": 0})
                continue
            print("open", url[:120])
            if url.split("#")[0] not in driver.current_url:
                driver.get(url)
            try:
                body = wait_body(driver, WAIT)
            except TimeoutException:
                results.append({"name": name, "status": "captcha_timeout", "title": driver.title, "body_len": 0})
                print("timeout waiting #js_content")
                continue
            title = ""
            for selector in ("#activity-name", "h1"):
                nodes = driver.find_elements(By.CSS_SELECTOR, selector)
                if nodes and nodes[0].text.strip():
                    title = nodes[0].text.strip()
                    break
            results.append(
                {
                    "name": name,
                    "status": "ok",
                    "title": title,
                    "body_len": len(body),
                    "url": driver.current_url,
                }
            )
            print("ok", title[:40], "body_len", len(body))
    finally:
        driver.quit()

    lines = ["# 浏览器试爬（不走红狐）", ""]
    for row in results:
        lines.append(f"- {row['name']}: {row['status']} {row.get('title','')} body_len={row.get('body_len',0)}")
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("wrote", OUT_FILE)


if __name__ == "__main__":
    main()
