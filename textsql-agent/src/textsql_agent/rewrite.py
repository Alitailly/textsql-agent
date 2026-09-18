"""小模型改写：无记忆、不查库、不输出 SQL。

输入固定为前缀、上次 SQL（可空）、查询摘要（可空）、当前问题。
输出恰好两段：改写问题、需求说明。
需求说明是自由文本，程序只原样存取，不解析它的内容（ADR 0002）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from textsql_agent.slots import ProgramSlots

QUESTION_LABEL = "改写问题"
SUMMARY_LABEL = "需求说明"
REWRITE_PREFIX = (
    "你是问句改写器。只根据下面三块输入，产出「改写问题」和「需求说明」两段。"
    "不要解释，不要 SQL，不要回答这个数是多少。"
)
# 代码里的前缀比 textsql-rewrite-prompt.md 短，且没有业务词。
# 通用规则在提示词文件；具体业务词只存在于 L2 配置与测试替身，不在通用设计里。


@dataclass(frozen=True)
class RewriteResult:
    rewritten_question: str
    summary: str
    """自由文本说明。与上次 SQL 成对存回程序槽，只给下一轮小模型看。"""
    output: str
    """收成两段后的原文，供人查看小模型到底说了什么。"""


class SmallModel(Protocol):
    """小模型边界。真实模型经适配器注入，见 docs/agents/undesigned.md。"""

    def complete(self, prompt: str) -> str:
        ...


class Rewriter:
    """拼装契约输入、调用小模型、只留下约定两段。"""

    def __init__(self, model: SmallModel) -> None:
        self._model = model

    def run(self, current_question: str, slots: ProgramSlots) -> RewriteResult:
        raw = self._model.complete(build_rewrite_prompt(current_question, slots))
        return parse_rewrite_output(raw, current_question)


def build_rewrite_prompt(current_question: str, slots: ProgramSlots) -> str:
    last_sql = slots.last_sql or ""
    summary = slots.query_summary or ""
    return (
        f"{REWRITE_PREFIX}\n\n"
        f"上次SQL：\n{last_sql}\n\n"
        f"查询摘要：\n{summary}\n\n"
        f"当前问题：\n{current_question}"
    )


def parse_rewrite_output(raw: str, current_question: str) -> RewriteResult:
    """把模型原文收成两段。

    「改写问题」是程序要用的强字段：只取该行标签后的一句。认不出来就退回当前问题——
    这是可见的软降级，不是静默兜底：模型原文仍在 output 里，退回也只少一层改写、不丢条件。

    「需求说明」取标签之后的全部文本，原样留着。它是自由文本，程序不解析、不按内容做分支
    （ADR 0002）。所以「摘要里不许出现 SQL / 报错 / 结果」这条约束只能由提示词层保证，
    解析层没有可依据的白名单——自由文本和逐行白名单本来就互斥。
    """
    question = _line_value(raw, QUESTION_LABEL) or current_question
    summary = _tail(raw, SUMMARY_LABEL)
    return RewriteResult(
        rewritten_question=question,
        summary=summary,
        output=f"{QUESTION_LABEL}：{question}\n{SUMMARY_LABEL}：{summary}",
    )


def _line_value(raw: str, label: str) -> str:
    prefix = f"{label}："
    for line in raw.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return ""


def _tail(raw: str, label: str) -> str:
    """标签之后的全部文本，原样留着。

    不折行、不改空白：摘要是自由文本，「原样存取」比「好看」重要。
    """
    _, found, rest = raw.partition(f"{label}：")
    if not found:
        return ""
    return rest.strip()
