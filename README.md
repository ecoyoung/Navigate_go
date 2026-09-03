# Navigate 通用资讯平台

Navigate 把网站、RSS/Atom、Sitemap、JSON API 和经授权的第三方 API 收成统一内容池，再为主题订阅生成中文卡片和每日简报。正式界面是 Vue 工作台，数据走 FastAPI，部署在本机 Docker，并通过 Cloudflare Tunnel 暴露域名。

美妆仍是内部领域配置之一，但读者不再订阅预设领域，而是订阅自己的主题。

## 当前正式能力

- 目录网站按北京时间每天 09:30、18:00 采集；五种执行引擎共享 HTTP、快照和入库。
- 管理员可补充网站、查看通路、手动抓取，并把爬不了的源移出列表。
- 主题用关键词或自然语言创建；创建不联网。主动发现来源时，Firecrawl Search 补 URL，五种引擎优先抽正文，失败再 Scrape。
- 入库后做去重、中英主题匹配和跨来源事件聚类。
- 可读稿生成 `content_editorial_zh` 中文卡片；探索、主题流和简报都读这张卡片。
- 主题简报按覆盖日排版，GET 不再另打 LLM。栏目用人事/融资/监管/产品/要闻。
- RedFox 公众号适配器保留，来源默认停用。

已退出运行时：静态 `home.json` 快照、Cloudflare Sites 前端、公众号浏览器采集、WeWe RSS。

完整分层见 [项目架构](./docs/architecture/项目架构.md)。

## 正式数据流

```text
目录源 / 手动抓取 / 主题发现
→ 执行引擎（优先）或 Firecrawl Scrape（兜底）
→ CrawlRun / PageSnapshot
→ RawItem → ContentItem / article.v1.1
→ 严格去重
→ TopicMatch
→ content_editorial_zh 中文卡片
→ 探索 / 为你精选 / 主题流 / 主题简报
```

Firecrawl 只用于主题 Search 和引擎抽不出正文时的单页 Scrape。目录站的每日两轮不走 Firecrawl。

## 项目目录

```text
backend/          FastAPI、采集引擎、卡片和主题
  app/
  config/         sites.json、公众号清单、领域配置
  scripts/        调度、回填、运维 CLI
  tests/
  alembic/
  data/           SQLite（不提交）
frontend/         Vue 3 工作台
  src/            App.vue、styles.css
  public/brand/   运行时品牌资源
docs/
  architecture/   当前架构
  contracts/      数据契约
  operations/     运行边界
navigate-brand/   设计源文件
output/           可选本地 CLI 产物
archive/          已退出运行时的历史材料
docker-compose.yml
```

## 本地启动

要求：Python 3.12+、uv、Node 22+。

```bash
cp .env.example .env
make setup
make check-secrets
make migrate
make seed-catalog
make backend
```

另开终端：

```bash
make frontend
```

- API：http://127.0.0.1:8000/docs
- 前端开发：http://127.0.0.1:3000 （默认请求 127.0.0.1:8000）

创建管理员：

```bash
cd backend
uv run python -m scripts.create_admin --email you@example.com --password '...' --name 管理员
```

## Docker 与域名

```bash
docker compose up -d --build
```

本机入口：http://127.0.0.1:3080

`web` 监听 80，`cloudflared` 与它共用网络命名空间。Zero Trust 里 Public Hostname 的 Service 填 `http://127.0.0.1:80`。

改 Python 或前端后重建对应服务。数据库在 `./backend/data`，重建镜像不会丢库。`crawler` 与 `backend` 共用 `navigate-backend:local` 镜像。

## 采集

启用中的目录源由 crawler 自动跑。手动补一轮：

```bash
make crawl-due
```

持续调度（容器默认就是这个）：

```bash
make crawl-scheduler
```

探测新 URL（本机 CLI，不开放匿名联网 API）：

```bash
cd backend && uv run python -m scripts.probe_source URL
```

存量可读稿重打中文卡片：

```bash
cd backend && uv run python -m scripts.rebuild_reader_editorials
```

第三方密钥放在根 `.env`：`DEEPSEEK_API_KEY`、`FIRECRAWL_API_KEY`、`REDFOX_API_KEY`、`CLOUDFLARE_TUNNEL_TOKEN`。`make check-secrets` 只报告是否配置。

### 公众号来源清单

人工入口是 [`backend/config/wechat_accounts.json`](./backend/config/wechat_accounts.json)。`sites.json` 不复制 RedFox 端点。改清单后运行 `make seed-catalog`。只有 `status=ready` 的账号会编译成来源，当前全部停用。

## 中游治理 CLI

领域分类、实体、价值评分和领域 HTML 日报仍可通过 Makefile 运行，供内部治理使用。它们不是读者工作台的主路径。

```bash
make process-content
make rebuild-strict-duplicates
make classify-beauty
make extract-entities
make rebuild-events
```

## 验证

```bash
make check
npm --prefix frontend run typecheck
```

测试使用本地 fixture，不依赖公网，也不调用付费 LLM。契约见 [`article.v1.1`](./docs/contracts/article.v1.1.md)、[`reader-surface.v1`](./docs/contracts/reader-surface.v1.md)、[`accounts-subscriptions.v1`](./docs/contracts/accounts-subscriptions.v1.md)、[`topic-subscriptions.v1`](./docs/contracts/topic-subscriptions.v1.md)、[`execution-engine.v1`](./docs/contracts/execution-engine.v1.md)、[`source-pipeline.v1`](./docs/contracts/source-pipeline.v1.md)。进度见 [进度与注意事项](./docs/operations/进度与注意事项.md)。

## 数据与安全边界

- `page_snapshots` 保存外部响应，支持审计与重放。
- `raw_items` 只在语义变化时追加版本；`content_items` 保存最新投影。
- 不绕过 robots、验证码、登录、付费墙。
- 密钥、Cookie、数据库不得提交；`.env.example` 只保留空槽位。
- 前台不展示内部评分、算法说明或聚类过程文案。
