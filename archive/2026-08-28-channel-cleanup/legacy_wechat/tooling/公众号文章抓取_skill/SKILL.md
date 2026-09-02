---
name: 公众号文章抓取
description: 公众号订阅源管理：功能1 按关键词/名称找出公众号并抓取文章链接制表（search / latest / batch，基于红狐广域库）；功能2 调用浏览器插件技能逐条校验已抓链接的有效性并产出修改后表格与人工补录清单；功能3 向飞书多维表批量写入/更新数据的操作规范（按 record_id 精确取数、小批量写入、写后逐行内容级核对）。
---

# 公众号文章抓取

本技能包含三个功能，按任务需要选用：

| 功能 | 能力 | 输入 | 输出 |
|------|------|------|------|
| 功能 1 | 关键词 → 公众号 → 文章 URL → 制表 | 关键词/公众号名单 | `output/` 下带日期的 Markdown 表格 |
| 功能 2 | 调用浏览器插件校验 URL → 制表 | 功能 1 产出的表格 | 修改后的表格 + 需人工补充文章链接的公众号清单 |
| 功能 3 | 飞书多维表批量写入规范 | 数据源文件 + 目标表 | 写入并核对后的飞书多维表 |

---

## 功能 1：关键词 → 公众号 → 文章 URL → 制表

按「关键词 → 找出公众号 → 抓取文章链接」的自动化工具，基于红狐广域库。

### 前置：API Key

三种配置任选其一：

| 方式 | 命令 |
|------|------|
| 环境变量（推荐） | `export REDFOX_API_KEY=ak_你的密钥` |
| 命令行参数 | 命令追加 `--api-key ak_你的密钥` |
| 配置文件 | `echo '{"api_key":"ak_你的密钥"}' > ~/.qoder/apis/redfox.json` |

Key 获取：https://redfox.hk/settings/api-keys

### 步骤 1：按名称搜公众号

```bash
python3 "$SKILL_PATH/assets/subscribe.py" search "且初"
python3 "$SKILL_PATH/assets/subscribe.py" search "且初" --limit 10   # 最多展示条数（默认 10）
```

返回：account（公众号标识）、wxId（`gh_` 开头）、bizInfo（biz 编码）、微信认证信息。

### 步骤 2：一键取某公众号的一篇文章

名称或 ID 均可，内部自动完成「名称 → searchUser 搜出 ID → queryWorkList 抓文章 → 取文」：

```bash
python3 "$SKILL_PATH/assets/subscribe.py" latest "KIMTRUE且初"              # 按名称
python3 "$SKILL_PATH/assets/subscribe.py" latest "kimtrue66"                # 按账号 ID
python3 "$SKILL_PATH/assets/subscribe.py" latest "KIMTRUE且初" --json       # JSON 行输出，适合批量回填
python3 "$SKILL_PATH/assets/subscribe.py" latest "KIMTRUE且初" --days 30    # 回溯天数（默认 30）
```

输出：文章标题、发布时间、原文 URL（红狐返回的 mp.weixin.qq.com 长链）。

**选文规则**（latest / batch 共用）：拉近 30 天文章按发布时间倒序 → 过滤广告/促销标题（广告/福利/抽奖/上新/直播/大促等营销词）→ 取过滤后**第 2 篇**；仅剩 1 篇取该篇；全部命中广告词则退回最新一篇。

### 步骤 3：批量抓取 Markdown 表格

```bash
python3 "$SKILL_PATH/assets/subscribe.py" batch --file 公众号名单.txt                          # 名单文件，每行一个名称
python3 "$SKILL_PATH/assets/subscribe.py" batch "KIMTRUE且初" "且初" "十点读书"                # 直接跟多个名称
python3 "$SKILL_PATH/assets/subscribe.py" batch --file 名单.txt --out output/公众号文章列表_20260813.md --group 20  # 自定义输出/每组数量
```

自动每 20 个一组分批请求，输出 4 列 Markdown 表格，查不到的账号标注原因、不混入结果：

```markdown
| 关键词 | 公众号 | 文章标题 | 文章链接 |
| --- | --- | --- | --- |
| KIMTRUE且初 | KIMTRUE且初 | 标题示例 | https://mp.weixin.qq.com/s?... |
| 查不到 | - | - | ⚠ 红狐广域库未收录或暂无文章数据 |
```

### 输出规范

- 表格输出到 **`output/`** 目录，命名带日期，如 `output/公众号文章列表_20260813.md`（对齐现有 `output/新增公众号列表_20260811.md`、`output/补充公众号列表_20260812.md` 风格）。
- 抓到链接的表里放链接；抓不到的在表里说明（红狐未收录/暂无文章数据），**不混入结果行**。
- 职责边界：功能 1 只做到制表为止，不做链接校验。

---

## 功能 2：调用浏览器插件校验 URL → 制表

用 **browser-agent-collaboration**（浏览器协同）技能逐条打开表格中的链接，判断有效性，再按实际情况修改表格。浏览器插件技能本身不动，本功能只规定「怎么调用它」。

### 输入

功能 1 产出的表格（公众号 + 文章链接）。原表里已经是说明文本的行（未抓到链接）**跳过不校验**。

### 调用方式

按 `skills/browser-agent-collaboration/SKILL.md` 的授权与命令流程执行：

1. `auth status` 确认授权可用（不显示密钥；无授权时按该技能流程取得授权）。
2. `status` 确认浏览器在线。
3. 逐条链接：`open '<url>' --group-title '公众号链接校验'` → `wait-page` → `extract --tab-id <tabId>` 读取页面判断状态。
4. 默认后台打开、设置清楚的标签组标题；优先 `extract` 拿正文，不先截图。

### 校验规则

| 打开后情况 | 处理 |
|---|---|
| 文章正常可读（有标题+正文） | 链接有效，**保留** |
| 反爬/「环境异常」 | 自动点击「去验证」（`click-text` 或 JS 点击）；通过则**保留**，仍失败则**替换为校验异常说明** |
| 内容已被删除（「该内容已被发布者删除」等） | 单元格**替换为校验异常说明** |
| 公众号已迁移（「公众号已迁移」等） | 单元格**替换为校验异常说明** |
| 打开失败/超时/空页面 | 重试 1 次；仍失败则**替换为校验异常说明** |
| 原表无链接（未抓到） | 不校验，直接进人工清单 |

校验异常说明格式：`校验异常：<具体情况>`，例如 `校验异常：该内容已被发布者删除`。

### 输出

1. **修改后的表**：输出新文件（保留原始表可追溯），命名 `output/公众号文章列表_YYYYMMDD_校验后.md`；异常链接的单元格替换为校验异常说明，有效链接原样保留。
2. **需人工补充文章链接的公众号清单**：= 没抓到链接的 + 链接异常的公众号，**只列公众号名，不列原因**，格式：

   > 以下公众号因 API 未收录或链接异常，需人工补充文章链接：xxx、xxx、xxx

### 已知坑

- 微信反爬「环境异常」是偶发拦截，重导航/点击「去验证」通常即通过（8/13 实测 8/87 触发、全部点击通过）；若连续失败或出现滑块，标记为校验异常，不无限重试。
- 批量校验频率过高会触发风控（实测刻意高频 170 跳 + 并发 reload 才触发），正常逐个访问间隔 1-2 秒基本不触发。
- 校验结果是"时点有效"，报告应含校验日期。

---

## 功能 3：飞书多维表批量写入规范

用户要求把一批数据写入/更新飞书多维表格时使用（例如把校验后的有效链接填回表格、批量更新字段值）。

### 核心规则

1. **以数据源文件为准，不凭记忆**：写入内容必须来自本轮权威数据（output/、temp/ 中的文件或工具结果），禁止从截断上下文、记忆或印象拼接 URL/字段值。
2. **按 record_id 精确取数**：先生成 record_id → 目标值的映射文件，写入时逐条取用，不整段复制长清单。
3. **小批量写入**：每批 ≤10 条并行，避免超时与 1254291 并发冲突；同表写入串行分批。
4. **写入后必须内容级核对**：重新读表，逐行对比「表格实际值 vs 数据源值」（链接要比对 __biz/mid/sn 等关键参数），不只检查"有没有值/非空"。
5. **写入前先确认字段可写**：只更新存储字段（text/select/url 等），公式、lookup、system 等只读字段会返回 ignored_fields。

### 标准流程

#### Step 1: 解析 base_token
wiki/分享链接不能直接作为 `--base-token`，先解析：

```
lark-cli base +url-resolve --url "<链接>"
```

#### Step 2: 读取表格与 record_id

```
lark-cli base +record-list --base-token <token> --table-id <table_id> --view-id <view_id> --limit <n> --offset <m>
```

- 返回结构：`.data.data` 是行数组，`.data.record_id_list` 是与行平行的 record_id 数组，`.data.field_id_list` 对应列序。
- 用 jq 配对提取：`[range(0;(.data.data|length)) as $i | {id:.data.record_id_list[$i], name:.data.data[$i][0], link:.data.data[$i][4]}]`

#### Step 3: 生成 record_id → 目标值映射文件
用脚本（非手工）从数据源文件生成 `temp/<name>.json` 或 jsonl，每行含 record_id 和要写入的值。

#### Step 4: 批量写入

```
lark-cli base +record-upsert --base-token <token> --table-id <table_id> --record-id <record_id> --json '{"字段ID或字段名": "值"}'
```

- 带 `--record-id` 是 PATCH 更新；不带是新建。
- 每批 ≤10 条并行；从映射文件逐条精确取 URL，不要从截断输出中拼接。
- 写操作先 dryRun 预览，确认后执行。

#### Step 5: 内容级核对

重新 record-list 读取，逐行对比表格实际值 vs 数据源值：
- 比对完整字段值（链接要比对 __biz/mid/sn 等关键参数），不是只查非空；
- 发现不一致立即报告并定位是哪一批/哪一行。

### 检查清单

- [ ] base_token 已通过 url-resolve 解析
- [ ] record_id 与行数据对齐正确（record_id_list 平行于 data）
- [ ] 写入值全部来自数据源文件，无凭记忆拼接
- [ ] 分批 ≤10 条，写入成功（updated:true）
- [ ] 写后逐行内容级核对通过，无遗漏不一致

### 已知坑

- **长清单截断**：大批量 URL/长文本不要一次性读入上下文再凭印象写，必须生成映射文件按 record_id 取数（2026-08 公众号 87 行写回事故：batch8 最后 6 行链接因此写错，`__biz/mid/sn` 与数据源不一致）。
- **只验证非空 ≠ 内容正确**：87 行写回后曾只验证"87/87 有链接"而漏过 6 行内容错误，必须逐行比对值。
- **--json 传参**：+record-upsert 的 --json 走 typed data 字段，不要放进 argv。
- **1254291 并发冲突**：同表批量写入避免高并发，每批 ≤10 并行较稳。
- 写入使用的 lark-cli 官方指南：lark-base skill（SKILL.md + references/）。

---

## 边界

- **未收录**：红狐广域库查不到或无文章数据时输出 error（3203），可联系 redfoxdata@proton.me 定制收录。
- **链接性质**：红狐返回的是带 `__biz` 参数的长链（有效）；确认正文需用浏览器协同校验（功能 2）或微信打开后复制的短链（`mp.weixin.qq.com/s/xxx`，可读全文）。
- **浏览器插件技能**：本技能只规定调用方式，不修改 browser-agent-collaboration 本身（它是项目内多个流程共用的能力）。
- **人工补录**：校验异常与未收录的公众号由人工补录文章链接，技能不自动生成链接。

## 依赖

```bash
pip3 install requests
```
