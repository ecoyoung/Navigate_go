# `reader-surface.v1`

读者工作台的展示契约。数据由 FastAPI 现读数据库生成，不再导出静态 `home.json`。

## 卡片

`TopicFeedItem` 是探索、主题流、为你精选和内容详情的统一形状：

- `title` / `excerpt` / `paragraphs`：优先来自合格的 `content_editorial_zh`。
- 没有当前 schema/validator 的编辑件时，才使用清洗后的抽取式摘要。
- `url` 是原文次入口，不能当作列表主点击。
- `quality_tier` 只用于内部过滤，界面不展示算法说明。
- 非中文原文必须先有中文卡片才能进入精选/简报主文案；不允许把英文标题摘要直接给中文读者。

## 简报

`GET /api/v1/topics/{topic_id}/daily-reports/{coverage_date}`：

- 选稿：该主题 `TopicMatch.include` 且发布日落在覆盖日。
- 标题、摘要、栏目标签复用卡片，不在 GET 上调用 LLM。
- 报头 `daily_lead.text` 最多覆盖 3 条头条的第一句。
- 栏目只能是人事、融资、监管、产品、要闻，不能用公司名或主题词当栏。

## 事件

今日事件只返回 `member_count >= 2` 且成员可读的跨来源聚类。单篇事件留在资讯流。

## 已退役

`navigate-home.v1` 静态快照不再生成，也不再作为前端数据源。
