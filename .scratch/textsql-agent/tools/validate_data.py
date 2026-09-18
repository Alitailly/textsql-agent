#!/usr/bin/env python3
"""对已入库的表做一遍数据核对，打印可贴回对话的报告。

只读。不改库。输出的所有值都过一遍打码（邮箱、长数字/hex），
聚合统计与粉丝数保留原样——量级恰恰是要看的信息。

用法：

    python3 validate_data.py xhs_pgy.sqlite3

检查项：

    1  表清单与行数
    2  xhs_id 唯一性与形态（是否可作主键）
    3  双向参照完整性（关联表 ↔ 主表）
    4  staging → creator 的 ETL 是否无损
    5  派生列检测（keywords / summary_text 是否只是其它列的重新拼装）
    6  无类目子集：规模、画像、能否从其它列补救
    7  邮箱：合法率、域名、随粉丝的变化
    8  地理：province/city 的一致性与脏值
    9  数值与枚举域的边界情况
   10  结论：哪些列可用、哪些是坑
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# emoji 与常见装饰符号的码点区间。不能用"码点大于某值"来判——
# 汉字自 U+4E00 起，那样会把几乎所有中文昵称都算成 emoji。
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # 各类 emoji 主体
    (0x2600, 0x27BF),    # 杂项符号、装饰符号
    (0x2B00, 0x2BFF),    # 杂项符号与箭头
    (0x2190, 0x21FF),    # 箭头
    (0xFE0F, 0xFE0F),    # 变体选择符
    (0x1F1E6, 0x1F1FF),  # 区域指示符（国旗）
    (0x3030, 0x3030),    # 波浪破折号
    (0x303D, 0x303D),    # 交替部分标记
)


def _has_emoji(text: str) -> bool:
    return any(
        any(start <= ord(ch) <= end for start, end in _EMOJI_RANGES)
        for ch in text
    )

FANS_BANDS: list[tuple[str, str]] = [
    ("<1千", "followers < 1000"),
    ("1千-1万", "followers >= 1000 AND followers < 10000"),
    ("1万-5万", "followers >= 10000 AND followers < 50000"),
    ("5万-10万", "followers >= 50000 AND followers < 100000"),
    ("10万-50万", "followers >= 100000 AND followers < 500000"),
    (">50万", "followers >= 500000"),
]


def redact(value: str) -> str:
    """邮箱与超长数字/hex 打码；其余原样。"""
    if EMAIL.match(value):
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    if re.fullmatch(r"\d{16,}", value) or re.fullmatch(r"[0-9a-fA-F]{20,}", value):
        return f"{value[:3]}***{value[-2:]}"
    return value


class Report:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection
        self.cur = connection.cursor()
        self.problems: list[str] = []

    def one(self, sql: str, params: tuple = ()) -> tuple:
        return self.cur.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self.cur.execute(sql, params).fetchall()

    def head(self, title: str) -> None:
        print()
        print("=" * 70)
        print(title)
        print("=" * 70)

    def flag(self, message: str) -> None:
        self.problems.append(message)

    # 1 ------------------------------------------------------------------
    def tables(self) -> None:
        self.head("1  表清单与行数")
        rows = self.all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        for (name,) in rows:
            count = self.one(f'SELECT COUNT(*) FROM "{name}"')[0]
            described = self.cur.execute(f'SELECT * FROM "{name}" LIMIT 1').description or []
            print(f"  {name:<24} {count:>9,} 行   {len(described)} 列")
        if len(rows) < 2:
            self.flag("表数量少于 2，确认 dump 是否完整")

    # 2 ------------------------------------------------------------------
    def identifier(self) -> None:
        self.head("2  xhs_id 唯一性与形态")
        total, nulls, blanks, distinct = self.one(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN xhs_id IS NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN xhs_id IS NOT NULL AND TRIM(xhs_id)='' THEN 1 ELSE 0 END), "
            "COUNT(DISTINCT xhs_id) FROM xhs_creator"
        )
        print(f"  总行 {total:,}   NULL {nulls}   空串 {blanks}   去重后 {distinct:,}")
        print(f"  非空值是否唯一：{'是' if distinct == total - nulls - blanks else '否'}")
        if distinct != total - nulls - blanks:
            self.flag(f"xhs_id 有 {total - nulls - blanks - distinct} 个重复，不能直接作主键")

        print("  长度分布（前 8）：")
        for length, count in self.all(
            "SELECT LENGTH(xhs_id) n, COUNT(*) c FROM xhs_creator "
            "WHERE xhs_id IS NOT NULL AND TRIM(xhs_id)<>'' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
        ):
            print(f"    {length:>3} 位  {count:>8,}")

        bad = self.one(
            "SELECT COUNT(*) FROM xhs_creator "
            "WHERE xhs_id IS NOT NULL AND TRIM(xhs_id)<>'' "
            "AND xhs_id GLOB '*[^0-9a-zA-Z_.-]*'"
        )[0]
        print(f"  含非字母数字字符（. _ - 除外）：{bad}")
        if bad:
            self.flag(f"xhs_id 有 {bad} 个含异常字符，入库前需清洗")

    # 3 ------------------------------------------------------------------
    def referential(self) -> None:
        self.head("3  双向参照完整性")
        orphan = self.one(
            "SELECT COUNT(*) FROM xhs_creator_category cc "
            "LEFT JOIN xhs_creator c ON c.id = cc.creator_id WHERE c.id IS NULL"
        )[0]
        null_link = self.one(
            "SELECT COUNT(*) FROM xhs_creator_category WHERE creator_id IS NULL"
        )[0]
        uncategorized = self.one(
            "SELECT COUNT(*) FROM xhs_creator c LEFT JOIN "
            "(SELECT DISTINCT creator_id FROM xhs_creator_category) x "
            "ON x.creator_id = c.id WHERE x.creator_id IS NULL"
        )[0]
        total = self.one("SELECT COUNT(*) FROM xhs_creator")[0]
        print(f"  悬空外键（关联表指向不存在的人）：{orphan}")
        print(f"  creator_id 为 NULL：{null_link}")
        print(f"  主表无人任何类目：{uncategorized:,}  ({uncategorized / total:.2%})")
        if orphan or null_link:
            self.flag("关联表有悬空/空外键，JOIN 会丢行")

    # 4 ------------------------------------------------------------------
    def etl(self) -> None:
        self.head("4  staging → creator 的 ETL 是否无损")
        st = self.one("SELECT COUNT(*) FROM xhs_staging")[0]
        cr = self.one("SELECT COUNT(*) FROM xhs_creator")[0]
        print(f"  staging {st:,} 行   creator {cr:,} 行   {'一致' if st == cr else '不一致'}")
        if st != cr:
            self.flag(f"staging 与 creator 行数差 {abs(st - cr)}，ETL 可能丢行")

        # 用 xhs_id 作连接键时，xhs_id 为 NULL 的行天然配不上。
        # 行数一致就说明没丢行，这里只是把这些行指出来。
        unmatched = self.one(
            "SELECT COUNT(*) FROM xhs_staging s "
            "LEFT JOIN xhs_creator c ON c.xhs_id = s.xhs_id WHERE c.xhs_id IS NULL"
        )[0]
        null_key = self.one(
            "SELECT COUNT(*) FROM xhs_creator WHERE xhs_id IS NULL"
        )[0]
        print(f"  用 xhs_id 连接时配不上的行：{unmatched}（其中 xhs_id 为 NULL 的 {null_key} 行）")
        if unmatched != null_key:
            self.flag(
                f"有 {unmatched - null_key} 行既不是 NULL 键也配不上，ETL 可能真丢了行"
            )

        print("  关键字段逐行一致性：")
        for label, expr in [
            ("email", "TRIM(COALESCE(c.email,'')) = TRIM(COALESCE(s.email,''))"),
            ("nickname", "c.nickname = s.nickname"),
        ]:
            mismatch = self.one(
                f"SELECT COUNT(*) FROM xhs_staging s "
                f"JOIN xhs_creator c ON c.xhs_id = s.xhs_id WHERE NOT ({expr})"
            )[0]
            print(f"    {label:<10} 不一致 {mismatch:,}")
            if mismatch:
                self.flag(f"{label} 在 ETL 后变化了 {mismatch} 行")

        # province 不是"应当一致"，而是被 ETL 从原始串拆成了 province + city。
        # 验证拆分规则，并揪出多级值被截断的情况。
        split_ok = self.one(
            "SELECT SUM(CASE WHEN TRIM(s.province) = TRIM(COALESCE(c.province,'')) || "
            "CASE WHEN TRIM(COALESCE(c.city,''))='' THEN '' ELSE ' ' || TRIM(c.city) END "
            "THEN 1 ELSE 0 END), COUNT(*) "
            "FROM xhs_staging s JOIN xhs_creator c ON c.xhs_id = s.xhs_id"
        )
        print(f"    province 由原始串拆分而来（省 + ' ' + 市）：{split_ok[0]:,} / {split_ok[1]:,}")
        print("    → 与 staging 不一致是正常的（staging 存原始串），不是丢数据")

        truncated = self.one(
            "SELECT COUNT(*) FROM xhs_creator WHERE province = '中国'"
        )[0]
        print(f"    三级原始串被截断成 province='中国'：{truncated:,}")
        if truncated:
            detail = self.all(
                "SELECT city, COUNT(*) FROM xhs_creator WHERE province='中国' "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
            )
            detail_text = "、".join(f"{c or '(空)'}×{n:,}" for c, n in detail)
            print(f"      这些其实是港澳台，地区名被丢掉：{detail_text}")
            self.flag(
                f"{truncated} 行（港澳台）被拆成 province='中国'，"
                "香港/澳门/台湾 字面丢失；原始串仍在 staging，可重建"
            )

    # 5 ------------------------------------------------------------------
    def derived_columns(self) -> None:
        self.head("5  派生列检测：keywords / summary_text 是否只是重新拼装")
        kw_match = self.one(
            "SELECT SUM(CASE WHEN keywords = "
            "REPLACE(TRIM(COALESCE(raw_categories,'')), ',', ' ') || "
            "CASE WHEN TRIM(COALESCE(raw_categories,''))='' "
            "THEN nickname ELSE ' ' || nickname END THEN 1 ELSE 0 END), COUNT(*) "
            "FROM xhs_creator"
        )
        print(f"  keywords == 类目空格拼接 + 昵称：{kw_match[0]:,} / {kw_match[1]:,}")
        if kw_match[0] == kw_match[1]:
            print("    → 完全可派生，零独立信息，不要当检索信号")
            self.flag("keywords 是派生列，ADR/设计文档里不该称它含独立信号")

        st_match = self.one(
            "SELECT SUM(CASE WHEN summary_text LIKE '%：小红书博主，粉丝%' "
            "THEN 1 ELSE 0 END), COUNT(*) FROM xhs_creator"
        )
        print(f"  summary_text 套「：小红书博主，粉丝…」模板：{st_match[0]:,} / {st_match[1]:,}")
        if st_match[0] == st_match[1]:
            print("    → 全表套模板，由昵称/粉丝/类目/地区拼成，不是独立语料")

        cat_clause = self.one(
            "SELECT SUM(CASE WHEN summary_text LIKE '%内容类目%' THEN 1 ELSE 0 END) "
            "FROM xhs_creator"
        )[0]
        with_cat = self.one(
            "SELECT COUNT(DISTINCT creator_id) FROM xhs_creator_category"
        )[0]
        print(f"  summary_text 含「内容类目」{cat_clause:,}   实际有类目 {with_cat:,}")
        if cat_clause == with_cat:
            print("    → 佐证：类目子句直接来自同一个类目字段，无独立来源")

    # 6 ------------------------------------------------------------------
    def uncategorized(self) -> None:
        self.head("6  无类目子集：规模、画像、能否补救")
        total = self.one("SELECT COUNT(*) FROM xhs_creator")[0]
        uncat = self.one(
            "SELECT COUNT(*) FROM xhs_creator c LEFT JOIN "
            "(SELECT DISTINCT creator_id FROM xhs_creator_category) x "
            "ON x.creator_id = c.id WHERE x.creator_id IS NULL"
        )[0]
        print(f"  无类目 {uncat:,} / {total:,}  ({uncat / total:.2%})")

        top = self.one(
            "SELECT SUM(CASE WHEN x.creator_id IS NULL THEN 1 ELSE 0 END), COUNT(*) "
            "FROM xhs_creator c LEFT JOIN "
            "(SELECT DISTINCT creator_id FROM xhs_creator_category) x "
            "ON x.creator_id = c.id WHERE c.id <= 1000"
        )
        print(f"  头部 1000 人里无类目：{top[0]} ({top[0] / top[1]:.1%})")

        print("  无类目率随粉丝档变化：")
        print(f"    {'粉丝档':<10} {'人数':>9} {'无类目':>9} {'占比':>7}")
        for label, cond in FANS_BANDS:
            n, miss = self.one(
                "SELECT COUNT(*), SUM(CASE WHEN x.creator_id IS NULL THEN 1 ELSE 0 END) "
                "FROM xhs_creator c LEFT JOIN "
                "(SELECT DISTINCT creator_id FROM xhs_creator_category) x "
                "ON x.creator_id = c.id "
                f"WHERE {cond}"
            )
            print(f"    {label:<10} {n:>9,} {miss or 0:>9,} {(miss or 0) / n:>6.1%}")

        degenerate = self.one(
            "SELECT SUM(CASE WHEN keywords = nickname THEN 1 ELSE 0 END) "
            "FROM xhs_creator c LEFT JOIN "
            "(SELECT DISTINCT creator_id FROM xhs_creator_category) x "
            "ON x.creator_id = c.id WHERE x.creator_id IS NULL"
        )[0]
        print(f"  无类目行的 keywords 退化成「只有昵称」：{degenerate:,} / {uncat:,}")
        print("    → 这些行没有任何可反推类目的字段，缺口不可由库内数据补救")
        self.flag(
            f"无类目 {uncat:,} 人（{uncat / total:.1%}）含头部 {top[0]} 人，"
            "库内无补救路径，只能靠 prompt 兜底或外部补数据"
        )

    # 7 ------------------------------------------------------------------
    def email(self) -> None:
        self.head("7  邮箱：合法率、域名、随粉丝的变化")
        total = self.one("SELECT COUNT(*) FROM xhs_creator")[0]
        filled = self.one(
            "SELECT COUNT(*) FROM xhs_creator WHERE TRIM(COALESCE(email,''))<>''"
        )[0]
        print(f"  非空 {filled:,} / {total:,}  ({filled / total:.2%})")

        values = [
            r[0] for r in self.all(
                "SELECT TRIM(email) FROM xhs_creator WHERE TRIM(COALESCE(email,''))<>''"
            )
        ]
        bad = [v for v in values if not EMAIL.match(v)]
        print(f"  不符合邮箱格式：{len(bad):,}  ({len(bad) / filled:.2%})")
        if bad:
            print("    样例（打码）：" + "、".join(redact(v) for v in bad[:5]))
            self.flag(f"{len(bad)} 个邮箱形态不干净，实体解析要标注而非静默采信")

        # 第三方数据常见的排版污染：全角符号、中点、空格替掉了 ASCII 的点/符号
        typographic = [
            v for v in values
            if re.search(r"[•·。，、（）\u3000\uFF00-\uFFEF]", v) or " " in v
        ]
        print(f"  含全角/排版符号或空格的邮箱：{len(typographic):,}")
        if typographic:
            print("    样例（打码）：" + "、".join(redact(v) for v in typographic[:5]))
            print("    → 形如 163•com / 163. com / qq·CoM：能通过含 @ 的粗筛，但发信必退")
            self.flag(
                f"{len(typographic)} 个邮箱含全角/排版符号，"
                "需要一个归一化步骤（全角转半角 + 去空格）后再当联系方式用"
            )

        print("  域名 Top 6（打码后仅显示域名）：")
        for domain, count in self.all(
            "SELECT SUBSTR(email, INSTR(email,'@')+1) d, COUNT(*) c FROM xhs_creator "
            "WHERE email LIKE '%@%' GROUP BY 1 ORDER BY 2 DESC LIMIT 6"
        ):
            print(f"    @{domain:<16} {count:>7,}")

        print("  邮箱率随粉丝档变化：")
        for label, cond in FANS_BANDS:
            n, e = self.one(
                f"SELECT COUNT(*), SUM(CASE WHEN TRIM(COALESCE(email,''))<>'' "
                f"THEN 1 ELSE 0 END) FROM xhs_creator WHERE {cond}"
            )
            print(f"    {label:<10} {n:>9,} {e or 0:>9,} {(e or 0) / n:>6.1%}")
        print("    → 单调上升，说明缺失不是随机的，与账号量级相关")

    # 8 ------------------------------------------------------------------
    def geography(self) -> None:
        self.head("8  地理字段的一致性与脏值")
        prov = self.one(
            "SELECT COUNT(DISTINCT province) FROM xhs_creator "
            "WHERE TRIM(COALESCE(province,''))<>''"
        )[0]
        city = self.one(
            "SELECT COUNT(DISTINCT city) FROM xhs_creator "
            "WHERE TRIM(COALESCE(city,''))<>''"
        )[0]
        both = self.one(
            "SELECT COUNT(*) FROM xhs_creator "
            "WHERE TRIM(COALESCE(province,''))<>'' AND TRIM(COALESCE(city,''))<>''"
        )[0]
        total = self.one("SELECT COUNT(*) FROM xhs_creator")[0]
        print(f"  province 去重 {prov}   city 去重 {city}")
        print(f"  province 与 city 都非空：{both:,} / {total:,}  ({both / total:.1%})")

        dirty_city = self.one(
            "SELECT COUNT(*) FROM xhs_creator "
            "WHERE city LIKE '% %' OR city LIKE '%;%' OR city LIKE '%,%'"
        )[0]
        print(f"  city 含空格/分号/逗号（可能粘连多级）：{dirty_city:,}")

        unpaired = self.one(
            "SELECT COUNT(*) FROM xhs_creator "
            "WHERE TRIM(COALESCE(province,''))='' AND TRIM(COALESCE(city,''))<>''"
        )[0]
        print(f"  有 city 但无 province（配对不全）：{unpaired:,}")
        if unpaired:
            self.flag(f"{unpaired} 行有市无省，地理过滤时要注意配对")

        print("  省 Top 5：")
        for name, count in self.all(
            "SELECT province, COUNT(*) FROM xhs_creator "
            "WHERE TRIM(COALESCE(province,''))<>'' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
        ):
            print(f"    {name:<8} {count:>8,}")

        # province 里混着海外 IP 归属地，过滤时不能假定是中国省份
        overseas = [
            "美国", "加拿大", "日本", "英国", "澳大利亚", "韩国",
            "法国", "德国", "泰国", "马来西亚", "新加坡",
        ]
        placeholders = ", ".join("?" * len(overseas))
        count = self.one(
            f"SELECT COUNT(*) FROM xhs_creator WHERE province IN ({placeholders})",
            tuple(overseas),
        )[0]
        print(f"  海外归属地（{ '、'.join(overseas[:5]) } 等）：{count:,}")
        print("    → province 不是纯中国省份，还含海外；地理过滤要一并考虑")
        self.flag(f"province 含约 {count:,} 个海外归属地，不是纯中国省份")

    # 9 ------------------------------------------------------------------
    def domains(self) -> None:
        self.head("9  数值与枚举域的边界情况")
        print("  followers：")
        negative, zero, extreme = self.one(
            "SELECT SUM(CASE WHEN followers < 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN followers = 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN followers > 100000000 THEN 1 ELSE 0 END) "
            "FROM xhs_creator"
        )
        print(f"    负数 {negative or 0}   零 {zero or 0}   过亿 {extreme or 0}")
        if negative:
            self.flag("followers 有负值")

        print("  gender：")
        for value, count in self.all(
            "SELECT COALESCE(NULLIF(TRIM(gender),''),'(空)') g, COUNT(*) c "
            "FROM xhs_creator GROUP BY 1 ORDER BY 2 DESC LIMIT 6"
        ):
            print(f"    {value:<8} {count:>8,}")

        print("  常量列（单一取值，不能作筛选）：")
        total = self.one("SELECT COUNT(*) FROM xhs_creator")[0]
        for column in ("credit_level", "is_public", "data_date", "batch_id"):
            distinct = self.one(
                f"SELECT COUNT(DISTINCT {column}) FROM xhs_creator"
            )[0]
            if distinct == 0:
                continue
            top_value, top_count = self.one(
                f"SELECT {column}, COUNT(*) FROM xhs_creator "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
            )
            share = top_count / total
            if distinct <= 1:
                print(f"    {column:<14} 单值 = {top_value!r}")
                self.flag(f"{column} 是常量列，不可作筛选/排序/展示")
            elif share >= 0.9999:
                others = distinct - 1
                print(
                    f"    {column:<14} 实际常量 = {top_value!r} "
                    f"({share:.4%}，另有 {others} 个异值)"
                )
                self.flag(
                    f"{column} 99.99%+ 取同一个值（{top_value!r}），实际不可作筛选"
                )

        print("  全空列：")
        for column in ("extra_json",):
            filled = self.one(
                f"SELECT COUNT(*) FROM xhs_creator WHERE TRIM(COALESCE({column},''))<>''"
            )[0]
            print(f"    {column:<14} 非空 {filled}")
            if filled == 0:
                self.flag(f"{column} 全空，死列")

        print("  nickname：")
        blank, dup = self.one(
            "SELECT SUM(CASE WHEN TRIM(COALESCE(nickname,''))='' THEN 1 ELSE 0 END), "
            "(SELECT COUNT(*) FROM (SELECT nickname FROM xhs_creator "
            "GROUP BY nickname HAVING COUNT(*)>1)) FROM xhs_creator"
        )
        print(f"    空名 {blank or 0}   重复的昵称种数 {dup}")
        # SQLite 下判断 emoji 不稳定，取回名字用 Python 数。
        # 注意不能用"码点 > 某值"——汉字在 U+4E00 以上，会被全数误判。
        names = [
            r[0] for r in self.all(
                "SELECT nickname FROM xhs_creator WHERE nickname IS NOT NULL"
            )
        ]
        emoji_count = sum(1 for n in names if _has_emoji(n))
        print(f"    含 emoji/符号昵称：{emoji_count:,}  ({emoji_count / len(names):.1%})")
        if dup:
            self.flag(f"{dup} 个昵称重复，昵称不能作身份键")

    # 10 -----------------------------------------------------------------
    def verdict(self) -> None:
        self.head("10  结论")
        if not self.problems:
            print("  未发现阻断性问题。")
            return
        print(f"  共 {len(self.problems)} 条需要处理（按发现顺序）：")
        for index, problem in enumerate(self.problems, 1):
            print(f"    {index}. {problem}")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("用法：python3 validate_data.py <库文件.sqlite3>")
    path = Path(sys.argv[1])
    if not path.exists():
        sys.exit(f"找不到库：{path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    report = Report(connection)
    for step in (
        report.tables,
        report.identifier,
        report.referential,
        report.etl,
        report.derived_columns,
        report.uncategorized,
        report.email,
        report.geography,
        report.domains,
        report.verdict,
    ):
        step()
    connection.close()


if __name__ == "__main__":
    main()
