# 个性化主题订阅与 Firecrawl 接入设计

状态：v0 已实现并通过本地验收（2026-09-02）。整站 Crawl、自动来源注册、个人日报投递仍不在 v0 范围。

## 1. 产品目标

用户订阅的对象不再是平台预设的 `beauty` 等领域，而是用户自己的兴趣意图，例如：

- 关键词：`防晒新品`、`具身智能融资`。
- 自然语言：`持续关注中国消费品牌出海东南亚的渠道合作、监管变化和融资事件，但排除纯营销稿。`

平台 `Domain` 继续承担内部治理、分类器和编辑政策；用户主题是独立对象，可以跨越一个或多个领域。

## 2. Firecrawl 的职责边界

Firecrawl 作为新的获取引擎和发现服务接入，不直接成为内容池，也不直接决定文章是否命中主题。

| 能力 | Navigate 用途 | 默认策略 |
| --- | --- | --- |
| Search | 现有来源与共享池覆盖不足时发现候选 URL | 每个新主题首次最多 10 个结果；相近主题共享查询缓存 |
| Scrape | 直连 HTML/RSS/API 失败或 JS 页面需要渲染时抓取单页 | 只请求 markdown 与 metadata；每页 1 credit |
| Map | 管理员验证新站点结构、发现栏目 URL | 人工触发，硬上限 20，不对普通用户开放 |
| Crawl | 已确认高价值站点的有限栏目补采 | 人工审核后触发，必须显式 `limit <= 20` |
| Batch Scrape | 多个已知候选 URL 的异步抓取 | 去重后批量提交，仍按页计费 |
| Change Tracking / Monitor | 少量固定政策、价格、产品页的变更监控 | 独立于新闻订阅；用户显式开启才计费 |
| Agent / Interact / JSON | 复杂研究和页面交互 | v1 禁用，避免动态和附加额度 |

Firecrawl 返回的成功只说明 API 请求成功；还必须检查目标页 `metadata.statusCode`。所有结果继续转换为 `article.v1.1`，进入 `RawItem → ContentItem → 去重 → 主题匹配`。语言、内容地域和排除条件不是供应商的隐式保证：搜索计划把它们保留为可审计语义，页面正文入池后再由中文编辑与主题匹配决定读者可见性。

## 3. 省额度原则

1. 先查本地共享数据池，同一篇文章只抓一次、可命中多个用户主题。
2. 新建或修改主题时才编译意图；正常运行不重复调用 LLM 解释同一段描述。
3. 主题发现前，LLM 只能生成严格的 `topic-search-plan.v1`；程序只接受 `query`、安全开关、位置、域名白/黑名单和受支持类别，禁止模型拼接任意 Firecrawl JSON。
4. Firecrawl Search 只补来源覆盖缺口，不作为每次刷新都执行的全文搜索。
5. 优先把发现的站点转成长期 RSS/Atom、Sitemap 或普通 HTML 来源；Firecrawl 是兜底引擎。
6. Crawl 和 Map 必须有硬页数上限；禁止使用官方 10,000 页默认值。
7. 按用户、主题和自然日记录 `credits_reserved / credits_used`；预算不足时降级为只匹配现有池。
8. 不启用自动充值；429、402 和目标页非 2xx 都进入可审计失败状态，不无限重试。

## 4. 数据契约

### `interest_topics`

- `id`, `user_id`
- `name`：短标题，如“消费品牌出海东南亚”
- `intent_text`：用户原始自然语言，保持不可替代的审计依据
- `compiled_intent`：版本化结构，含正向关键词、排除项、实体、事件类型、地区、语言和来源偏好
- `compiler_name`, `compiler_version`, `intent_hash`
- `cadence`：`realtime | daily | weekly`
- `status`：`active | paused | draft`
- `daily_credit_limit`, `created_at`, `updated_at`

### `topic_source_candidates`

- `topic_id`, `canonical_url`, `host`
- `discovery_method`：`pool | firecrawl_search | user_url | admin_template`
- `source_id`：通过审核并注册为共享来源后关联现有 `sources`
- `confidence`, `status`, `evidence`, `last_checked_at`

### `topic_matches`

- 唯一键：`topic_id + content_item_id + matcher_version`
- `decision`：`include | exclude | review`
- `score`, `reasons`, `matched_signals`
- `input_content_hash`, `matched_at`

### `topic_runs`

- `topic_id`, `run_date`, `stage`, `status`
- `pool_candidates`, `search_candidates`, `fetched_pages`, `matched_items`
- `firecrawl_credits_reserved`, `firecrawl_credits_used`, `llm_tokens_used`
- `error_code`, `started_at`, `finished_at`

现有 `user_subscriptions` 后续迁移为主题投递偏好，不再以 `domain_id` 作为订阅核心。正式迁移需先兼容已有数据，再删除旧约束。

## 5. 处理链

```text
用户输入关键词 / 自然语言
→ 一次性意图编译（结构化正向、排除、实体、地域、事件类型）
→ LLM `topic-search-plan.v1`（查询、语言/内容地域语义、位置、允许域、排除域、类别、安全开关）
→ 程序编译 Firecrawl Search（固定 `sources=[web]`；首次 `tbs=qdr:w`，后续 `tbs=qdr:d`）
→ 查询共享 ContentItem 池（FTS/BM25 + 确定性信号）
→ 覆盖不足时 Firecrawl Search 发现 URL
→ URL 规范化与全局去重
→ 已注册来源优先走 RSS/HTML/API；失败页走 Firecrawl Scrape
→ article.v1.1 / RawItem / ContentItem
→ 主题初筛
→ 仅边界候选批量 LLM 复核
→ topic_matches
→ 用户“为你精选”时间流 / 日报
```

## 6. API 草案

- `POST /api/v1/topics`：创建关键词或自然语言主题。
- `GET /api/v1/topics`：读取当前用户主题及今日新增数。
- `GET /api/v1/topics/{id}`：读取主题、编译规则和运行状态。
- `PATCH /api/v1/topics/{id}`：修改描述、频率、预算或暂停状态。
- `GET /api/v1/topics/{id}/feed`：读取命中内容。
- `POST /api/v1/topics/{id}/preview`：只用现有池预览命中，不消耗 Firecrawl。
- `POST /api/v1/topics/{id}/discover`：用户确认后执行有预算上限的来源发现。
- `GET /api/v1/feed/for-you`：合并所有活动主题并去重后的个人首页。

## 7. 网站信息架构

参考 AIHOT 的侧栏、时间流和主题地图，但不展示内部 AI 分数或推荐算法说明。

- `为你精选`：所有个人主题合并后的时间流，显示命中主题和来源。
- `我的主题`：主题卡片、今日新增、运行状态、编辑和暂停。
- `探索`：平台公共热点、预设主题模板和跨行业浏览。
- `每日简报`：按用户主题生成的个人日报。
- `收藏`：用户保存的文章。
- `账号`：资料、投递频率和额度使用。

首次登录首页使用一个明确的主题输入器：

> 告诉我你想持续关注什么
>
> 例如：关注国产美妆品牌在东南亚开店、经销合作和监管变化，排除促销软文。

提交后先展示“仅基于现有池”的预览；用户确认主题后才允许 Firecrawl 搜索补缺。

## 8. Firecrawl 适配器落点

- 新增 `firecrawl` execution engine，不把密钥写进来源配置。
- 统一从 `FIRECRAWL_API_KEY` 读取；日志只保存请求 ID、endpoint、页数、credits 和状态。
- `Search` 结果先成为 `topic_source_candidates`；用户显式发现时，剩余额度内的页面继续 Scrape 并通过统一内容契约入池。
- Search 请求缓存键覆盖 query、limit 与全部白名单参数。`tbs` 只用于 `web` 候选召回，不能替代 `article.v1.1.published_at` 的最终日期校验；`news` 未接入该日期语义。
- `Scrape` 响应保存最小必要原始证据和内容哈希；创建的来源证据默认停用，不能未经审核进入定时调度，也不持久化短期截图 URL。
- Webhook 上线前必须校验 `X-Firecrawl-Signature` 的 HMAC-SHA256；本地版先采用异步轮询。

## 9. v0 实现范围

v0 完成：主题创建/编辑/暂停、现有池预览、个人时间流、额度面板，以及用户显式触发的 Firecrawl Search → 有界 Scrape → 统一内容入池。整站 Crawl、Monitor、邮件投递和自动启用来源均不进入 v0。
