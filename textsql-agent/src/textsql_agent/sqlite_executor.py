"""只读 SQLite 执行器：跑一条 SELECT，回报行、精确基数与失败类型。

三重只读：连接用 `mode=ro`（引擎层拒写）、SQL 必须以 SELECT/WITH 开头、
`sqlite3` 的 execute 只允许一条语句。

**命中基数单独数**：`SELECT COUNT(*) FROM (<去掉尾部 LIMIT 的 SQL>)`。
所以它和取行上限、回填截断都无关——「从 N 条里回填 M 条」这句采样说明才有出处。
去掉尾部 LIMIT 是必须的，否则 `COUNT(*)` 只会数到 LIMIT 那么多。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from textsql_agent.sql_session import RowSet, SqlError, Timeout

READ_ONLY_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
TRAILING_LIMIT = re.compile(
    r"\s+limit\s+\d+(?:\s*,\s*\d+|\s+offset\s+\d+)?\s*$", re.IGNORECASE
)
FORBIDDEN_WORDS = re.compile(
    r"\b(attach|detach|pragma|vacuum|reindex|insert|update|delete|drop|alter|create)\b",
    re.IGNORECASE,
)
_PROGRESS_EVERY = 2000


class SqliteReadonlyExecutor:
    """一个 SQLite 文件上的只读执行器。每次调用新开一条只读连接，用完就关。"""

    def __init__(self, db_path: str | Path, *, row_limit: int = 200) -> None:
        self._path = Path(db_path)
        self._row_limit = row_limit

    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def row_limit(self) -> int:
        return self._row_limit

    def execute(self, sql: str, *, timeout_seconds: int) -> RowSet | SqlError | Timeout:
        statement = sql.strip().rstrip(";").strip()
        if not statement:
            return SqlError("空 SQL")
        if not READ_ONLY_START.match(statement):
            return SqlError("只允许一条 SELECT 或 WITH 查询")
        if FORBIDDEN_WORDS.search(statement):
            return SqlError("SQL 里出现了只读查询之外的语句")
        deadline = time.monotonic() + timeout_seconds
        connection = sqlite3.connect(self._uri(), uri=True)
        try:
            connection.set_progress_handler(_guard(deadline), _PROGRESS_EVERY)
            matched = self._count(connection, statement)
            columns, rows = self._rows(connection, statement)
        except sqlite3.Error as error:
            return Timeout() if _interrupted(error) else SqlError(str(error))
        finally:
            connection.close()
        return RowSet(columns=columns, rows=rows, matched_rows=matched)

    def _count(self, connection: sqlite3.Connection, statement: str) -> int:
        """数命中基数。数不出来就算这次调用失败——基数不可信的代价比失败大。"""
        counted = TRAILING_LIMIT.sub("", statement)
        cursor = connection.execute(f"SELECT COUNT(*) FROM ({counted})")
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            raise sqlite3.OperationalError("COUNT 没有结果")
        return int(row[0])

    def _rows(
        self, connection: sqlite3.Connection, statement: str
    ) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
        cursor = connection.execute(statement)
        try:
            columns = tuple(item[0] for item in cursor.description or ())
            fetched = cursor.fetchmany(self._row_limit)
        finally:
            cursor.close()
        rows = tuple(
            tuple("" if value is None else str(value) for value in row) for row in fetched
        )
        return columns, rows

    def _uri(self) -> str:
        return f"file:{quote(str(self._path.resolve()))}?mode=ro"


def _guard(deadline: float) -> Callable[[], int]:
    """给 SQLite 的进度回调：过点就返回非 0，让引擎自己中断。"""

    def check() -> int:
        return 1 if time.monotonic() > deadline else 0

    return check


def _interrupted(error: sqlite3.Error) -> bool:
    return "interrupt" in str(error).lower()
