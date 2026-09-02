# source-pipeline.v1

`source-pipeline.v1` 将来源载体、传输通道、执行引擎、发现方式、正文获取、解析方式和增量策略分开表达。

## 状态

| 状态 | 含义 |
|---|---|
| `draft` | Probe 建议，尚未完成 fixture 与人工确认 |
| `verified` | 已完成验证，可以编译为当前 `parser_config` |
| `blocked` | robots 或访问政策禁止 |
| `unsupported` | 当前入口不是列表源，或执行引擎尚未实现 |

Probe 永远不会输出 `verified`。状态转换必须发生在独立验收步骤中。

## 当前执行维度

- 通道：`web / rss / api / third_party_feed`
- 引擎：`static_http / feed_direct / sitemap_http / json_api / provider_api`
- 未来但未启用：`browser_rendered`，只允许处于 `unsupported`
- 发现：`official_feed / json_listing / sitemap / html_listing / manual_seed`
- 正文：`feed_full_content / feed_summary / html_detail / json_detail / metadata_only`
- 解析：`feed_parser / structured_data / configured_selector / generic_article_parser / json_mapping`
- 增量：ETag、Last-Modified、外部 ID、cursor、水位和内容重叠窗口

公众号离线导入、公众号浏览器采集和 WeWe RSS 不在枚举中，不能生成合法 Pipeline。

## 通用发布时间时区

HTML、RSS/Atom 和 JSON 来源可以在 `parser_config` 中声明可选的
`publication_timezone`，其值必须是有效的 IANA 时区，例如 `Europe/Paris`。

- 上游时间自带 UTC 偏移时，保留它表达的绝对时刻，并统一转换为 UTC 入库。
- 上游时间没有 UTC 偏移时，先按来源 `publication_timezone` 本地化，再转换为 UTC。
- 未声明来源时区的无偏移时间沿用兼容行为；新来源验收不得依赖这一回退。
- 页面结构化数据与 Feed 同时提供时间时，应通过 fixture 或 canary 验证二者代表同一绝对时刻。

这一字段解决“内容实际何时发布”；只有需要按自然日闭合发现范围的日期型任务，才另外把
`coverage_date` 和 `publication_timezone` 冻结到 `CrawlRun`。

## RedFox 日期约束

当前 RedFox Provider Pipeline 必须包含：

```json
{
  "publication_date_mode": "previous_day",
  "publication_timezone": "Asia/Shanghai",
  "exclude_explicit_pinned": true,
  "exclude_explicit_advertising": true,
  "max_listing_pages": 10,
  "max_articles_per_day": 100
}
```

首次创建任务时，由 `CrawlRun.started_at` 转换到 `publication_timezone` 后减一天，并将结果冻结为 `CrawlRun.coverage_date`；执行和重试只读取冻结值。列表项先以 `publishTime` 是否落在目标自然日判断，再按明确的置顶/广告布尔字段、类型或标签排除；标题关键词和 `orderNum` 不属于证据。运行时不允许 `skip_first_article`、`skip_ad_titles` 或 `skip_pinned`。分页必须出现早于目标日的条目或到达接口声明的列表末尾；否则运行失败。目标日没有文章属于健康空结果。完整规则见 [`crawl-run-coverage.v1`](./crawl-run-coverage.v1.md)。

## 与当前运行时兼容

`pipeline_to_legacy_parser_config()` 只接受 `verified` Pipeline，并投影到现有扁平 `parser_config`。它不会注册或启用来源。正式注册仍需显式操作，确保 Probe 误判不会改变生产路径。

编译结果必须同时写入 `execution_engine` 与兼容的 `discovery_method`。运行时按 [`execution-engine.v1`](./execution-engine.v1.md) 注册表选择唯一引擎；两者冲突时拒绝配置，不进行隐式回退。
