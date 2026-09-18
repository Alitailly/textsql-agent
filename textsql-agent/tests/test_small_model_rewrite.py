"""工单 04：小模型按契约改写。

唯一测试缝：一轮。夹具注入会话和程序槽，送一句用户话，
断言管线向外露出的改写问题、需求说明、改写两段原文。
小模型不查库、不输出 SQL。
"""

from textsql_agent.planner import Planner
from textsql_agent.query_tool import RewriteQueryPipeline
from textsql_agent.rewrite import REWRITE_PREFIX, Rewriter, SmallModel, build_rewrite_prompt
from textsql_agent.session import MAIN_AGENT, USER, Message, Session
from textsql_agent.slots import ProgramSlots
from textsql_agent.turn import run_turn

from .fakes import (
    SUMMARY_TEMPLATE,
    SALES_CUES,
    ExtraLinesSmallModel,
    KeywordSmallModel,
    RecordingSmallModel,
    StubQueryConversation,
)

SALES_DDL = "SELECT SUM(amount) AS 销售额 FROM sales WHERE month='2026-08' LIMIT 50"
AUGUST_SUMMARY = SUMMARY_TEMPLATE.format(
    metric="销售额", time="2026年8月", group="无", caliber="未说明"
)


def _pipeline(model: SmallModel) -> RewriteQueryPipeline:
    return RewriteQueryPipeline(
        rewriter=Rewriter(model),
        query_conversation=StubQueryConversation(),
    )


def _keyword_pipeline() -> RewriteQueryPipeline:
    return _pipeline(KeywordSmallModel())


class GarbledSmallModel:
    """不按契约说话的小模型，用来观察改写问题怎么退。"""

    def complete(self, prompt: str) -> str:
        del prompt
        return "我不太确定你问的是什么。"


def test_empty_slots_rewrite_from_current_question_only() -> None:
    result = run_turn(
        "2026年8月销售额是多少",
        brain=Planner(SALES_CUES),
        query_pipeline=_keyword_pipeline(),
    )

    assert result.query_tool_call_count == 1
    assert result.query_tool_arguments == {"当前问题": "2026年8月销售额是多少"}
    assert result.rewritten_question == "查询2026年8月的销售额"
    assert result.query_summary == AUGUST_SUMMARY
    assert result.rewrite_output == (
        "改写问题：查询2026年8月的销售额\n"
        f"需求说明：{AUGUST_SUMMARY}"
    )


def test_follow_up_by_city_stacks_time_and_group() -> None:
    result = run_turn(
        "那按城市呢",
        brain=Planner(SALES_CUES),
        slots=ProgramSlots(last_sql=SALES_DDL, query_summary=AUGUST_SUMMARY),
        query_pipeline=_keyword_pipeline(),
    )

    assert result.query_tool_call_count == 1
    assert result.query_tool_arguments == {"当前问题": "那按城市呢"}
    assert result.rewritten_question == "查询2026年8月按城市分组的销售额"
    assert result.query_summary == SUMMARY_TEMPLATE.format(
        metric="销售额", time="2026年8月", group="按城市分组", caliber="未说明"
    )
    assert result.rewrite_output == (
        "改写问题：查询2026年8月按城市分组的销售额\n"
        f"需求说明：{result.query_summary}"
    )
    assert "SELECT" not in result.rewrite_output
    assert "SQL" not in result.rewrite_output
    assert result.rewrite_output.count("\n") == 1


def test_missing_time_caliber_filter_are_unspecified_not_invented() -> None:
    result = run_turn(
        "销售额是多少",
        brain=Planner(SALES_CUES),
        query_pipeline=_keyword_pipeline(),
    )

    assert result.query_tool_call_count == 1
    assert result.rewritten_question == "查询销售额"
    assert result.query_summary == SUMMARY_TEMPLATE.format(
        metric="销售额", time="未说明", group="无", caliber="未说明"
    )
    assert result.rewrite_output == (
        "改写问题：查询销售额\n"
        f"需求说明：{result.query_summary}"
    )
    assert "2026" not in result.rewrite_output
    assert "城市" not in result.rewrite_output
    assert "含税" not in result.rewrite_output
    assert "SELECT" not in result.rewrite_output


def test_rewrite_extracts_the_question_line_and_takes_the_rest_as_summary() -> None:
    """两段契约：改写问题精确提取，需求说明拿走余下全文。

    「摘要里不许有 SQL / 解释」是提示词层的约束，不是解析层的白名单——
    摘要既然是自由文本，解析层就没有可依据的白名单。这条在下面单测提示词。
    """
    result = run_turn(
        "销售额是多少",
        brain=Planner(SALES_CUES),
        query_pipeline=_pipeline(ExtraLinesSmallModel()),
    )

    assert result.query_tool_call_count == 1
    assert result.rewritten_question == "查询销售额"
    assert result.query_summary == (
        "口径未说明\nSELECT SUM(amount) FROM sales\n解释：这是改写结果"
    )
    assert result.rewrite_output == (
        "改写问题：查询销售额\n"
        "需求说明：口径未说明\n"
        "SELECT SUM(amount) FROM sales\n"
        "解释：这是改写结果"
    )


def test_prompt_carries_the_no_sql_no_explanation_rule_to_the_model() -> None:
    """摘要是自由文本，解析层挡不住 SQL；唯一挡得住的是喂给模型的规则。"""
    prompt = build_rewrite_prompt(
        "销售额是多少",
        ProgramSlots(last_sql=None, query_summary=None),
    )

    assert REWRITE_PREFIX in prompt
    assert "不要 SQL" in prompt
    assert "不要解释" in prompt


def test_unparseable_rewrite_falls_back_to_current_question() -> None:
    """认不出「改写问题」时退回当前问题，不编一句假的，也不吞掉模型原文。"""
    result = run_turn(
        "销售额是多少",
        brain=Planner(SALES_CUES),
        query_pipeline=_pipeline(GarbledSmallModel()),
    )

    assert result.query_tool_call_count == 1
    assert result.rewritten_question == "销售额是多少"
    assert result.query_summary == ""
    assert result.rewrite_output == "改写问题：销售额是多少\n需求说明："


def test_small_model_sees_slots_and_current_question_not_dialogue() -> None:
    model = RecordingSmallModel(KeywordSmallModel())
    session = Session(
        messages=(
            Message(role=USER, content="2026年8月销售额是多少"),
            Message(role=MAIN_AGENT, content="查到了。还可以按城市拆。"),
        )
    )

    result = run_turn(
        "那按城市呢",
        brain=Planner(SALES_CUES),
        session=session,
        slots=ProgramSlots(last_sql=SALES_DDL, query_summary=AUGUST_SUMMARY),
        query_pipeline=_pipeline(model),
    )

    assert result.query_tool_call_count == 1
    assert len(model.prompts) == 1
    prompt = model.prompts[0]
    assert "那按城市呢" in prompt
    assert SALES_DDL in prompt
    assert AUGUST_SUMMARY in prompt
    assert "查到了。还可以按城市拆。" not in prompt
    assert result.rewritten_question == "查询2026年8月按城市分组的销售额"
