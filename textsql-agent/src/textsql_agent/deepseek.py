"""DeepSeek 适配器：小模型、查库大模型、主 Agent 规划者都走这里。

只用标准库。钥匙、base_url、模型名全从环境变量读，不写死；这个文件在 `src/` 里，
所以一个业务词都不能有（ADR 0002）。

`deepseek-flash` 默认是思考模型：不关思考时 token 会全花在 reasoning 上、正文可能为空，
延迟也翻倍。所以默认关思考、temperature=0。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from textsql_agent.brain import CallTool, Speak, TurnInput
from textsql_agent.prompts import PLANNER_JSON_CONTRACT, QUERY_LLM_INSTRUCTIONS
from textsql_agent.sql_session import SqlSessionTurn
from textsql_agent.tools import QUERY_TOOL, WEB_SEARCH

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

ACTION_KEY = "动作"
SPEAK_ACTION = "说话"
CALL_TOOL_ACTION = "调用工具"
TOOL_KEY = "工具"
ARGS_KEY = "参数"
REPLY_KEY = "回复"
QUESTION_KEY = "当前问题"

_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_SQL_START = re.compile(r"(?is)^(select|with)\b")
OPENING = "（无）"

UNPARSED_REPLY = "我这句没拿准，能不能再说一遍要点？"
"""规划者的 JSON 认不出来时的回话。可见的软降级：不猜、不编，也不假装查过库。"""


class DeepSeekError(RuntimeError):
    """模型侧调用失败。消息原样带给上层，不吞。"""


class ChatClient(Protocol):
    """三个适配器唯一需要的东西：一次 prompt 换一次回话。

    边界故意收窄到这里，测试就能拿替身接进真适配器，而不用碰网络。
    """

    def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        ...


@dataclass(frozen=True)
class DeepSeekCall:
    """一次模型调用的可观察记录。原文留着，方便人核对模型到底说了什么。"""

    model: str
    seconds: float
    prompt_tokens: int
    completion_tokens: int
    content: str


@dataclass
class DeepSeekClient:
    """DeepSeek 的 OpenAI 兼容 `/chat/completions`。"""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 60
    max_tokens: int = 2048
    thinking: bool = False
    _calls: list[DeepSeekCall] = field(default_factory=list, repr=False)

    @classmethod
    def from_env(cls) -> DeepSeekClient:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise DeepSeekError("没有找到 OPENAI_API_KEY / DEEPSEEK_API_KEY")
        base_url = (
            os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("DEEPSEEK_BASE_URL")
            or DEFAULT_BASE_URL
        )
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
        )

    @property
    def calls(self) -> tuple[DeepSeekCall, ...]:
        return tuple(self._calls)

    def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        if not self.thinking:
            body["thinking"] = {"type": "disabled"}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        payload = self._post(body)
        seconds = time.monotonic() - started
        choices = payload.get("choices") or []
        if not choices:
            raise DeepSeekError(f"返回里没有 choices：{str(payload)[:300]}")
        content = choices[0].get("message", {}).get("content") or ""
        usage = payload.get("usage") or {}
        self._calls.append(
            DeepSeekCall(
                model=self.model,
                seconds=round(seconds, 3),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                content=content,
            )
        )
        return content

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise DeepSeekError(f"HTTP {error.code}：{detail[:500]}") from error
        except urllib.error.URLError as error:
            raise DeepSeekError(f"连不上 {self.base_url}：{error.reason}") from error
        except TimeoutError as error:
            raise DeepSeekError(f"请求超时（{self.timeout_seconds} 秒）") from error
        if not isinstance(payload, dict):
            raise DeepSeekError(f"返回不是 JSON 对象：{str(payload)[:200]}")
        if "error" in payload:
            raise DeepSeekError(f"模型报错：{payload['error']}")
        return payload


class DeepSeekSmallModel:
    """B3 小模型：一次调用，原文原样交回给解析层。"""

    def __init__(self, client: ChatClient) -> None:
        self._client = client

    def complete(self, prompt: str) -> str:
        return self._client.complete(prompt)


class DeepSeekQueryLlm:
    """B5 查库大模型：每次只回一条 SQL。

    取不出 SQL 就回空串。空串不是异常——它会被只读执行器判成一次失败调用，
    在查库对话自己的 5 次保险丝里重试，比整轮崩掉更接近规格。
    """

    def __init__(self, client: ChatClient) -> None:
        self._client = client

    def next_sql(self, turn: SqlSessionTurn) -> str:
        return extract_sql(self._client.complete(build_sql_prompt(turn)))


class DeepSeekPlanner:
    """主 Agent 规划者：一次调用一个 JSON 决定，要么说话，要么调一个已挂载工具。"""

    def __init__(self, client: ChatClient) -> None:
        self._client = client

    def decide(self, turn_input: TurnInput) -> Speak | CallTool:
        text = self._client.complete(build_planner_prompt(turn_input), json_mode=True)
        decision = parse_planner_output(text)
        if decision is None:
            return Speak(UNPARSED_REPLY)
        return decision


def build_sql_prompt(turn: SqlSessionTurn) -> str:
    lines = [
        QUERY_LLM_INSTRUCTIONS,
        "",
        "改写问题：",
        turn.rewritten_question,
        "",
        "表结构材料：",
        turn.rag_hits.ddl or OPENING,
        "",
        "文档与口径材料：",
        turn.rag_hits.documentation or OPENING,
        "",
        "问题-SQL 样例材料：",
        turn.rag_hits.question_sql or OPENING,
    ]
    if turn.prior_returns:
        lines += ["", "这次对话里已经跑过的只读调用（按顺序）："]
        for item in turn.prior_returns:
            detail = f" → {item.payload}" if item.payload else ""
            lines.append(f"- [{item.kind}] {item.sql}{detail}")
    lines += ["", f"可用工具：{'、'.join(turn.tools)}", "", "只回一条 SQL。"]
    return "\n".join(lines)


def build_planner_prompt(turn_input: TurnInput) -> str:
    session = turn_input.session
    lines = [
        turn_input.instructions,
        "",
        f"已挂载工具：{'、'.join(turn_input.mounted_tools) or OPENING}",
        "",
        "会话（最后一句是用户刚说的）：",
        *(_render(message.role, message.content) for message in session.messages),
        "",
        PLANNER_JSON_CONTRACT,
    ]
    return "\n".join(lines)


def _render(role: str, content: str) -> str:
    return f"{role}：{content}"


def extract_sql(text: str) -> str:
    """从模型输出里取一条 SQL：先看代码块，再看第一行像 SQL 的。

    取不出 SQL 就回空串——**不**把整段散文当 SQL 交出去，那样报错信息会误导模型。
    """
    fenced = _FENCE.search(text)
    if fenced is not None:
        return _clean(fenced.group(1))
    for line in text.splitlines():
        if _SQL_START.match(line.strip()):
            return _clean(line)
    return ""


def parse_planner_output(text: str) -> Speak | CallTool | None:
    """认不出契约就回 None，由调用方决定怎么软降级。"""
    payload = _json_object(text)
    if payload is None:
        return None
    action = str(payload.get(ACTION_KEY, "")).strip()
    if action == SPEAK_ACTION:
        reply = str(payload.get(REPLY_KEY, "")).strip()
        return Speak(reply) if reply else None
    if action != CALL_TOOL_ACTION:
        return None
    name = str(payload.get(TOOL_KEY, "")).strip()
    if name not in (QUERY_TOOL, WEB_SEARCH):
        return None
    arguments = payload.get(ARGS_KEY)
    if not isinstance(arguments, dict):
        return None
    args = {str(key): str(value) for key, value in arguments.items()}
    if name == QUERY_TOOL and not args.get(QUESTION_KEY):
        return None
    return CallTool(name, args)


def _clean(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced is not None:
        candidate = fenced.group(1).strip()
    payload = _loads(candidate)
    if payload is None:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            payload = _loads(candidate[start : end + 1])
    return payload


def _loads(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
