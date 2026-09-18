"""查数工具：主 Agent 只传入当前问题，内部跑完环 B 再回填。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from textsql_agent.rag import InMemoryVanna, RagHits, VannaRag
from textsql_agent.rewrite import Rewriter
from textsql_agent.slots import ProgramSlots
from textsql_agent.sql_session import (
    QueryLlm,
    ReadonlyExecutor,
    ReadonlyReturn,
    SqlSession,
)


@dataclass(frozen=True)
class QueryToolOutcome:
    fillback: str
    slots: ProgramSlots
    rewritten_question: str | None = None
    rewrite_output: str | None = None
    rag_hits: RagHits | None = None
    sql_text: str | None = None
    readonly_returns: tuple[ReadonlyReturn, ...] = ()
    sql_session_discarded: bool = False
    sql_session_tools: tuple[str, ...] = ()
    entities: tuple[dict[str, str], ...] = ()
    """B6.2 从行集解析出的实体。本轮不实现，先留可观察位。"""
    enrichments: tuple[str, ...] = ()
    """每个实体对应的公开补充。本轮不实现，先留可观察位。"""
    enrichment_call_count: int = 0
    miss_logged: bool = False
    """本轮是否写了未命中记录。环 A、以及查库失败用尽时都必须为 False。"""


class QueryPipeline(Protocol):
    def run(self, current_question: str, slots: ProgramSlots) -> QueryToolOutcome:
        """内部跑完查数工作流。程序槽由本工具读取并在成功时成对覆盖。"""
        ...


class UnwiredQueryPipeline:
    """未接线：小模型 / Vanna / 查库大模型 / 库都还没接。

    明说没查成，不编数、不动程序槽。真管线经适配器注入替换本类。
    """

    _FILLBACK = "查数工具未接线：本轮没有真查库，也就没有数。"

    def run(self, current_question: str, slots: ProgramSlots) -> QueryToolOutcome:
        del current_question
        return QueryToolOutcome(fillback=self._FILLBACK, slots=slots)


@dataclass(frozen=True)
class QueryConversationResult:
    sql: str
    fillback: str


class QueryConversation(Protocol):
    def run(self, rewritten_question: str, rag_hits: RagHits) -> QueryConversationResult:
        """查库大模型吃到改写问题和这一次命中材料。不在检索里执行 SQL。"""
        ...


class RewriteQueryPipeline:
    """真改写 + Vanna 一次检索；查库对话经适配器注入。"""

    def __init__(
        self,
        *,
        rewriter: Rewriter,
        query_conversation: QueryConversation,
        vanna: VannaRag | None = None,
    ) -> None:
        self._rewriter = rewriter
        self._vanna = vanna or InMemoryVanna()
        self._conversation = query_conversation

    def run(self, current_question: str, slots: ProgramSlots) -> QueryToolOutcome:
        rewritten = self._rewriter.run(current_question, slots)
        hits = self._vanna.retrieve(rewritten.rewritten_question)
        spoken = self._conversation.run(rewritten.rewritten_question, hits)
        return QueryToolOutcome(
            fillback=spoken.fillback,
            slots=ProgramSlots(last_sql=spoken.sql, query_summary=rewritten.summary),
            rewritten_question=rewritten.rewritten_question,
            rewrite_output=rewritten.output,
            rag_hits=hits,
        )


class ReadonlySqlQueryPipeline:
    """工单 06：真改写 + 一次检索 + 查库对话只调只读查库工具。"""

    def __init__(
        self,
        *,
        rewriter: Rewriter,
        query_llm: QueryLlm,
        executor: ReadonlyExecutor,
        vanna: VannaRag | None = None,
    ) -> None:
        self._rewriter = rewriter
        self._vanna = vanna or InMemoryVanna()
        self._session = SqlSession(query_llm, executor)

    def run(self, current_question: str, slots: ProgramSlots) -> QueryToolOutcome:
        rewritten = self._rewriter.run(current_question, slots)
        hits = self._vanna.retrieve(rewritten.rewritten_question)
        session_outcome = self._session.run(rewritten.rewritten_question, hits)
        if session_outcome.success:
            next_slots = ProgramSlots(
                last_sql=session_outcome.sql_text,
                query_summary=rewritten.summary,
            )
        else:
            next_slots = slots
        return QueryToolOutcome(
            fillback=session_outcome.fillback,
            slots=next_slots,
            rewritten_question=rewritten.rewritten_question,
            rewrite_output=rewritten.output,
            rag_hits=hits,
            sql_text=session_outcome.sql_text,
            readonly_returns=session_outcome.readonly_returns,
            sql_session_discarded=session_outcome.discarded,
            sql_session_tools=session_outcome.tools,
        )
