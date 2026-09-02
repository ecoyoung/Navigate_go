# entity-mentions.v1

`entity-mentions.v1` 描述一篇标准内容中可追溯的实体提及。实体系统位于领域归属之后、事件聚类之前；平台内核不包含美妆专用实体表。

## 实体类型

- `organization`
- `brand`
- `person`
- `product`
- `location`
- `substance`
- `regulation`
- `technology`

无法可靠判断类型的提及可在加工结果中使用 `unknown`，但 `unknown` 不能创建实体主记录。

## 输出结构

```json
{
  "schema_version": "entity-mentions.v1",
  "content_ref": "content:111",
  "input_content_hash": "sha256",
  "mentions": [
    {
      "mention_id": 1,
      "entity_id": 2,
      "entity_type": "brand",
      "surface": "Rabanne",
      "field": "title",
      "start_offset": 24,
      "end_offset": 31,
      "evidence_text": "TNT Group partners with Rabanne...",
      "confidence": 1.0,
      "resolution_status": "resolved"
    }
  ]
}
```

## 证据与偏移

- `field` 首版只能来自 `title`、`excerpt` 或 `body`。
- `start_offset` 含起点，`end_offset` 不含终点；对原字段切片必须逐字等于 `surface`。
- `evidence_text` 是包含该提及的有限上下文，不能用模型背景知识代替。
- 相同内容哈希、提取器版本和配置哈希只产生一个成功处理结果。
- 内容或配置变化时追加新的处理结果和提及，旧结果保留；当前查询只读取与 ContentItem 当前内容哈希匹配的成功结果。

## 解析规则

1. 配置种子使用稳定 `registry_key`，而不是依赖数据库自增 ID。
2. 唯一匹配的已确认别名可以自动链接实体。
3. 一个表面名称命中多个实体时，提及保持 `ambiguous`、`entity_id=null`，候选写入 `entity_resolution_candidates`。
4. 不得仅根据字符串相似度自动合并品牌、公司、人物或产品。
5. LLM 后续只生成带证据的候选；实体创建、别名确认与合并仍经过确定性校验或人工决定。
