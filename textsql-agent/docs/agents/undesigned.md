# 未设计 / 未接线

接手 Agent 在接真实模型、真实库、真实 Vanna，或补规格之前读本文件。

完成标准：下面每一行都能标成四个状态之一，不要把「替身已通」当成已经能查真实库。

| 状态 | 意思 |
|------|------|
| **契约已定** | 规格已写清接口和保险丝 |
| **替身已通** | 一轮测试用假数据走过；生产适配器没有 |
| **真实未接** | 契约和替身都有，缺真实系统 |
| **规格未定** | 连选型或具体值都没有；不要猜，先补规格或问人 |

假数据从哪来：`src/` 里已经没有假数（ADR 0002 已落地）。默认 `run_turn` 走 `UnwiredQueryPipeline`，回填直说「查数工具未接线」，不编数、不动程序槽。规划者是规则 `Planner`（几乎总会调查数工具）。RAG 是内存关键词 `InMemoryVanna`。业务词示踪弹（销售额 / 上个月 / 按城市 / 含税）连同 `KeywordSmallModel`、`StubQueryConversation`、`ScriptedQueryLlm`、`ScriptedExecutor` 一起搬进了 `tests/fakes.py`，只作测试替身。

**注意**：上一段说的是**默认入口**。四个真适配器已经写好了，只是没有默认接上去——
如果它们在，就别再看「未接线」那句话，见下面「适配器已经写好了」。

---

## 真实未接（契约已定 + 替身已通）

这些边界已经有 Protocol。真实实现写在适配器里，测试仍只走一轮缝 `run_turn`。

| 边界 | 代码 | 现在用的替身 | 规格要求的真实物 |
|------|------|----------------|------------------|
| 主 Agent 规划者 | `MainAgentBrain` · `brain.py` | `Planner(cues)`（if/else：先问口径才说话，否则调查数工具）；业务词经 `PlannerCues` 注入 | DeepSeek Harness 上的 ReAct 规划者；默认只挂「说话」和查数工具 |
| 小模型 | `SmallModel` · `rewrite.py` | `KeywordSmallModel`（在 `tests/fakes.py`；只认销售额/上个月/按城市/含税，**专有示踪弹**） | 无记忆改写模型；**通用的是两段契约，不是这套关键词**。完整前缀见 `textsql-rewrite-prompt.md` |
| Vanna RAG | `VannaRag` · `rag.py` | `InMemoryVanna`（关键词对照，不是向量库） | 框架内 train/retrieve；禁止 `generate_sql` 全链 |
| 查库大模型 | `QueryLlm` · `sql_session.py` | `ScriptedQueryLlm`、`StubQueryConversation`（都在 `tests/fakes.py`） | 查库对话里只调只读查库工具；提示词原则已定，原文未锁 |
| 只读库执行 | `ReadonlyExecutor` · `sql_session.py` | `ScriptedExecutor`（在 `tests/fakes.py`，测试排队返回） | 只读账号跑 SELECT；超时 30 秒算一次失败；`RowSet.matched_rows` 要给精确基数 |
| 联网搜索引擎 | `SearchEngine` · `web_search.py` | `RecordingSearchEngine` | 契约已定；**默认不挂载**，开启是后续工作 |

`ReadonlySqlQueryPipeline` 已能编排改写 → 检索一次 → 查库对话，但必须注入改写器（`Rewriter`）、查库大模型和执行器，三个都没默认值。默认入口没有注入，网页上就直说未接线。

### 适配器已经写好了（2026-09-17）

`src/textsql_agent/` 里已经有四个真实现，都只用标准库、都没有业务词。
但**默认入口仍然不接**——`run_turn` 不传就还是 `UnwiredQueryPipeline` + 规则 `Planner`：

| 边界 | 适配器（新） | 怎么接 |
|---|---|---|
| 主 Agent 规划者 | `DeepSeekPlanner` · `deepseek.py` | `run_turn(..., brain=DeepSeekPlanner(client))` |
| 小模型 | `DeepSeekSmallModel` · `deepseek.py` | `Rewriter(DeepSeekSmallModel(client))` |
| 查库大模型 | `DeepSeekQueryLlm` · `deepseek.py` | `ReadonlySqlQueryPipeline(query_llm=...)` |
| 只读库执行 | `SqliteReadonlyExecutor` · `sqlite_executor.py` | `ReadonlySqlQueryPipeline(executor=...)` |

四个适配器只依赖一个窄边界 `ChatClient`（`complete(prompt, *, json_mode)`），
所以测试拿替身接进真适配器，测的还是真解析和真执行，只是不碰网络
（`tests/test_wired_adapters.py`）。

`DeepSeekClient.from_env()` 从 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`、`OPENAI_BASE_URL`、
`DEEPSEEK_MODEL` 读，密钥不写死。默认 `deepseek-flash`，**默认关思考**——不关的话
token 全花在 reasoning 上、正文可能为空、延迟翻倍。

`SqliteReadonlyExecutor` 三重只读：`mode=ro` URI、SQL 必须以 SELECT/WITH 开头、
`sqlite3.execute` 只允许一条语句；另加禁写词表与 `set_progress_handler` 超时。

整条链在真库 470,399 行上跑通过。每一跳的实测与判据见
`.scratch/textsql-agent/observability.md`；跑通的同时暴露了四个未决问题，也在那一节。

---

## 小模型：契约通用，示踪弹专有

不要把 `KeywordSmallModel` 或工单 04 的销售额例子当成通用改写器去接生产。

| 层 | 通不通用 | 是什么 |
|----|----------|--------|
| 输入形状 | 通用 | 固定前缀 + 上次 SQL（可空）+ 查询摘要（可空）+ 当前问题；不加近几轮对话 |
| 输出形状 | 通用 | 恰好两段：改写问题、需求说明 |
| 规则 1–5 | 通用 | 空槽只看当前问题；有槽把当前问题当修改；拿不准就说拿不准，不编；需求说明写成一行；严格按格式 |
| 「需求说明」写什么 | 通用（自由） | 程序只原样存取，不解析、不按内容做分支（ADR 0002）；写法由模型自己定 |
| 口径举例「含税/未税」 | **专有** | 提示词把口径举成税务口径；别的业务可能是渠道、币种、类目，规格没定 |
| `KeywordSmallModel` | **专有** | 在 `tests/fakes.py`；写死「销售额」「上个月」「按城市分组」「含税/未税」；问订单数/客户数会改不出指标 |
| 工单 04 测试 | **示踪弹** | 只覆盖销售额叠「按城市」这一条路径 |
| 代码里的 `REWRITE_PREFIX` | 不完整 | 比 `textsql-rewrite-prompt.md` 少了那五条规则；接真模型应以前缀文件为准，不要用替身关键词表 |

接真小模型时：喂提示词文件里的固定前缀 + 每次三块输入；用真实模型补全两段。不要把 `KeywordSmallModel` 里的销售额规则搬进生产。段名已锁；需求说明的写法**规格未定**。

---

## 规格未定

规格 `Out of Scope` 或只写了原则、没有可执行细节。接手后先补规格，再写代码。

| 项 | 规格怎么说 | 代码现状 |
|----|------------|----------|
| 表白名单具体表 | 未定 | 没有表白名单 |
| LIMIT 具体值 | 未定；提示词只说「要 LIMIT」 | 没有给 SQL 补 LIMIT |
| 禁写规则 | 「再挡一层禁写语句」 | **SQLite 已挡**：SQL 必须以 SELECT/WITH 开头 + 禁写词表 + 单语句。真实库若要写权限收口，仍要按库再定 |
| 只读库连接 | 要 host/端口/库名/只读账号 | **SQLite 已解决**：`SqliteReadonlyExecutor` 收一个文件路径，`mode=ro` 打开。MySQL / Postgres 仍未定，也没有配置项 |
| Vanna 版本 | 0.x 与 2.0 未选；对接原则已定 | 没有依赖真正的 Vanna 包。**这是现在最大的一个缺口**：`InMemoryVanna` 是关键词替身，中文问句的 **DDL / 文档两路**材料**全有全无**（整份或全空，不是「必然全空」——2026-09-17 复核实测更正），三路全空时模型只能自己编表名；第三路「问题-SQL」是按条过滤的列表，`问法` ≥2 条时可出现**部分命中**（见 `observability.md` F3） |
| 训练语料正文 | 三类都要 train，正文未定 | **已落地到 L2**：`textsql-agent/config/blogger_ddl.sql`（表结构）、`blogger_docs.md`（文档与口径，含 114 个类目名）。问题-SQL 那一路随金标走，**金标问法还没填**，所以现在还是 0 字 |
| 多业务是否拆训练空间 | 未定 | 没有 |
| 三个模型分别用哪个 | 只出现「小模型 / 查库大模型 / DeepSeek Harness」 | **已定**：三个都走 `deepseek-flash`，经 `DeepSeekClient.from_env()`；密钥读环境变量，不写死 |
| 规划误判话术 | 该查走环 A / 该聊走环 B 的固定话术不规定 | `Planner` 把「你好」也会送去查数 |
| UI | 规格明确不做网页版裸 LLM、不做 React | `__main__.py` 只是本地演示页，不是产品 UI |
| 部署 / 多用户会话 | 未写 | 演示页一个进程一份会话 |
| 与 demo2 白名单架构的关系 | 不在本规格里 | 本仓库按「同学 Text-SQL」管线实现，不是「模型只选不造」 |

不准救活的方案仍以 `../.scratch/textsql-agent/spec.md` Implementation Decisions 为准，不要用规格未定当借口改架构。

---

## 销售额不是建库前提（已核对原文）

`CONTEXT.md` 里没有「销售额」「sales」「含税」。规格 Out of Scope 写明：表白名单具体表未定、训练语料正文未定。流程里「销售额对应哪一列」是 RAG 对照说明的举例。用户故事里的「上个月销售额」「那按城市呢」「含税/未税」是为了让一轮行为可观察，不是要先建一张 sales 表。

被误写成运行时前提的位置（接手不要当真实库结构抄）。ADR 0002 之后这些位置**都只在测试层**，`src/` 里一个都没有：

| 位置 | 写死了什么 |
|------|------------|
| `tests/fakes.py` 的 `SALES_CUES` | 口径问句固定答「含税成交额」；「查吧」只从上一句里抠「销售额」 |
| `tests/fakes.py` 的 `KeywordSmallModel` | 只认销售额 / 上个月 / 按城市 / 含税 |
| `tests/fakes.py` 的 `StubQueryConversation` | 固定 `FROM sales`，回填 `销售额 1280000` |
| `tests/fakes.py` 的 `SUMMARY_TEMPLATE` | 需求说明的自由文本在替身里长成「指标 …；时间 …」；产品不约束这个写法 |

测试夹具用销售额可以：工单 04 的验收句就是这条示踪弹。夹具 DDL ≠ 真实库。真实表、指标名、口径枚举仍 **规格未定**。

验收：`grep -rn "销售额\|含税\|按城市\|上个月\|博主\|美妆" textsql-agent/src/` 必须为空。

---

## 已定且不要重做

一轮缝、主 Agent 决策循环（环 A / 环 B 是它的两条去路）、查数工具最多一次、两槽成对覆盖、0 行算成功、查库对话 5 次/30 秒、回填截断 50 行或约 4000 字、联网搜索默认关闭。词以仓库外 `CONTEXT.md` 为准。

回填里那句「命中 N 条，回填 M 条」是采样说明的唯一来源：N 由只读执行器经 `RowSet.matched_rows` 给，与取行上限、截断都无关。执行器不填就退化成 `len(rows)`，所以真实执行器**必须**填。

截断值有个三处不一致，本版没动代码，先记着：本文件与现状代码是 50 行 / 约 4000 字，`.scratch/textsql-agent/flow-design.md` §4 写的是 `LIMIT 200` → 回填再截到 10 行。要以哪个为准需要先拍板。
