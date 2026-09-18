#!/usr/bin/env python3
"""问法体检：只给一句问法，把它在真管线上跑一遍，把每一跳摆出来。

写问法之前就能用——你手里还没有金标 SQL 也能跑，跑完再决定这句值不值得进金标。

用法：

    uv run --with pyyaml --python 3.12 python check_question.py xhs_pgy.sqlite3 "问法"

它回答的是：
    1  主 Agent 把这句收成了什么当前问题
    2  小模型改写成什么（软，只报告，不判）
    3  Vanna 三路材料各命中多少字。哪一路是空的，就是哪一路没料
    4  查库大模型写的 SQL，以及有没有用到材料里根本没有的名字
    5  命中基数，以及另一条通路独立重数的结果
    6  问句里的中文词哪些能对上 114 个类目名；对不上的就是需要同义词的词
    7  SQL 有没有踩库里已知的坑（D1 港澳台、city 空值、邮箱全角、类目名不在名单里）

只读。不进 src/。业务词只出现在 config/ 与库数据里。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textsql_harness import (  # noqa: E402
    build_stack,
    category_names,
    category_probe,
    describe_env,
    judge,
    load_config,
    render,
)

_CATEGORY_LITERAL = re.compile(r"category_name\s*=\s*'([^']+)'")
_HK_TW = re.compile(r"%\s*(香港|澳门|台湾|港|澳|台)\s*%")
_EMAIL_LIKE = re.compile(r"email\s+(?:NOT\s+)?LIKE", re.IGNORECASE)
_CITY_FILTER = re.compile(r"\bcity\b\s*(=|LIKE|IN)", re.IGNORECASE)
_CHINA = re.compile(r"province\s*=\s*'中国'")


def known_risks(sql: str, names: list[str]) -> list[str]:
    """库里已经量出来的坑，按 SQL 里出现的写法逐条报。"""
    risks: list[str] = []
    if _HK_TW.search(sql):
        risks.append(
            "D1：港澳台在 province 里查不到（含『香港』字样的行 0 条）。"
            "这 2,498 行落成 province='中国'，按省级行政区筛一律 0 行——是数据缺口，不是查询写错"
        )
    if _CHINA.search(sql):
        risks.append("province='中国' 有 2,498 行，它不是省级行政区，多数问法不该命中它")
    if _CITY_FILTER.search(sql):
        risks.append("city 有 105,154 行为空（22.4%），按城市筛会静默漏掉这批人")
    if _EMAIL_LIKE.search(sql):
        risks.append("email 含全角符号（581 行），LIKE '%@%' 会漏；要同时判 NULL 与空串")
    for literal in _CATEGORY_LITERAL.findall(sql):
        if literal not in names:
            risks.append(
                f"category_name='{literal}' 不在 114 个类目名里，必然 0 行（不是报错）"
            )
    return risks


def main() -> int:
    parser = argparse.ArgumentParser(description="问法体检")
    parser.add_argument("db", type=Path, help="库文件，例如 xhs_pgy.sqlite3")
    parser.add_argument("question", help="一句问法，用引号括起来")
    parser.add_argument("--row-limit", type=int, default=200, help="执行器取行上限")
    args = parser.parse_args()

    config = load_config()
    names = category_names(args.db)
    stack = build_stack(args.db, config, row_limit=args.row_limit)

    print(f"环境      {describe_env()}")
    print(f"库        {args.db}   类目名 {len(names)} 个")
    print("─" * 72)

    result = stack.run(args.question)
    hop = judge({"原话": args.question}, args.db, config, result)
    print(render(hop))

    print("─" * 72)
    probe_source = result.rewritten_question or args.question
    exact, near = category_probe(probe_source, names)
    print("类目对账")
    if exact:
        print(f"  问句里直接出现了类目名：{exact}")
    else:
        print("  问句里没有任何一个完整的类目名")
    for piece, hits in near:
        shown = "、".join(hits[:6]) + ("…" if len(hits) > 6 else "")
        print(f"  「{piece}」→ 名字里含它的：{shown}")
    if not near:
        print("  问句里的词和 114 个类目名一个都对不上——要么不是类目词，要么同义词还差一层")
    print(
        "  ↳ 要按类目筛，问句里必须有完整类目名，或者补进 config/blogger_docs.md 的同义词映射。"
        "库里 dict_category_alias 是空表，映射也可以落到那里"
    )

    risks = known_risks(result.sql_text or "", names)
    print("已知坑")
    if risks:
        for risk in risks:
            print(f"  ! {risk}")
    else:
        print("  没有踩到已知的坑")

    print(f"\n本轮模型调用 {len(stack.calls_this_turn)} 次")
    for call in stack.calls_this_turn:
        print(f"  {call.seconds}s  in={call.prompt_tokens} out={call.completion_tokens}")

    if risks and result.matched_rows == 0:
        print("\n结论      这句现在查不到人，且原因在数据侧（见上）。别急着改 SQL")
        return 2
    return 1 if hop.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
