# source-sync-state.v1

`source_sync_states` 是来源级可变同步状态，只服务采集推进，不属于 `article.v1.1`，也不能作为文章事实对外发布。

| 字段 | 类型 | 语义 |
|---|---|---|
| `source_id` | integer | 与来源一对一的主键 |
| `sync_version` | string | 当前为 `feed-sync.v1` |
| `etag` | string / null | 最近一次可安全提交的 Feed ETag |
| `last_modified` | string / null | 最近一次可安全提交的 Last-Modified |
| `recent_entries` | array | 最近条目的 `{id, fingerprint}` 有序窗口 |
| `published_watermark` | datetime / null | 本次已处理条目的最大发布时间 |
| `updated_watermark` | datetime / null | 本次已处理条目的最大更新时间 |
| `last_committed_run_id` | integer / null | 最近一次推进状态的 CrawlRun |
| `updated_at` | datetime | 状态提交时间 |

条目 `id` 优先使用 Atom ID / RSS GUID，缺失时使用规范化链接，再缺失时才使用标题和发布时间的确定性回退值。`fingerprint` 对条目的标题、链接、发布时间、更新时间、摘要、Feed 正文和标签做确定性 SHA-256，用于识别同一 ID 的内容变化。

## 提交规则

- 条件头只发送到 `rss + feed + GET` 的发现请求，不发送给 robots、文章详情、POST Feed 或第三方 Provider。
- HTTP 304 保存空正文 PageSnapshot，CrawlRun 记为健康的 `unchanged`。
- 新增或指纹变化的条目优先处理，剩余名额才用于最近条目重叠复查。
- 当待处理新增/变化条目超过 `max_articles` 时，不接纳响应中的新 ETag / Last-Modified；逐轮排空 backlog 后再接纳，避免后续 304 导致漏采。
- 只有 `succeeded` 或 `unchanged` 才推进状态；`partial` / `failed` 保留旧 checkpoint，已落库文章依靠现有幂等规则安全重放。

默认窗口为 100，默认重叠 5；可分别用 `feed_recent_window`、`feed_overlap_entries` 和 `feed_scan_limit` 调整，上限由运行时限制。
