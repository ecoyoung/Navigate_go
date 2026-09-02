# Navigate 通用资讯平台

Navigate 将网站、RSS/Atom、Sitemap、JSON API 和经授权的第三方 API 内容统一转换为 `article.v1.1`，再进行去重、领域归属、实体提取、事件聚类、中文编辑加工与日报生成。平台内核保持领域无关，美妆是第一个领域配置。

## 当前正式能力

- 配置驱动的网站 HTML、RSS/Atom、Sitemap 和 JSON API 采集；RSS/Atom 已支持条件请求与 GUID/Atom ID 增量窗口。
- `execution-engine.v1` 已将 HTML、RSS/Atom、Sitemap、JSON API 和 RedFox Provider 拆为五个独立注册引擎，共享 HTTP、快照和统一入库底座。
- RedFox API 适配器仍保留，正式规则在任务创建时冻结来源时区内的覆盖日、完整翻页，并排除有明确结构化证据的置顶/广告；当前公众号在线来源默认停用，不作为稳定增量渠道承诺。
- 采集运行、HTTP 快照、不可变原始版本和最新内容投影。
- `article.v1.1` 统一内容契约、URL 身份、内容哈希和跨来源严格重复关系。
- 多领域归属、领域无关事件聚类和可版本化算法结果；beauty 当前使用“确定性初筛 + LLM 逐篇复核”的混合准入器，不再按媒体来源整站放行。
- `entity-mentions.v1` 通用实体主库、别名、原文提及证据和保守消歧；配置化确定性提取、最多 5 篇的缓存化 LLM 候选 canary，以及可审计的 create/link/reject 候选确认流程已跑通，模型不能直接创建或合并实体。
- `content-value-score.v1` 独立资讯价值评分、全量池/精选池决策、冻结时点和逐项解释；评分确定性运行，不调用 LLM。
- Vue 3 + TypeScript 个性化情报工作台：用户可用关键词或自然语言创建跨行业主题，立即预览共享池匹配，合并多个主题形成“为你精选”，并可暂停主题或显式发现候选来源。英文来源优先使用合格中文编辑工件。跨来源事件保留为中游能力，当前不设前台一级模块。
- Firecrawl v2 已接入有界 Search/Scrape：用户意图先由 DeepSeek Flash 编译为可审计的 `topic-search-plan.v1`，再由程序生成受白名单约束的请求；首次发现使用 7 天、后续使用 1 天的 `web` 召回窗，最终仍以页面解析出的发布日期准入。创建主题不联网，只有用户主动“发现来源”才执行 Search 并在剩余额度内抓取页面；单次最多 10 个结果、主题每日额度最高 20，12 小时查询缓存可跨用户复用。成功页面自动写入统一内容池并立即出现在该主题，来源证据默认停用，不会自动加入定时采集。
- 两阶段 LLM 中文编辑：单篇标题/摘要/标签工件与整期栏目编排。
- 严格按来源发布时间生成 D-1 HTML 日报，并可按用户主题、覆盖日查看历史版本；所有来源首次采集默认回溯 7 天、之后按冻结覆盖日增量 1 天，日期或正文不足时自动回退规范网页解析。

以下路径已从正式项目移除并转入存档：公众号离线导入、公众号浏览器采集、WeWe RSS。它们不再有 Makefile、CLI、测试或正式架构入口；历史内容仍保留在数据库中。

## 正式数据流

```text
来源目录
→ 渠道适配器
→ 执行引擎（static_http / feed_direct / sitemap_http / json_api / provider_api）
→ CrawlRun / PageSnapshot
→ RawItem 不可变版本
→ ContentItem / article.v1.1
→ 严格重复关系
→ 多领域归属
→ 通用实体与提及
→ 通用事件层
→ 独立价值评分 / 全量池与精选池
→ content_editorial.zh.v2
→ daily_edition.zh.v1
→ HTML / API 产品视图
```

采集方式只负责合法获取和标准化内容；美妆、科技、金融等行业判断全部位于中游领域配置，不进入采集器硬编码。

## 项目目录

```text
backend/
  app/            正式运行时代码
  config/         来源、聚类和领域编辑配置
  scripts/        可执行运维入口
  tests/          正式能力测试
  alembic/        数据库迁移
  data/           本地运行数据库（忽略提交）
frontend/         Vue 3 + TypeScript 资讯前端
docs/
  architecture/   当前架构唯一说明
  contracts/      稳定数据契约
  operations/     当前进度和来源运行边界
output/           当前正式输出与最终验收基线
archive/          已停用实现、历史过程和旧结果
```

存档内容不属于运行时，禁止从正式代码反向引用。完整边界见 [当前项目架构](./docs/architecture/项目架构.md) 和 [归档清单](./archive/2026-08-28-channel-cleanup/MANIFEST.md)。

## 本地启动

要求：Python 3.12+、uv。

```bash
cp .env.example .env
make setup
make check-secrets
make migrate
make seed-catalog
make backend
```

- API 文档：http://127.0.0.1:8000/docs
- 存活检查：http://127.0.0.1:8000/health/live
- 就绪检查：http://127.0.0.1:8000/health/ready

前端使用 Vue 3、Composition API、TypeScript 和 Vite：

```bash
cd frontend
npm install
npm run dev
```

如需使用与最终构建一致、且不依赖外部托管权限的本机版本：

```bash
cd frontend
npm run local
```

访问 http://127.0.0.1:3000/ 。当前交付以该本机地址为准，不以 ChatGPT Site 作为访问入口。

### 本地账号与订阅

账号、会话和订阅保存在同一 SQLite 数据库。首次部署先迁移数据库，再用本机 CLI 创建管理员；CLI 只接受生成临时密码，不接受命令行明文密码：

```bash
make migrate
cd backend
uv run python -m scripts.create_admin --generate-password
```

管理员临时密码只打印一次，数据库只保存 scrypt 带盐哈希；登录会话使用 HttpOnly Cookie，数据库只保存会话令牌的 SHA-256 哈希。管理员存在后，前端才开放普通读者注册。旧的领域日报订阅仍按 `user + domain + delivery_type` 保存以兼容现有数据；新的产品入口以用户兴趣主题为核心，不限制为 beauty。接口契约见 [`accounts-subscriptions.v1`](./docs/contracts/accounts-subscriptions.v1.md) 和 [`topic-subscriptions.v1`](./docs/contracts/topic-subscriptions.v1.md)。

## 本机 Docker 与 Cloudflare Tunnel

本地容器把前端和 API 放在同一源，再用 Cloudflare Tunnel 接到已配置的域名。Tunnel token 只放在忽略提交的 `.env` 中。

```bash
# .env 中设置 CLOUDFLARE_TUNNEL_TOKEN，可选设置 NAVIGATE_PUBLIC_ORIGIN=https://你的域名
docker compose up -d --build
```

- 本机访问：http://127.0.0.1:3080/
- 公网访问：Cloudflare Zero Trust 里该 Tunnel 绑定的域名
- 停止：`docker compose down`

如果公网打开后页面空白或登录失败，到 Zero Trust → Networks → Tunnels 把 Public Hostname 的 Service 设为 `http://127.0.0.1:80`。cloudflared 与 Nginx 共用网络命名空间，因此 `localhost:80`、`localhost:3000`、`localhost:8000` 和 `localhost:8080` 都会进入同一前端入口。


## 采集与调度

新来源先运行只读结构探测。对本地 fixture 不访问网络：

```bash
cd backend
uv run python -m scripts.probe_source https://example.com/feed.xml \
  --input-file tests/fixtures/source_probe/rss-full.xml \
  --content-type application/rss+xml
```

对真实公开 URL 进行探测时，CLI 会先检查公网地址和 robots，限制重定向、端口、超时与响应大小：

```bash
cd backend
uv run python -m scripts.probe_source https://example.com/
```

Probe 只向标准输出写 JSON，不写数据库、不注册来源、不自动启用建议。当前没有开放匿名联网 Probe API；须先完成管理端认证、限流和异步任务基础。

```bash
make crawl-catalog
make crawl-due
make crawl-scheduler
```

在 RedFox 暂停期间，全量直连采集必须在命令层显式排除 Provider：

```bash
cd backend
uv run python -m scripts.crawl_catalog --exclude-provider redfox
```

正式渠道入口：

| `channel_type` | 当前用途 | 发现方式 |
|---|---|---|
| `web` | 静态 HTML 或 Sitemap | `html` / `sitemap` |
| `rss` | RSS / Atom | `feed` |
| `api` | 公开或商业 JSON API | `json` |
| `third_party_feed` | 经授权的供应商 API/Feed；当前仅保留 RedFox 适配器 | `json` / `feed` |

新增来源目前仍需人工确认和试跑。自动结构探测已具备只读 CLI，RSS/Atom 已具备真正 HTTP 增量同步；Probe 持久化、人工审核注册和组合式执行引擎是渠道层下一阶段，详见 [渠道接入架构](./docs/architecture/项目架构.md#新增来源的标准接入流程)。

第三方密钥统一放在项目根 `.env`（本地）或部署环境变量中：`DEEPSEEK_API_KEY`、`REDFOX_API_KEY` 与 `FIRECRAWL_API_KEY`。应用会自动读取根 `.env`，但不会覆盖进程已注入的值；CLI 不接受命令参数中的明文 key，也不再交互式读取。`make check-secrets` 只报告 configured/missing，不显示值。当前 RedFox 适配器已接入但来源保持 `is_enabled: false`；Firecrawl 只在主题所有者显式执行来源发现时调用。

### 公众号来源清单

公众号的唯一正式人工维护入口是 [`backend/config/wechat_accounts.json`](./backend/config/wechat_accounts.json)，Schema 为 `redfox-wechat-accounts.v1`。`sites.json` 只维护网站、RSS 和普通 API 来源，不再复制 RedFox 公共端点。修改公众号清单后运行 `make seed-catalog`，目录加载器只把 `status=ready` 的账号编译为完整 RedFox 来源。

每项必须保持 `catalog_id` 和 Provider 身份唯一。只有已有可靠 `account`、`wxId` 或 `bizInfo` 证据的项目才能标记 `ready`；只有显示名称的项目保持 `pending`，且不能启用。当前清单合并了用户最近给出的 51 项与此前填写的 206 项，按 RedFox 原始账号 ID 去重后为 244 个账号、247 个名称（含 3 个改名别名），全部 ready、全部 disabled。账号搜索响应保存在本地忽略目录用于幂等复核；以后不得把显示名称直接当作 Provider 选择器。

数据库 `sources` 只是配置投影和历史证据，不应手工修改。存档中的 `archive/2026-08-28-channel-cleanup/private/data/wechat_mp_accounts.json` 与 `wechat_mp_names.txt` 保持历史原貌，不参与正式运行，也不随新清单改写。

## 中游与日报

```bash
make process-content
make rebuild-strict-duplicates
make classify-beauty
# 需要逐篇语义复核时，在确认预算后执行 make classify-beauty-llm，
# 再将 domain_hybrid / beauty-domain-hybrid.v1 同步为活动分类器。
make extract-entities
make rebuild-events
cd backend && uv run python -m scripts.score_content \
  --domain beauty --as-of 2026-08-30T00:00:00+08:00 --apply
make frontend-snapshot
make daily-report
```

面向中文读者生成 LLM 编辑版日报：

```bash
cd backend
uv run python -m scripts.generate_daily_report \
  --domain beauty --llm --llm-model deepseek-v4-flash
```

请求省略 `max_tokens`。单篇结果按内容、Prompt、Schema、校验器和模型指纹缓存；整期只读取已验证的单篇工件、事件和领域策略。离线重建使用：

```bash
cd backend
uv run python -m scripts.generate_daily_report --domain beauty --llm-cache-only
```

## 验证

```bash
make check
```

测试使用本地 fixture，不依赖公网，也不调用付费 LLM。当前契约见 [`article.v1.1`](./docs/contracts/article.v1.1.md)、[`accounts-subscriptions.v1`](./docs/contracts/accounts-subscriptions.v1.md)、[`topic-subscriptions.v1`](./docs/contracts/topic-subscriptions.v1.md)、[`entity-mentions.v1`](./docs/contracts/entity-mentions.v1.md)、[`entity-candidates.v1`](./docs/contracts/entity-candidates.v1.md)、[`content-value-score.v1`](./docs/contracts/content-value-score.v1.md)、[`navigate-home.v1`](./docs/contracts/navigate-home.v1.md)、[`source-probe-result.v1`](./docs/contracts/source-probe-result.v1.md)、[`source-pipeline.v1`](./docs/contracts/source-pipeline.v1.md)、[`execution-engine.v1`](./docs/contracts/execution-engine.v1.md) 和 [`crawl-run-coverage.v1`](./docs/contracts/crawl-run-coverage.v1.md)，最新进度见 [进度与注意事项](./docs/operations/进度与注意事项.md)。

## 数据与安全边界

- `page_snapshots` 保存外部响应和错误，支持审计与重放。
- `raw_items` 只在语义内容变化时追加不可变版本。
- `content_items` 保存每个逻辑内容的最新标准化投影。
- 历史公众号内容继续作为既有数据参与中游，不代表其采集路径仍受支持。
- 不绕过 robots、验证码、登录、付费墙或明确访问限制。
- 密钥、Cookie、数据库和私有原始归档不得提交；项目内历史敏感文件已迁入被忽略的存档私有区。
- `.env.example` 只能保留空槽位，真实根 `.env` 权限保持 600；任何状态命令、日志和错误都不得输出密钥值或片段。

`frontend/` 已完成 Vue 3 + TypeScript 个性化主题工作台；前台不展示内部评分、算法说明或聚类过程文案。主题意图已经按版本/哈希进行 LLM 编译，候选内容由主题专属批次统一生成中文标题、摘要和标签并复核相关性。主题页保留待补全内容，合并流只展示可读内容；侧栏按当前主题和发布日期打开中文历史日报。个人日报投递、邮件发送、支付、实时搜索索引、热点趋势和实体审核工作台仍待实现。
