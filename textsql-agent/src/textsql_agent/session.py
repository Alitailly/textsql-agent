"""主 Agent 会话：用户原话和自己说过的话。不做槽位状态机。"""

from __future__ import annotations

from dataclasses import dataclass

USER = "用户"
MAIN_AGENT = "主 Agent"


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Session:
    messages: tuple[Message, ...] = ()
