"""工单 02：查数工具一次（内部全替身）走通。

唯一测试缝：一轮。夹具可注入会话和程序槽，送一句用户话，
断言回复、查数工具次数、工具入参/回填、结束后上次 SQL 与查询摘要。
"""

from textsql_agent.slots import ProgramSlots
from textsql_agent.turn import run_turn

from .fakes import (
    SUMMARY_TEMPLATE,
    AlwaysCallQueryToolBrain,
    QueryThenSpeakBrain,
    SpeakOnlyBrain,
    StubRowsQueryPipeline,
)

SALES_SUMMARY = SUMMARY_TEMPLATE.format(
    metric="销售额", time="上个月", group="无", caliber="未说明"
)


def test_asking_for_a_number_calls_query_tool_once_with_current_question() -> None:
    result = run_turn(
        "就按第一个",
        brain=QueryThenSpeakBrain(current_question="查询上个月含税销售额"),
    )

    assert result.query_tool_call_count == 1
    assert result.query_tool_arguments == {"当前问题": "查询上个月含税销售额"}
    assert result.query_tool_arguments["当前问题"] != "就按第一个"


def test_reply_uses_numbers_from_query_tool_fillback() -> None:
    pipeline = StubRowsQueryPipeline(
        sql="SELECT SUM(amount) AS 销售额 FROM sales LIMIT 50",
        amount="6410288",
        summary=SALES_SUMMARY,
    )
    result = run_turn(
        "上个月销售额多少",
        brain=QueryThenSpeakBrain(current_question="查询上个月销售额"),
        query_pipeline=pipeline,
    )

    assert result.query_tool_fillback is not None
    assert "6410288" in result.query_tool_fillback
    assert "6410288" in result.reply
    assert "SELECT" not in result.reply


def test_successful_query_overwrites_last_sql_and_summary_as_a_pair() -> None:
    prior_slots = ProgramSlots(
        last_sql="SELECT old FROM sales",
        query_summary="上一轮的说明原文",
    )
    pipeline = StubRowsQueryPipeline(
        sql="SELECT SUM(amount) AS 销售额 FROM sales LIMIT 50",
        amount="6410288",
        summary=SALES_SUMMARY,
    )
    result = run_turn(
        "上个月销售额多少",
        brain=QueryThenSpeakBrain(current_question="查询上个月销售额"),
        query_pipeline=pipeline,
        slots=prior_slots,
    )

    assert result.query_tool_arguments == {"当前问题": "查询上个月销售额"}
    assert result.last_sql == "SELECT SUM(amount) AS 销售额 FROM sales LIMIT 50"
    assert result.query_summary == SALES_SUMMARY


def test_second_query_tool_call_in_same_turn_is_rejected() -> None:
    result = run_turn(
        "上个月销售额多少",
        brain=AlwaysCallQueryToolBrain(),
    )

    assert result.query_tool_call_count == 1
    # 拒绝之后不许静默收场。这个替身认死了要调那个已经被摘掉的工具，
    # 所以走兜底：用户至少拿到一句话，reply_is_fallback 把"这是兜底"标出来。
    assert result.reply
    assert result.reply_is_fallback is True
    # 兜底话术也要落进会话，否则下一轮主 Agent 的记忆里这一轮是空白的。
    assert result.session.messages[-1].content == result.reply


def test_ring_a_still_does_not_call_query_tool() -> None:
    result = run_turn("你好", brain=SpeakOnlyBrain("你好，我可以帮你问数。"))

    assert result.query_tool_call_count == 0
    assert result.last_sql is None
    assert result.query_summary is None
