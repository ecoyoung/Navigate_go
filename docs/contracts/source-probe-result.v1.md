# source-probe-result.v1

`source-probe-result.v1` 是新来源接入前的只读结构探测契约。它只回答“响应中发现了什么”，不创建 `Source`、`CrawlRun`、快照或正式 Pipeline。

## 核心字段

| 字段 | 含义 |
|---|---|
| `probe_id` | URL、响应状态、正文哈希和探测器版本生成的稳定 ID |
| `requested_url/final_url` | 输入与最终响应地址 |
| `probed_at` | 带时区的观测时间 |
| `outcome` | `success / partial / blocked / unreachable / invalid` |
| `detected_format` | `rss / atom / sitemap_urlset / sitemap_index / json / html / blocked / empty / unknown` |
| `access` | robots、访问级别和 challenge 分类 |
| `evidence` | 带稳定 ref 的机器证据，不保存正文或敏感响应头 |
| `candidates` | 已验证或待验证的 Feed、Sitemap、HTML、JSON API 候选 |
| `article_samples` | 最多 10 个结构样本 URL |
| `json_item_paths` | 自动发现的候选数组路径，仅供验证 |
| `recommended_pipeline` | 一份始终不可自动启用的 Pipeline 建议 |

## 重要约束

- 相同响应事实产生相同 `probe_id` 和判定。
- HTML 声明的 RSS/Atom 只是 `verified=false` 候选，必须独立请求并解析成功。
- Sitemap index 不得被当成文章 URL 集合。
- HTTP 200 的验证码、挑战、登录、维护或软 404 优先于 HTML 类型判断。
- Content-Type 只是证据；实际结构与 MIME 冲突时记录诊断。
- 行业、语言和正文关键词不参与渠道判断。
- 探测结果不能直接修改生产来源配置。

纯结构判断位于 `backend/app/source_probe.py`，受限联网读取位于 `backend/app/source_probe_fetch.py`，公网地址策略位于 `backend/app/outbound_policy.py`；规则版本为 `source-probe.rules.v1`。
