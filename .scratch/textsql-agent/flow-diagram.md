# Text-SQL 主 Agent · 流程图

本图只画**设计**。哪些已经接线、哪些还是替身，见 `README.md` 的「哪些是真接通的」一节；实现边界见 `START-HERE.md`。

配套的设计正文在仓库根目录 `textsql-agent-flow.md`，HTML 版在 `textsql-agent-flow.html`。

---

## 一、主 Agent 是一个决策循环

每一圈只问一句话：**「手上这些材料，够不够回答用户？」**

```mermaid
flowchart TD
    U(["用户说话"]) --> THINK{"思考<br/>够不够回答用户？"}

    THINK -->|"够了"| SPEAK["说话"]
    SPEAK --> REPLY(["回复用户<br/>本轮结束"])

    THINK -->|"要库里的确切事实"| QT["调查数工具<br/>默认开启 · 每轮最多 1 次"]
    THINK -->|"拿到库内容后判断不够"| WS["联网搜索<br/>默认关闭 · 每轮最多 3 次"]

    QT --> QB["环 B 内部<br/>B3 改写 → B4 检索 → B5 查库 → B6 回填"]
    QB --> OBS1["观察回填<br/>有行 / 没查到 / 报错"]
    WS --> OBS2["观察搜索结果"]

    OBS1 --> THINK
    OBS2 --> THINK

    classDef act fill:#2c4a7a,color:#fff,stroke:#3d6a9a
    classDef ok fill:#1f6f4a,color:#fff,stroke:#2a8a5e
    classDef tool fill:#6a4a1c,color:#fff,stroke:#8a6428

    class THINK,SPEAK act
    class U,REPLY ok
    class QT,WS tool
```

**读法**：`调查数工具` 和 `联网搜索` 都会回到「思考」，而不是直接结束本轮。所以一轮里可以查完库、判断不够、再联网搜，最后才说话。

### 三条边界

1. **改写不归主 Agent。** 它只写出「当前问题」交给查数工具，改写问题全部由工具内部的小模型产出。
2. **联网搜索不在环 B 内。** 它不封装进查数工具，也不是环 A 的专利——是主 Agent 自己的一个动作，典型发生在拿到查数回填之后。
3. **被拒的工具不打断整轮。** 查数工具用超了、或搜索熔断用尽，程序把那个工具从主 Agent 能看见的工具集里摘掉，循环继续，让它用手上已有的回填说话；始终没开口才兜底。

---

## 二、环 B：查数工具内部

主 Agent 调一次工具即进入，跑完回填、离开环 B。它插不了步。

```mermaid
flowchart TD
    B2["B2 主 Agent 写出当前问题<br/>调查数工具一次"] --> B3["B3 小模型改写（无记忆）<br/>改写问题 + 需求说明"]
    B3 --> B4["B4 Vanna 内 RAG<br/>DDL / 文档口径 / 问题-SQL 三路对照一次"]
    B4 --> B5{"B5 查库对话（全新）<br/>只调只读查库工具"}
    B5 -->|"SQL 报错或超时"| RETRY["对话内改 SQL 再跑"]
    RETRY -->|"未满 5 次"| B5
    B5 -->|"有行"| B61["B6 回填：SQL + 截断结果"]
    B5 -->|"0 行"| B62["B6 回填：没查到"]
    RETRY -->|"5 次用尽"| B63["B6 回填：最后 SQL + 报错"]

    B61 --> SLOT["成对覆盖槽位<br/>上次 SQL + 查询摘要"]
    B62 --> SLOT
    B63 --> NOSLOT["两槽不动<br/>本轮摘要扔掉"]

    SLOT --> FB(["回到主 Agent 决策循环"])
    NOSLOT --> FB

    classDef real fill:#1f6f4a,color:#fff,stroke:#2a8a5e
    classDef slot fill:#4a3d68,color:#fff,stroke:#5a4d78
    class B2,B3,B4,B5,RETRY,B61,B62,B63,FB real
    class SLOT,NOSLOT slot
```

**0 行也算查库成功**，所以照写成对覆盖；只有「5 次用尽仍失败」才是两槽不动。

---

## 三、槽位：唯一一处程序写状态

```mermaid
flowchart LR
    R{"查数工具返回"} -->|"有行"| S1["last_sql ← 新 SQL<br/>query_summary ← 本轮需求说明"]
    R -->|"0 行"| S2["last_sql ← 空查询 SQL<br/>query_summary ← 本轮需求说明"]
    R -->|"5 次用尽仍失败"| S3["两个槽都不动<br/>本轮需求说明丢弃"]

    S1 --> OK["只给下一轮小模型看"]
    S2 --> OK
    S3 --> OK

    classDef real fill:#1f6f4a,color:#fff
    class S1,S2,S3,OK,R real
```

硬约束：**成对覆盖**——只有查库成功才把两个槽一起写，失败就原样返回。富化结果和搜索结果都**不进槽位**。

---

## 四、已定的数字

| 项 | 值 |
|---|---|
| 查数工具 | 默认开启，每轮最多 1 次 |
| 每段查库对话最多只读调用 | 5 |
| 单次 SQL 超时 | 30 秒 |
| 回填截断 | 50 行或约 4000 字 |
| 联网搜索 | 默认关闭；主 Agent 在拿到库内容后按需调用 |
| 联网搜索开启后：每轮最多调用 | 3 |
| 联网搜索开启后：单次超时 | 15 秒 |
| 主 Agent 决策循环上限 | 3 + 3 = 6 |

> 回填截断值在 `flow-design.md`（§4，写 `LIMIT 200` → 截到 10 行）与规格 + 现状代码（50 行 / 4000 字）之间对不上，尚未拍板。本图按现状代码画。
