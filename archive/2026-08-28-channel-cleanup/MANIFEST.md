# 渠道收敛与项目归档清单

归档日期：2026-08-28。

本目录保存从正式项目移除的历史实现和过程材料。归档内容不属于运行时，不应被 `backend/app`、`backend/scripts`、Makefile 或当前文档反向引用。

## 移除原因

- 公众号离线导入只能重放一次性归档，不能发现新增文章或持续增量同步。
- 公众号浏览器与后台脚本受验证码、会话、账号权限和风控影响，没有接入正式调度、快照与验收状态机。
- WeWe RSS 只有参考记录，没有部署、来源配置、provider 实现或测试。
- 一次性契约升级工具已完成历史数据库升级，不再属于长期运行入口。
- 阶段性报告、旧日报基线和原始研究材料不应与当前正式输出混放。

## 目录

| 路径 | 内容 | 恢复含义 |
|---|---|---|
| `legacy_wechat/runtime/` | 离线公众号导入服务 | 仅历史实现，不恢复为生产通道 |
| `legacy_wechat/scripts/` | 浏览器、后台、fakeid、RedFox 批跑和离线导入脚本 | 仅实验/一次性工具 |
| `legacy_wechat/results/` | 浏览器和 RedFox 历史报告 | 原样证据，不代表稳定能力 |
| `legacy_wechat/tooling/` | 旧独立公众号抓取 skill | 与正式后端重复，不再维护 |
| `one_time_upgrade/` | `article.v1.1` 一次性升级代码及测试 | 当前数据库已升级完成 |
| `legacy_scripts/` | 已被来源目录取代的一次性 seed 脚本 | 统一使用 `seed_catalog` |
| `historical_outputs/` | 阶段性站点报告与 LLM 前日报 | 非最终结果 |
| `research/` | 初始网站清单和参考项目笔记 | 非当前来源注册表或能力声明 |
| `architecture/` | 含已移除路径的旧 SVG | 新架构以 `docs/architecture/` 为准 |
| `private/` | Cookie、Key、fakeid、公众号原始 JSONL 等 | 被 `.gitignore` 排除，不得提交或公开 |
| `system_metadata/` | 空目录和系统元数据 | 无运行价值 |
| `generated_caches/` | 清理时从正式源码目录移出的 Python/测试缓存 | 可随运行重新生成 |

## 原位保留

- `backend/data/navigate.db`：当前正式数据库，包含既有历史公众号内容。
- 数据库中的 72 个 `third_party_feed` 来源记录均保持停用；保留它们是为了维持 73 条历史公众号内容的来源外键和溯源，不代表保留离线导入能力。
- `backend/app/redfox_wechat.py`：保留的 RedFox API 解析器。
- `backend/tests/test_redfox_wechat.py`：正式 provider 契约测试。
- `backend/config/sites.json` 中停用的 RedFox 来源：保留未来重新验收所需配置，但不会自动运行。
- `output/daily/2026-08-27-beauty.html`：当前正式日报。
- `output/site_crawl_report_20260827_132008.*` 与 `source_validation_summary_20260827.md`：最终网站验收基线。

## 安全

`private/` 可能包含历史凭据、会话或受限原始数据。不得打开、提交、复制到公开目录或重新用于请求；需要恢复实验时必须先重新评估授权、轮换凭据并建立新的稳定性验收。

当前存档共 214 个文件，约 2.6 MB；增加量来自验证 SourceProbe 后再次迁入的可再生 Python/测试缓存。`private/` 文件权限已收紧为仅当前用户可读写。
