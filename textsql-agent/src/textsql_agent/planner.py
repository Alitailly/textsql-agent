"""主 Agent 规划者：从会话把用户原话收成可独立理解的当前问题。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from textsql_agent.brain import CallTool, Speak, TurnInput
from textsql_agent.session import MAIN_AGENT, USER
from textsql_agent.tools import QUERY_TOOL

_CURRENT_QUESTION = "当前问题"
_QUERY_PREFIX = "查询"
_FIRST_OPTION = re.compile(r"第一个是([^，。；\n]+)")
_STATED_CALIBER = re.compile(r"口径是([^。\n]+)")


@dataclass(frozen=True)
class PlannerCues:
    """规则规划者认得的词。业务词全部经此注入，src/ 里一个都不留。

    空词表是诚实的退化：认不出就照用户原话当当前问题，不假装认识。
    """

    caliber_answer: str = ""
    """用户先要口径时主 Agent 回的那句话。空表示不答口径，照常查数。"""

    metrics: tuple[str, ...] = ()
    """能从历史用户话里认回来的指标词。"""

    times: tuple[str, ...] = ()
    """能从历史用户话里认回来的时间词。"""

    confirm: tuple[str, ...] = ()
    """用户用来确认上一轮口径的短话，例如「查吧」。"""

    def metric_in(self, texts: tuple[str, ...]) -> str | None:
        return _first_seen(self.metrics, texts)

    def time_in(self, texts: tuple[str, ...]) -> str | None:
        return _first_seen(self.times, texts)


class Planner:
    """规则规划替身，不是 DeepSeek Harness。真实规划者经适配器注入。"""

    def __init__(self, cues: PlannerCues | None = None) -> None:
        self._cues = cues or PlannerCues()

    def decide(self, turn_input: TurnInput) -> Speak | CallTool:
        fillback = _query_fillback(turn_input)
        if fillback is not None:
            return Speak(_reply_from_fillback(fillback))
        if self._cues.caliber_answer and _asks_caliber_before_query(turn_input.utterance):
            return Speak(self._cues.caliber_answer)
        return CallTool(
            QUERY_TOOL,
            {
                _CURRENT_QUESTION: _standalone_current_question(
                    turn_input, self._cues
                )
            },
        )


def _first_seen(words: tuple[str, ...], texts: tuple[str, ...]) -> str | None:
    for text in texts:
        for word in words:
            if word in text:
                return word
    return None


def _query_fillback(turn_input: TurnInput) -> str | None:
    messages = turn_input.session.messages
    last_user_index = None
    for index, message in enumerate(messages):
        if message.role == USER and message.content == turn_input.utterance:
            last_user_index = index
    if last_user_index is None:
        return None
    for message in messages[last_user_index + 1 :]:
        if message.role == QUERY_TOOL:
            return message.content
    return None


def _reply_from_fillback(fillback: str) -> str:
    if fillback.startswith("没查到"):
        return "没查到"
    error = _labeled_line(fillback, "报错:")
    if error is not None:
        return f"查库失败：{error}"
    result = _labeled_line(fillback, "结果:")
    if result is not None:
        return f"查到{result}。"
    return "没有查到可用的数。"


def _labeled_line(fillback: str, prefix: str) -> str | None:
    for line in fillback.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return None


def _asks_caliber_before_query(utterance: str) -> bool:
    return "先" in utterance and "口径" in utterance


def _standalone_current_question(turn_input: TurnInput, cues: PlannerCues) -> str:
    if "第一个" in turn_input.utterance:
        listed = _latest_agent_capture(turn_input, _FIRST_OPTION)
        if listed is not None:
            time = cues.time_in(_prior_user_texts(turn_input)) or ""
            return f"{_QUERY_PREFIX}{time}{listed}"
    if turn_input.utterance in cues.confirm:
        metric = cues.metric_in(_prior_user_texts(turn_input))
        caliber = _latest_agent_capture(turn_input, _STATED_CALIBER)
        if metric is not None and caliber is not None:
            return f"{_QUERY_PREFIX}{metric}，口径为{caliber}"
    return turn_input.utterance


def _latest_agent_capture(turn_input: TurnInput, pattern: re.Pattern[str]) -> str | None:
    for message in reversed(turn_input.session.messages):
        if message.role != MAIN_AGENT:
            continue
        matched = pattern.search(message.content)
        if matched is not None:
            return matched.group(1).strip()
    return None


def _prior_user_texts(turn_input: TurnInput) -> tuple[str, ...]:
    return tuple(
        message.content
        for message in turn_input.session.messages
        if message.role == USER and message.content != turn_input.utterance
    )
