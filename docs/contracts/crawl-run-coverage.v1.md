# crawl-run-coverage.v1

日期型采集任务在创建时冻结其发布日上下文，执行阶段和重试阶段不得再次从当前时间或已变化的来源配置推导。

## CrawlRun 字段

| 字段 | 类型 | 语义 |
|---|---|---|
| `coverage_date` | date / null | 来源发布时区中的目标自然日；普通滚动来源为 `null` |
| `publication_timezone` | IANA timezone / null | 创建任务时冻结的来源发布时区；`coverage_date` 非空时必须非空 |
| `retry_of_run_id` | integer / null | 本任务直接重试的原任务 ID；首次运行和手动补采为 `null` |

## 创建规则

- `previous_day` 来源的首次调度：以任务 `started_at` 在来源发布时区中的自然日减一天，写入 `coverage_date`。
- 手动补采：可以显式传入早于任务本地运行日的 `coverage_date`；非日期型来源拒绝该参数。
- 重试：逐字继承原任务的 `coverage_date` 与 `publication_timezone`，并写入 `retry_of_run_id`；跨日重试不能改抓新的前一天。
- 同一来源只能有一个 `pending/running` 任务。同一不可变上下文的重复请求幂等返回已有任务；不同覆盖日或不同重试来源返回冲突。
- 来源配置后续变更不影响已经创建的任务。执行器只读取 CrawlRun 中冻结的日期与时区。

## 时间存储规则

- 上游无偏移时间先按冻结的 `publication_timezone` 本地化，再转换为 UTC 写入 `article.v1.1.published_at`。
- `RawItem` 是不可变事实版本。历史时区修复只能追加新 Raw 版本，并让 `ContentItem` 最新投影指向修正版；不得覆盖旧 Raw。
- 显式历史覆盖日生成日报时，默认出版日固定为覆盖日加一天，避免重建日期随执行当天漂移。
