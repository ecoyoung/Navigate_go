# Navigate 统一内容数据契约

当前版本：`article.v1.1`。HTML、RSS/Atom、公开 JSON API 与经授权的第三方 API/Feed 内容必须先转换为该契约，才能进入统一内容池。契约描述内容事实，不承诺某个来源或采集方式具备生产稳定性。

## 分层边界

| 层 | 存储 | 语义 |
| --- | --- | --- |
| 原始响应 | `page_snapshots` | 外部 HTTP 响应、状态码、响应头和错误，不覆盖 |
| 内容版本 | `raw_items` | 每次语义内容变化后追加的不可变 `article.v1.x` 版本 |
| 最新投影 | `content_items` | 每个逻辑内容条目的最新标准化视图 |
| 加工结果 | `content_processing_results` | 相关性等可重算、带输入哈希的业务判断 |
| 重复关系 | `content_items.duplicate_of_id` | 可解释的跨来源严格重复关系 |

历史 `raw_items` 不修改。契约升级通过追加新版本并切换 `content_items.raw_item_id` 完成。

## article.v1.1

```json
{
  "schema_version": "article.v1.1",
  "source_id": 1,
  "source_name": "Example Media",
  "source_region": "US",
  "source_type": "trade_media",
  "source_external_id": null,
  "external_item_id": "article-123",
  "channel_type": "rss",
  "provider": "direct",
  "language": "en",
  "access_level": "public",
  "content_type": "article",
  "title": "Article title",
  "original_url": "https://example.com/articles/123",
  "canonical_url": "https://example.com/articles/123",
  "content_url": "https://example.com/articles/123",
  "discovery_url": "https://example.com/feed.xml",
  "author": "Example Author",
  "published_at": "2026-08-25T17:48:24Z",
  "updated_at": null,
  "captured_at": "2026-08-27T12:00:00Z",
  "excerpt": "Article excerpt",
  "body_text": "Normalized article body.",
  "word_count": 1061,
  "topics": ["美妆", "护肤"],
  "media": [
    {"type": "image", "url": "https://example.com/cover.jpg", "alt": "封面"}
  ],
  "quality": {
    "body_complete": null,
    "metadata_only": false,
    "validation_warnings": ["completeness_unknown"]
  },
  "is_sponsored": false,
  "is_roundup": false,
  "content_hash": "sha256"
}
```

## 字段规则

- `source_external_id`：来源在外部系统的原生身份；不能用本地 `catalog_id` 冒充。
- `external_item_id`：内容在来源系统的原生 ID，例如 RSS GUID、API ID、RedFox `workUuid`；无法证明时为 `null`。
- `channel_type`：`web`、`rss`、`api` 或 `third_party_feed`。
- `provider`：直接采集为 `direct`，第三方服务使用其稳定名称。
- `published_at` 与 `updated_at` 分开；不能用抓取时间代替来源更新时间。
- `word_count`：英文按单词计数，中文按汉字计数，用于跨语言内容规模估计。
- `body_complete`：`true` 表示有明确完整性依据，`false` 表示部分内容，`null` 表示未知；正文达到长度阈值不自动等于完整。
- `media` 只保存公开 HTTP(S) 媒体地址，不下载付费或受限资源。
- `content_hash` 只覆盖标题、正文、摘要和 topics；采集运行、媒体和质量元数据变化不会让中游业务判断失效。

## 渠道映射

| 字段 | HTML | RSS/Atom | JSON API | 授权第三方 API/Feed |
| --- | --- | --- | --- | --- |
| `external_item_id` | JSON-LD `identifier` | GUID / entry ID | 配置的 `json_external_id_path` | provider 的稳定条目 ID |
| `updated_at` | `dateModified` / modified meta | entry updated | `json_updated_path` | 无可靠字段则 `null` |
| `media` | JSON-LD、OG、正文图片 | enclosure、media、正文图片 | `json_media_path` | provider 映射的公开媒体 |
| `quality` | 站点配置或 unknown | Feed 正文/摘要分别标记 | API 配置 full/partial | provider 配置 full/partial |

## 溯源信封

`crawl_run_id` 和 `page_snapshot_id` 不参与文章语义哈希，避免每次调度制造内容新版本。查询 API 通过当前 `RawItem` 关系返回：

```json
{
  "crawl_run_id": 130,
  "page_snapshot_id": 226
}
```

`page_snapshot_id` 可以为 `null`，但必须由明确的非 HTTP 输入或历史兼容原因解释，并在 `quality.validation_warnings` 标记；新建正式 HTTP/API 渠道不得默认省略快照。

## 身份与幂等

1. 已绑定 `external_item_id` 时优先匹配同来源外部 ID。
2. 兼容历史数据时，继续尝试旧 identity key 和规范化 canonical URL。
3. 找到历史条目后永久沿用原 `identity_key`，防止契约升级制造重复内容。
4. 完全相同的语义 payload 重跑返回 `skipped`；内容变化追加 `RawItem` 并更新最新投影。

## 历史兼容

当前数据库已经完成 `article.v1.1` 升级。一次性升级工具和离线归档导入器已迁入存档，不再作为正式运行入口；后续契约升级必须通过 Alembic、版本化重放和独立验收实现。
