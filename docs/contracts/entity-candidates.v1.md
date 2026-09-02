# entity-candidates.v1

`entity-candidates.v1` 是 LLM 实体候选输出，不是实体主库写入许可。模型只能从有界 evidence 中提取原文逐字出现的候选；本地校验和消歧通过后，才投影为 `entity-mentions.v1`。

## 输出

```json
{
  "schema_version": "entity-candidates.v1",
  "items": [
    {
      "content_ref": "content:111",
      "input_content_hash": "sha256",
      "mentions": [
        {
          "surface": "Rabanne",
          "entity_type": "brand",
          "canonical_name_candidate": "Rabanne",
          "evidence_ref": "content:111@hash:title:0-70:digest",
          "confidence": 1.0
        }
      ]
    }
  ]
}
```

## 硬约束

- 单批最多 5 篇内容。
- 输入只包含标题、最多 1000 字符摘要和最多 2100 字符正文。
- `surface` 必须是 `evidence_ref` 对应文本的逐字子串；本地程序重新定位所有偏移。
- content 顺序、content hash、evidence ref、类型和重复候选均由本地校验。
- 模型不创建实体、不确认别名、不合并实体，也不补充原文没有的全称、母公司或关系。
- 已知别名唯一命中时才解析；无命中保持 `unresolved`，多命中保持 `ambiguous`。
- 缓存指纹包含输入、Prompt、Schema、校验器、模型和有界输入配置。
- canary 不执行模型输出修复调用；校验失败即停止，避免不可控的第二次积分消耗。

## 2026-08-30 canary

内容 34、35、38、93、111 以一次 Flash 请求处理。模型返回 49 个不同候选，本地展开为 64 个原文出现位置；全部偏移校验通过。3 个位置解析到既有实体，61 个保持未解析。人工抽查发现 `NEXT50 2026` 被勉强归为 product、`Science` 被归为 organization，说明下一版 Prompt/类型策略需要明确跳过无法落入实体类型的活动、栏目或出版物边界；这些候选没有自动创建实体，因此没有污染主库。

## 候选审查与晋升

未解析 LLM 提及按 `entity_type + normalized_surface` 分组为候选审查记录，初始状态固定为 `pending`。只有显式决定可以改变状态：

- `create`：创建新的实体主记录，并确认本次名称为别名。
- `link`：链接到类型一致的现有实体，并确认别名。
- `reject`：标记该候选不属于当前实体体系，不创建实体。

每个决定必须保存操作者、原因、时间、证据和受影响的 mention id。已决定记录不能改成另一种动作；相同决定重复执行必须幂等。抽取结果 JSON 保留模型当时的原始解析状态，`entity_mentions` 是经过审查后的当前解析投影。
