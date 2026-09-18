"""真适配器接进唯一测试缝：一轮。

被测对象是 `deepseek.py` 的三个适配器（规划者 / 小模型 / 查库大模型）加
`sqlite_executor.py` 的只读执行器。网络那一段由 `ScriptedDeepSeekClient` 替身接住，
真解析、真执行、真库一个都不换；SQL 落在 `tmp_path` 里的真 SQLite 文件上。

数据与表名都是中性的（item / kind / size），不是任何业务场景。
业务词的示踪弹只住在 `fakes.py`，ADR 0002 的 grep 覆盖不到本文件。
"""

import sqlite3
from pathlib import Path

import pytest

from textsql_agent.deepseek import (
    UNPARSED_REPLY,
    DeepSeekPlanner,
    DeepSeekQueryLlm,
    DeepSeekSmallModel,
)
from textsql_agent.query_tool import ReadonlySqlQueryPipeline
from textsql_agent.rag import InMemoryVanna
from textsql_agent.rewrite import Rewriter
from textsql_agent.slots import ProgramSlots
from textsql_agent.sqlite_executor import SqliteReadonlyExecutor
from textsql_agent.turn import run_turn

from .fakes import ScriptedDeepSeekClient, planner_call_tool, planner_speak, rewrite_reply

ITEM_DDL = "CREATE TABLE item (id TEXT PRIMARY KEY, kind TEXT, size INTEGER, tag TEXT)"
ITEM_DOC = "item 表的 kind 记录条目分类，alpha 与 beta 各占一半。"
SAMPLE_QUESTION = "query all item rows"
SAMPLE_SQL = "SELECT id FROM item"
ITEM_ROWS = 300
ALPHA_ROWS = ITEM_ROWS // 2

QUESTION = "query all item rows"
REWRITTEN = "query all item rows"
SUMMARY = "this turn asks for every item row"

FILTERED_SQL = "SELECT id, kind FROM item WHERE kind='alpha' LIMIT 3"
FULL_SQL = "SELECT id FROM item"
BAD_SQL = "SELECT nope FROM item"
WRITE_SQL = "```sql\nSELECT 1; DROP TABLE item\n```"
DENIED_MESSAGE = "SQL 里出现了只读查询之外的语句"
SLOW_SQL = "SELECT COUNT(*) FROM item a, item b, item c, item d"
TIMEOUT_MESSAGE = "超时"


def _make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(ITEM_DDL)
        connection.executemany(
            "INSERT INTO item (id, kind, size, tag) VALUES (?, ?, ?, ?)",
            [
                (
                    f"item-{index:03d}",
                    "alpha" if index % 2 else "beta",
                    index,
                    "odd" if index % 2 else "even",
                )
                for index in range(1, ITEM_ROWS + 1)
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _trained_vanna() -> InMemoryVanna:
    vanna = InMemoryVanna()
    vanna.train(ddl=ITEM_DDL)
    vanna.train(documentation=ITEM_DOC)
    vanna.train(question=SAMPLE_QUESTION, sql=SAMPLE_SQL)
    return vanna


@pytest.fixture(name="db_path")
def fixture_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "items.sqlite3"
    _make_db(path)
    return path


def _stack(db_path: Path, client: ScriptedDeepSeekClient, *, row_limit: int = 50):
    return (
        DeepSeekPlanner(client),
        ReadonlySqlQueryPipeline(
            rewriter=Rewriter(DeepSeekSmallModel(client)),
            query_llm=DeepSeekQueryLlm(client),
            executor=SqliteReadonlyExecutor(db_path, row_limit=row_limit),
            vanna=_trained_vanna(),
        ),
    )


def _count_rows(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute("SELECT COUNT(*) FROM item")
        row = cursor.fetchone()
    finally:
        connection.close()
    return int(row[0])


def test_wired_turn_answers_from_the_real_sqlite_file(db_path: Path) -> None:
    client = ScriptedDeepSeekClient(
        (
            planner_call_tool(QUESTION),
            rewrite_reply(REWRITTEN, SUMMARY),
            FILTERED_SQL,
            planner_speak("一共 150 条，给你看前 3 条。"),
        )
    )
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    assert result.query_tool_call_count == 1
    assert result.query_tool_arguments == {"当前问题": QUESTION}
    assert result.rewritten_question == REWRITTEN
    assert result.query_summary == SUMMARY
    assert result.sql_text == FILTERED_SQL
    assert result.last_sql == FILTERED_SQL
    assert result.readonly_call_count == 1
    assert result.readonly_returns[0].kind == "有行"
    assert result.matched_rows == ALPHA_ROWS
    assert result.sql_session_discarded is True
    assert result.miss_logged is False
    assert result.reply == "一共 150 条，给你看前 3 条。"
    # 回填里同时有基数和采样行数——「从 150 条里回填 3 条」这句就是采样说明。
    assert f"命中: {ALPHA_ROWS} 条，回填 3 条" in (result.query_tool_fillback or "")
    assert "item-001" in (result.query_tool_fillback or "")
    assert "alpha" in (result.query_tool_fillback or "")


def test_material_reaches_each_prompt(db_path: Path) -> None:
    """每一跳吃到的材料都要能在真 prompt 里找到，可观察性从提示词层就成立。"""
    client = ScriptedDeepSeekClient(
        (
            planner_call_tool(QUESTION),
            rewrite_reply(REWRITTEN, SUMMARY),
            FILTERED_SQL,
            planner_speak("查到了。"),
        )
    )
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    # 第 0 次：规划者看见用户原话与 JSON 契约。
    assert QUESTION in client.prompts[0]
    assert "只回一个 JSON 对象" in client.prompts[0]
    # 第 1 次：小模型看见当前问题和两个可空的上一轮槽。
    assert f"当前问题：\n{QUESTION}" in client.prompts[1]
    assert "上次SQL：\n\n" in client.prompts[1]
    # 第 2 次：查库大模型看见改写问题与三路材料。
    assert f"改写问题：\n{REWRITTEN}" in client.prompts[2]
    assert ITEM_DDL in client.prompts[2]
    assert ITEM_DOC in client.prompts[2]
    assert f"问题：{SAMPLE_QUESTION}" in client.prompts[2]
    # 第 3 次：规划者看见查数工具的回填，而不是裸数。
    assert result.query_tool_fillback in client.prompts[3]
    # 只有规划者要 JSON；小模型和查库大模型都要原文。
    assert client.json_modes == [True, False, False, True]


def test_trailing_limit_does_not_shrink_the_reported_cardinality(db_path: Path) -> None:
    """LIMIT 只削回填行数，不削基数——否则「从几条里取几条」是假的。"""
    client = ScriptedDeepSeekClient(
        (
            planner_call_tool(QUESTION),
            rewrite_reply(REWRITTEN, SUMMARY),
            f"{FULL_SQL} LIMIT 3",
            planner_speak("查到。"),
        )
    )
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    assert result.matched_rows == ITEM_ROWS
    assert result.readonly_returns[0].matched_rows == ITEM_ROWS
    assert f"命中: {ITEM_ROWS} 条，回填 3 条" in (result.query_tool_fillback or "")


def test_row_limit_keeps_cardinality_separate_from_fillback(db_path: Path) -> None:
    """执行器的取行上限再小，基数也还是整表的。"""
    client = ScriptedDeepSeekClient(
        (
            planner_call_tool(QUESTION),
            rewrite_reply(REWRITTEN, SUMMARY),
            FULL_SQL,
            planner_speak("查到。"),
        )
    )
    brain, pipeline = _stack(db_path, client, row_limit=5)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    assert result.matched_rows == ITEM_ROWS
    assert f"命中: {ITEM_ROWS} 条，回填 5 条" in (result.query_tool_fillback or "")
    assert "item-006" not in (result.query_tool_fillback or "")


def test_sql_error_is_retried_inside_the_session(db_path: Path) -> None:
    client = ScriptedDeepSeekClient(
        (
            planner_call_tool(QUESTION),
            rewrite_reply(REWRITTEN, SUMMARY),
            BAD_SQL,
            FULL_SQL,
            planner_speak("查到。"),
        )
    )
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    assert result.readonly_call_count == 2
    assert [item.kind for item in result.readonly_returns] == ["报错", "有行"]
    assert "no such column" in result.readonly_returns[0].payload
    assert result.last_sql == FULL_SQL
    # 第二次的 SQL 提示词里带着上一次的报错，模型才改得动。
    assert BAD_SQL in client.prompts[3]
    assert "报错" in client.prompts[3]


def test_empty_answers_exhaust_the_fuse_and_leave_slots_untouched(db_path: Path) -> None:
    """模型五句都不给 SQL：五次空 SQL 算失败，旧槽一动不动。"""
    prior = ProgramSlots(last_sql="SELECT old FROM item", query_summary="上一轮的说明原文")
    client = ScriptedDeepSeekClient(
        (
            planner_call_tool(QUESTION),
            rewrite_reply(REWRITTEN, SUMMARY),
            "嗯，我看看。",
            "再想想。",
            "这个不好说。",
            "让我组织一下。",
            "总之。",
            planner_speak("这次没查成。"),
        )
    )
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, slots=prior, query_pipeline=pipeline)

    assert result.readonly_call_count == 5
    assert [item.kind for item in result.readonly_returns] == ["报错"] * 5
    assert result.readonly_returns[0].payload == "空 SQL"
    assert result.query_tool_fillback == "SQL: \n报错: 空 SQL"
    assert result.last_sql == prior.last_sql
    assert result.query_summary == prior.query_summary
    assert result.matched_rows is None
    assert result.miss_logged is False
    assert result.sql_session_discarded is True
    # 失败的原话原样进了规划者的上下文，主 Agent 才知道该说什么。
    assert result.query_tool_fillback in client.prompts[-1]


def test_write_attempt_is_denied_and_the_file_is_unchanged(db_path: Path) -> None:
    """写库尝试连跑五次都被拒；跑完直接读文件，证明它真的没动。"""
    client = ScriptedDeepSeekClient(
        (
            planner_call_tool(QUESTION),
            rewrite_reply(REWRITTEN, SUMMARY),
            WRITE_SQL,
            WRITE_SQL,
            WRITE_SQL,
            WRITE_SQL,
            WRITE_SQL,
            planner_speak("查不了。"),
        )
    )
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    assert result.readonly_call_count == 5
    assert [item.kind for item in result.readonly_returns] == ["报错"] * 5
    assert result.readonly_returns[0].payload == DENIED_MESSAGE
    assert result.readonly_returns[0].sql == "SELECT 1; DROP TABLE item"
    assert result.matched_rows is None
    # 环境态验证：表还在，行数一条没少。
    assert _count_rows(db_path) == ITEM_ROWS


def test_timeout_counts_as_a_failure_and_retries(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("textsql_agent.sql_session.TIMEOUT_SECONDS", 1)
    client = ScriptedDeepSeekClient(
        (
            planner_call_tool(QUESTION),
            rewrite_reply(REWRITTEN, SUMMARY),
            SLOW_SQL,
            f"{FULL_SQL} LIMIT 2",
            planner_speak("查到。"),
        )
    )
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    assert result.readonly_call_count == 2
    assert [item.kind for item in result.readonly_returns] == ["超时", "有行"]
    assert result.readonly_returns[0].payload == TIMEOUT_MESSAGE
    assert result.matched_rows == ITEM_ROWS
    assert result.last_sql == f"{FULL_SQL} LIMIT 2"


def test_unparseable_planner_output_speaks_instead_of_guessing(db_path: Path) -> None:
    client = ScriptedDeepSeekClient(("嗯，我看看你的意思。",))
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    assert result.reply == UNPARSED_REPLY
    assert result.query_tool_call_count == 0
    assert result.readonly_call_count == 0
    assert client.call_count == 1


def test_planner_will_not_call_an_unmounted_tool(db_path: Path) -> None:
    """规划者点了一个不在已挂载工具里的名字：不猜、不降级成一个别的工具。"""
    client = ScriptedDeepSeekClient(
        (
            '{"动作":"调用工具","工具":"联网搜索","参数":{"查询":"item"}}',
            planner_speak("我不会用没挂的工具。"),
        )
    )
    brain, pipeline = _stack(db_path, client)

    result = run_turn(QUESTION, brain=brain, query_pipeline=pipeline)

    assert result.query_tool_call_count == 0
    assert result.web_search_call_count == 0
    assert result.readonly_call_count == 0
    assert result.reply == "我不会用没挂的工具。"
    assert result.mounted_tools == ("查数工具",)
