"""Resolve WeChat MP fakeid via searchbiz. Session stays in backend/data/."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

COOKIE_FILE = Path(__file__).resolve().parents[1] / "data" / "wx_cookies.json"
NAMES_FILE = Path(__file__).resolve().parents[1] / "config" / "wechat_mp_names.txt"
OUT_FILE = Path(__file__).resolve().parents[1] / "config" / "wechat_mp_accounts.json"
FAIL_FILE = Path(__file__).resolve().parents[1] / "data" / "wechat_fakeid_failures.json"
SEARCH_URL = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
GAP_SECONDS = 10
FREQ_WAIT_SECONDS = 120


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    session = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    token = str(session.get("token") or "").strip()
    cookie = str(session.get("cookie") or "").strip()
    names = [line.strip() for line in NAMES_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    existing = {item["name"]: item for item in load_json(OUT_FILE, {"accounts": []}).get("accounts", [])}
    failures = load_json(FAIL_FILE, [])
    failed_names = {item["name"] for item in failures}
    pending = [name for name in names if name not in existing and name not in failed_names]
    print(f"total={len(names)} have={len(existing)} fail={len(failed_names)} pending={len(pending)}")
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        for index, name in enumerate(pending):
            payload = None
            for attempt in range(2):
                response = client.get(
                    SEARCH_URL,
                    params={
                        "action": "search_biz",
                        "begin": 0,
                        "count": 5,
                        "query": name,
                        "token": token,
                        "lang": "zh_CN",
                        "f": "json",
                        "ajax": "1",
                    },
                )
                payload = response.json()
                status = (payload.get("base_resp") or {}).get("ret")
                message = (payload.get("base_resp") or {}).get("err_msg")
                if status == 200013 and attempt == 0:
                    print(f"{name}: freq control, wait {FREQ_WAIT_SECONDS}s")
                    time.sleep(FREQ_WAIT_SECONDS)
                    continue
                if status == 200003:
                    print("invalid session, stop")
                    save_json(OUT_FILE, {"accounts": list(existing.values())})
                    save_json(FAIL_FILE, failures)
                    return
                if status != 0:
                    failures.append({"name": name, "reason": f"{status} {message}"})
                    print(f"{name}: {status} {message}")
                    payload = None
                break
            if payload and (payload.get("base_resp") or {}).get("ret") == 0:
                hits = payload.get("list") or []
                exact = next((item for item in hits if (item.get("nickname") or "").strip() == name), None)
                chosen = exact or (hits[0] if hits else None)
                if not chosen:
                    failures.append({"name": name, "reason": "not_found"})
                    print(f"{name}: not found")
                else:
                    record = {
                        "name": name,
                        "fakeid": chosen.get("fakeid"),
                        "alias": chosen.get("alias") or "",
                        "nickname": chosen.get("nickname") or "",
                        "exact": bool(exact),
                    }
                    existing[name] = record
                    mark = "" if exact else f" ~{chosen.get('nickname')}"
                    print(f"{name}: {record['fakeid']}{mark}")
            save_json(OUT_FILE, {"accounts": list(existing.values())})
            save_json(FAIL_FILE, failures)
            if index + 1 < len(pending):
                time.sleep(GAP_SECONDS)
    print(f"done have={len(existing)} fail={len(failures)}")


if __name__ == "__main__":
    main()
