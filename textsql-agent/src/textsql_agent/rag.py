"""Vanna RAG：改写问题作 query，对照 DDL、文档/口径、问题-SQL 各一路。

只检索，不执行 SQL。不要走默认 generate_sql 全链。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

_YEAR_MONTH = re.compile(r"\d{4}年\d{1,2}月")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CHINESE = re.compile(r"[\u4e00-\u9fff]+")
_PARTICLE = re.compile(r"[的是在与及了和]")
_STOP = frozenset(
    {
        "查询",
        "的",
        "是",
        "多少",
        "对应",
        "口径",
        "问题",
        "create",
        "table",
        "integer",
        "text",
        "select",
        "sum",
        "from",
        "where",
        "limit",
        "as",
        "and",
        "or",
        "null",
        "sql",
    }
)


@dataclass(frozen=True)
class RagHits:
    """一次三路命中材料。交给查库对话，不再检索第二遍。"""

    ddl: str
    documentation: str
    question_sql: str


class VannaRag(Protocol):
    """Vanna 检索边界。真实向量库未接，见 docs/agents/undesigned.md。"""

    def retrieve(self, query: str) -> RagHits:
        """只检索，不执行 SQL。"""
        ...


@dataclass
class InMemoryVanna:
    """内存关键词对照替身，不是真实 Vanna。"""

    retrieve_queries: list[str] = field(default_factory=list)
    generate_sql_questions: list[str] = field(default_factory=list)
    executed_sql: list[str] = field(default_factory=list)
    _ddl: list[str] = field(default_factory=list)
    _documentation: list[str] = field(default_factory=list)
    _question_sql: list[tuple[str, str]] = field(default_factory=list)

    def train(
        self,
        *,
        ddl: str | None = None,
        documentation: str | None = None,
        question: str | None = None,
        sql: str | None = None,
    ) -> None:
        if ddl is not None:
            self._ddl.append(ddl)
        if documentation is not None:
            self._documentation.append(documentation)
        if question is not None and sql is not None:
            self._question_sql.append((question, sql))
        elif question is not None or sql is not None:
            raise TypeError("问题-SQL 必须成对 train")

    def retrieve(self, query: str) -> RagHits:
        self.retrieve_queries.append(query)
        query_terms = _terms(query)
        docs = [item for item in self._documentation if _shares(query_terms, item)]
        samples = [
            (question, sql)
            for question, sql in self._question_sql
            if _shares(query_terms, question)
        ]
        expanded = set(query_terms)
        for item in docs:
            expanded.update(_terms(item))
        for question, sql in samples:
            expanded.update(_terms(question))
            expanded.update(_terms(sql))
        ddls = [item for item in self._ddl if _shares(expanded, item)]
        return RagHits(
            ddl="\n".join(ddls),
            documentation="\n".join(docs),
            question_sql="\n".join(
                f"问题：{question}\nSQL：{sql}" for question, sql in samples
            ),
        )

    def generate_sql(self, question: str) -> str:
        """Vanna 默认全链：会再 retrieve 一次并执行 SQL。本管线禁止走这条。"""
        self.generate_sql_questions.append(question)
        hits = self.retrieve(question)
        sql = (
            hits.question_sql.rsplit("SQL：", 1)[-1].strip()
            if hits.question_sql
            else "SELECT 1"
        )
        self.executed_sql.append(sql)
        return sql


def _terms(text: str) -> set[str]:
    terms: set[str] = set(_YEAR_MONTH.findall(text))
    remainder = _YEAR_MONTH.sub(" ", text)
    for ident in _IDENT.findall(remainder):
        lowered = ident.lower()
        if lowered not in _STOP:
            terms.add(lowered)
    for chunk in _CHINESE.findall(remainder):
        for piece in _PARTICLE.split(chunk):
            if piece not in _STOP and len(piece) >= 2:
                terms.add(piece)
    return terms


def _shares(query_terms: set[str], item: str) -> bool:
    item_lower = item.lower()
    item_terms = _terms(item)
    return any(term in item_terms or term in item_lower for term in query_terms)
