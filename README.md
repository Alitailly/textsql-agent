# textsql-agent

把一句中文口语问题，变成一条能跑的 SQL，再把结果讲回给用户。

一句话设计：**主 Agent 在一个决策循环里转圈——思考、行动、观察、再思考——直到它认为材料够回答，就说话。**

---

## 设计思路

### 1. 主 Agent 是一个决策循环，不是一次性分流

每一圈它只问一个问题：**「手上这些材料，够不够回答用户？」**

```text
用户说话
    ↓
主 Agent 决策循环（思考 → 行动 → 观察 → 再思考）
    │
    ├─ 够回答 ─→ 说话 ──────────→ 回复用户（本轮结束）
    │
    ├─ 要库里的确切事实 ─→ 调查数工具（每轮最多 1 次）
    │        ↓ 观察回填（有行 / 没查到 / 报错）
    │        └────────────────→ 回到「思考」
    │
    └─ 拿到库内容后判断不够 ─→ 联网搜索（默认关闭）
             ↓ 观察搜索结果
             └────────────────→ 回到「思考」
```

一轮（用户的一句话）里可以转好几圈，中途可以换工具。主 Agent 手上只有三个动作：**说话**、**调查数工具**、**联网搜索**。

工具被拒绝时**不打断整轮**——程序把那个工具从它能看见的工具集里摘掉，循环继续，让它用手上已有的回填说话。始终没开口才兜底，并且兜底会打标记，不让你对着一个空回复猜。

### 2. 改写不归主 Agent

主 Agent 不写 SQL、不查库，**也不参与改写问题**。它交给查数工具的只有「当前问题」这一句；改写问题全部由工具内部的小模型产出。

### 3. 联网搜索是主 Agent 的决定，不在查数工具内部

这一点是设计的核心，也最容易被画错：

```text
主 Agent 调查数工具 → 拿到库里的确切事实（是谁、多少粉、什么类目、哪个邮箱）
        ↓
判断：光拿这些回复用户，不够 / 不对
        ↓
联网搜索，补充库外的时效性内容
        ↓
把「库里的锚点」和「网上的补充」合成一句回复
```

**库给不可幻觉的锚点，搜索给时效性，主 Agent 给合成。** 三样缺一样，就跟通用网页版没区别了。

所以联网搜索既不是某个「纯聊天环」的专利，也不是查数管线里的一个环节——**它是主 Agent 自己的一个动作，不封装进查数工具**。

### 4. 查数工具里面是一条固定管线

主 Agent 调一次查数工具，进入它内部的固定顺序，自己插不了步：

```text
B3 小模型改写（无记忆）      → 改写问题 + 需求说明
        ↓
B4 Vanna 内 RAG             → DDL / 文档口径 / 问题-SQL 三路各检索一次
        ↓
B5 查库对话（全新）          → 查库大模型只调只读查库工具；报错或超时就对话内改 SQL 再跑
        ↓
B6 回填主 Agent             → 有行 / 没查到 / 报错
```

### 5. 唯一一处程序写状态

跨轮连续性只有两个槽：`上次 SQL` + `查询摘要`。硬约束是**成对覆盖**——只有查库成功（含 0 行）才把两个槽一起写；失败就原样返回，绝不出现「SQL 是新的、摘要是旧的」。富化结果和搜索结果都不进槽位。

完整设计（两环对照、保险丝数字、Vanna 配置）见 `textsql-agent-flow.md`，附图 `textsql-agent-flow.html`。

---

## 项目结构

```text
textsql-agent-handoff/
├── README.md                     本文件：设计思路 + 项目结构 + 怎么跑
├── CONTEXT.md                    词源锚：所有术语的定义。改词条要配 ADR
├── textsql-agent-flow.md         ← 设计说明（正文）：决策循环 + 两条去路
├── textsql-agent-flow.html         同上，HTML 版（浏览器里看，带卡片和流程图）
├── textsql-rewrite-prompt.md     B3 小模型的完整提示前缀（固定前缀 + 规则 1–5）
├── .gitignore                    真库与构建产物不进仓库（含 *.sqlite3）
│
├── docs/adr/                     架构决策记录。改 L1 不变量要配新 ADR
│   ├── 0001-main-agent-decision-loop.md          主 Agent 是决策循环，不是一次性分流
│   └── 0002-free-text-summary-and-clean-src.md   摘要自由文本 + 业务词不进 src/
│
├── textsql-agent/                代码
│   ├── pyproject.toml            Python ≥ 3.11 · 运行时依赖为零 · mypy strict
│   ├── uv.lock                   依赖锁文件（锁定 uv 的解析结果）
│   ├── AGENTS.md                 给编码 agent 的项目约定
│   ├── .gitignore                构建产物不进仓库（cache / egg-info / venv）
│   │
│   ├── config/                   业务材料。L2：换库只改这里，不改代码
│   │   ├── blogger_ddl.sql       表结构与列，给人看也给模型看（B4 的 DDL 那一路）
│   │   ├── blogger_docs.md       类目枚举 + 分位数 + 已知脏值（B4 的文档那一路）
│   │   └── blogger.yml           金标：问法 / 金标 SQL / 基数 / 锚点（B4 的问题-SQL 那一路）
│   │
│   ├── docs/agents/
│   │   └── undesigned.md         未设计与未接线清单。接手前必读
│   │
│   ├── src/textsql_agent/        实现（模块清单见下）
│   │   ├── __init__.py           包标记，内容为空
│   │   └── py.typed              标记本包带类型信息，供 mypy 消费
│   │
│   └── tests/                    55 个测试 + 替身夹具。一条工单一个文件
│       ├── fakes.py              系统边界替身。业务词只住在这里
│       ├── test_ring_a_one_turn.py              工单 01：环 A 一轮能说话
│       ├── test_query_tool_stub_success.py      工单 02：查数工具一次（内部全替身）走通
│       ├── test_standalone_current_question.py  工单 03：当前问题消指代
│       ├── test_small_model_rewrite.py          工单 04：小模型按契约改写
│       ├── test_vanna_rag_once.py               工单 05：Vanna RAG 只检索一次
│       ├── test_readonly_sql_session.py         工单 06：查库对话真只读跑 SQL
│       ├── test_web_search_disabled.py          工单 07：联网搜索契约在，默认不挂
│       └── test_wired_adapters.py               真适配器接进唯一测试缝
│
└── .scratch/textsql-agent/       设计与调研文档
    ├── START-HERE.md             入口：读法顺序 / 实现到了哪 / 卡在哪 / 验证命令
    ├── flow-design.md            主设计，十节，含数据缺陷清单
    ├── data-profile.md           真实库画像（365 行）。所有阈值的原始依据
    ├── observability.md          怎么判「这条 SQL 在回答那句话」
    ├── spec.md                   原始规格快照（部分条款已被 ADR 取代）
    ├── flow-diagram.md           流程图（mermaid，GitHub 上可直接渲染）
    ├── issues/                   七张工单，一张一个可验证目标
    │   ├── 01-ring-a-one-turn.md              环 A 一轮能说话
    │   ├── 02-query-tool-stub-success.md      查数工具一次（内部全替身）走通
    │   ├── 03-standalone-current-question.md  当前问题消指代
    │   ├── 04-small-model-rewrite.md          小模型按契约改写
    │   ├── 05-vanna-rag-once.md               Vanna RAG 只检索一次
    │   ├── 06-readonly-sql-session.md         查库对话真只读跑 SQL
    │   └── 07-web-search-disabled.md          联网搜索契约在，默认不挂
    └── tools/                    数据脚本与评测夹具。真库不在这（见下节）
        ├── dump_to_sqlite.py     把 mysqldump 转成 SQLite，一次解析多次查
        ├── profile_table.py      整理原始表并打印一份小画像报告
        ├── validate_data.py      对已入库的表做数据核对（只读，输出全程打码）
        ├── textsql_harness.py    把真管线装起来，供下面两个脚本共用
        ├── check_question.py     单句问法体检：跑一遍真管线，把每一跳摆出来
        └── run_eval.py           跑金标：每条问法过真管线，把每一跳和判定打出来
```

### `textsql-agent/src/textsql_agent/` 模块清单

按数据流从上到下：

| 模块 | 干什么 |
|---|---|
| `turn.py` | **唯一的测试缝**。`run_turn` 收一句用户话，跑完决策循环，返回 `TurnResult`。签名锁定，不为下层另开测试缝 |
| `brain.py` | 主 Agent 的规划接口：`MainAgentBrain` Protocol，两个决定——`Speak` / `CallTool` |
| `prompts.py` | 主 Agent 规划者本轮能看见的固定说明 |
| `tools.py` | 工具名常量与默认挂载清单 |
| `planner.py` | 未接线版的规则规划者：把用户原话收成可独立理解的「当前问题」 |
| `deepseek.py` | DeepSeek 适配器：`DeepSeekPlanner` / `DeepSeekSmallModel` / `DeepSeekQueryLlm` 都走这里 |
| `query_tool.py` | 查数工具：入参是当前问题，内部跑完 B3→B6 再回填。`UnwiredQueryPipeline` 是未接线替身 |
| `rewrite.py` | B3 小模型改写：`Rewriter` + 提示词拼装 + 输出解析 |
| `rag.py` | B4：`VannaRag` Protocol + `InMemoryVanna` 关键词替身 |
| `sql_session.py` | B5：查库对话 + 只读执行器 Protocol。回填截断常量 `MAX_RESULT_ROWS` / `MAX_RESULT_CHARS` 在这里 |
| `sqlite_executor.py` | `SqliteReadonlyExecutor`：三重只读的 SQLite 执行器 |
| `web_search.py` | 联网搜索：`SearchEngine` Protocol + `WebSearch` 包装 + 熔断（`src/` 里没有实现） |
| `slots.py` | 程序槽：上次 SQL + 查询摘要成对存放，不经主 Agent 传入 |
| `session.py` | 主 Agent 会话：用户原话 + 自己说过的话。不做槽位状态机 |
| `__main__.py` | 本地演示页。不是规格里的产品 UI |


## 读法顺序

1. `textsql-agent-flow.md` —— **设计说明**。先看这个，知道它想做成什么样
2. `CONTEXT.md` —— 统一词表。这是读代码的前提，代码里的命名都照着它来
3. `docs/adr/` —— 两条关键决策：主 Agent 是决策循环（0001）、业务词不进 `src/`（0002）
4. `.scratch/textsql-agent/START-HERE.md` —— **实现到了哪**。读它是为了**别把设计当现状**
5. `.scratch/textsql-agent/flow-design.md` —— 主设计，十节，含数据缺陷分析
6. `.scratch/textsql-agent/spec.md` —— 原始规格快照。部分条款已被 ADR 取代，文件头有清单

只想看代码的话：`textsql-agent/src/textsql_agent/turn.py` 是唯一的测试缝，从它开始追。

## 跑起来

### 未接线版（不需要任何密钥和数据）

```bash
cd textsql-agent
uv run --python 3.12 python -m textsql_agent
# → http://127.0.0.1:8765
```

这一版用的是规则规划者 + 未接线查数工具。页面会**直说「未接线」**，不会编假数据。它的用途是让你在不配任何东西的情况下看清楚一轮对话的骨架。

### 真管线版（需要自备数据，见下节）

```bash
cd textsql-agent
export OPENAI_API_KEY=...          # 或 DEEPSEEK_API_KEY
uv run --python 3.12 python -m textsql_agent \
  --db   /path/to/your.sqlite3 \
  --ddl  config/blogger_ddl.sql \
  --docs config/blogger_docs.md
# → http://127.0.0.1:8765
```

给了 `--db` 就会装配真组件（真模型客户端 + 真只读执行器 + 真库），页面上把每一跳都摆出来：当前问题 → 改写问题 → SQL → 命中基数 → 回填。

### 测试与类型检查

```bash
cd textsql-agent
uv run --with pytest --python 3.12 python -m pytest -q
uv run --with mypy   --python 3.12 mypy
```

## 关于数据（重要）

**这个仓库不包含数据库。** 原项目跑的是一个真实的博主库，里面含真实联系邮箱，而且体积 223MB，超出 GitHub 单文件上限。所以真库被 `.gitignore` 排除了，没有随仓库分发。

设计文档（`.scratch/` 下的 md）里出现的博主昵称、`xhs_id`、邮箱**都已替换为合成值或打码**——保留的只有聚合统计（行数、分位数、分布形状）。所以文档里的 `阮小美Austin`、`小花dao` 这类不是任何真实账号，别拿去搜。

代码本身不绑定任何具体数据库：库路径从命令行传入，DDL 和业务文档都是普通文件。**要跑真管线版，你需要自备一个同结构的 SQLite 库**，结构见 `textsql-agent/config/blogger_ddl.sql`——那份 DDL 有三张表：

- `xhs_creator` —— 主表，每个账号一行
- `xhs_creator_category` —— 账号与类目的一对多
- `dict_category_alias` —— 类目别名。原库里是空表，但代码会去建它

`.scratch/textsql-agent/tools/` 下的脚本可以帮你体检数据：

```bash
cd .scratch/textsql-agent/tools
uv run --with pyyaml --python 3.12 python check_question.py <你的库> "一句问法"
uv run --with pyyaml --python 3.12 python run_eval.py <你的库> --self-test
```

`--self-test` 用退化解校准判据本身，不调模型、不需要 key。

评测的正片需要先往 `textsql-agent/config/blogger.yml` 的 `问法` 栏填真实问法。原项目里这一栏**是空的**——真实问法只有业务方能给，不许自己编（编过一次，结果被当成真实库结构）。

## 哪些是真接通的、哪些还是替身

这一节是刻意保留的。这类项目最容易的失败方式，是拿一堆替身跑通了就宣称「能查库了」。

**真接通的（真组件，不是替身）**：

- 主 Agent 规划 `DeepSeekPlanner`
- 小模型改写 `DeepSeekSmallModel`
- 查库大模型 `DeepSeekQueryLlm`
- 只读执行器 `SqliteReadonlyExecutor`

执行器是**三重只读**：`mode=ro` URI 打开 + SQL 必须以 `SELECT`/`WITH` 开头 + 只允许单语句，另加禁写词表和 `set_progress_handler` 超时（熔断 5 次 / 单条 30 秒）。四个组件都通过窄 Protocol 注入，`run_turn` 的签名一个字没动。

**还没接通的**：

- **Vanna 检索**。`rag.py` 里的 `InMemoryVanna` 是个关键词替身：整份 DDL、整份文档各当一个字符串做子串匹配。**这是子串匹配，不是检索**——没有分块、没有 embedding、没有排序、没有 top-k、没有阈值。而且 DDL 与文档这两路的命中是**全有全无**，这是**结构性**的：整份 DDL 和整份文档各只作为一条训练项（各 `train()` 一次），所以这两路只能返回「整份」或「空」。第三路「问题-SQL」不一样，它是按条过滤的列表，现在恒空只是因为 `问法: []` 是空的。`VannaRag` Protocol 和注入口都在，换真 Vanna 是**接线，不是重构**。
- **联网搜索**。`SearchEngine` 只是个 Protocol，`src/` 里零实现，且 `web_search_enabled` 默认为 `False`。
- **「查完库还能再联网搜」这条路**。代码里的决策循环本身是通的，但同一轮里查数与联网搜索目前互斥（`turn.py` 里 `query_tool_call_count > 0` 就把搜索摘掉）。设计要的是「拿到库内容后判断不够、再去搜」——**这版没实现**。
- **B6.2–B6.5**。`entities` / `enrichments` / `enrichment_call_count` / `miss_logged` 四个字段声明了、管道也通了，但**从来没被赋过值**。环 B 今天回填的是「一行行文本」，不是「一组实体」。

## 硬边界

这些是项目自我约束，也是它看起来和一般 demo 不太一样的原因：

- **业务词一律不进 `src/`**（ADR 0002）。验收命令：`grep -rn "销售额\|含税\|按城市\|上个月\|博主\|美妆" textsql-agent/src/` 必须为空。业务词只住在 `config/` 和 `.scratch/`。
- **测试缝只有一轮：`run_turn`**，签名锁定。不为小模型、RAG、查库大模型、执行器另开测试缝。
- **可观察量长在查数工具内**，经 `QueryToolOutcome` → `TurnResult` 出来，不另建观察通路。
- **失败必须可见**。不许出现「静默空回复」或「页面点了没反应」——那分不清是系统坏了、没查到，还是没人回答。
- **运行时依赖为零**。`dependencies = []`，`mypy strict` 全绿。

## 许可证

仓库尚未附许可证文件。在加上之前，默认保留所有权利。
