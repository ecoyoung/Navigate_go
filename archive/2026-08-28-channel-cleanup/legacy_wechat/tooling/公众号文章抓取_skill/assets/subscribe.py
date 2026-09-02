#!/usr/bin/env python3
"""
公众号文章抓取 — 关键词 → 找出公众号 → 抓取文章链接
================================================
基于红狐广域库：按名称搜出公众号账号，抓取近 30 天文章并按规则取一篇。

Usage:
    python3 subscribe.py search "且初"              # 按名称搜公众号，拿 account/wxId/bizInfo
    python3 subscribe.py latest "KIMTRUE且初"       # 一键取该号最新一篇（名称或ID均可）
    python3 subscribe.py latest "kimtrue66" --json  # JSON 行输出，方便批量回填
    python3 subscribe.py batch "且初" "KIMTRUE且初"  # 批量取最新一篇，输出 Markdown 表格
    python3 subscribe.py batch --file 名单.txt --out 表.md --group 20
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ─── 配置 ─────────────────────────────────────────────────────────────────────────
API_URL = "https://redfox.hk/story/api/gzh/data/queryWorkList"
SEARCH_URL = "https://redfox.hk/story/api/gzh/data/searchUser"
CONFIG_DIR = Path.home() / ".qoder" / "apis"
CONFIG_FILE = CONFIG_DIR / "redfox.json"
SKILL_CONFIG_FILE = Path(__file__).resolve().parents[1] / "config" / "redfox.json"
ENV_KEY = "REDFOX_API_KEY"
SOURCE = "公众号账号订阅-GitHub"




# ─── 终端颜色 ──────────────────────────────────────────────────────────────────────
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def info(msg):
    print(f"{GREEN}[✓]{RESET} {msg}")


def warn(msg):
    print(f"{YELLOW}[!]{RESET} {msg}")


def error(msg):
    print(f"{RED}[✗]{RESET} {msg}")


def step(msg):
    print(f"{CYAN}[→]{RESET} {msg}")


# ─── API Key 管理 ──────────────────────────────────────────────────────────────────
def get_api_key(cli_key=None):
    """Get API key: CLI arg > env var > config file."""
    if cli_key:
        return cli_key
    env_key = os.environ.get(ENV_KEY)
    if env_key:
        return env_key
    for path in (SKILL_CONFIG_FILE, CONFIG_FILE):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            key = data.get("api_key")
            if key:
                return key
        except (json.JSONDecodeError, OSError):
            continue
    return None


# ─── 数据获取 ──────────────────────────────────────────────────────────────────────
def fetch_account_articles(session, account_id, account_name, date_str):
    """获取单个公众号文章列表（广域库 T+1，查昨天及近 7 天），需提供账号 ID，最多 5 次请求（每次 20 条，共 100 条）"""
    if not account_id:
        warn(f"「{account_name}」缺少账号 ID，无法查询。请使用 add 命令重新订阅并提供账号 ID")
        return []

    id_label = f" (ID: {account_id})"
    all_articles = []

    # T+1: 广域库查不到当天数据，取昨天往前 7 天
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    end_date = target_date - timedelta(days=1)
    start_date = end_date - timedelta(days=7)
    end_str = end_date.strftime("%Y-%m-%d")
    start_str = start_date.strftime("%Y-%m-%d")

    for page in range(5):  # 5 页，每页 20 条
        offset = page * 20
        payload = {
            "account": account_id,
            "offset": offset,
            "sortType": "0",
            "publishTimeStart": f"{start_str} 00:00:00",
            "publishTimeEnd": f"{end_str} 23:59:59",
            "source": SOURCE,
        }
        try:
            resp = session.post(API_URL, json=payload, timeout=20)
            result = resp.json()
        except requests.exceptions.Timeout:
            warn(f"请求超时: {account_name}{id_label}")
            return all_articles if all_articles else []
        except Exception as e:
            warn(f"请求失败: {account_name}{id_label}: {e}")
            return all_articles if all_articles else []

        code = result.get("code")
        if code == 3108:
            warn("触发频率限制，等待 5s...")
            time.sleep(5)
            try:
                resp = session.post(API_URL, json=payload, timeout=20)
                result = resp.json()
                code = result.get("code")
            except Exception:
                return all_articles if all_articles else []

        if code not in (200, 2000):
            if code in (3106, 3107):
                error(f"API Key 错误 (code {code}): {result.get('msg', '')}")
            elif code == 3203:
                warn(f"「{account_name}」不在广域库中，暂未收录")
                print(f"    💡 如需查询此账号，请联系红狐数据获取定制支持：redfoxdata@proton.me")
            elif code:
                warn(f"API 返回错误 (code {code}): {result.get('msg', '')} — {account_name}")
            return all_articles if all_articles else []

        data_raw = result.get("data", {})
        if not data_raw:
            break  # 无更多数据

        # 兼容多种响应结构
        if isinstance(data_raw, list):
            articles = data_raw
        elif isinstance(data_raw, dict):
            articles = data_raw.get("list") or data_raw.get("articles") or data_raw.get("records") or []
        else:
            articles = []

        if not articles:
            break  # 空页，停止翻页

        # 为每篇文章附加公众号信息
        for article in articles:
            article["_accountId"] = account_id
            article["_accountName"] = account_name
            article["_url"] = article.get("workUrl") or article.get("url") or "#"

        all_articles.extend(articles)

        # 返回不足 20 条说明已是最后一页
        if len(articles) < 20:
            break

    return all_articles


def build_session(api_key):
    """构造带鉴权的 HTTP 会话（兼容 REDFOX_API_KEY / X-API-KEY 两类请求头）"""
    session = requests.Session()
    session.verify = True
    session.headers.update({
        "Content-Type": "application/json",
        "REDFOX_API_KEY": api_key,
        "X-API-KEY": api_key,
    })
    return session


# ─── 账号搜索 & 单篇抓取 ──────────────────────────────────────────────────────────
def search_user(session, keyword, limit=20):
    """按关键词搜索公众号（红狐广域库 searchUser），返回 (账号列表, 错误信息)"""
    try:
        resp = session.post(SEARCH_URL, json={"keyword": keyword, "offset": 0}, timeout=20)
        result = resp.json()
    except Exception as e:
        return [], f"搜索请求失败: {e}"

    if result.get("code") != 2000:
        msg = result.get("msg") or result.get("message") or "搜索失败"
        return [], str(msg)

    data = result.get("data") or {}
    if isinstance(data, list):
        lst = data
    elif isinstance(data, dict):
        lst = data.get("list") or data.get("accounts") or data.get("records") or []
    else:
        lst = []
    return lst[:limit], None


def resolve_account(session, keyword):
    """按公众号名称搜索解析账号：精确匹配 accountName 优先，无精确匹配提示手动确认"""
    lst, err = search_user(session, keyword, limit=20)
    if err:
        return None, err
    if not lst:
        return None, f"「{keyword}」在红狐广域库中未找到（可能未收录）"
    for it in lst:
        if (it.get("accountName") or "").strip() == keyword.strip():
            return it, None
    return None, (f"广域库未找到与「{keyword}」完全匹配的账号（可能未收录）。"
                  f"可运行 search 查看近似候选，确认 ID 后用 latest <ID> 拉文章")


def command_search(session, keyword, limit=10):
    """search 子命令：搜索公众号并展示微信号/ID 信息"""
    lst, err = search_user(session, keyword, limit)
    if err:
        error(err)
        return
    if not lst:
        warn(f"「{keyword}」在红狐广域库中未找到")
        return

    info(f"关键词「{keyword}」匹配到账号（展示前 {len(lst)} 条）:")
    print()
    for i, it in enumerate(lst, 1):
        name = it.get("accountName") or "-"
        account = it.get("account") or "-"
        wxid = it.get("wxId") or "-"
        biz = it.get("bizInfo") or "-"
        verify = (it.get("verifyInfo") or "")[:40] or "未认证"
        print(f"  {i:>2}. {YELLOW}{name}{RESET}   (account: {CYAN}{account}{RESET})")
        print(f"      wxId: {wxid}   bizInfo: {biz}")
        print(f"      认证: {verify}")
    print()


# 广告/促销标题特征词：命中任一视为广告链接，取文时优先跳过
AD_TITLE_KEYWORDS = [
    "广告", "推广", "福利", "秒杀", "开奖", "抽奖", "直降", "满减",
    "优惠", "折扣", "特惠", "大促", "促销", "回购", "囤货", "上新",
    "首发", "预售", "开售", "上市", "礼遇", "会员日",
    "618", "双11", "双十一", "双12", "聚划算", "年货节", "直播",
]


def _is_ad_article(a):
    t = (a.get("title") or a.get("name") or "").strip()
    return any(k in t for k in AD_TITLE_KEYWORDS)


def _pick_article(articles):
    """已按时间倒序的列表中取一篇：过滤广告标题后取第 2 篇；
    若过滤后仅 1 篇则取该篇；若全部为广告则退回最新一篇。"""
    non_ad = [a for a in articles if not _is_ad_article(a)]
    if not non_ad:
        return articles[0]
    return non_ad[1] if len(non_ad) >= 2 else non_ad[0]


def fetch_latest_article(session, account_id, account_name, days=30):
    """拉取指定公众号在近 days 天内最新的一篇文章，返回 (文章dict 或 None, 错误信息)"""
    start_date = datetime.now() - timedelta(days=days)
    payload = {
        "account": account_id,
        "offset": 0,
        "sortType": "0",
        "publishTimeStart": f"{start_date.strftime('%Y-%m-%d')} 00:00:00",
        "publishTimeEnd": f"{datetime.now().strftime('%Y-%m-%d')} 23:59:59",
        "source": SOURCE,
    }
    try:
        resp = session.post(API_URL, json=payload, timeout=20)
        result = resp.json()
    except Exception as e:
        return None, f"文章请求失败: {e}"

    if result.get("code") != 2000:
        msg = result.get("msg") or result.get("message") or "未知错误"
        return None, str(msg)

    data = result.get("data") or {}
    if isinstance(data, list):
        articles = data
    elif isinstance(data, dict):
        articles = data.get("list") or data.get("articles") or data.get("records") or []
    else:
        articles = []

    if not articles:
        return None, "该账号在库中暂无文章数据（可能未收录）"

    for a in articles:
        a["_accountId"] = account_id
        a["_accountName"] = account_name
        a["_url"] = a.get("workUrl") or a.get("url") or "#"

    articles.sort(key=lambda a: a.get("publicTime") or a.get("publishDate") or a.get("publishTime") or a.get("date") or "", reverse=True)
    return _pick_article(articles), None


def _friendly_error(msg):
    """压缩红狐原始错误为可读摘要（3203 未收录 → 简短提示）"""
    if not msg:
        return "未知错误"
    s = str(msg)
    if "3203" in s or "未查询到相关数据" in s or "暂不在库" in s:
        return "红狐广域库未收录或暂无文章数据"
    return s[:80]


def query_latest(session, keyword, days=30):
    """查询单个公众号最新一篇，返回统一 dict（不打印）：
    {keyword, accountName, accountId, title, publishTime, url, error}
    成功时 error 为 None；失败时 url 为空、error 为原因。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"keyword": "", "accountName": "", "accountId": "", "title": "", "publishTime": "", "url": "", "error": "关键词为空"}

    has_cjk = any('\u4e00' <= ch <= '\u9fff' for ch in keyword)

    def success(article):
        return {
            "keyword": keyword,
            "accountName": article.get("_accountName") or keyword,
            "accountId": article.get("_accountId") or keyword,
            "title": article.get("title") or article.get("name") or "无标题",
            "publishTime": (article.get("publicTime") or article.get("publishDate") or article.get("publishTime") or article.get("date") or "-")[:19],
            "url": article.get("_url") or "#",
            "error": None,
        }

    def failure(msg):
        return {"keyword": keyword, "accountName": keyword, "accountId": "", "title": "", "publishTime": "", "url": "", "error": _friendly_error(msg)}

    if has_cjk:
        # 中文名称：按名称精确解析 → 抓最新一篇
        cand, rerr = resolve_account(session, keyword)
        if rerr:
            return failure(rerr)
        account_id = cand.get("account") or cand.get("wxId") or cand.get("bizInfo")
        article, err = fetch_latest_article(session, account_id, cand.get("accountName") or keyword, days)
        if article is None:
            return failure(err or "抓取失败")
        return success(article)

    # 纯 ID/英文路径：先直查，成功补全名称；失败再按名称解析
    article, err = fetch_latest_article(session, keyword, keyword, days)
    if article is not None:
        cand, _ = resolve_account(session, keyword)
        if cand:
            article["_accountName"] = cand.get("accountName") or keyword
            article["_accountId"] = cand.get("account") or keyword
        return success(article)

    cand, rerr = resolve_account(session, keyword)
    if rerr:
        return failure(rerr)
    account_id = cand.get("account") or cand.get("wxId") or cand.get("bizInfo")
    article, err = fetch_latest_article(session, account_id, cand.get("accountName") or keyword, days)
    if article is None:
        return failure(err or "抓取失败")
    return success(article)


def command_latest(session, keyword, as_json=False, days=30):
    """latest 子命令：名称或 ID → 解析账号 → 抓取最新一篇。"""
    r = query_latest(session, keyword, days)
    if r.get("error"):
        if as_json:
            print(json.dumps({"error": r["error"]}, ensure_ascii=False))
        else:
            error(r["error"])
        return
    if as_json:
        print(json.dumps({k: r[k] for k in ("accountName", "accountId", "title", "publishTime", "url")}, ensure_ascii=False))
    else:
        info(f"公众号「{r['accountName']}」(ID: {r['accountId']}) 文章:")
        print(f"  {YELLOW}{r['title']}{RESET}")
        print(f"  发布时间: {r['publishTime']}")
        print(f"  {CYAN}链接: {r['url']}{RESET}")


def command_batch(session, keywords, out_path, days=30, group_size=20):
    """batch 子命令：批量取最新一篇，每 group_size 个一组分批请求，结果存 Markdown 表格。

    表格列：关键词 | 公众号 | 文章链接；查不到的账号链接列标注原因（不混入错误结果）。
    """
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        error("没有可处理的关键词")
        return

    total = len(keywords)
    groups = (total + group_size - 1) // group_size
    step(f"批量处理 {total} 个公众号，每 {group_size} 个一组，共 {groups} 组...")

    rows = []
    for start in range(0, total, group_size):
        group = keywords[start:start + group_size]
        step(f"第 {start // group_size + 1} 组（{len(group)} 个）")
        for i, kw in enumerate(group, 1):
            r = query_latest(session, kw, days)
            rows.append(r)
            mark = "✓" if not r["error"] else "✗"
            detail = "" if not r["error"] else f" — {r['error']}"
            print(f"  [{start + i}/{total}] {mark} {kw}{detail}")
            time.sleep(0.2)
        if start + group_size < total:
            info(f"本组完成，剩余 {total - (start + group_size)} 个，暂停 1s")
            time.sleep(1)

    ok = sum(1 for r in rows if not r["error"])
    lines = [
        "# 公众号文章链接",
        "",
        f"> 共处理 {total} 个公众号，成功 {ok}，失败 {total - ok}；生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| 关键词 | 公众号 | 文章标题 | 文章链接 |",
        "| --- | --- | --- | --- |",
    ]
    for r in rows:
        if r["error"]:
            lines.append(f"| {r['keyword']} | {r['accountName']} | - | ⚠ {r['error']} |")
        else:
            title = (r.get("title") or "").replace("|", "\\|")
            lines.append(f"| {r['keyword']} | {r['accountName']} | {title} | {r['url']} |")
    md_text = "\n".join(lines) + "\n"

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md_text, encoding="utf-8")

    print()
    info(f"完成：成功 {ok}/{total}，Markdown 已保存到 {out}")
    print()
    print(f"{BOLD}结果一览{RESET}")
    for r in rows:
        if r["error"]:
            print(f"  [✗] {r['keyword']} — {r['error']}")
        else:
            print(f"  [✓] {r['keyword']} → {r['accountName']} | {r['title']} | {r['url']}")


def main():
    parser = argparse.ArgumentParser(
        description="公众号文章抓取 — 关键词 → 找出公众号 → 抓取文章链接（基于红狐广域库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 subscribe.py search "且初"                     # 按名称搜公众号，拿 account/wxId/bizInfo
  python3 subscribe.py latest "KIMTRUE且初"              # 一键取该号最新一篇（名称或ID均可）
  python3 subscribe.py latest "kimtrue66" --json         # JSON 行输出，方便批量回填
  python3 subscribe.py batch "且初" "KIMTRUE且初"         # 批量取最新一篇，输出 Markdown 表格
  python3 subscribe.py batch --file 名单.txt --out 表.md --group 20
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # ── search 子命令 ──
    search_parser = subparsers.add_parser("search", help="按名称/关键词搜索公众号，获取微信号(account)/wxId/bizInfo")
    search_parser.add_argument("keyword", help="公众号名称或关键词，如: 且初")
    search_parser.add_argument("--limit", type=int, default=10,
                               help="最多展示条数（默认 10）")

    # ── latest 子命令 ──
    latest_parser = subparsers.add_parser("latest", help="获取指定公众号最新一篇（支持名称或账号ID）")
    latest_parser.add_argument("keyword", help="公众号名称 或 账号ID（account/wxId/bizInfo 三选一）")
    latest_parser.add_argument("--days", type=int, default=30,
                               help="向前回溯天数（默认 30 天）")
    latest_parser.add_argument("--json", action="store_true",
                               help="以 JSON 行输出（方便批量回填表格）")

    # ── batch 子命令 ──
    batch_parser = subparsers.add_parser("batch", help="批量取最新一篇：每 20 个一组分批请求，输出 Markdown 表格")
    batch_parser.add_argument("names", nargs="*", help="公众号名称/关键词（可多个）")
    batch_parser.add_argument("--file", dest="names_file",
                              help="名单文件路径，每行一个名称/关键词")
    batch_parser.add_argument("--out", default="gzh_latest_articles.md",
                              help="Markdown 输出路径（默认: gzh_latest_articles.md）")
    batch_parser.add_argument("--group", type=int, default=20,
                              help="每组数量（默认 20，超过 20 自动拆分）")
    batch_parser.add_argument("--days", type=int, default=30,
                              help="向前回溯天数（默认 30 天）")

    # ── 全局参数 ──
    parser.add_argument("--api-key", help="API Key")

    args = parser.parse_args()

    # ── 检查依赖 ──
    if not HAS_REQUESTS:
        error("缺少 requests 库，请安装: pip3 install requests")
        sys.exit(1)

    # ── 分发命令 ──
    if args.command not in ("search", "latest", "batch"):
        parser.print_help()
        print()
        print("链路: 关键词 → search 找公众号 → latest/batch 抓文章")
        return

    api_key = get_api_key(cli_key=args.api_key)
    if not api_key:
        error("未配置 API Key，请通过以下方式之一配置：")
        print("  export REDFOX_API_KEY=ak_你的密钥")
        print("  python3 subscribe.py --api-key ak_你的密钥")
        print("  echo '{\"api_key\":\"ak_你的密钥\"}' > ~/.qoder/apis/redfox.json")
        print("注册获取 Key: https://redfox.hk/settings/api-keys")
        sys.exit(1)

    session = build_session(api_key)

    if args.command == "search":
        command_search(session, args.keyword, args.limit)
    elif args.command == "latest":
        command_latest(session, args.keyword, args.json, args.days)
    else:  # batch
        keywords = list(args.names or [])
        if args.names_file:
            nf = Path(args.names_file)
            if not nf.exists():
                error(f"名单文件不存在: {nf}")
                sys.exit(1)
            keywords += [ln.strip() for ln in nf.read_text(encoding="utf-8").splitlines() if ln.strip()]
        command_batch(session, keywords, args.out, days=args.days, group_size=args.group)


if __name__ == "__main__":
    main()
