"""程序槽：上次 SQL 与查询摘要成对存放。不经主 Agent 传入。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgramSlots:
    last_sql: str | None = None
    query_summary: str | None = None
    """小模型改写时产出的自然语言说明，与上次 SQL 成对落盘。

    名目不参与程序分支（ADR 0002）：程序只原样存取，不按它的内容做判断。
    只给下一轮小模型看，不是主 Agent 的记忆。
    """
