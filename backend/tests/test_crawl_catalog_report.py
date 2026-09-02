import asyncio
import json
from types import SimpleNamespace

from scripts import crawl_catalog


def test_write_report_includes_updated_count(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl_catalog, "OUTPUT_DIR", tmp_path)
    result = {
        "id": "example",
        "name": "Example News",
        "region": "Global",
        "language": "en",
        "status": "succeeded",
        "fetched": 1,
        "new": 0,
        "updated": 1,
        "skipped": 0,
        "rejected": 0,
        "errors": 0,
        "error_summary": None,
    }

    json_path, md_path = crawl_catalog.write_report([result])

    assert json.loads(json_path.read_text(encoding="utf-8"))[0]["updated"] == 1
    report = md_path.read_text(encoding="utf-8")
    assert report.startswith("# 网站采集运行报告")
    assert "| 抓取 | 新增 | 更新 | 重复跳过 |" in report
    assert "| succeeded | 1 | 0 | 1 | 0 |" in report


def test_run_excludes_provider_before_scheduling(monkeypatch):
    redfox = SimpleNamespace(
        id="wechat_source",
        name="公众号",
        parser_config={"provider": "redfox"},
    )
    monkeypatch.setattr(crawl_catalog, "load_catalog", lambda _path: [redfox])

    results = asyncio.run(crawl_catalog.run(exclude_providers={"redfox"}))

    assert results == []
