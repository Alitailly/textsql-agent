"""一轮：送入一句用户话，主 Agent 只规划一次。

本模块是规格里唯一测试缝的公共接口。
"""

from __future__ import annotations

from dataclasses import dataclass

from textsql_agent.brain import CallTool, MainAgentBrain, Speak, TurnInput
from textsql_agent.prompts import MAIN_AGENT_INSTRUCTIONS
from textsql_agent.query_tool import QueryPipeline, UnwiredQueryPipeline
from textsql_agent.rag import RagHits
from textsql_agent.session import MAIN_AGENT, USER, Message, Session
from textsql_agent.slots import ProgramSlots
from textsql_agent.sql_session import ReadonlyReturn
from textsql_agent.tools import DEFAULT_MOUNTED_TOOLS, QUERY_TOOL, WEB_SEARCH
from textsql_agent.web_search import (
    WEB_SEARCH_MAX_CALLS_PER_RING_A,
    WEB_SEARCH_QUERY_KEY,
    SearchEngine,
    SearchFailure,
    SearchHit,
    SearchSuccess,
    WebSearch,
    fillback_text,
)

# 查数：调用+说话；搜索：最多 3 次 + 保险丝回填 + 说话。
_TURN_DECISION_CAP = WEB_SEARCH_MAX_CALLS_PER_RING_A + 3

# 工具本轮用完、主 Agent 却始终没开口时的兜底话术。
# 宁可明说，也不要静默回一个空串——空串在网页上等于"什么也没发生"，
# 用户分不清是系统坏了、没查到，还是没人回答。
# 判断这一句是不是兜底，看 TurnResult.reply_is_fallback，不要靠认这行字。
NO_REPLY_FALLBACK = "抱歉，这一轮没能给出回答。请再问一次，或者把问题换个说法。"


@dataclass(frozen=True)
class TurnResult:
    """一轮结束后的可观察结果。"""

    reply: str
    query_tool_call_count: int
    web_search_call_count: int
    mounted_tools: tuple[str, ...]
    session: Session
    reply_is_fallback: bool = False
    """True 表示主 Agent 本轮始终没开口，reply 是兜底话术而不是它的回答。"""
    query_tool_arguments: dict[str, str] | None = None
    query_tool_fillback: str | None = None
    last_sql: str | None = None
    query_summary: str | None = None
    rewritten_question: str | None = None
    rewrite_output: str | None = None
    rag_hits: RagHits | None = None
    sql_text: str | None = None
    readonly_call_count: int = 0
    readonly_returns: tuple[ReadonlyReturn, ...] = ()
    matched_rows: int | None = None
    """最后一次只读调用的结果集基数。失败用尽时为 None。"""
    entities: tuple[dict[str, str], ...] = ()
    """B6.2 解析出的实体。本轮不实现，先留可观察位。"""
    enrichments: tuple[str, ...] = ()
    """每个实体对应的公开补充。本轮不实现，先留可观察位。"""
    enrichment_call_count: int = 0
    """本轮富化调用了几次。环 A 必须为 0。"""
    miss_logged: bool = False
    """本轮是否写了未命中记录。环 A、以及查库失败用尽时都必须为 False。"""
    sql_session_discarded: bool = False
    sql_session_tools: tuple[str, ...] = ()
    web_search_query: str | None = None
    web_search_hits: tuple[SearchHit, ...] = ()
    web_search_error: str | None = None


def run_turn(
    utterance: str,
    *,
    brain: MainAgentBrain,
    session: Session | None = None,
    query_pipeline: QueryPipeline | None = None,
    slots: ProgramSlots | None = None,
    search_engine: SearchEngine | None = None,
    web_search_enabled: bool = False,
) -> TurnResult:
    """跑完一句用户话：规划一次；要查数则调查数工具一次再回复。"""
    prior = session or Session()
    current_slots = slots or ProgramSlots()
    mounted_tools = DEFAULT_MOUNTED_TOOLS
    pipeline = query_pipeline or UnwiredQueryPipeline()
    web_search: WebSearch | None = None
    if web_search_enabled:
        if search_engine is None:
            raise TypeError("开启联网搜索时必须提供搜索引擎")
        mounted_tools = (*DEFAULT_MOUNTED_TOOLS, WEB_SEARCH)
        web_search = WebSearch(search_engine)
    messages: tuple[Message, ...] = prior.messages + (
        Message(role=USER, content=utterance),
    )
    query_tool_call_count = 0
    web_search_call_count = 0
    query_tool_arguments: dict[str, str] | None = None
    query_tool_fillback: str | None = None
    rewritten_question: str | None = None
    rewrite_output: str | None = None
    rag_hits: RagHits | None = None
    sql_text: str | None = None
    readonly_returns: tuple[ReadonlyReturn, ...] = ()
    entities: tuple[dict[str, str], ...] = ()
    enrichments: tuple[str, ...] = ()
    enrichment_call_count = 0
    miss_logged = False
    sql_session_discarded: bool = False
    sql_session_tools: tuple[str, ...] = ()
    web_search_query: str | None = None
    web_search_hits: tuple[SearchHit, ...] = ()
    web_search_error: str | None = None
    reply = ""
    web_search_fuse_exhausted = False
    # 本轮已经用过的工具：从规划者能看见的工具集里摘掉，而不是 break 掉整轮。
    # 依据是 CONTEXT.md「要查数时只调一次查数工具」——第二次调用照样拒绝，
    # 但拒绝之后要给主 Agent 机会，用手上已有的回填说话。
    refused_tools: set[str] = set()
    for _ in range(_TURN_DECISION_CAP):
        available_tools = tuple(
            tool for tool in mounted_tools if tool not in refused_tools
        )
        decision = brain.decide(
            TurnInput(
                utterance=utterance,
                session=Session(messages=messages),
                mounted_tools=available_tools,
                instructions=MAIN_AGENT_INSTRUCTIONS,
            )
        )
        if isinstance(decision, Speak):
            reply = decision.reply
            break
        if not isinstance(decision, CallTool):
            raise TypeError("本轮规划必须是说话或调用工具")
        if decision.name not in available_tools:
            continue
        if decision.name == QUERY_TOOL:
            if web_search_call_count > 0 or query_tool_call_count >= 1:
                refused_tools.add(QUERY_TOOL)
                continue
            query_tool_call_count = 1
            query_tool_arguments = dict(decision.arguments)
            query_outcome = pipeline.run(decision.arguments["当前问题"], current_slots)
            query_tool_fillback = query_outcome.fillback
            current_slots = query_outcome.slots
            rewritten_question = query_outcome.rewritten_question
            rewrite_output = query_outcome.rewrite_output
            rag_hits = query_outcome.rag_hits
            sql_text = query_outcome.sql_text
            readonly_returns = query_outcome.readonly_returns
            entities = query_outcome.entities
            enrichments = query_outcome.enrichments
            enrichment_call_count = query_outcome.enrichment_call_count
            miss_logged = query_outcome.miss_logged
            sql_session_discarded = query_outcome.sql_session_discarded
            sql_session_tools = query_outcome.sql_session_tools
            messages = messages + (
                Message(role=QUERY_TOOL, content=query_tool_fillback),
            )
            continue
        if decision.name == WEB_SEARCH and web_search is not None:
            if query_tool_call_count > 0 or web_search_fuse_exhausted:
                refused_tools.add(WEB_SEARCH)
                continue
            web_search_query = decision.arguments[WEB_SEARCH_QUERY_KEY]
            search_outcome = web_search.call(web_search_query)
            if web_search.call_count == web_search_call_count:
                web_search_fuse_exhausted = True
            else:
                web_search_call_count = web_search.call_count
            if isinstance(search_outcome, SearchSuccess):
                web_search_hits = search_outcome.hits
                web_search_error = None
            elif isinstance(search_outcome, SearchFailure):
                web_search_error = search_outcome.error
            messages = messages + (
                Message(role=WEB_SEARCH, content=fillback_text(search_outcome)),
            )
            continue
        break
    # 主 Agent 到计划次数用完都没开口（比如它认死了要调那个已经被摘掉的工具），
    # 或者它"说了"一句空白。兜一句，让用户至少知道这一轮没成，而不是对着空回复猜。
    # 用 strip 判：空白串在网页上和空串一样，什么都看不见。
    reply_is_fallback = not reply.strip()
    if reply_is_fallback:
        reply = NO_REPLY_FALLBACK
    next_session = Session(
        messages=messages + (Message(role=MAIN_AGENT, content=reply),)
    )
    return TurnResult(
        reply=reply,
        query_tool_call_count=query_tool_call_count,
        web_search_call_count=web_search_call_count,
        mounted_tools=mounted_tools,
        session=next_session,
        reply_is_fallback=reply_is_fallback,
        query_tool_arguments=query_tool_arguments,
        query_tool_fillback=query_tool_fillback,
        last_sql=current_slots.last_sql,
        query_summary=current_slots.query_summary,
        rewritten_question=rewritten_question,
        rewrite_output=rewrite_output,
        rag_hits=rag_hits,
        sql_text=sql_text,
        readonly_call_count=len(readonly_returns),
        readonly_returns=readonly_returns,
        matched_rows=(
            readonly_returns[-1].matched_rows if readonly_returns else None
        ),
        entities=entities,
        enrichments=enrichments,
        enrichment_call_count=enrichment_call_count,
        miss_logged=miss_logged,
        sql_session_discarded=sql_session_discarded,
        sql_session_tools=sql_session_tools,
        web_search_query=web_search_query,
        web_search_hits=web_search_hits,
        web_search_error=web_search_error,
    )
