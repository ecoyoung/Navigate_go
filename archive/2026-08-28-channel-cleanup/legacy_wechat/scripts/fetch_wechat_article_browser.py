#!/usr/bin/env python3
"""Open a WeChat article in Chrome and extract #js_content after you pass captcha.

This is not a captcha solver. If the page shows 环境异常, click 去验证 in the
opened window. The script waits, then reads the article body.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

DEFAULT_URL = (
    "https://mp.weixin.qq.com/s?__biz=MzU0Mzg0MjA3MA==&mid=2247718359"
    "&idx=1&sn=8437b6468c7c9960b1cee26293bc926b"
)


def extract(url: str, wait_seconds: int) -> dict:
    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, wait_seconds).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#js_content"))
            )
        except TimeoutException as exc:
            raise SystemExit(
                "等待超时：页面里没有 #js_content。"
                "请在弹出的 Chrome 里完成验证后再跑，或把 --wait 加大。"
            ) from exc
        title = ""
        for selector in ("#activity-name", "h1", "title"):
            nodes = driver.find_elements(By.CSS_SELECTOR, selector)
            if nodes and nodes[0].text.strip():
                title = nodes[0].text.strip()
                break
        body = driver.find_element(By.CSS_SELECTOR, "#js_content").text
        body = re.sub(r"\s+", " ", body).strip()
        return {"title": title, "url": driver.current_url, "body": body}
    finally:
        driver.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract WeChat article after manual captcha")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--wait", type=int, default=90, help="seconds to wait for #js_content")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    article = extract(args.url, args.wait)
    print(f"title: {article['title']}")
    print(f"url: {article['url']}")
    print(f"body_len: {len(article['body'])}")
    print(article["body"][:300])
    if args.out:
        args.out.write_text(
            f"# {article['title']}\n\n{article['url']}\n\n{article['body']}\n",
            encoding="utf-8",
        )
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
