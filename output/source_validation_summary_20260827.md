# 33 个来源验证总结（2026-08-27）

## 结论

- 当前已打通：23 个。
- 当前受站点防护或维护影响：5 个。
- 按 robots.txt 或站点条款停爬：3 个。
- 站点关闭或持续不可用：2 个。

“当前已打通”表示本次真实请求成功获取并解析了文章、新闻或报告，尚不等同于经过多日运行验证的生产稳定。

## 当前已打通（23）

英文来源：BeautyMatter、Glossy Beauty、WWD Beauty、Vogue Business Beauty、Cosmetics Business、The Business of Fashion Beauty、Allure Beauty Industry、NewBeauty、The Klog、Global Cosmetics News、Cosmetic Executive Women。

中文来源：化妆品报、C2CC传媒、聚美丽、青眼、品观、CBNData、艾瑞咨询、魔镜市场情报、亿邦动力、36氪、界面新闻·消费、第一财经。

其中艾瑞咨询采用合规的元数据降级：公开报告标题、摘要、日期和详情链接进入统一库，`content_type` 为 `report`；不尝试下载登录或授权后的 PDF。

## 当前受防护或维护影响（5）

| 来源 | 当前证据 | 后续策略 |
| --- | --- | --- |
| Beauty Independent | 首页返回 403 挑战 | 浏览器适配候选，不绕过登录或付费限制 |
| Premium Beauty News | 正确英文入口当前为维护页面，未发现文章 URL | 定时复测站点恢复情况 |
| Beauty Packaging | robots 请求返回 403/防护页 | 浏览器适配候选，先验证 robots 可读性 |
| HAPPI | robots 请求返回 403/防护页 | 浏览器适配候选，先验证 robots 可读性 |
| Cosmetics & Toiletries | robots 请求返回 403/防护页 | 浏览器适配候选，先验证 robots 可读性 |

## 按规则停爬（3）

- Byrdie News：站点声明禁止未经许可的自动抓取、数据挖掘和创建数据集。
- 未来迹：`robots.txt` 对通用用户代理禁止全站抓取。
- 蝉妈妈：`robots.txt` 禁止报告、热点和详情正文路径；不抓正文。

## 关闭或持续不可用（2）

- CosmeticsDesign：发布方已关闭站点，当前入口返回 410，只保留历史来源记录。
- 刀法研究所：主页与 robots 入口持续超时，等待恢复后再验证。

## 统一入库 JSON：`article.v1`

采集层只负责文章、新闻、报告等内容形式的发现、解析和入库。`language` 与 `source_region` 区分中英文来源；行业相关性不写回原始内容，而由中游结果单独保存。

```json
{
  "schema_version": "article.v1",
  "source_id": 27,
  "source_name": "艾瑞咨询",
  "source_region": "CN",
  "source_type": "research_media",
  "language": "zh-CN",
  "access_level": "partial",
  "content_type": "report",
  "title": "iR-2026年第15周-美妆行业周度市场观察",
  "original_url": "https://www.iresearch.com.cn/report/detail?id=804",
  "canonical_url": "https://www.iresearch.com.cn/report/detail?id=804",
  "author": null,
  "published_at": "2026-04-06T00:00:00Z",
  "captured_at": "2026-08-27T05:20:08Z",
  "excerpt": "公开摘要……",
  "body_text": "公开摘要或正文……",
  "topics": [],
  "is_sponsored": false,
  "is_roundup": false,
  "content_hash": "sha256"
}
```

## 中游筛选 JSON：`industry-rules.v1`

```json
{
  "content_item_id": 32,
  "processor_name": "industry_rules",
  "processor_version": "industry-rules.v1",
  "is_relevant": true,
  "matched_topics": ["彩妆", "美妆"],
  "matched_events": [],
  "reason": "rule_match",
  "processed_at": "2026-08-27T05:19:03Z"
}
```

## 本轮数据与验证

- 统一内容库：36 条；英文 23、中文 13。
- 内容形式：`article` 28、`news` 7、`report` 1。
- 中游结果：相关 29、不相关 7；不相关内容仍保留在原始层和统一内容库。
- Ruff 检查通过，18 项自动测试通过。
- 原始全量状态报告：`site_crawl_report_20260827_132008.md` 与同名 JSON。
