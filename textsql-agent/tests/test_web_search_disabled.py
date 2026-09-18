"""工单 07：联网搜索契约在，默认不挂。

唯一测试缝：一轮。夹具可注入搜索引擎替身，送一句用户话，
断言挂载列表、本轮联网搜索次数、引擎是否被调用。
默认关闭时次数必须为 0，外网引擎不得被调。
"""

from textsql_agent.tools import QUERY_TOOL, WEB_SEARCH
from textsql_agent.turn import run_turn
from textsql_agent.web_search import SearchHit

from .fakes import (
    CallWebSearchThenSpeakBrain,
    RecordingSearchEngine,
    SearchRepeatedlyBrain,
)


def test_web_search_stays_unmounted_and_engine_is_not_invoked() -> None:
    engine = RecordingSearchEngine()

    result = run_turn(
        "含税口径是什么？",
        brain=CallWebSearchThenSpeakBrain(),
        search_engine=engine,
    )

    assert WEB_SEARCH not in result.mounted_tools
    assert QUERY_TOOL in result.mounted_tools
    assert result.web_search_call_count == 0
    assert result.query_tool_call_count == 0
    assert engine.call_count == 0
    assert result.reply == "联网搜索未挂载，只能说话。"


def test_enabled_web_search_returns_at_most_five_hits_with_truncated_snippets() -> None:
    long_snippet = "甲" * 400
    engine = RecordingSearchEngine(
        hits=tuple(
            SearchHit(title=f"标题{i}", url=f"https://example.com/{i}", snippet=long_snippet)
            for i in range(1, 7)
        )
    )

    result = run_turn(
        "含税口径",
        brain=CallWebSearchThenSpeakBrain(),
        search_engine=engine,
        web_search_enabled=True,
    )

    assert WEB_SEARCH in result.mounted_tools
    assert result.web_search_call_count == 1
    assert result.web_search_query == "含税口径"
    assert engine.calls == [("含税口径", 15)]
    assert len(result.web_search_hits) == 5
    assert result.web_search_hits[0] == SearchHit(
        title="标题1",
        url="https://example.com/1",
        snippet="甲" * 300,
    )
    assert result.web_search_hits[-1].title == "标题5"
    assert result.web_search_error is None
    assert any(message.role == WEB_SEARCH for message in result.session.messages)
    assert "标题1" in result.reply


def test_enabled_web_search_failure_returns_error_verbatim() -> None:
    engine = RecordingSearchEngine(error="连接超时：example.com")

    result = run_turn(
        "含税口径",
        brain=CallWebSearchThenSpeakBrain(),
        search_engine=engine,
        web_search_enabled=True,
    )

    assert result.web_search_call_count == 1
    assert result.web_search_hits == ()
    assert result.web_search_error == "连接超时：example.com"
    assert result.reply == "公开资料：连接超时：example.com"
    assert "1234567.89" not in result.reply


def test_enabled_web_search_empty_hits_are_a_failure() -> None:
    engine = RecordingSearchEngine(hits=())

    result = run_turn(
        "含税口径",
        brain=CallWebSearchThenSpeakBrain(),
        search_engine=engine,
        web_search_enabled=True,
    )

    assert result.web_search_call_count == 1
    assert result.web_search_hits == ()
    assert result.web_search_error == "无结果"
    assert result.reply == "公开资料：无结果"


def test_disabled_path_never_reaches_web_search_fuse() -> None:
    engine = RecordingSearchEngine(
        hits=(SearchHit("标题", "https://example.com/a", "摘要"),)
    )

    result = run_turn(
        "先搜含税口径",
        brain=SearchRepeatedlyBrain(times=4),
        search_engine=engine,
    )

    assert WEB_SEARCH not in result.mounted_tools
    assert result.web_search_call_count == 0
    assert engine.call_count == 0
    assert result.reply == "停止搜索，对人说明公开口径。"


def test_enabled_web_search_fuse_caps_calls_at_three_per_ring_a_turn() -> None:
    engine = RecordingSearchEngine(
        hits=(SearchHit("标题", "https://example.com/a", "摘要"),)
    )

    result = run_turn(
        "先搜含税口径",
        brain=SearchRepeatedlyBrain(times=4),
        search_engine=engine,
        web_search_enabled=True,
    )

    assert engine.call_count == 3
    assert result.web_search_call_count == 3
    assert result.web_search_error == "本句环 A 联网搜索已达 3 次上限"
    assert result.reply == "停止搜索，对人说明公开口径。"


def test_repeated_search_past_the_fuse_still_gets_a_reply() -> None:
    """保险丝用尽之后还调搜索：拒绝，但要把说话的机会留给主 Agent。

    和查数工具的第二次调用是同一条规矩（F1）。times=5 才踩得到这一支：
    前 4 次决定里第 4 次只是让保险丝置位，第 5 次才真被拒绝。
    """
    engine = RecordingSearchEngine(
        hits=(SearchHit("标题", "https://example.com/a", "摘要"),)
    )

    result = run_turn(
        "先搜含税口径",
        brain=SearchRepeatedlyBrain(times=5),
        search_engine=engine,
        web_search_enabled=True,
    )

    assert engine.call_count == 3
    assert result.web_search_call_count == 3
    assert result.reply == "停止搜索，对人说明公开口径。"
    assert result.reply_is_fallback is False
