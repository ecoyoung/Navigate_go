# execution-engine.v1

`execution-engine.v1` 定义来源 Pipeline 到运行时采集引擎的一对一映射。渠道类型描述数据如何到达，执行引擎描述发现、增量和解析由哪段实现负责；两者不能混用。

## 当前引擎注册表

| `execution_engine` | 合法渠道 | 负责范围 |
|---|---|---|
| `static_http` | `web` | HTML 列表发现与 HTML/JSON 正文解析 |
| `feed_direct` | `rss`、`third_party_feed` | RSS/Atom 条目发现、条件请求、增量窗口与 Feed 正文 |
| `sitemap_http` | `web` | Sitemap URL 发现与详情页解析 |
| `json_api` | `api`、`third_party_feed` | JSON 列表映射与 HTML/JSON 详情解析 |
| `provider_api` | `third_party_feed` | 已注册供应商的专属分页、日期规则与详情协议；当前只实现 RedFox |

## 统一接口

每个引擎必须声明：

- 稳定的 `key`、唯一 `discovery_method` 和列表快照类型。
- 是否使用条件同步，以及发现请求需要的增量请求头。
- 如何将列表响应处理为详情 URL、Feed 内联文章或供应商专属入库结果。
- 如何把详情响应解析为统一文章输入。

共享编排器只负责 HTTP 客户端、robots、重试、PageSnapshot、计数、错误状态与统一入库，不包含具体渠道的发现规则。

## 配置规则

- `source-pipeline.v1.engine` 编译为 `parser_config.execution_engine`。
- API 注册、更新和来源目录同步都会写入规范化的引擎键。
- 对没有显式引擎键的历史配置，运行时可由 `channel_type + discovery_method + provider` 唯一推导，随后在下一次配置同步时固化。
- 显式引擎与渠道、发现方式或 Provider 冲突时拒绝注册，不能静默改道。
- 新增普通引擎只注册新实现及合法渠道映射；不得向渠道适配器或共享 HTTP 编排器继续增加 Provider 条件分支。
