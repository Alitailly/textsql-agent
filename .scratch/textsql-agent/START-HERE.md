# 从这里继续

上下文已被清空过。这份文件是唯一入口，其余都是被它指向的细节。

---

## 一、按这个顺序读

| # | 文件 | 读它是为了知道 |
|---|---|---|
| 1 | `CONTEXT.md`（交接包根目录） | **词源锚**。所有文档都写「词以 CONTEXT.md 为准」，改词条要配 ADR |
| 2 | `docs/adr/0002-free-text-summary-and-clean-src.md` | 摘要回到自由文本 + 业务词不进 `src/` + L1/L2/L3 三层稳定性。含**修改清单**（7+5 条）和**可执行验收命令** |
| 3 | `.scratch/textsql-agent/flow-design.md` | **主设计文档**，十节。博主场景的 B6 展开（解析实体 → 富化 → 组装 → 未命中记录）、字段取舍、数据缺陷清单 |
| 4 | `.scratch/textsql-agent/data-profile.md` | 真实库画像（365 行）。所有阈值的原始依据 |
| 5 | `textsql-agent-flow.md`、`spec.md`、`undesigned.md` | 总流程、规格、未设计项。**动手前扫一眼 `spec.md` 的「不准救活」清单** |
| 6 | `.scratch/textsql-agent/observability.md` | **怎么判「这条 SQL 在回答那句话」**：可观察量长在 B 环内、判据、脚本用法、接真组件后的实测发现（F1–F7，F1／F6／F7 已修） |

## 二、场景一句话

库里存小红书博主（昵称/小红书号/粉丝/类目/邮箱），面向"用户找博主"。**先查库锁定确切身份，再联网富化补充动态，最后由主 Agent 合成。** 价值主张＝跟通用网页版拉开差距：**库提供不可幻觉的锚点，搜索提供时效性，主 Agent 提供合成。** 富化全部收在查数工具内部，主 Agent 契约一字不改。

## 三、实现到了哪（**别把设计当现状**）

设计文档写得很满，但代码只兑现了一部分。下面是 2026-09-17 逐条读源码 + 真库实测过的边界，
接手前先认这个，不要按 `flow-design.md` 的完整形态去理解现状。

**分流机制是真的。** 环别不是哪个字段声明的，是**从计数器涌现**的：`query_tool_call_count > 0` 就是环 B。
互斥靠 `turn.py:145`（查库前看搜索/查库是否已发生）和 `turn.py:169`（搜索前看查库/熔断）两条规则。
拒绝的方式**不是 break 整轮**，而是把那件工具从「规划者看得见的工具集」里摘掉（`refused_tools`，`turn.py:124,146,170`），
循环继续，让主 Agent 用手上已有的回填说话——这是 F1 的修法，别再改回去。循环上限 `_TURN_DECISION_CAP` = 3 + 3 = 6。

**B1–B5 是真组件，B4 是替身。** B2 = `DeepSeekPlanner`；B3 = `Rewriter` + `DeepSeekSmallModel`（两段契约：`改写问题` 强字段 + `需求说明` 自由文本）；
B4 = `InMemoryVanna`（**关键词替身，见下**）；B5 = `SqlSession` + `DeepSeekQueryLlm` + `SqliteReadonlyExecutor`（真只读，熔断 5 次 / 30 秒超时）。

**B6 只做了一半——这是最容易误判的地方。** B6.1 判结果 + 回填能跑，但：

| 步 | 字段 | 现状 |
|---|---|---|
| B6.2 解析实体 | `entities` | 恒 `()` |
| B6.3 富化 | `enrichments` / `enrichment_call_count` | 恒 `()` / 恒 `0` |
| B6.4 按来源组装 | —— | 没有 |
| B6.5 未命中记录 | `miss_logged` | 恒 `False` |

这四个字段在 `QueryToolOutcome` / `TurnResult` 里**声明了、管道也通了，但从没被赋值**：
`query_tool.py:115-133` 构造 `QueryToolOutcome` 时根本没传它们，`turn.py:217-220` 把恒为默认值的空值原样透传。
docstring 自己写着「本轮不实现，先留可观察位」。
⇒ **环 B 今天回填的是「一行行文本」，不是「一组实体」。** 中间那三步（解析→富化→组装）还是空的。

**RAG 没有做好：是子串匹配，不是检索**（`rag.py`）：

- `train()` 把整份 DDL 当**一个字符串**、整份文档当**一个字符串**存。`_shares()` 只问「有没有任何一个词出现在这个字符串里」，
  所以**一个词偶然命中就把整份文档全返回**（实测返回 3287 字 == `wc -m config/blogger_docs.md`）。没有分块、没有 embedding、
  没有向量库、没有排序、没有 top-k、没有阈值；Vanna 连依赖都不是（`pyproject.toml` 的 `dependencies = []`）。
- 中文断词按 `_PARTICLE = [的是在与及了和]` 切开留 `len >= 2` 的片段做子串匹配，**切出来的片段基本不是词** →
  命中落不到「刚好那一段」上，**只会落到两端**。**这是 F3 的根因，不是模型的问题。**
- 更具体地说：**DDL 与文档这两路的命中是全有全无**，而且是结构性的——整份 DDL、整份文档各只当**一条**训练项
  （两路各 `train()` 一次；`__main__.py:231-234` 与 `textsql_harness.py:175-176` 都是分两次调，一次 ddl、一次文档），
  所以这两路只能是「整份」或「空」。
  **两组共 31 句实测**（13 句 + 18 句，问法集不同，`问法` 均为空）**没有一句返回「部分」**：
  有的返回整份（看着"三路都有料"其实是假的全中），有的三路全空。
  **哪一端多随问法集变（7/6、8/10 都出现过），别按「哪边是多数」来设计。**
  （旧记录写的「中文问句几乎必然三路全空」是**错的**——那是拿单个 0/0/0 观察当通例，已于 2026-09-17 复核实测纠正。）
- 第三路「问题-SQL」**和上面两路不同**：它是**按条过滤的列表**，现在**恒为 0** 只因 `blogger.yml` 的 `问法: []`
  ——**是材料缺口，不是算法缺口**；**一填进 ≥2 条就会出现部分命中**（复核已构造出反例）。
- **边界是对的**：`VannaRag` Protocol + 注入口都在，换真 Vanna 是**接线不是重构**。缺的是：Vanna 版本没选（0.x vs 2.0）、
  没有向量依赖、没有 `问法`、**分块与「取前 k 条」策略没定**（现在整份当一条 train 把这个设计问题掩盖住了）。

**环 A 的联网搜索没有实现。** 分流、熔断（上限 3 次）、`WebSearch` 包装（15 秒超时 / 5 条 / 摘要 300 字）、
`fillback_text` 断丝回填都是真的，但 `SearchEngine` **只是 Protocol，`src/` 里零实现**
（全仓库唯一实现是 `tests/test_web_search_disabled.py` 的 `RecordingSearchEngine` 替身），
且 `run_turn` 的 `web_search_enabled` 默认 `False`，演示页也没开。纯对话（主 Agent 直接 Speak）是通的。

**已经在代码里强制的纪律**（不是文档口号）：`run_turn` 是唯一测试缝、签名锁定；
程序槽**成对覆盖**——只有查库成功才把 `last_sql` + `query_summary` 一起写，失败就原样返回（`query_tool.py:116-122`）。

## 四、卡在哪

1. **3–10 个真实问法** —— 只有你能给。`textsql-agent/config/blogger.yml` 的 `问法` 栏现在空着，
   用 `tools/check_question.py` 体检一句再填一句。**这条不能由我编**
   （原来那个销售示踪弹就是为了可观察而编、结果被当成真实库结构）。
2. **四个未决问题** —— 见 `observability.md` 第三节：`SELECT *` 绕过排除列（F2）、
   中文问句的 DDL / 文档两路材料全有全无、整份或全空，第三路可部分命中（F3）、答案类型没有判据（F4）、两个诊断脚本的打码会漏（F5）。
   **F1 空回复已于 2026-09-17 修掉**（拒绝第二次调工具时不再闭麦，改让主 Agent 用已有回填说话；
   兜底话术有 `reply_is_fallback` 标记，判据同步补了「主 Agent 说了话」）。
   **F6 `名字核对` 把字符串字面量当"编的名字"也已修**（一条对的 SQL 曾被判 FAIL；
   现在先剥注释和单引号字面量再抽标识符，自检补了「真编名照样抓」防止修成永远通过）。
   全角 `＠` 打码同样已补。
   **F7 演示页的坏路径静默断连也已修**（`--db` 指到不存在的库时，服务照起、页面照 200、
   还宣称"真组件已接线"，然后每次查询客户端收不到任何响应；现在起服务前先验文件，
   畸形请求回 400 JSON、轮次炸了回 500 JSON。**执行器连接失败仍抛而不返回 `SqlError`，
   靠 HTTP 层兜着——这条没修**）。

**网页**：`uv run --python 3.12 python -m textsql_agent` 起本地演示页（`127.0.0.1:8765`）。
不给参数 = 未接线版（规则规划者 + 假工具，页面直说「未接线」）；给
`--db <库> --ddl config/blogger_ddl.sql --docs config/blogger_docs.md` = 真管线，
每轮把当前问题 / 改写问题 / SQL / 命中基数 / 回填都摆出来。判定（✓/✗）不在页面上——
那住在 `.scratch` 的夹具里，`src/` 不该知道「什么叫对」。

3. **三个有出入但没动的数值 / 行为**（都摆着等拍板，没改）：
   - **回填截断值三处不一致**：`undesigned.md:95` 与**现状代码**都写 50 行 / 4000 字
     （`sql_session.py` 的 `MAX_RESULT_ROWS` / `MAX_RESULT_CHARS`），`flow-design.md:166`
     却写 `LIMIT 200`→截到 10 行。本次按现状代码落地，`MAX_RESULT_ROWS` 没动。
   - **`--ddl` / `--docs` 在不给 `--db` 时被静默忽略**：服务照起、走未接线版、
     参数无效也不报错。这是复核子代理给出 PASS **之后**才提的观察，
     改了就作废刚拿到的 PASS，所以故意没改（纪律见 memory 里的
     `feedback_verification_loop_discipline.md` 第 4 条）。
   - **执行器连接失败仍抛而不返回 `SqlError`**（`sqlite_executor.py:57` 的
     `sqlite3.connect` 在 `try` 块外）——现在靠 HTTP 层的 try/except 兜着，
     根因没修。

已拍板、不用再问的：**未命中日志放独立 SQLite 文件**；**D1 港澳台先不修**，
钉成 0 命中回归基线（它是全库唯一真实的 0 命中用例，正好测 B6.5）。

## 五、数据状况速查

库：`tools/xhs_pgy.sqlite3`（224MB，从一份 MySQL 8.0 dump 转换而来）。**该库未随仓库分发**（真实数据且超出 GitHub 单文件上限），要跑真管线须自备同结构的库，见 `README.md`。

**可用**：470,399 行、`xhs_id` 零重复、关联表零悬空外键、无丢行。

**三条高危**（必须程序侧兜底）：邮箱需归一化（581 个含全角符号）／无类目 126,676 人且库内无补救路径／昵称不可作身份键（11,623 种重名）。

**身份键 = `xhs_id`（小红书号），不是 `id`、不是 `nickname`**。`id` 是粉丝降序的排序序号。

**细节全在** flow-design 第十节（D1–D7 + 严重度表）——不要在这里重复判断。

## 六、验证命令

```bash
# 库在哪、有多少行
cd .scratch/textsql-agent/tools
python3 validate_data.py xhs_pgy.sqlite3    # 只读。注意：只挡「干净的单值邮箱」，
                                            # 内部带空格/两个 @ 的值会原样输出（F5，见 observability.md 第三节）

# 判据本身对不对（退化解，不调模型、不需要 key）
uv run --with pyyaml --python 3.12 python run_eval.py xhs_pgy.sqlite3 --self-test

# 体检一句问法：每一跳 + 类目对账 + 已知坑
uv run --with pyyaml --python 3.12 python check_question.py xhs_pgy.sqlite3 "一句问法"

# 跑金标（config 里没填问法时会明说，不会自己编）
uv run --with pyyaml --python 3.12 python run_eval.py xhs_pgy.sqlite3

# ADR 0002 的验收：改完 src/ 后这条应为空
cd ../../..            # 回到交接包根目录
grep -rn "销售额\|含税\|按城市\|上个月\|博主\|美妆" textsql-agent/src/
```

代码验证：

```bash
cd textsql-agent
uv run --with pytest --python 3.12 python -m pytest -q
uv run --with mypy --python 3.12 mypy
```

## 七、处理这批数据的纪律

- 174MB 的 `.sql` **绝不能用 Read 读**，只能流式走脚本。
- 分析时打码：邮箱 → `a***@domain`，长数字/hex → 前 3 后 2；**昵称替换为合成名**（保留 emoji / 中英混写等形态特征，形态影响设计）；聚合统计不打码。
- 产品形态里邮箱**明文回填给用户**（用户拿它去联系），但**绝不进外部搜索词**、日志里打码。
- **不要从演示场景（销售额）里借字段名或问法。** 交接包专门写了「销售额不是建库前提」一节警告这一点。
