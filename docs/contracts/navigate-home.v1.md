# `navigate-home.v1`

`navigate-home.v1` 是中文资讯首页的只读展示契约。它由当前领域精选结果、事件关系和已校验中文编辑工件导出，不反向修改数据库。

## 核心约束

- `stories[].title_zh` 与 `stories[].summary_zh` 是唯一前台文案字段，必须包含中文。
- `original_language` 只记录来源语言，不允许前端以此字段回退展示英文标题或摘要。
- 非中文内容缺少与当前 `content_hash` 匹配的 `content_editorial.zh.v2` 工件时，导出器必须省略该内容并计入 `counts.omitted_untranslated`。
- `ranking_score` 是内部排序数据，前端不得默认展示。
- `source_name`、`published_at`、`url` 和 `tags` 是读者可见的来源与内容元数据。
- `multi_source_events[].members[]` 同样只提供 `title_zh`；缺少中文展示文案的成员不得回退到原文。

## 顶层字段

- `schema_version`: 固定为 `navigate-home.v1`。
- `generated_at`: 快照生成时间。
- `domain`: 当前领域标识与中文名称。
- `edition`: 当前日报导语与覆盖信息。
- `counts`: 内部数量口径，包括 `selected`、`displayed` 与 `omitted_untranslated`。
- `stories`: 可直接展示的中文精选内容。
- `multi_source_events`: 可直接展示的中文跨来源事件。
