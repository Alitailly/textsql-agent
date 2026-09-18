#!/usr/bin/env python3
"""把原始表整理成 SQLite，并打印一份小画像报告。

原始数据和生成的 .sqlite3 都留在你本机。屏幕上打印的那几十行报告才是要贴给
设计者的东西——里面只有字段形态，没有完整数据，邮箱、长 id 会自动打码。

用法：

    python3 profile_table.py 原始文件.csv

    可选参数：
      --table NAME    表名，默认取文件名
      --out PATH      输出的 SQLite 路径，默认 <表名>.sqlite3
      --sample N      样例行数，默认 5
      --max-enum N    枚举列的取值上限，超过只打印前 N 个，默认 30
      --sheet NAME    读 Excel 时的 sheet 名（需要 openpyxl）

支持 .csv / .tsv / .txt（自动判断编码与分隔符）。Excel 请先另存为 CSV，
或把 --sheet 传上并确保本机装了 openpyxl。

零第三方依赖（除可选的 openpyxl）。大文件是流式写入，不会一次性读进内存。
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sqlite3
import sys
from pathlib import Path

ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1")
DELIMITERS = (",", "\t", ";", "|")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^1[3-9]\d{9}$")
LONG_DIGITS = re.compile(r"^\d{16,}$")
LONG_HEX = re.compile(r"^[0-9a-fA-F]{20,}$")


def open_text(path: Path):
    """按候选编码逐个试，返回能读通的文本流。"""
    raw = path.read_bytes()[:65536]
    for encoding in ENCODINGS:
        try:
            raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return path.open("r", encoding=encoding, newline="")
    return path.open("r", encoding="utf-8", errors="replace", newline="")


def sniff_delimiter(path: Path) -> str:
    text = open_text(path).read(8192)
    try:
        return csv.Sniffer().sniff(text, delimiters="".join(DELIMITERS)).delimiter
    except csv.Error:
        # 退回"哪一列最多就用哪个"
        counts = {d: text.count(d) for d in DELIMITERS}
        return max(counts, key=lambda d: counts[d])


def dedupe(header: list[str]) -> list[str]:
    """列名去空白、补空名、去重。"""
    seen: dict[str, int] = {}
    result = []
    for index, name in enumerate(header):
        clean = (name or "").strip() or f"col_{index + 1}"
        if clean in seen:
            seen[clean] += 1
            clean = f"{clean}_{seen[clean]}"
        else:
            seen[clean] = 0
        result.append(clean)
    return result


def read_rows(path: Path, sheet: str | None):
    """产出 (header, rows)。rows 是字符串元组的迭代器。"""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # type: ignore
        except ImportError:
            sys.exit("读 Excel 需要 openpyxl：pip install openpyxl，或先另存为 CSV。")
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet_obj = book[sheet] if sheet else book.active
        iterator = sheet_obj.iter_rows(values_only=True)
        header = next(iterator)
        header = dedupe(["" if c is None else str(c) for c in header])
        width = len(header)

        def row_gen(rows):
            for row in rows:
                cells = ["" if c is None else str(c) for c in row][:width]
                cells += [""] * (width - len(cells))
                yield tuple(cells)

        return header, row_gen(iterator)

    stream = open_text(path)
    reader = csv.reader(stream, delimiter=sniff_delimiter(path))
    header = dedupe(next(reader))
    width = len(header)

    def row_gen(rows):
        for row in rows:
            cells = row[:width]
            cells += [""] * (width - len(cells))
            yield tuple(cells)

    return header, row_gen(reader)


def load_sqlite(path: Path, table: str, out: Path, sheet: str | None) -> int:
    header, rows = read_rows(path, sheet)
    if out.exists():
        out.unlink()
    columns = ", ".join(f'"{name}" TEXT' for name in header)
    connection = sqlite3.connect(out)
    connection.execute(f'CREATE TABLE "{table}" ({columns})')
    placeholders = ", ".join("?" * len(header))
    batch: list[tuple[str, ...]] = []
    total = 0
    for row in rows:
        batch.append(tuple(str(cell) for cell in row))
        if len(batch) >= 2000:
            connection.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', batch)
            total += len(batch)
            batch.clear()
    if batch:
        connection.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', batch)
        total += len(batch)
    connection.commit()
    connection.close()
    return total


def redact(value: str) -> str:
    """只挡能定位到人的东西：邮箱、手机号、超长数字串（真实用户 id 那种）。
    粉丝数这类数字保留原样——量级恰恰是要看的信息。"""
    if EMAIL.match(value):
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    if PHONE.match(value):
        return f"{value[:3]}****{value[-2:]}"
    if LONG_DIGITS.match(value) or LONG_HEX.match(value):
        return f"{value[:3]}***{value[-2:]}"
    if len(value) > 24:
        return value[:24] + "…"
    return value


def fmt_number(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:g}"


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def looks_numeric(values: list[str]) -> bool:
    filled = [v for v in values if v.strip()]
    if not filled:
        return False
    ok = sum(1 for v in filled if re.fullmatch(r"-?\d+(\.\d+)?", v.strip()))
    return ok / len(filled) >= 0.9


def report(out: Path, table: str, sample: int, max_enum: int) -> None:
    connection = sqlite3.connect(out)
    cursor = connection.cursor()
    columns = [row[1] for row in cursor.execute(f"PRAGMA table_info({quote(table)})")]
    (total,) = cursor.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()

    print("=" * 68)
    print(f"表：{table}    总行数：{total}    字段数：{len(columns)}")
    print("=" * 68)
    print()

    for name in columns:
        col = quote(name)
        (files,) = cursor.execute(
            f"SELECT COUNT(*) FROM {quote(table)} "
            f"WHERE {col} IS NULL OR TRIM({col}) = ''"
        ).fetchone()
        (distinct,) = cursor.execute(
            f"SELECT COUNT(DISTINCT {col}) FROM {quote(table)}"
        ).fetchone()
        blank = f"{files / total:.0%}" if total else "-"
        print(f"— {name}")
        print(f"    空值 {files}（{blank}）    去重后 {distinct} 个不同值")

        if distinct == 0:
            print()
            continue

        if distinct <= max_enum:
            pairs = cursor.execute(
                f"SELECT {col}, COUNT(*) FROM {quote(table)} "
                f"GROUP BY {col} ORDER BY 2 DESC, 1 LIMIT {max_enum}"
            ).fetchall()
            rendered = "、".join(
                f"{redact(value) if value else '(空)'}×{count}" for value, count in pairs
            )
            print(f"    取值：{rendered}")
        else:
            pairs = cursor.execute(
                f"SELECT {col}, COUNT(*) FROM {quote(table)} "
                f"GROUP BY {col} ORDER BY 2 DESC LIMIT 5"
            ).fetchall()
            rendered = "、".join(
                f"{redact(value) if value else '(空)'}×{count}" for value, count in pairs
            )
            print(f"    取值（共 {distinct} 个，最常见的 5 个）：{rendered}")

        sample_values = [
            row[0] or ""
            for row in cursor.execute(
                f"SELECT {col} FROM {quote(table)} "
                f"WHERE {col} IS NOT NULL AND TRIM({col}) != '' "
                f"LIMIT 200"
            ).fetchall()
        ]
        if looks_numeric(sample_values):
            numbers = sorted(float(v) for v in sample_values if re.fullmatch(r"-?\d+(\.\d+)?", v.strip()))
            if numbers:
                middle = numbers[len(numbers) // 2]
                print(
                    f"    看着像数字：小 {fmt_number(numbers[0])}"
                    f" / 中 {fmt_number(middle)}"
                    f" / 大 {fmt_number(numbers[-1])}"
                )
        print()

    print("-" * 68)
    print(f"样例 {sample} 行（邮箱、长数字已打码）")
    print("-" * 68)
    for row in cursor.execute(f"SELECT * FROM {quote(table)} LIMIT {sample}").fetchall():
        pairs = [f"{name}={redact(str(value or ''))}" for name, value in zip(columns, row)]
        print("  " + " | ".join(pairs))
    print()
    print("=" * 68)
    print(f"SQLite 已生成：{out.resolve()}")
    print("把上面这段（从「表：」到样例结束）贴回对话即可。")
    print("=" * 68)
    connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="原始表 → SQLite + 画像报告")
    parser.add_argument("source", type=Path)
    parser.add_argument("--table", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--sample", type=int, default=5)
    parser.add_argument("--max-enum", type=int, default=30)
    parser.add_argument("--sheet", default=None)
    args = parser.parse_args()

    if not args.source.exists():
        sys.exit(f"找不到文件：{args.source}")

    table = args.table or re.sub(r"\W+", "_", args.source.stem).strip("_") or "t"
    out = args.out or args.source.with_suffix(".sqlite3")

    rows = load_sqlite(args.source, table, out, args.sheet)
    print(f"已读入 {rows} 行 → {out}")
    print()
    report(out, table, args.sample, args.max_enum)


if __name__ == "__main__":
    main()
