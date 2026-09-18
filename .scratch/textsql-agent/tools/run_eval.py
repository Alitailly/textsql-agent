#!/usr/bin/env python3
"""跑金标：每条问法过一遍真管线，把每一跳和判定打出来。

判据分两层，别混：
  硬  基数（另开一条只读连接独立重数再比）／锚点必含必不含／期望分支／
      模型用到材料里没有的名字／必须提示
  软  改写问题长什么样。只报告，不判——改写好不好靠人读

用法：

    uv run --with pyyaml --python 3.12 python run_eval.py xhs_pgy.sqlite3
    uv run --with pyyaml --python 3.12 python run_eval.py xhs_pgy.sqlite3 --only q01,q03
    uv run --with pyyaml --python 3.12 python run_eval.py xhs_pgy.sqlite3 --backfill

金标过期的判定：config 里的锚点行数、或某条的 `基数`，和库对不上就先报过期，
这一条不参与判定——不然分不清"模型查错了"和"金标记错了"。

只读（除 --backfill 会写 config/blogger.yml 的 `基数` 栏）。不进 src/。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textsql_harness import (  # noqa: E402
    and_conjuncts,
    attribute,
    build_stack,
    describe_env,
    judge,
    load_config,
    readonly_count,
    mask_email,
    render,
    stamp,
    unknown_names,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_QUESTIONS = 2
EXIT_STALE = 3

# 自检用的退化 SQL。它们的用处是校准判据本身，不是业务问法，千万别抄进金标。
# 这几条不调模型，所以没有 API key 也能跑。
WHOLE_TABLE = "SELECT xhs_id FROM xhs_creator WHERE xhs_id IS NOT NULL"
SAME_MEANING = "SELECT nickname, xhs_id FROM xhs_creator WHERE xhs_id IS NOT NULL ORDER BY xhs_id"
DROPPED = (
    "SELECT xhs_id FROM xhs_creator "
    "WHERE xhs_id IS NOT NULL AND followers >= 1000 AND email IS NOT NULL"
)
DROPPING = "SELECT xhs_id FROM xhs_creator WHERE xhs_id IS NOT NULL AND followers >= 1000"
COMPLEMENT = "SELECT xhs_id FROM xhs_creator WHERE xhs_id IS NULL"
# 带字符串字面量的正确 SQL。曾经把字面量里的值当"模型编的名字"，把对的判成 FAIL（F6）。
# 表名列名全对，只有一个筛选值，名字核对必须报「无」。
WITH_LITERAL = (
    "SELECT xhs_id FROM xhs_creator "
    "WHERE email = 'not-a-real@example.com' AND xhs_id IS NOT NULL"
)
# 字面量里塞进 SQL 关键字和注释符号，再验一遍剥离不会剥过头、把真名字也吃掉。
LITERAL_WITH_NOISE = (
    "SELECT xhs_id FROM xhs_creator "
    "WHERE nickname = '-- select from drop table' AND email = 'a@b.com' -- 注释"
)
# **剥过头的载重判据**：字面量里含 `--`，后面还跟着一个真编的库名。
# 早期实现「先剥注释、再剥字面量」，字面量里的 `--` 会吃掉整行，把 member 也吞掉，
# 真缺陷变成假 PASS（实测）。上面那条 LITERAL_WITH_NOISE 挡不住这个——它剥完之后
# 后面没有需要抓的名字，所以怎么剥过头都照样绿，是个装饰性的判据（复核时指出）。
LITERAL_HIDES_NAME = (
    "SELECT xhs_id FROM xhs_creator WHERE nickname = '--' AND member = 1"
)
# 真编了名字的要照样抓出来——剥离字面量不能把这条判据变成永远通过。
INVENTED_NAME = "SELECT xhs_id FROM member WHERE email IS NOT NULL"


def rows_in_db(db_path: Path) -> int:
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT COUNT(*) FROM xhs_creator").fetchone()
    finally:
        connection.close()
    return int(row[0])


def backfill(path: Path, updates: dict[str, int]) -> list[str]:
    """把实测基数写回 config 的 `基数` 栏。只动那一行，其余原文不动。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    current: str | None = None
    changed: list[str] = []
    for index, line in enumerate(lines):
        found = re.match(r"\s*-\s*id:\s*(\S+)", line)
        if found:
            current = found.group(1)
            continue
        if current is None or current not in updates:
            continue
        if line.lstrip().startswith("#") or not re.match(r"\s*基数:\s*", line):
            continue
        lines[index] = re.sub(r"(基数:\s*).*", rf"\g<1>{updates[current]}", line, count=1)
        changed.append(current)
        current = None
    path.write_text("".join(lines), encoding="utf-8")
    return changed


def self_test(db_path: Path, config: Config) -> int:
    """用退化解校准判据本身：判据不对，真问法的判定就没有意义。

    不调模型，只看比对器、名字核对和归因变体这三件机械的事对不对。
    """
    failures = 0

    def check(label: str, ok: bool, detail: str) -> None:
        nonlocal failures
        print(f"  [{'✓' if ok else '✗'}] {label:<28}{detail}")
        if not ok:
            failures += 1

    whole = readonly_count(db_path, WHOLE_TABLE)
    check("全表基数", whole == 470345, f"{whole}（档案里记 470,345）")

    same = readonly_count(db_path, SAME_MEANING)
    check("同语义另一种写法", same == whole, f"{same} vs {whole}——判据不靠 SQL 字面")

    invented = unknown_names(SAME_MEANING, config.ddl, config.documentation)
    check("名字核对不误报", not invented, f"未知名字 {invented or '无'}")

    literal = unknown_names(WITH_LITERAL, config.ddl, config.documentation)
    check("字面量不当名字", not literal, f"未知名字 {literal or '无'}（字面量里的值不算名字）")

    noise = unknown_names(LITERAL_WITH_NOISE, config.ddl, config.documentation)
    check("注释不当名字", not noise, f"未知名字 {noise or '无'}（注释和带关键字的字面量都不算）")

    # 剥过头的载重判据：字面量里的 `--` 不许把后面真编的名字一起吞掉。
    # 判据是「member 必须被抓到」——不是「结果为空」。方向反了就成了装饰。
    hidden = [n.lower() for n in unknown_names(LITERAL_HIDES_NAME, config.ddl, config.documentation)]
    check("剥过头会暴露", "member" in hidden, f"抓到 {hidden or '无'}（字面量里的 -- 不许吞掉后文）")

    real = unknown_names(INVENTED_NAME, config.ddl, config.documentation)
    check("真编名照样抓", "member" in [n.lower() for n in real], f"抓到 {real or '无'}")

    gold = readonly_count(db_path, DROPPED)
    model = readonly_count(db_path, DROPPING)
    notes = attribute(db_path, DROPPED, model)
    lost = [c for c in and_conjuncts(DROPPED) if c not in and_conjuncts(DROPPING)]
    reported = any(lost[0] in note for note in notes) if lost else False
    check(
        "丢条件能被归因",
        bool(lost) and reported,
        f"金标 {gold} 模型 {model}；应报出少了「{lost[0] if lost else '?'}」→ {notes}",
    )

    complement = readonly_count(db_path, COMPLEMENT)
    check(
        "补集方向可判",
        complement == 470399 - whole,
        f"{complement}（= 总行数 − 全表 {whole}），补集数远小于全表，不是笼统报个数字",
    )

    print(f"\n自检{'通过' if not failures else f'未过 {failures} 项'}")
    return EXIT_OK if not failures else EXIT_FAIL


def main() -> int:
    parser = argparse.ArgumentParser(description="跑金标")
    parser.add_argument("db", type=Path, help="库文件，例如 xhs_pgy.sqlite3")
    parser.add_argument("--only", default="", help="只跑这几条，逗号分隔，例如 q01,q03")
    parser.add_argument("--row-limit", type=int, default=200, help="执行器取行上限")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="只用退化解校准判据本身，不调模型、不读金标问法",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="把实测基数写回 config/blogger.yml（不写库，只写配置）",
    )
    args = parser.parse_args()

    if args.self_test:
        config = load_config()
        print(f"跑于      {stamp()}\n库        {args.db}\n自检      退化 SQL，不是业务问法\n")
        return self_test(args.db, config)

    config = load_config()
    questions = config.questions
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        questions = [item for item in questions if str(item.get("id")) in wanted]

    if not questions:
        # 这一步必须排在 describe_env() 前面：读环境要 API key，没 key 的机器上
        # 会抛 DeepSeekError，把「还没填问法」这条最该先看到的提示顶掉。
        print(f"跑于      {stamp()}")
        print(f"库        {args.db}")
        print(f"金标      {config.path}")
        print("\n还没填问法。config/blogger.yml 的 `问法` 现在是空的。")
        print("先用 check_question.py 体检一句，确认它能落成有区分度的 SQL 再写进去。")
        return EXIT_NO_QUESTIONS

    print(f"跑于      {stamp()}")
    print(f"环境      {describe_env()}")
    print(f"库        {args.db}")
    print(f"金标      {config.path}")

    real_rows = rows_in_db(args.db)
    stale: list[str] = []
    recorded = config.anchor.get("行数")
    if recorded is not None and int(recorded) != real_rows:
        stale.append(f"锚点行数 记的是 {recorded}，库里是 {real_rows}")
    if config.anchor.get("快照日"):
        print(f"快照日    {config.anchor['快照日']}   行数 记 {recorded} / 实 {real_rows}")
    if stale:
        print("\n金标过期：")
        for item in stale:
            print(f"  ! {item}")

    stack = build_stack(args.db, config, row_limit=args.row_limit)
    updates: dict[str, int] = {}
    tally = {"PASS": 0, "PARTIAL": 0, "FAIL": 0, "STALE": 0}

    for sample in questions:
        identifier = str(sample.get("id") or "?")
        gold_sql = str(sample.get("金标SQL") or "").strip()
        recorded_count = sample.get("基数")
        if gold_sql:
            live = readonly_count(args.db, gold_sql)
            if recorded_count is not None and int(recorded_count) != live:
                print(f"\n{'═' * 72}\n{identifier}  金标过期")
                print(f"  `基数` 记的是 {recorded_count}，按金标 SQL 重数是 {live}")
                print("  先解决这个：库变了，或者金标 SQL 变了。这一条不参与判定")
                tally["STALE"] += 1
                continue
            if recorded_count is None:
                updates[identifier] = live

        expected_question = str(sample.get("当前问题") or "").strip()
        utterance = str(sample.get("原话") or expected_question)
        result = stack.run(utterance)
        hop = judge(sample, args.db, config, result)

        print(f"\n{'═' * 72}\n{identifier}")
        print(render(hop))
        if expected_question and result.query_tool_arguments:
            got = result.query_tool_arguments.get("当前问题", "")
            same = got.strip() == expected_question
            mark = "✓ 与金标一致" if same else "! 与金标不同"
            print(f"  主 Agent 收的问题  {mark}  金标：{mask_email(expected_question)}")
            if not same:
                print(f"                    实得：{mask_email(got)}")
        print(f"  本轮模型调用 {len(stack.calls_this_turn)} 次")

        verdict = "FAIL" if hop.failed else ("PARTIAL" if hop.warned else "PASS")
        tally[verdict] += 1

    print(f"\n{'═' * 72}\n汇总      PASS {tally['PASS']}   PARTIAL {tally['PARTIAL']}   "
          f"FAIL {tally['FAIL']}   金标过期 {tally['STALE']}")

    if updates:
        if args.backfill:
            changed = backfill(config.path, updates)
            print(f"回填基数  写了 {changed} 到 {config.path}")
        else:
            print("基数未填   " + "  ".join(f"{k}={v}" for k, v in updates.items()))
            print("           加 --backfill 写回 config/blogger.yml")

    if tally["STALE"]:
        return EXIT_STALE
    if tally["FAIL"]:
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
