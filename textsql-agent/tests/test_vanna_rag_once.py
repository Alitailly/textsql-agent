"""工单 05：Vanna RAG 只检索一次。

唯一测试缝：一轮。夹具注入会话、程序槽和 Vanna 替身，送一句用户话，
断言管线向外露出的 RAG 材料。改写问题作为 query，对照 DDL、文档/口径、
问题-SQL 各一路；检索不执行 SQL；默认 generate_sql 全链不走；环 A 查数工具仍为 0。
"""

from textsql_agent.planner import Planner
from textsql_agent.query_tool import QueryConversation, RewriteQueryPipeline
from textsql_agent.rag import InMemoryVanna, RagHits
from textsql_agent.rewrite import Rewriter
from textsql_agent.turn import run_turn

from .fakes import (
    SALES_CUES,
    KeywordSmallModel,
    RecordingQueryConversation,
    SpeakOnlyBrain,
    StubQueryConversation,
)

SALES_DDL = "CREATE TABLE sales (amount INTEGER, month TEXT, city TEXT)"
HR_DDL = "CREATE TABLE employees (id INTEGER, name TEXT)"
SALES_DOC = "销售额对应 sales.amount，口径为含税成交额"
SAMPLE_QUESTION = "查询2026年8月的销售额"
SAMPLE_SQL = "SELECT SUM(amount) FROM sales WHERE month='2026-08' LIMIT 50"
SAMPLE_HIT = f"问题：{SAMPLE_QUESTION}\nSQL：{SAMPLE_SQL}"


def _trained_vanna() -> InMemoryVanna:
    vanna = InMemoryVanna()
    vanna.train(ddl=SALES_DDL)
    vanna.train(ddl=HR_DDL)
    vanna.train(documentation=SALES_DOC)
    vanna.train(question=SAMPLE_QUESTION, sql=SAMPLE_SQL)
    return vanna


def _pipeline(
    vanna: InMemoryVanna, conversation: QueryConversation | None = None
) -> RewriteQueryPipeline:
    return RewriteQueryPipeline(
        rewriter=Rewriter(KeywordSmallModel()),
        query_conversation=conversation or StubQueryConversation(),
        vanna=vanna,
    )


def test_rewritten_question_retrieves_three_roads_once() -> None:
    vanna = _trained_vanna()

    result = run_turn(
        "2026年8月销售额是多少",
        brain=Planner(SALES_CUES),
        query_pipeline=_pipeline(vanna),
    )

    assert result.query_tool_call_count == 1
    assert result.rewritten_question == SAMPLE_QUESTION
    assert result.rag_hits == RagHits(
        ddl=SALES_DDL,
        documentation=SALES_DOC,
        question_sql=SAMPLE_HIT,
    )
    assert vanna.retrieve_queries == [SAMPLE_QUESTION]
    assert result.rag_hits is not None
    assert HR_DDL not in result.rag_hits.ddl


def test_query_conversation_receives_this_retrievals_hits() -> None:
    vanna = _trained_vanna()
    conversation = RecordingQueryConversation()

    result = run_turn(
        "2026年8月销售额是多少",
        brain=Planner(SALES_CUES),
        query_pipeline=_pipeline(vanna, conversation),
    )

    assert result.query_tool_call_count == 1
    assert conversation.rewritten_questions == [SAMPLE_QUESTION]
    assert conversation.rag_hits_received == [result.rag_hits]
    assert result.rag_hits == RagHits(
        ddl=SALES_DDL,
        documentation=SALES_DOC,
        question_sql=SAMPLE_HIT,
    )


def test_generate_sql_chain_is_not_used_so_there_is_no_second_retrieve() -> None:
    vanna = _trained_vanna()

    result = run_turn(
        "2026年8月销售额是多少",
        brain=Planner(SALES_CUES),
        query_pipeline=_pipeline(vanna),
    )

    assert result.query_tool_call_count == 1
    assert vanna.retrieve_queries == [SAMPLE_QUESTION]
    assert vanna.generate_sql_questions == []


def test_retrieve_does_not_execute_sql() -> None:
    vanna = _trained_vanna()

    result = run_turn(
        "2026年8月销售额是多少",
        brain=Planner(SALES_CUES),
        query_pipeline=_pipeline(vanna),
    )

    assert result.query_tool_call_count == 1
    assert vanna.executed_sql == []
    assert result.rag_hits is not None
    assert "SELECT" in result.rag_hits.question_sql


def test_ring_a_still_skips_query_tool_and_rag() -> None:
    vanna = _trained_vanna()

    result = run_turn(
        "你好",
        brain=SpeakOnlyBrain("你好，我可以帮你问数。"),
        query_pipeline=_pipeline(vanna),
    )

    assert result.query_tool_call_count == 0
    assert result.rag_hits is None
    assert vanna.retrieve_queries == []
    assert vanna.generate_sql_questions == []
    assert vanna.executed_sql == []
