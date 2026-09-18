# textsql-agent

把一句中文口语问题，变成一条能跑的 SQL，再把结果讲回给用户。

这是一个**多轮**的 Text-SQL 主 Agent。每一轮用户说一句话，主 Agent 先判断这句话要不要查库：不要查就正常对话（环 A），要查就走完整的查数管线（环 B）——改写问题 → 取材料 → 生成 SQL → 只读执行 → 把结果回填成人话。整个过程的可观察量（当前问题、改写后的问题、SQL、命中基数、回填内容）都固定在一条通路上，不另开观察旁路。

## 这个仓库里有什么

| 路径 | 是什么 |
|---|---|
| `textsql-agent/` | 代码。Python ≥ 3.11，**运行时依赖为零** |
| `textsql-agent/config/` | 业务材料：DDL、给模型看的业务文档、问题-SQL 金标 |
| `.scratch/textsql-agent/` | 设计与调研文档：规格、主设计、数据画像、可观察性、七张工单 |
| `.scratch/textsql-agent/tools/` | 数据脚本与评测夹具 |
| `CONTEXT.md` | 词源锚。改词条要配 ADR |
| `docs/adr/` | 架构决策记录 |
| `textsql-agent-flow.md` / `.html` | 总流程图 |
| `textsql-rewrite-prompt.md` | 小模型改写的完整提示前缀 |

## 读法顺序

想快速看懂这个项目，按这个顺序读：

1. `.scratch/textsql-agent/START-HERE.md` —— 入口。读法顺序、实现到了哪、卡在哪、验证命令
2. `.scratch/textsql-agent/spec.md` —— 规格。动手前先扫「不准救活」清单
3. `.scratch/textsql-agent/flow-design.md` —— 主设计，十节，含数据缺陷分析
4. `CONTEXT.md` —— 统一词表。这是读代码的前提，代码里的命名都照着它来
5. `docs/adr/` —— 两条关键决策：一轮一计划（0001）、业务词不进 `src/`（0002）
6. `textsql-agent-flow.md` —— 总流程，带图

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
- **B6.2–B6.5**。`entities` / `enrichments` / `enrichment_call_count` / `miss_logged` 四个字段声明了、管道也通了，但**从来没被赋过值**。环 B 今天回填的是「一行行文本」，不是「一组实体」。

## 硬边界

这些是项目自我约束，也是它看起来和一般 demo 不太一样的原因：

- **业务词一律不进 `src/`**（ADR 0002）。验收命令：`grep -rn "销售额\|含税\|按城市\|上个月\|博主\|美妆" textsql-agent/src/` 必须为空。业务词只住在 `config/` 和 `.scratch/`。
- **测试缝只有一轮：`run_turn`**，签名锁定。不为小模型、RAG、查库大模型、执行器另开测试缝。
- **可观察量长在环 B 内**，经 `QueryToolOutcome` → `TurnResult` 出来，不另建观察通路。
- **失败必须可见**。不许出现「静默空回复」或「页面点了没反应」——那分不清是系统坏了、没查到，还是没人回答。
- **运行时依赖为零**。`dependencies = []`，`mypy strict` 全绿。

## 许可证

仓库尚未附许可证文件。在加上之前，默认保留所有权利。
