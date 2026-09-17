**English** | [简体中文](README.md)

# Navigate General Information Platform

Navigate consolidates websites, RSS/Atom, Sitemap, JSON APIs, and authorized third-party APIs into a unified content pool, then generates Chinese-language cards and daily briefings for topic subscriptions. The production interface is a Vue workbench, with data served by FastAPI, deployed on local Docker, and exposed through a domain via Cloudflare Tunnel.

Beauty remains one of the internal domain configurations, but readers no longer subscribe to preset domains — they subscribe to their own topics.

## Current Production Capabilities

- Catalog websites are crawled daily at 09:30 and 18:00 Beijing time; the five execution engines share HTTP, snapshots, and ingestion.
- Admins can add websites, inspect runs, trigger manual crawls, and remove sources that cannot be crawled from the list.
- Topics are created with keywords or natural language; creation does not touch the network. When proactively discovering sources, Firecrawl Search fills in URLs; the five engines extract the article body first, falling back to Scrape on failure.
- After ingestion, the pipeline performs deduplication, Chinese/English topic matching, and cross-source event clustering.
- Readable articles are rendered into `content_editorial_zh` Chinese cards; Explore, topic feeds, and briefings all read this card.
- Topic briefings are laid out by coverage day, and GET no longer makes additional LLM calls. Sections use Personnel/Funding/Regulation/Product/Top News.
- The RedFox official-account adapter is retained, with its source disabled by default.

Retired from the runtime: the static `home.json` snapshot, the Cloudflare Sites frontend, browser-based official-account collection, and WeWe RSS.

See [Project Architecture](./docs/architecture/项目架构.md) for the full layering.

## Production Data Flow

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

Firecrawl is used only for topic Search and for single-page Scrape when the engines cannot extract the article body. The twice-daily catalog-site rounds do not use Firecrawl.

## Project Directory

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

## Local Setup

Requirements: Python 3.12+, uv, Node 22+.

```bash
cp .env.example .env
make setup
make check-secrets
make migrate
make seed-catalog
make backend
```

In another terminal:

```bash
make frontend
```

- API: http://127.0.0.1:8000/docs
- Frontend dev: http://127.0.0.1:3000 (requests default to 127.0.0.1:8000)

Create an admin:

```bash
cd backend
uv run python -m scripts.create_admin --email you@example.com --password '...' --name 管理员
```

## Docker and Domain

```bash
docker compose up -d --build
```

Local entry point: http://127.0.0.1:3080

`web` listens on port 80, and `cloudflared` shares its network namespace. In Zero Trust, set the Public Hostname's Service to `http://127.0.0.1:80`.

Rebuild the corresponding service after changing Python or frontend code. The database lives in `./backend/data` and survives image rebuilds. `crawler` and `backend` share the `navigate-backend:local` image.

## Crawling

Enabled catalog sources run automatically via the crawler. To run an extra round manually:

```bash
make crawl-due
```

Continuous scheduling (the container default):

```bash
make crawl-scheduler
```

Probe new URLs (local CLI; no anonymous internet-facing API is exposed):

```bash
cd backend && uv run python -m scripts.probe_source URL
```

Re-render Chinese cards for existing readable articles:

```bash
cd backend && uv run python -m scripts.rebuild_reader_editorials
```

Third-party keys live in the root `.env`: `DEEPSEEK_API_KEY`, `FIRECRAWL_API_KEY`, `REDFOX_API_KEY`, `CLOUDFLARE_TUNNEL_TOKEN`. `make check-secrets` only reports whether they are configured.

### Official Account Source List

The manual entry point is [`backend/config/wechat_accounts.json`](./backend/config/wechat_accounts.json). `sites.json` does not duplicate RedFox endpoints. After editing the list, run `make seed-catalog`. Only accounts with `status=ready` are compiled into sources; all are currently disabled.

## Midstream Governance CLI

Domain classification, entities, value scoring, and per-domain HTML daily reports can still be run via the Makefile for internal governance. They are not the main path of the reader workbench.

```bash
make process-content
make rebuild-strict-duplicates
make classify-beauty
make extract-entities
make rebuild-events
```

## Validation

```bash
make check
npm --prefix frontend run typecheck
```

Tests use local fixtures, do not depend on the public internet, and do not call paid LLMs. See the contracts: [`article.v1.1`](./docs/contracts/article.v1.1.md), [`reader-surface.v1`](./docs/contracts/reader-surface.v1.md), [`accounts-subscriptions.v1`](./docs/contracts/accounts-subscriptions.v1.md), [`topic-subscriptions.v1`](./docs/contracts/topic-subscriptions.v1.md), [`execution-engine.v1`](./docs/contracts/execution-engine.v1.md), [`source-pipeline.v1`](./docs/contracts/source-pipeline.v1.md). For progress, see [Progress and Notes](./docs/operations/进度与注意事项.md).

## Data and Security Boundaries

- `page_snapshots` stores external responses, supporting audit and replay.
- `raw_items` appends a version only when semantics change; `content_items` stores the latest projection.
- No bypassing robots, captchas, logins, or paywalls.
- Secrets, cookies, and databases must not be committed; `.env.example` keeps only empty slots.
- The front end does not expose internal scores, algorithm explanations, or clustering-process copy.
