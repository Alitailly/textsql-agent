"""一轮测试用的系统边界替身。小模型 / Vanna RAG / 查库大模型 / 库 / 联网搜索
由后续工单替换；本工单只替主 Agent 的规划者（外部模型边界）。

ADR 0002：业务词（销售额/上个月/按城市/含税）只存在于本文件，src/ 里一个都不留。
`KeywordSmallModel` 与 `SALES_CUES` 都是从 src/ 搬出来的示踪弹替身，不是通用设计。
"""

import json
import re

from textsql_agent.brain import CallTool, Speak, TurnInput
from textsql_agent.planner import PlannerCues
from textsql_agent.query_tool import QueryConversationResult, QueryToolOutcome
from textsql_agent.rag import RagHits
from textsql_agent.rewrite import SmallModel
from textsql_agent.slots import ProgramSlots
from textsql_agent.sql_session import RowSet, SqlError, SqlSessionTurn, Timeout
from textsql_agent.tools import QUERY_TOOL, WEB_SEARCH
from textsql_agent.web_search import SearchFailure, SearchHit, SearchSuccess

SALES_CUES = PlannerCues(
    caliber_answer="销售额口径是含税成交额。",
    metrics=("销售额",),
    times=("上个月",),
    confirm=("查吧",),
)

# 需求说明是自由文本，程序不解析。这里给出一份替身自己认得的写法，
# 只为让 KeywordSmallModel 能在下一轮把它读回来，不代表产品约束。
SUMMARY_TEMPLATE = "指标 {metric}；时间 {time}；过滤 {group}；口径 {caliber}"


class SpeakOnlyBrain:
    """永远不调查数工具、只说话的主 Agent 规划替身。"""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    def decide(self, turn_input: TurnInput) -> Speak:
        del turn_input
        return Speak(self._reply)


class BlankSpeakBrain:
    """"说话"但内容是空白。空白串在网页上和空串一样看不见，也算没开口。"""

    def decide(self, turn_input: TurnInput) -> Speak:
        del turn_input
        return Speak("   ")


class CallMountedToolsBrain:
    """若工具已挂载就调用，用来观察次数不是写死的。"""

    def decide(self, turn_input: TurnInput) -> CallTool | Speak:
        if QUERY_TOOL in turn_input.mounted_tools:
            return CallTool(QUERY_TOOL, {"当前问题": turn_input.utterance})
        if WEB_SEARCH in turn_input.mounted_tools:
            return CallTool(WEB_SEARCH, {"查询": turn_input.utterance})
        return Speak("只能说话。")


class QueryThenSpeakBrain:
    """先调查数工具一次，再按回填说话。当前问题由测试给定，不必等于用户原话。"""

    def __init__(self, current_question: str) -> None:
        self._current_question = current_question

    def decide(self, turn_input: TurnInput) -> CallTool | Speak:
        for message in turn_input.session.messages:
            if message.role == QUERY_TOOL:
                return Speak(_human_reply_from_fillback(message.content))
        return CallTool(QUERY_TOOL, {"当前问题": self._current_question})


class AlwaysCallQueryToolBrain:
    """同一句里反复调查数工具，用来观察第二次被拒绝。"""

    def decide(self, turn_input: TurnInput) -> CallTool:
        return CallTool(QUERY_TOOL, {"当前问题": turn_input.utterance})


class StubRowsQueryPipeline:
    """查数工具内部全替身：固定返回有行回填，并成对覆盖程序槽。"""

    def __init__(self, *, sql: str, amount: str, summary: str) -> None:
        self._sql = sql
        self._amount = amount
        self._summary = summary

    def run(self, current_question: str, slots: ProgramSlots) -> QueryToolOutcome:
        del current_question, slots
        return QueryToolOutcome(
            fillback=(
                f"SQL: {self._sql}\n命中: 1 条，回填 1 条\n结果: 销售额 {self._amount}"
            ),
            slots=ProgramSlots(last_sql=self._sql, query_summary=self._summary),
        )


class RecordingQueryPipeline:
    """记下小模型侧实际吃到的当前问题和程序槽，用来证明看不到近几轮对话。"""

    def __init__(self, inner: StubRowsQueryPipeline) -> None:
        self._inner = inner
        self.current_questions: list[str] = []
        self.slot_inputs: list[ProgramSlots] = []

    def run(self, current_question: str, slots: ProgramSlots) -> QueryToolOutcome:
        self.current_questions.append(current_question)
        self.slot_inputs.append(slots)
        return self._inner.run(current_question, slots)


def _human_reply_from_fillback(fillback: str) -> str:
    if fillback.startswith("没查到"):
        return "没查到"
    error = None
    result = None
    for line in fillback.splitlines():
        if line.startswith("报错:"):
            error = line.removeprefix("报错:").strip()
        if line.startswith("结果:"):
            result = line.removeprefix("结果:").strip()
    if error is not None:
        return f"查库失败：{error}"
    if result is not None:
        return f"查到{result}。"
    return "没有查到可用的数。"


class CallWebSearchThenSpeakBrain:
    """联网搜索已挂载则调一次再说话，否则只说话。用来锁住默认不挂。"""

    def __init__(self) -> None:
        self._searched = False

    def decide(self, turn_input: TurnInput) -> CallTool | Speak:
        if WEB_SEARCH in turn_input.mounted_tools and not self._searched:
            self._searched = True
            return CallTool(WEB_SEARCH, {"查询": turn_input.utterance})
        if WEB_SEARCH not in turn_input.mounted_tools:
            return Speak("联网搜索未挂载，只能说话。")
        for message in reversed(turn_input.session.messages):
            if message.role == WEB_SEARCH:
                return Speak(f"公开资料：{message.content}")
        return Speak("没有搜索回填。")


class SearchRepeatedlyBrain:
    """联网搜索已挂载则连调多次，否则只说话。用来观察每句环 A 的次数保险丝。"""

    def __init__(self, times: int) -> None:
        self._times = times
        self._called = 0

    def decide(self, turn_input: TurnInput) -> CallTool | Speak:
        if WEB_SEARCH in turn_input.mounted_tools and self._called < self._times:
            self._called += 1
            return CallTool(WEB_SEARCH, {"查询": f"口径第{self._called}次"})
        return Speak("停止搜索，对人说明公开口径。")


class RecordingSearchEngine:
    """外网搜索引擎替身。记录是否被调、入参和超时；可模拟命中或失败。"""

    def __init__(
        self,
        *,
        hits: tuple[SearchHit, ...] = (),
        error: str | None = None,
    ) -> None:
        self._hits = hits
        self._error = error
        self.calls: list[tuple[str, int]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def search(self, query: str, *, timeout_seconds: int) -> SearchSuccess | SearchFailure:
        self.calls.append((query, timeout_seconds))
        if self._error is not None:
            return SearchFailure(error=self._error)
        return SearchSuccess(hits=self._hits)


class RecordingSmallModel:
    """记下小模型实际吃到的契约输入，用来证明只看到槽和当前问题。"""

    def __init__(self, inner: SmallModel) -> None:
        self._inner = inner
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._inner.complete(prompt)


class StubQueryConversation:
    """查库对话替身：固定回填销售额 1280000。真实查库大模型未接。"""

    _SQL = "SELECT SUM(amount) AS 销售额 FROM sales LIMIT 50"
    _AMOUNT = "1280000"

    def run(self, rewritten_question: str, rag_hits: RagHits) -> QueryConversationResult:
        del rewritten_question, rag_hits
        return QueryConversationResult(
            sql=self._SQL,
            fillback=f"SQL: {self._SQL}\n结果: 销售额 {self._AMOUNT}",
        )


class RecordingQueryConversation:
    """记下查库对话实际吃到的改写问题和这一次 RAG 命中。"""

    def __init__(self) -> None:
        self.rewritten_questions: list[str] = []
        self.rag_hits_received: list[RagHits] = []
        self._inner = StubQueryConversation()

    def run(self, rewritten_question: str, rag_hits: RagHits) -> QueryConversationResult:
        self.rewritten_questions.append(rewritten_question)
        self.rag_hits_received.append(rag_hits)
        return self._inner.run(rewritten_question, rag_hits)


class ScriptedQueryLlm:
    """查库大模型替身：按预定 SQL 依次调只读查库工具，并记下每次对话输入。"""

    def __init__(self, sqls: tuple[str, ...]) -> None:
        self._sqls = list(sqls)
        self.turns: list[SqlSessionTurn] = []

    def next_sql(self, turn: SqlSessionTurn) -> str:
        self.turns.append(turn)
        return self._sqls.pop(0)


class ScriptedExecutor:
    """只读库替身：按预定返回依次给出有行 / 0 行 / 报错 / 超时。"""

    def __init__(self, outcomes: tuple[RowSet | SqlError | Timeout, ...]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, int]] = []

    def execute(self, sql: str, *, timeout_seconds: int) -> RowSet | SqlError | Timeout:
        self.calls.append((sql, timeout_seconds))
        return self._outcomes.pop(0)


_YEAR_MONTH = re.compile(r"\d{4}年\d{1,2}月")
_UNSPECIFIED = "未说明"
_NONE = "无"


def _summary_fields(summary: str) -> dict[str, str]:
    fields = {"指标": "", "时间": "", "过滤": "", "口径": ""}
    for part in summary.split("；"):
        label, _, value = part.partition(" ")
        if label in fields:
            fields[label] = value.strip()
    return fields


class KeywordSmallModel:
    """销售额示踪弹的规则替身，不是通用小模型。只认识销售额/上个月/按城市/含税。

    ADR 0002 之前它住在 src/textsql_agent/rewrite.py；现在只作测试替身，
    顺便示范「需求说明」这一路自由文本怎么被下一轮读回来。
    """

    def complete(self, prompt: str) -> str:
        current_question, prior = _input_from_prompt(prompt)
        metric = _metric(current_question, prior)
        time = _time(current_question, prior)
        group = _group(current_question, prior)
        caliber = _caliber(current_question, prior)
        question = _rewritten_question(metric, time, group)
        summary = SUMMARY_TEMPLATE.format(
            metric=metric, time=time, group=group, caliber=caliber
        )
        return f"改写问题：{question}\n需求说明：{summary}"


def _input_from_prompt(prompt: str) -> tuple[str, dict[str, str]]:
    current_question = prompt.split("当前问题：", 1)[-1].strip()
    summary_block = prompt.split("查询摘要：", 1)[-1].split("当前问题：", 1)[0]
    return current_question, _summary_fields(summary_block.strip())


def _metric(current_question: str, prior: dict[str, str]) -> str:
    if "销售额" in current_question:
        return "销售额"
    return prior.get("指标", "")


def _time(current_question: str, prior: dict[str, str]) -> str:
    matched = _YEAR_MONTH.search(current_question)
    if matched is not None:
        return matched.group(0)
    if "上个月" in current_question:
        return "上个月"
    prior_time = prior.get("时间", "")
    if prior_time and prior_time != _UNSPECIFIED:
        return prior_time
    return _UNSPECIFIED


def _group(current_question: str, prior: dict[str, str]) -> str:
    if "按城市" in current_question:
        return "按城市分组"
    prior_group = prior.get("过滤", "")
    if prior_group and prior_group != _NONE:
        return prior_group
    return _NONE


def _caliber(current_question: str, prior: dict[str, str]) -> str:
    if "含税" in current_question:
        return "含税"
    if "未税" in current_question:
        return "未税"
    prior_caliber = prior.get("口径", "")
    if prior_caliber and prior_caliber != _UNSPECIFIED:
        return prior_caliber
    return _UNSPECIFIED


def _rewritten_question(metric: str, time: str, group: str) -> str:
    if group == "按城市分组" and time != _UNSPECIFIED:
        return f"查询{time}按城市分组的{metric}"
    if group == "按城市分组":
        return f"查询按城市分组的{metric}"
    if time != _UNSPECIFIED:
        return f"查询{time}的{metric}"
    return f"查询{metric}"


class ExtraLinesSmallModel:
    """在两段之外再吐 SQL 和解释，用来观察输出被收成两段。"""

    def complete(self, prompt: str) -> str:
        del prompt
        return (
            "改写问题：查询销售额\n"
            "需求说明：口径未说明\n"
            "SELECT SUM(amount) FROM sales\n"
            "解释：这是改写结果"
        )


class ScriptedDeepSeekClient:
    """DeepSeek 客户端替身：按预定回话依次作答，并记下每次 prompt 与是否要 JSON。

    接进真适配器（真 `DeepSeekPlanner` / `DeepSeekSmallModel` / `DeepSeekQueryLlm`），
    所以测的还是真解析和真执行，只是不碰网络。
    """

    def __init__(self, replies: tuple[str, ...]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.json_modes: list[bool] = []

    @property
    def call_count(self) -> int:
        return len(self.prompts)

    def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        self.prompts.append(prompt)
        self.json_modes.append(json_mode)
        return self._replies.pop(0)


def planner_call_tool(question: str, tool: str = QUERY_TOOL) -> str:
    """规划者该回的那种 JSON。"""
    return json.dumps(
        {"动作": "调用工具", "工具": tool, "参数": {"当前问题": question}},
        ensure_ascii=False,
    )


def planner_speak(reply: str) -> str:
    return json.dumps({"动作": "说话", "回复": reply}, ensure_ascii=False)


def rewrite_reply(question: str, summary: str = "这一轮要什么，跟上一轮比改了什么") -> str:
    """小模型该回的那种两段原文。"""
    return f"改写问题：{question}\n需求说明：{summary}"
