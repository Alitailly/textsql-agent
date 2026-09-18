"""主 Agent 规划者：对一句用户话做出一次决定。

说话是默认通道，不是工具。查数工具是否调用由本轮这一次决定观察。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from textsql_agent.session import Session


@dataclass(frozen=True)
class TurnInput:
    """一轮开始时规划者能看见的东西。"""

    utterance: str
    session: Session
    mounted_tools: tuple[str, ...]
    instructions: str


@dataclass(frozen=True)
class Speak:
    """环 A：不调查数工具，对人说话。"""

    reply: str


@dataclass(frozen=True)
class CallTool:
    """规划者要调一个已挂载工具。未挂载的调用不计数。"""

    name: str
    arguments: dict[str, str]


class MainAgentBrain(Protocol):
    """主 Agent 规划者。真实模型未接，见 docs/agents/undesigned.md。"""

    def decide(self, turn_input: TurnInput) -> Speak | CallTool:
        ...
