# 2026-08-29 覆盖日与 RedFox 历史时区修复存档

## 恢复点

- `navigate-before-redfox-timezone-repair.db`：应用 73 条历史发布时间修复前、已升级到 Alembic `0017_crawl_run_coverage` 的完整 SQLite 数据库。
- `generated-caches/`：本轮最终测试产生的 Python、pytest 与 Ruff 缓存；不属于运行时源码，可安全忽略。

## 修复结果

- 候选：73 条当前 RedFox / RedFox archive 内容，分布在 72 个来源。
- 新增：73 个不可变 Raw 版本和 72 个 `trigger=repair` 的审计运行。
- 更新：73 个 ContentItem 最新投影的 `raw_item_id`、`published_at` 与 `normalizer_version`。
- 保持不变：旧 300 个 Raw 版本、109 条内容身份、标题、正文、标签、URL 和 `content_hash`。
- 幂等验证：应用后 dry-run 候选为 0。
- 恢复方式：停止写入后，以本文件旁的数据库副本替换 `backend/data/navigate.db`；替换会同时撤销修复后产生的所有数据库写入。

数据库副本属于本机敏感运行数据，受项目 `.gitignore` 保护，不应提交或外传。
