#!/usr/bin/env python3
"""把 `xhs_pgy` 的 mysqldump 转成 SQLite，便于反复查询（一次解析，多次使用）。

用法：

    python3 dump_to_sqlite.py /path/to/dump.sql [--out xhs_pgy.sqlite3]

要点：
  - 流式读取（逐行），174MB 不进内存；
  - INSERT 的值用逐字符状态机解析，正确处理中文/emoji/\\' / '' / NULL / JSON，
    不依赖 split("),(") 这类脆弱的启发式；
  - 保留 NULL 与空字符串的区别（画像里要分开统计）；
  - 一张表解析完就批量写盘，边解析边打印行数。

零第三方依赖。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 建表语句
# 类型尽量贴近 MySQL DDL；未加约束（UNIQUE 等）以便观察脏数据。
SCHEMAS = {
    "xhs_creator": """
        CREATE TABLE IF NOT EXISTS xhs_creator (
            id             INTEGER,
            nickname       TEXT,
            xhs_id         TEXT,
            gender         TEXT,
            followers      INTEGER,
            credit_level   INTEGER,
            province       TEXT,
            city           TEXT,
            email          TEXT,
            raw_categories TEXT,
            keywords       TEXT,
            summary_text   TEXT,
            is_public      INTEGER,
            data_date      TEXT,
            batch_id       INTEGER,
            extra_json     TEXT,
            src_row_index  INTEGER,
            imported_at    TEXT
        )""",
    "xhs_creator_category": """
        CREATE TABLE IF NOT EXISTS xhs_creator_category (
            creator_id    INTEGER,
            category_name TEXT
        )""",
    "xhs_staging": """
        CREATE TABLE IF NOT EXISTS xhs_staging (
            src_row_index   INTEGER,
            nickname        TEXT,
            xhs_id          TEXT,
            gender_raw      TEXT,
            followers_raw   TEXT,
            categories_raw  TEXT,
            credit_level_raw TEXT,
            province        TEXT,
            email           TEXT
        )""",
    "dict_category_alias": """
        CREATE TABLE IF NOT EXISTS dict_category_alias (
            category_name TEXT,
            alias         TEXT
        )""",
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_c_followers ON xhs_creator(followers)",
    "CREATE INDEX IF NOT EXISTS idx_c_xhs_id ON xhs_creator(xhs_id)",
    "CREATE INDEX IF NOT EXISTS idx_c_province ON xhs_creator(province)",
    "CREATE INDEX IF NOT EXISTS idx_c_city ON xhs_creator(city)",
    "CREATE INDEX IF NOT EXISTS idx_cat_creator ON xhs_creator_category(creator_id)",
    "CREATE INDEX IF NOT EXISTS idx_cat_name ON xhs_creator_category(category_name)",
    "CREATE INDEX IF NOT EXISTS idx_st_xhs_id ON xhs_staging(xhs_id)",
]

INSERT_RE = re.compile(r"^INSERT INTO `(?P<table>\w+)`\s*(\((?P<cols>[^)]*)\))?\s*VALUES\s*", re.I)

ESCAPES = {
    "0": "\0", "b": "\b", "n": "\n", "r": "\r",
    "t": "\t", "Z": "\x1a", "\\": "\\", "'": "'", '"': '"',
}


def parse_values(text: str, pos: int):
    """解析 `(...),(...),...;` 形式的值列表，yield 每个 tuple（list[str|None]）。

    逐字符状态机：跟踪是否在字符串字面量内、是否被反斜杠转义。
    """
    n = len(text)
    i = pos

    def skip_ws(j):
        while j < n and text[j] in " \t\r\n":
            j += 1
        return j

    while True:
        i = skip_ws(i)
        if i >= n:
            return
        if text[i] == ";":
            return
        if text[i] == ",":
            i += 1
            continue
        if text[i] != "(":
            raise ValueError(f"unexpected char {text[i]!r} at {i}")
        i += 1  # 跳过 '('

        row: list = []
        while True:
            i = skip_ws(i)
            if i >= n:
                raise ValueError("EOF inside tuple")
            ch = text[i]

            if ch == "'":
                i += 1
                buf: list[str] = []
                while i < n:
                    c = text[i]
                    if c == "\\":
                        nxt = text[i + 1] if i + 1 < n else ""
                        buf.append(ESCAPES.get(nxt, nxt))
                        i += 2
                        continue
                    if c == "'":
                        if i + 1 < n and text[i + 1] == "'":  # '' 转义
                            buf.append("'")
                            i += 2
                            continue
                        i += 1
                        break
                    buf.append(c)
                    i += 1
                row.append("".join(buf))

            elif ch == ")":  # 空列（理论上不该出现）
                row.append(None)

            elif ch == ",":
                if not row:
                    row.append(None)
                i += 1
                continue

            else:  # 裸 token：数字 / NULL / true / 科学计数法
                j = i
                while j < n and text[j] not in ",)":
                    j += 1
                tok = text[i:j].strip()
                row.append(None if tok.upper() == "NULL" else tok)
                i = j

            i = skip_ws(i)
            if i >= n:
                raise ValueError("EOF after value")
            if text[i] == ",":
                i += 1
                continue
            if text[i] == ")":
                i += 1
                break
            raise ValueError(f"expected , or ) got {text[i]!r} at {i}")

        yield row


def coerce(table: str, row: list):
    """按表把字符串数字转成 int，'' 保持 ''。"""
    int_cols = {
        "xhs_creator": {0, 4, 5, 12, 14, 16},
        "xhs_staging": {0},
        "xhs_creator_category": {0},
        "dict_category_alias": set(),
    }[table]
    out = []
    for idx, val in enumerate(row):
        if idx in int_cols and isinstance(val, str):
            try:
                out.append(int(val))
                continue
            except ValueError:
                pass
        out.append(val)
    return out


def iter_statements(path: Path, encoding: str):
    """逐行读，累加到完整语句（以 ; 结尾）再 yield (lineno, text)。"""
    with path.open("r", encoding=encoding, errors="replace", newline="") as fh:
        buf: list[str] = []
        start = 0
        for lineno, line in enumerate(fh, 1):
            if not buf:
                start = lineno
            buf.append(line)
            if line.rstrip("\r\n").rstrip().endswith(";"):
                yield start, "".join(buf)
                buf = []
        if buf:
            yield start, "".join(buf)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--encoding", default="utf-8")
    args = ap.parse_args()

    src = args.source
    out = args.out or src.with_name("xhs_pgy.sqlite3")
    if out.exists():
        out.unlink()

    conn = sqlite3.connect(out)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    for ddl in SCHEMAS.values():
        conn.execute(ddl)

    counts = {t: 0 for t in SCHEMAS}
    skipped = 0
    t0 = time.time()
    pending = {t: [] for t in SCHEMAS}
    placeholders = {t: ", ".join("?" * 18) for t in SCHEMAS}
    placeholders["xhs_creator_category"] = ", ".join("?" * 2)
    placeholders["xhs_staging"] = ", ".join("?" * 9)
    placeholders["dict_category_alias"] = ", ".join("?" * 2)
    width = {"xhs_creator": 18, "xhs_creator_category": 2,
             "xhs_staging": 9, "dict_category_alias": 2}

    for lineno, stmt in iter_statements(src, args.encoding):
        m = INSERT_RE.match(stmt)
        if not m:
            continue
        table = m.group("table")
        if table not in SCHEMAS:
            print(f"  ! 未知表 {table}（第 {lineno} 行），跳过")
            skipped += 1
            continue
        body_start = m.end()
        try:
            for row in parse_values(stmt, body_start):
                if len(row) != width[table]:
                    # 列数不符：补齐/截断并计数
                    row = (row + [None] * width[table])[:width[table]]
                pending[table].append(tuple(coerce(table, row)))
        except ValueError as exc:
            print(f"  ! 解析失败 第 {lineno} 行 {table}: {exc}")
            skipped += 1
            continue

        if len(pending[table]) >= 5000:
            conn.executemany(
                f"INSERT INTO {table} VALUES ({placeholders[table]})", pending[table])
            counts[table] += len(pending[table])
            pending[table].clear()

    for table in SCHEMAS:
        if pending[table]:
            conn.executemany(
                f"INSERT INTO {table} VALUES ({placeholders[table]})", pending[table])
            counts[table] += len(pending[table])
            pending[table].clear()
    conn.commit()

    print("== 导入完成 ==")
    for table in SCHEMAS:
        (n,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        print(f"  {table:24s} 解析 {counts[table]:>8,}  SQLite 实际 {n:>8,}")
    if skipped:
        print(f"  跳过的语句：{skipped}")

    for ddl in INDEXES:
        conn.execute(ddl)
    conn.commit()
    conn.close()
    print(f"SQLite: {out.resolve()}  ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    sys.exit(main())
