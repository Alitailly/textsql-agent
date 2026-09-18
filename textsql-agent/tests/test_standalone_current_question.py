"""工单 03：当前问题消指代。

唯一测试缝：一轮。夹具注入会话和程序槽，送一句用户话，
断言回复、查数工具次数、联网搜索次数、工具入参是否为可独立理解的当前问题。
小模型侧只通过管线替身观察：当前问题加可空的上次 SQL / 查询摘要。
用户原话只留在主 Agent 会话里。
"""

from textsql_agent.planner import Planner
from textsql_agent.session import MAIN_AGENT, USER, Message, Session
from textsql_agent.slots import ProgramSlots
from textsql_agent.tools import QUERY_TOOL
from textsql_agent.turn import run_turn

from .fakes import SUMMARY_TEMPLATE, SALES_CUES, RecordingQueryPipeline, StubRowsQueryPipeline


def _rows_pipeline(*, 时间: str = "上个月", 口径: str = "含税") -> StubRowsQueryPipeline:
    return StubRowsQueryPipeline(
        sql="SELECT SUM(amount) AS 销售额 FROM sales LIMIT 50",
        amount="6410288",
        summary=SUMMARY_TEMPLATE.format(
            metric="销售额", time=时间, group="无", caliber=口径
        ),
    )


def _options_session() -> Session:
    return Session(
        messages=(
            Message(role=USER, content="查上个月销售额"),
            Message(
                role=MAIN_AGENT,
                content="可以按两种口径。第一个是含税销售额，第二个是未税销售额。",
            ),
        )
    )


def test_choosing_the_first_option_sends_a_standalone_current_question() -> None:
    result = run_turn(
        "就按你说的第一个",
        brain=Planner(SALES_CUES),
        session=_options_session(),
        query_pipeline=_rows_pipeline(),
    )

    assert result.query_tool_call_count == 1
    assert result.web_search_call_count == 0
    assert result.query_tool_arguments == {"当前问题": "查询上个月含税销售额"}
    assert result.query_tool_arguments["当前问题"] != "就按你说的第一个"
    assert "6410288" in result.reply
    assert any(
        message.role == USER and message.content == "就按你说的第一个"
        for message in result.session.messages
    )


def test_asking_caliber_first_stays_in_ring_a() -> None:
    result = run_turn("先告诉我销售额口径", brain=Planner(SALES_CUES))

    assert result.query_tool_call_count == 0
    assert result.web_search_call_count == 0
    assert result.query_tool_arguments is None
    assert result.reply == "销售额口径是含税成交额。"
    assert any(
        message.role == USER and message.content == "先告诉我销售额口径"
        for message in result.session.messages
    )


def test_query_after_caliber_chat_includes_clarified_caliber() -> None:
    prior = run_turn("先告诉我销售额口径", brain=Planner(SALES_CUES))
    pipeline = RecordingQueryPipeline(_rows_pipeline(时间="未说明", 口径="含税成交额"))

    result = run_turn(
        "查吧",
        brain=Planner(SALES_CUES),
        session=prior.session,
        query_pipeline=pipeline,
    )

    assert prior.query_tool_call_count == 0
    assert result.query_tool_call_count == 1
    assert result.web_search_call_count == 0
    assert result.query_tool_arguments == {"当前问题": "查询销售额，口径为含税成交额"}
    assert result.query_tool_arguments["当前问题"] != "查吧"
    assert pipeline.current_questions == ["查询销售额，口径为含税成交额"]
    assert pipeline.slot_inputs == [ProgramSlots()]
    assert "查吧" not in pipeline.current_questions[0]
    assert "先告诉我销售额口径" not in pipeline.current_questions[0]
    assert "6410288" in result.reply
    assert any(
        message.role == USER and message.content == "查吧"
        for message in result.session.messages
    )


def test_small_model_sees_current_question_and_nullable_slots_not_recent_turns() -> None:
    prior_slots = ProgramSlots(
        last_sql="SELECT old FROM sales",
        query_summary=SUMMARY_TEMPLATE.format(
            metric="销售额", time="上个月", group="无", caliber="未说明"
        ),
    )
    empty = RecordingQueryPipeline(_rows_pipeline())
    filled = RecordingQueryPipeline(_rows_pipeline())

    empty_result = run_turn(
        "就按你说的第一个",
        brain=Planner(SALES_CUES),
        session=_options_session(),
        query_pipeline=empty,
    )
    filled_result = run_turn(
        "就按你说的第一个",
        brain=Planner(SALES_CUES),
        session=_options_session(),
        slots=prior_slots,
        query_pipeline=filled,
    )

    assert empty_result.query_tool_call_count == 1
    assert filled_result.query_tool_call_count == 1
    assert empty.current_questions == ["查询上个月含税销售额"]
    assert filled.current_questions == ["查询上个月含税销售额"]
    assert empty.slot_inputs == [ProgramSlots()]
    assert filled.slot_inputs == [prior_slots]
    assert "就按你说的第一个" not in filled.current_questions[0]
    assert "可以按两种口径" not in filled.current_questions[0]
    assert "查上个月销售额" not in filled.current_questions[0]
    assert any(
        message.role == USER and message.content == "就按你说的第一个"
        for message in filled_result.session.messages
    )


def test_prior_query_fillback_does_not_stop_this_turn_writing_current_question() -> None:
    prior = Session(
        messages=(
            Message(role=USER, content="查上个月销售额"),
            Message(role=QUERY_TOOL, content="SQL: SELECT old FROM sales\n结果: 销售额 1"),
            Message(
                role=MAIN_AGENT,
                content="上个月查到了。还可以换口径。第一个是含税销售额，第二个是未税销售额。",
            ),
        )
    )

    result = run_turn(
        "就按你说的第一个",
        brain=Planner(SALES_CUES),
        session=prior,
        query_pipeline=_rows_pipeline(),
    )

    assert result.query_tool_call_count == 1
    assert result.query_tool_arguments == {"当前问题": "查询上个月含税销售额"}
