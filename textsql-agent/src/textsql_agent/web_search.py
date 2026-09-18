"""联网搜索：主 Agent 在环 A 查公开网页资料的工具。默认不挂载。

入参一句搜索词。成功最多 5 条（标题、网址、摘要约 300 字）；
失败返回错误原文。开启后每句环 A 最多 3 次、单次 15 秒。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

WEB_SEARCH_QUERY_KEY = "查询"
WEB_SEARCH_MAX_HITS = 5
WEB_SEARCH_SNIPPET_CHARS = 300
WEB_SEARCH_TIMEOUT_SECONDS = 15
WEB_SEARCH_MAX_CALLS_PER_RING_A = 3


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchSuccess:
    hits: tuple[SearchHit, ...]


@dataclass(frozen=True)
class SearchFailure:
    error: str


class SearchEngine(Protocol):
    """外网搜索引擎。真实外网未接，见 docs/agents/undesigned.md。"""

    def search(self, query: str, *, timeout_seconds: int) -> SearchSuccess | SearchFailure:
        ...


class WebSearch:
    """联网搜索工具：截断条数和摘要，并把超时、每句次数上限交给实现。"""

    def __init__(self, engine: SearchEngine) -> None:
        self._engine = engine
        self._calls = 0

    @property
    def call_count(self) -> int:
        return self._calls

    def call(self, query: str) -> SearchSuccess | SearchFailure:
        if self._calls >= WEB_SEARCH_MAX_CALLS_PER_RING_A:
            return SearchFailure(error="本句环 A 联网搜索已达 3 次上限")
        self._calls += 1
        raw = self._engine.search(query, timeout_seconds=WEB_SEARCH_TIMEOUT_SECONDS)
        if isinstance(raw, SearchFailure):
            return raw
        hits = tuple(
            SearchHit(
                title=hit.title,
                url=hit.url,
                snippet=hit.snippet[:WEB_SEARCH_SNIPPET_CHARS],
            )
            for hit in raw.hits[:WEB_SEARCH_MAX_HITS]
        )
        if not hits:
            return SearchFailure(error="无结果")
        return SearchSuccess(hits=hits)


def fillback_text(outcome: SearchSuccess | SearchFailure) -> str:
    if isinstance(outcome, SearchFailure):
        return outcome.error
    return "\n\n".join(
        f"{hit.title}\n{hit.url}\n{hit.snippet}" for hit in outcome.hits
    )
