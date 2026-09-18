"""工单 01：环 A 一轮能说话。

唯一测试缝：一轮。夹具可注入会话（本工单为空会话），送一句用户话，
断言给用户的回复、本轮查数工具次数、本轮联网搜索次数。
环 A 用例查数工具必须为 0；默认关闭时联网搜索必须为 0。
"""

from textsql_agent.tools import BASH, QUERY_TOOL, WEB_SEARCH, WRITE_FILE
from textsql_agent.turn import run_turn

from .fakes import BlankSpeakBrain, CallMountedToolsBrain, SpeakOnlyBrain


def test_chitchat_gets_human_reply_without_calling_query_tool() -> None:
    result = run_turn("你好", brain=SpeakOnlyBrain("你好，我可以帮你问数，也可以先说明口径。"))

    assert result.reply == "你好，我可以帮你问数，也可以先说明口径。"
    assert result.query_tool_call_count == 0
    assert result.session.messages[0].content == "你好"
    assert result.session.messages[1].content == result.reply


def test_web_search_is_not_called_when_disabled() -> None:
    result = run_turn("销售额口径是什么？", brain=SpeakOnlyBrain("销售额口径是含税成交额。"))

    assert result.web_search_call_count == 0
    assert result.query_tool_call_count == 0


def test_runtime_mounts_query_tool_not_bash_write_or_web_search() -> None:
    result = run_turn("你好", brain=SpeakOnlyBrain("好的。"))

    assert QUERY_TOOL in result.mounted_tools
    assert BASH not in result.mounted_tools
    assert WRITE_FILE not in result.mounted_tools
    assert WEB_SEARCH not in result.mounted_tools


def test_caliber_question_does_not_present_unqueried_numbers_as_from_the_db() -> None:
    result = run_turn(
        "上个月销售额是多少？你先告诉我口径，先别查",
        brain=SpeakOnlyBrain("销售额口径是含税成交额。还没有查库，不能报精确数。"),
    )

    assert result.query_tool_call_count == 0
    assert result.reply == "销售额口径是含税成交额。还没有查库，不能报精确数。"
    assert "1234567.89" not in result.reply


def test_query_tool_call_count_is_observed_when_planner_calls_it() -> None:
    result = run_turn("你好", brain=CallMountedToolsBrain())

    assert result.query_tool_call_count == 1
    assert result.web_search_call_count == 0
    # 第二次调用被拒绝之后，整轮不能就此收场：工具对规划者不可见后它说了话。
    # 修 F1 之前这里 reply 是空串，用户什么也拿不到（.scratch/textsql-agent/observability.md）。
    assert result.reply == "只能说话。"
    assert result.reply_is_fallback is False


def test_blank_speech_counts_as_the_agent_not_speaking() -> None:
    result = run_turn("你好", brain=BlankSpeakBrain())

    assert result.reply.strip()
    assert result.reply_is_fallback is True
    assert result.session.messages[-1].content == result.reply


def test_ring_a_enriches_nothing_and_writes_no_miss_log() -> None:
    """环 A 不富化、不写未命中记录，也没有结果集基数可报。"""
    result = run_turn("你好", brain=SpeakOnlyBrain("你好。"))

    assert result.enrichment_call_count == 0
    assert result.miss_logged is False
    assert result.entities == ()
    assert result.enrichments == ()
    assert result.matched_rows is None
