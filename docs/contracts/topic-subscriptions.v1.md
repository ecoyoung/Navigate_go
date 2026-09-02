# topic-subscriptions.v1

状态：已实现（2026-09-02）。本契约描述用户自定义兴趣主题、内容匹配和来源发现，不把平台领域当作用户订阅对象。

## 核心对象

### `interest_topics`

- `user_id`：所有权边界；任何读取、修改和发现请求都必须校验当前登录用户。
- `name`、`intent_text`：展示名与用户原始描述。
- `compiled_intent`：版本化正向词、排除词及编译证据；原始描述不可被其替代。
- `compiler_name`、`compiler_version`、`intent_hash`：保证规则可追溯和可重算。
- `cadence`：`realtime | daily | weekly`。
- `status`：`active | paused | draft`。
- `daily_credit_limit`：单主题、上海自然日的 Firecrawl 上限；API 范围 `0..20`。

### `topic_matches`

唯一键为 `topic_id + content_item_id + matcher_version`。保存 `input_content_hash`、`decision`、`score`、`reasons`、`matched_signals` 和 `matched_at`；内容变化后必须按新哈希重算。v1 的展示门槛为 `decision=include`，同一内容命中多个主题时在合并流中只出现一次。

### `topic_source_candidates`

Firecrawl Search 结果先保存为候选来源，包含规范 URL、host、标题、描述、发现方法、置信度、状态和证据。随后在主题剩余额度内 Scrape；成功页面创建或复用一个默认停用的来源证据，并严格经过 `CrawlRun → PageSnapshot → RawItem → article.v1.1 / ContentItem` 入池。它不会被自动启用为定时采集源。

### `topic_runs` 与 `provider_request_cache`

`topic_runs` 审计每次发现的候选数、状态、预留/实际 credits 和安全错误码；`provider_request_cache` 以 `provider + operation + request_hash` 全局复用查询。Firecrawl Search 缓存默认 12 小时。

### 读者可用性与补抓

- `verified_full`：有发布日期，且抓取器确认正文完整；可进入日报和主题流。
- `partial`：有发布日期和不少于 200 字正文，但完整性未确认；可进入读者流，并保留质量警告。
- `needs_enrichment`：缺发布日期、只有元数据或正文不足；保留在统一内容池和候选记录中，但不进入日报或默认主题流。
- 相同规范 URL 的完整内容跨用户主题复用，不再 Scrape；元数据内容仅在已有抓取失败记录超过 24 小时后才允许一次补抓，防止重复计费。

### 主题 LLM 工件

- `topic-intent.llm.v1` 将原始意图编译为行业、产品、实体、事件类型、地区、正向词、排除词和查询扩展；输入、Prompt、Schema、校验器和模型指纹共同形成缓存键。
- `topic-content-editorial.v1` 在同一批次内比较最多 12 篇内容，输出相关决策、排序分、中文标题、中文摘要、中文标签、事件类型、实体和逐字证据。单篇工件键为 `topic:{topic_id}:content:{content_id}`，不同主题不得共用编辑结论。
- LLM 结果必须通过主题 ID、意图哈希、内容哈希、输入顺序、中文字段和逐字证据校验后，才能更新 `topic_matches`。相同输入重跑必须命中缓存且不新增 tokens。

## API

- `POST /api/v1/topics`：创建主题并立即匹配现有共享池；不会联网。
- `GET /api/v1/topics`：列出当前用户主题。
- `PATCH /api/v1/topics/{id}`：修改意图、关键词、排除项、频率、状态或额度，并重算现有池。
- `GET /api/v1/topics/{id}/preview`：返回主题及现有池预览；不会联网。
- `GET /api/v1/topics/{id}/feed`：返回单主题时间流。
- `GET /api/v1/feed/for-you`：合并所有活动主题并按内容去重。
- `POST /api/v1/topics/{id}/discover`：显式执行来源发现和有界页面入库，`limit` 范围 `1..10`；响应同时返回候选、抓取/入库计数和持久化后的主题 `items`。

`TopicFeedItem` 的稳定字段为 `content_id`、`title`、`excerpt`、`source_name`、`url`、`published_at`、`discovered_at`、`language`、`topic_ids`、`topic_names`、`tags` 和 `match_score`。单主题 LLM 工件优先于通用中文工件；单主题流按相关分优先、发布时间次序展示，合并流仍以发布时间优先。

## 成本与失败边界

- 创建、预览和普通刷新只读取 SQLite 共享池，Firecrawl credits 为 0。
- 来源发现必须由已登录的主题所有者显式触发；单次 Search 和后续 Scrape 均最多 10 个页面。
- Search 计 2 credits、缓存命中计 0，Scrape 按实际调用页数计费；两者合计不得超过该主题上海自然日额度。
- 相同规范 URL 已在共享池时直接复用，不再 Scrape。新页面逐篇提交，慢站或失败页不能持有 SQLite 写事务。
- `metadata_only` 是可升级状态：以后再次发现相同 URL 且额度允许时会 Scrape，并原位更新同一个 ContentItem 为完整正文，不创建重复文章。
- 本地 v0 不自动整站 Crawl、Map、Monitor，也不会把发现来源自动设为定时启用。
- 额度不足、429 和 HTTP 失败返回脱敏错误码，不回显 key，不无限重试。
- `FIRECRAWL_API_KEY` 只从统一 secret 管理读取，响应、日志和数据库均不得保存密钥。
