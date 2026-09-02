# `content-value-score.v1`

## 目的

该契约把领域内的 ContentItem 分为“全量池”和“精选池”，并保存可重放、可解释的确定性评分。它不替代领域归属、事件聚类、实体解析或 LLM 编辑结果，也不直接决定日报栏目和最终位置。

## 运行级字段

`content_value_score_runs` 冻结一次可重放的评分上下文：

| 字段 | 语义 |
|---|---|
| `domain_id` | 本次评分所属领域；同一内容可在不同领域独立评分 |
| `algorithm_version` | 确定性算法版本 |
| `schema_version` | 固定为 `content-value-score.v1` |
| `config` / `config_hash` | 完整版本化配置及规范化哈希 |
| `as_of` | 评分时点；时效性只读取该冻结值，不读取运行时钟 |
| `input_hash` | 领域、时点、配置和全部输入证据的联合指纹 |
| `status` | `running`、`succeeded` 或 `failed` |
| `input_count` / `selected_count` | 本次输入量和精选量 |

相同 `input_hash` 只允许一个运行；重复应用返回原运行，不追加结果。

## 逐篇字段

`content_value_scores` 每篇保存：

- `input_content_hash`：必须与当前 ContentItem 内容哈希一致，API 才将结果视为当前结果。
- `total_score`：0—100 的最终分。
- `component_scores`：每个信号的 `normalized`、`weight` 和 `points`。
- `penalties`：结构化扣分代码和分值。
- `gates`：禁止进入精选池的硬门槛代码。
- `decision`：`selected` 或 `full_pool`。
- `reasons`：发布时间龄、来源类型、完整性证据、事件成员/来源数、确认实体数和领域置信度等原始解释。

## v1 信号边界

| 信号 | 来源 | 边界 |
|---|---|---|
| 时效性 | `published_at` 与冻结 `as_of` | 缺发布时间得 0 且触发门槛；未来内容不进入运行 |
| 来源质量 | 配置化 `source_type` 映射 | 不按具体媒体名称硬编码 |
| 内容完整性 | `quality.body_complete`、正文字符覆盖、`access_level` | 正文长度只衡量可读证据是否完整，不代表重要性 |
| 跨来源佐证 | 当前活动 Event 的成员数和独立来源数 | 单篇事件得 0，不伪造佐证 |
| 已确认实体 | 当前内容哈希对应成功提及中的非空 `entity_id` 去重数 | pending/rejected 候选不参与；v1 因全量覆盖不一致保存信号但权重为 0 |
| 领域置信度 | 当前内容哈希对应 include assignment | 与其他领域独立评分 |

v1 硬门槛为 `missing_published_at` 和 `duplicate`；赞助内容使用配置化扣分。门槛只阻止进入精选池，事实内容仍保留在全量池。

## 输入指纹

`input_hash` 至少包含领域、冻结时点、配置哈希，以及逐篇的内容哈希、当前领域 assignment、发布时间、来源类型、访问级别、质量字段、赞助/重复状态、当前事件成员/来源统计和已确认实体数。任何影响分数的事实变化都会产生新运行，旧运行保持审计可查。

## 产品使用

`GET /api/v1/value-scores?domain_key=beauty&decision=selected` 默认读取该领域最新成功运行，并只返回仍匹配当前 ContentItem 哈希的结果。日报编排接入前仍使用既有业务契约；评分层不会在本版本中静默改变已发布日报。
