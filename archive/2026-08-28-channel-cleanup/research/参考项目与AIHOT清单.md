# 资讯平台参考项目与 AIHOT

## 一、核心参考项目

### 1. Infinitum

- 地址：[shawnxie94/infinitum](https://github.com/shawnxie94/infinitum)
- 协议：MIT
- 简介：基于 RSS 的资讯聚合工作台，包含 RSS 入库、去重、事件聚类、AI 摘要/分类和日报生成。
- 对我们的参考：重点借鉴“采集 → 统一内容 → 去重/聚类 → 摘要 → 日报”的内容处理链路。

### 2. TrendRadar

- 地址：[sansan0/TrendRadar](https://github.com/sansan0/TrendRadar)
- 协议：GPL-3.0
- 简介：聚合微博、小红书、抖音、知乎等热榜，支持关键词过滤、热度排序和消息推送。
- 对我们的参考：重点借鉴关键词规则、热点排序、热度计算和推送逻辑。

### 3. ForgeRSS

- 地址：[tmwgsicp/ForgeRSS](https://github.com/tmwgsicp/ForgeRSS)
- 协议：AGPL-3.0
- 简介：将没有 RSS 的网站转换为 RSS，支持多种抓取引擎、反爬处理、页面解析、去重缓存和定时更新。
- 对我们的参考：重点借鉴普通网页采集器和“网页 → Feed/统一条目”的处理方式。

### 4. WeWe RSS

- 地址：[cooderl/wewe-rss](https://github.com/cooderl/wewe-rss)
- 协议：MIT（项目已 archived）
- 简介：通过微信读书相关流程订阅微信公众号，并生成 RSS、Atom 或 JSON Feed。
- 对我们的参考：重点借鉴公众号订阅、账号管理、定时更新和 Feed 输出方式。

### 5. NewsNow

- 地址：[ourongxing/newsnow](https://github.com/ourongxing/newsnow)
- 协议：MIT
- 简介：针对知乎、微博、抖音、B站、百度等平台编写采集器，获取热榜并转换为统一数据结构。
- 对我们的参考：重点借鉴“每个平台一个独立适配器”，分别处理接口调用、网页解析、字段映射和异常情况。

## 二、产品参考

### AIHOT

- 网站：[aihot.virxact.com](https://aihot.virxact.com/)
- 简介：AI 资讯聚合产品，包含全部动态、48 小时热点榜、AI 日报、主题和收藏等功能。
- 对我们的参考：主要参考产品结构和页面组织方式。我们第一期将其替换为美妆行业资讯内容，并增加全量池、精选池和用户自定义日报。

## 三、当前形成的借鉴关系

| 平台环节 | 主要参考项目 | 借鉴内容 |
|---|---|---|
| RSS 获取 | Infinitum | RSS 解析、增量同步 |
| API / 热榜获取 | NewsNow | 独立适配器、字段映射、异常处理 |
| 普通网页获取 | ForgeRSS | 多引擎抓取、解析、缓存、去重 |
| 公众号获取 | WeWe RSS | 账号订阅、Feed 生成、定时更新 |
| 去重、聚类、摘要、日报 | Infinitum | 内容加工链路 |
| 热度、关键词、热点榜 | TrendRadar | 过滤规则、热度排序、推送 |
| 页面与产品形态 | AIHOT | 全部动态、热点榜、主题、收藏、日报 |
