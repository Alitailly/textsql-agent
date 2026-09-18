"""查库对话：查库大模型只调只读查库工具跑 SQL。

有行或 0 行则离开；报错或超时在对话内改 SQL 再跑。
每段对话最多 5 次只读调用，单次超时 30 秒。离开后丢掉这次对话。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from textsql_agent.rag import RagHits

READONLY_SQL_TOOL = "只读查库工具"
MAX_READONLY_CALLS = 5
TIMEOUT_SECONDS = 30
MAX_RESULT_ROWS = 50
MAX_RESULT_CHARS = 4000


@dataclass(frozen=True)
class RowSet:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    matched_rows: int | None = None
    """这条 SQL 的结果集基数，与取行上限、回填截断无关。

    为 None 表示执行器没有单独计数，此时基数即 len(rows)。
    真实执行器必须填这个值：它是「从多少人里取了几条」这句采样说明的唯一来源。
    """


@dataclass(frozen=True)
class SqlError:
    message: str


@dataclass(frozen=True)
class Timeout:
    pass


@dataclass(frozen=True)
class ReadonlyReturn:
    kind: str
    sql: str
    payload: str = ""
    matched_rows: int | None = None
    """本次只读调用的结果集基数。报错与超时时为 None（没有结果集）。"""


@dataclass(frozen=True)
class SqlSessionTurn:
    rewritten_question: str
    rag_hits: RagHits
    tools: tuple[str, ...]
    prior_returns: tuple[ReadonlyReturn, ...]


@dataclass(frozen=True)
class SqlSessionOutcome:
    fillback: str
    sql_text: str | None
    success: bool
    readonly_returns: tuple[ReadonlyReturn, ...]
    discarded: bool
    tools: tuple[str, ...]


class QueryLlm(Protocol):
    """查库大模型边界。真实模型未接，见 docs/agents/undesigned.md。"""

    def next_sql(self, turn: SqlSessionTurn) -> str:
        ...


class ReadonlyExecutor(Protocol):
    """只读库执行边界。真实库未接，见 docs/agents/undesigned.md。"""

    def execute(self, sql: str, *, timeout_seconds: int) -> RowSet | SqlError | Timeout:
        ...


class SqlSession:
    """一次查库对话。每次 run 都从全新对话开始，离开后丢掉。"""

    def __init__(self, query_llm: QueryLlm, executor: ReadonlyExecutor) -> None:
        self._query_llm = query_llm
        self._executor = executor
        self._prior: tuple[ReadonlyReturn, ...] = ()

    def run(self, rewritten_question: str, rag_hits: RagHits) -> SqlSessionOutcome:
        self._prior = ()
        returns: list[ReadonlyReturn] = []
        last_sql: str | None = None
        last_error = ""
        for _ in range(MAX_READONLY_CALLS):
            sql = self._query_llm.next_sql(
                SqlSessionTurn(
                    rewritten_question=rewritten_question,
                    rag_hits=rag_hits,
                    tools=(READONLY_SQL_TOOL,),
                    prior_returns=self._prior,
                )
            )
            last_sql = sql
            outcome = self._executor.execute(sql, timeout_seconds=TIMEOUT_SECONDS)
            if isinstance(outcome, Timeout):
                ret = ReadonlyReturn(kind="超时", sql=sql, payload="超时")
                returns.append(ret)
                self._prior = self._prior + (ret,)
                last_error = "超时"
                continue
            if isinstance(outcome, SqlError):
                ret = ReadonlyReturn(kind="报错", sql=sql, payload=outcome.message)
                returns.append(ret)
                self._prior = self._prior + (ret,)
                last_error = outcome.message
                continue
            if not outcome.rows:
                ret = ReadonlyReturn(kind="0行", sql=sql, matched_rows=0)
                returns.append(ret)
                self._prior = ()
                return SqlSessionOutcome(
                    fillback="没查到",
                    sql_text=sql,
                    success=True,
                    readonly_returns=tuple(returns),
                    discarded=True,
                    tools=(READONLY_SQL_TOOL,),
                )
            result_text, shown_rows = _truncate(outcome)
            matched = _matched(outcome)
            ret = ReadonlyReturn(
                kind="有行",
                sql=sql,
                payload=result_text,
                matched_rows=matched,
            )
            returns.append(ret)
            self._prior = ()
            return SqlSessionOutcome(
                fillback=(
                    f"SQL: {sql}\n"
                    f"命中: {matched} 条，回填 {shown_rows} 条\n"
                    f"结果: {result_text}"
                ),
                sql_text=sql,
                success=True,
                readonly_returns=tuple(returns),
                discarded=True,
                tools=(READONLY_SQL_TOOL,),
            )
        self._prior = ()
        return SqlSessionOutcome(
            fillback=f"SQL: {last_sql}\n报错: {last_error}",
            sql_text=last_sql,
            success=False,
            readonly_returns=tuple(returns),
            discarded=True,
            tools=(READONLY_SQL_TOOL,),
        )


def _matched(rowset: RowSet) -> int:
    if rowset.matched_rows is None:
        return len(rowset.rows)
    return rowset.matched_rows


def _truncate(rowset: RowSet) -> tuple[str, int]:
    """截断回填文本，并回报文本里实际出现了几行。

    行数按换行符数算：被字符上限从中间削断的那一行也算出现（它确实回填了一半）。
    """
    lines = [
        " ".join(
            f"{column} {value}"
            for column, value in zip(rowset.columns, row, strict=True)
        )
        for row in rowset.rows[:MAX_RESULT_ROWS]
    ]
    text = "\n".join(lines)[:MAX_RESULT_CHARS]
    shown = text.count("\n") + 1 if text else 0
    return text, shown
