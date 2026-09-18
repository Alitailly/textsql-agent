"""工单 06：查库对话真只读跑 SQL。

唯一测试缝：一轮。夹具注入会话、程序槽、查库大模型和只读库替身，
送一句用户话，断言回填、只读次数与每次返回、结束后上次 SQL 与查询摘要、
查库对话是否被丢掉。系统不因 0 行自动再调查数工具。
"""

from textsql_agent.planner import Planner
from textsql_agent.query_tool import ReadonlySqlQueryPipeline
from textsql_agent.rag import InMemoryVanna, RagHits
from textsql_agent.rewrite import Rewriter
from textsql_agent.slots import ProgramSlots
from textsql_agent.sql_session import READONLY_SQL_TOOL, RowSet, SqlError, Timeout
from textsql_agent.turn import run_turn

from .fakes import SUMMARY_TEMPLATE, KeywordSmallModel, ScriptedExecutor, ScriptedQueryLlm

SALES_DDL = "CREATE TABLE sales (amount INTEGER, month TEXT, city TEXT)"
SALES_DOC = "销售额对应 sales.amount，口径为含税成交额"
SAMPLE_QUESTION = "查询上个月的销售额"
SAMPLE_SQL = "SELECT SUM(amount) AS 销售额 FROM sales WHERE month='prev' LIMIT 50"
ROWS_SQL = "SELECT SUM(amount) AS 销售额 FROM sales LIMIT 50"
BAD_SQL = "SELECT SUM(amnt) AS 销售额 FROM sales LIMIT 50"
SQL_ERROR = "column amnt does not exist"
SALES_SUMMARY = SUMMARY_TEMPLATE.format(
    metric="销售额", time="上个月", group="无", caliber="未说明"
)
PRIOR_SUMMARY = "上一轮的说明原文"


def _trained_vanna() -> InMemoryVanna:
    vanna = InMemoryVanna()
    vanna.train(ddl=SALES_DDL)
    vanna.train(documentation=SALES_DOC)
    vanna.train(question=SAMPLE_QUESTION, sql=SAMPLE_SQL)
    return vanna


def _pipeline(
    llm: ScriptedQueryLlm,
    executor: ScriptedExecutor,
    *,
    vanna: InMemoryVanna | None = None,
) -> ReadonlySqlQueryPipeline:
    return ReadonlySqlQueryPipeline(
        rewriter=Rewriter(KeywordSmallModel()),
        vanna=vanna or _trained_vanna(),
        query_llm=llm,
        executor=executor,
    )


def test_rows_fill_back_sql_and_result_then_overwrite_slots_and_drop_session() -> None:
    prior = ProgramSlots(
        last_sql="SELECT old FROM sales",
        query_summary=PRIOR_SUMMARY,
    )
    llm = ScriptedQueryLlm((ROWS_SQL,))
    executor = ScriptedExecutor(
        (RowSet(columns=("销售额",), rows=(("1280000",),)),)
    )

    result = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        slots=prior,
        query_pipeline=_pipeline(llm, executor),
    )

    assert result.query_tool_call_count == 1
    assert result.query_tool_fillback == (
        f"SQL: {ROWS_SQL}\n命中: 1 条，回填 1 条\n结果: 销售额 1280000"
    )
    assert result.matched_rows == 1
    assert result.sql_text == ROWS_SQL
    assert result.last_sql == ROWS_SQL
    assert result.query_summary == SALES_SUMMARY
    assert result.readonly_call_count == 1
    assert result.readonly_returns[0].kind == "有行"
    assert result.sql_session_discarded is True
    assert "1280000" in result.reply
    assert "SELECT" not in result.reply
    assert result.rag_hits == RagHits(
        ddl=SALES_DDL,
        documentation=SALES_DOC,
        question_sql=f"问题：{SAMPLE_QUESTION}\nSQL：{SAMPLE_SQL}",
    )
    assert llm.turns[0].rag_hits == result.rag_hits
    assert llm.turns[0].tools == (READONLY_SQL_TOOL,)
    assert executor.calls == [(ROWS_SQL, 30)]


def test_row_fillback_truncates_to_fifty_rows() -> None:
    rows = tuple((f"行{i}",) for i in range(1, 61))
    result = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        query_pipeline=_pipeline(
            ScriptedQueryLlm((ROWS_SQL,)),
            ScriptedExecutor((RowSet(columns=("name",), rows=rows),)),
        ),
    )

    assert result.query_tool_call_count == 1
    assert result.readonly_returns[0].kind == "有行"
    assert "name 行50" in (result.query_tool_fillback or "")
    assert "name 行51" not in (result.query_tool_fillback or "")
    # 命中基数与回填行数分离：这两句就是「从 60 人里取 50 条」的采样说明。
    assert result.matched_rows == 60
    assert result.readonly_returns[0].matched_rows == 60
    assert "命中: 60 条，回填 50 条" in (result.query_tool_fillback or "")


def test_row_fillback_truncates_to_about_4000_chars() -> None:
    result = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        query_pipeline=_pipeline(
            ScriptedQueryLlm((ROWS_SQL,)),
            ScriptedExecutor((RowSet(columns=("x",), rows=(("甲" * 5000,),)),)),
        ),
    )

    assert result.query_tool_fillback is not None
    result_text = result.query_tool_fillback.split("结果:", 1)[1].strip()
    assert len(result_text) == 4000
    assert "甲" * 5000 not in result.query_tool_fillback


def test_zero_rows_says_not_found_overwrites_slots_and_does_not_query_again() -> None:
    prior = ProgramSlots(
        last_sql="SELECT old FROM sales",
        query_summary=PRIOR_SUMMARY,
    )
    llm = ScriptedQueryLlm((ROWS_SQL,))
    executor = ScriptedExecutor((RowSet(columns=("销售额",), rows=()),))

    result = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        slots=prior,
        query_pipeline=_pipeline(llm, executor),
    )

    assert result.query_tool_call_count == 1
    assert result.query_tool_fillback == "没查到"
    assert result.reply == "没查到"
    assert result.last_sql == ROWS_SQL
    assert result.query_summary == SALES_SUMMARY
    assert result.readonly_call_count == 1
    assert result.readonly_returns[0].kind == "0行"
    assert result.readonly_returns[0].matched_rows == 0
    assert result.matched_rows == 0
    assert result.sql_session_discarded is True
    assert executor.calls == [(ROWS_SQL, 30)]
    assert len(llm.turns) == 1


def test_sql_error_counts_and_retries_inside_the_session() -> None:
    vanna = _trained_vanna()
    llm = ScriptedQueryLlm((BAD_SQL, ROWS_SQL))
    executor = ScriptedExecutor(
        (
            SqlError(SQL_ERROR),
            RowSet(columns=("销售额",), rows=(("1280000",),)),
        )
    )

    result = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        query_pipeline=_pipeline(llm, executor, vanna=vanna),
    )

    assert result.query_tool_call_count == 1
    assert result.readonly_call_count == 2
    assert [item.kind for item in result.readonly_returns] == ["报错", "有行"]
    assert result.readonly_returns[0].payload == SQL_ERROR
    assert result.last_sql == ROWS_SQL
    assert "1280000" in result.reply
    assert "SELECT" not in result.reply
    assert llm.turns[1].prior_returns[0].kind == "报错"
    assert vanna.retrieve_queries == [SAMPLE_QUESTION]


def test_timeout_counts_and_retries_inside_the_session() -> None:
    llm = ScriptedQueryLlm(("SELECT * FROM sales", ROWS_SQL))
    executor = ScriptedExecutor(
        (
            Timeout(),
            RowSet(columns=("销售额",), rows=(("1280000",),)),
        )
    )

    result = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        query_pipeline=_pipeline(llm, executor),
    )

    assert result.query_tool_call_count == 1
    assert result.readonly_call_count == 2
    assert [item.kind for item in result.readonly_returns] == ["超时", "有行"]
    assert result.last_sql == ROWS_SQL
    assert executor.calls[0] == ("SELECT * FROM sales", 30)
    assert executor.calls[1] == (ROWS_SQL, 30)
    assert llm.turns[1].prior_returns[0].kind == "超时"


def test_five_failures_keep_old_slots_drop_summary_and_drop_session() -> None:
    prior = ProgramSlots(
        last_sql="SELECT good FROM sales LIMIT 50",
        query_summary=PRIOR_SUMMARY,
    )
    failed_sqls = tuple(f"SELECT bad{index} FROM sales" for index in range(5))
    llm = ScriptedQueryLlm(failed_sqls)
    executor = ScriptedExecutor(tuple(SqlError(f"err{index}") for index in range(5)))

    result = run_turn(
        "2026年8月销售额是多少",
        brain=Planner(),
        slots=prior,
        query_pipeline=_pipeline(llm, executor),
    )

    assert result.query_tool_call_count == 1
    assert result.readonly_call_count == 5
    assert [item.kind for item in result.readonly_returns] == ["报错"] * 5
    assert result.last_sql == "SELECT good FROM sales LIMIT 50"
    assert result.query_summary == prior.query_summary
    assert result.query_tool_fillback == f"SQL: {failed_sqls[-1]}\n报错: err4"
    assert result.sql_text == failed_sqls[-1]
    assert result.reply == "查库失败：err4"
    # 失败用尽不是「空结果」而是「没查成」：不写未命中记录，也没有基数。
    assert result.miss_logged is False
    assert result.matched_rows is None
    assert result.sql_session_discarded is True
    assert result.rewritten_question == "查询2026年8月的销售额"
    assert len(llm.turns) == 5


def test_next_turn_starts_a_fresh_sql_session() -> None:
    llm = ScriptedQueryLlm((BAD_SQL, ROWS_SQL, ROWS_SQL))
    executor = ScriptedExecutor(
        (
            SqlError(SQL_ERROR),
            RowSet(columns=("销售额",), rows=(("1",),)),
            RowSet(columns=("销售额",), rows=(("2",),)),
        )
    )
    pipeline = _pipeline(llm, executor)

    first = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        query_pipeline=pipeline,
    )
    second = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        session=first.session,
        query_pipeline=pipeline,
    )

    assert first.readonly_call_count == 2
    assert second.readonly_call_count == 1
    assert first.sql_session_discarded is True
    assert second.sql_session_discarded is True
    assert llm.turns[2].prior_returns == ()


def test_query_llm_only_has_the_readonly_sql_tool() -> None:
    llm = ScriptedQueryLlm((ROWS_SQL,))
    result = run_turn(
        "上个月销售额多少",
        brain=Planner(),
        query_pipeline=_pipeline(
            llm,
            ScriptedExecutor((RowSet(columns=("销售额",), rows=(("1280000",),)),)),
        ),
    )

    assert result.sql_session_tools == (READONLY_SQL_TOOL,)
    assert result.web_search_call_count == 0
    assert llm.turns[0].tools == (READONLY_SQL_TOOL,)
    assert "问用户" not in result.sql_session_tools
    assert "联网搜索" not in result.sql_session_tools
    assert "Vanna RAG" not in result.sql_session_tools

