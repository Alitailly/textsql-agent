"""把真管线装起来，供 check_question.py 和 run_eval.py 共用。

不是产品代码，是测流程用的夹具。真组件一个不换：
真 DeepSeek（规划者 / 小模型 / 查库大模型）、真只读执行器、真库。

只用标准库，只有 yaml 是例外（要读 config/blogger.yml，所以走 `uv run --with pyyaml`）。
工具里的打码规则跟 START-HERE 一致，分两层：
自由文本行（回复 / SQL / 当前问题 / 需求说明）只打邮箱 → a***@domain，数字原样；
逐行数据的行（回填行）邮箱 + 长数字一起打 → 前 3 后 2。理由见 mask_email / mask。
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "textsql-agent" / "config"
SRC = ROOT / "textsql-agent" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from textsql_agent.deepseek import (  # noqa: E402
    DeepSeekClient,
    DeepSeekPlanner,
    DeepSeekQueryLlm,
    DeepSeekSmallModel,
)
from textsql_agent.query_tool import ReadonlySqlQueryPipeline  # noqa: E402
from textsql_agent.rag import InMemoryVanna  # noqa: E402
from textsql_agent.rewrite import Rewriter  # noqa: E402
from textsql_agent.sqlite_executor import SqliteReadonlyExecutor  # noqa: E402
from textsql_agent.turn import TurnResult, run_turn  # noqa: E402

TRAILING_LIMIT = re.compile(
    r"\s+limit\s+\d+(?:\s*,\s*\d+|\s+offset\s+\d+)?\s*$", re.IGNORECASE
)
# 邮箱：从「邮箱里允许出现的字符」正面定义，不从「分隔符」反面枚举。
# 反面枚举是输不起的：每补一个分隔符，就还有下一个漏的（实测补到第 21 个仍在漏，
# `alice@x.com/bob@y.com` → `a***@x.com/bob@y.com`）。正面定义天然终止——
# 凡是邮箱里不可能出现的字符（空白、逗号、引号、括号、中文标点……）都会断开这一段，
# 不需要一个个列出来。
_EMAIL_CHARS = r"[A-Za-z0-9._%+\-]"
# at 号有全角变体，一并算。全角 ＠（U+FF20）不是"畸形输入"——中文输入法正常打字就会
# 出全角，而「问法」那一行是用户原话、要进 stdout：用户问「帮我查 alice＠x.com 这个号」
# 就能触发。﹫（U+FE6B，SMALL COMMERCIAL AT）是同家族，一起收。真库 0 例，但输入空间不止库。
_AT = "[@\uff20\ufe6b]"
# 一个「邮箱形状的连续段」：本地部分 + 至少一个 at号+域名。允许段内出现多个 at 号，
# 因为 `alice@x.com-bob@y.com` 这种「两个邮箱只隔一个 `-`/`.`」的情况里，
# `-` 和 `.` 本身就是合法邮箱字符，按字符类切不开——那就整段吞下来一起打。
EMAIL = re.compile(rf"{_EMAIL_CHARS}+(?:{_AT}{_EMAIL_CHARS}+)+")
# 段内所有 at 号前面的本地部分只留首字母。正常单个邮箱走这一支，输出还是 `a***@x.com`；
# 多个 at 号挤在一起的退化情形每个本地部分都打，一个都不漏。
# 只替换本地部分、不动 at 号本身，所以全角还是全角——只挡 PII，不改写原文。
_LOCAL_PART = re.compile(rf"{_EMAIL_CHARS}+(?={_AT})")
LONG_NUMBER = re.compile(r"\b\d{6,}\b")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# 单引号字面量（`''` 是转义的单引号）和两种注释，**合成一个交替**，且字面量那支排第一。
# 双引号**不算**——SQLite 里 `"foo"` 是标识符（找不到该列时才回退成字符串），`"名字"` 要照抓。
#
# 为什么必须是「一个交替」而不是「先剥注释、再剥字面量」：`re.sub` 从左往右扫，在每个位置
# 取第一个能匹配的分支。分开跑两趟时，`WHERE x = '--' AND member = 1` 里字面量内那个 `--`
# 会先被注释那趟吃掉**整行**（`--[^\n]*`），连带把后面真编的 `member` 一起吞掉 →
# `名字核对` 报「无」，真缺陷变成假 PASS（实测）。合成交替、字面量支在前，字面量会被整段
# 先消费掉，注释就没机会从它内部开始。顺序反了就是同一个洞（这是本次返工的原因）。
_STRUCT = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)
# SQL 自带的名字，不算"模型编的"。只用来把标识符和材料名区分开。
SQL_WORDS = frozenset(
    """
    select from where as and or not null is in limit offset order by group having
    count sum avg min max distinct desc asc like between case when then else end
    join left right inner outer on with union all any exists cast int integer text
    real numeric round abs coalesce ifnull trim lower upper replace substr length
    random total strftime date glob escape collate using natural cross
    """.split()
)


def require_yaml() -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover
        raise SystemExit(
            "需要 pyyaml。用这条命令跑：\n"
            "  uv run --with pyyaml --python 3.12 python <脚本> ..."
        )
    return yaml


def mask_email(text: str) -> str:
    """只打码邮箱，数字原样。给自由文本行用。

    回复 / SQL / 当前问题 / 需求说明 里都可能出现用户或模型写进去的邮箱，必须打；
    但里面的长数字多是阈值和聚合数——START-HERE 的规则明确写了「聚合统计不打码」，
    而且把 `followers >= 100000` 打成 `100**00` 会让这句 SQL 没法读，工具就白做了。
    """

    def redact(match: re.Match[str]) -> str:
        return _LOCAL_PART.sub(lambda local: f"{local.group(0)[:1]}***", match.group(0))

    return EMAIL.sub(redact, text)


def mask(text: str) -> str:
    """邮箱 + 长数字一起打，只给逐行数据的行用（回填行）。

    那是真行记录：邮箱是 PII，长数字多是 xhs_id 或粉丝数。注意 xhs_id 是混合字母
    数字（`00000100000` 全数字、`0000000000ly` 带字母），这层只挡得住全数字那批；
    带字母的挡不住，那是执行器列投影的事，不是打码能解决的。
    """

    masked = mask_email(text)
    return LONG_NUMBER.sub(lambda m: f"{m.group(0)[:3]}**{m.group(0)[-2:]}", masked)


@dataclass(frozen=True)
class Config:
    path: Path
    raw: dict[str, Any]
    ddl: str
    documentation: str

    @property
    def questions(self) -> list[dict[str, Any]]:
        return list(self.raw.get("问法") or [])

    @property
    def anchor(self) -> dict[str, Any]:
        return dict(self.raw.get("锚点") or {})


def load_config() -> Config:
    yaml = require_yaml()
    path = CONFIG / "blogger.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config(
        path=path,
        raw=raw,
        ddl=(CONFIG / "blogger_ddl.sql").read_text(encoding="utf-8"),
        documentation=(CONFIG / "blogger_docs.md").read_text(encoding="utf-8"),
    )


@dataclass
class Stack:
    """一套装好的真管线，可以连着跑很多句。"""

    db_path: Path
    client: DeepSeekClient
    vanna: InMemoryVanna
    pipeline: ReadonlySqlQueryPipeline
    brain: DeepSeekPlanner
    ddl: str
    documentation: str
    calls_before: int = 0

    def run(self, utterance: str) -> TurnResult:
        self.calls_before = len(self.client.calls)
        return run_turn(utterance, brain=self.brain, query_pipeline=self.pipeline)

    @property
    def calls_this_turn(self) -> tuple[Any, ...]:
        return self.client.calls[self.calls_before :]


def build_stack(db_path: Path, config: Config, *, row_limit: int = 200) -> Stack:
    client = DeepSeekClient.from_env()
    vanna = InMemoryVanna()
    vanna.train(ddl=config.ddl)
    vanna.train(documentation=config.documentation)
    for sample in config.questions:
        question = (sample.get("当前问题") or sample.get("原话") or "").strip()
        sql = (sample.get("金标SQL") or "").strip()
        if question and sql:
            vanna.train(question=question, sql=sql)
    return Stack(
        db_path=db_path,
        client=client,
        vanna=vanna,
        pipeline=ReadonlySqlQueryPipeline(
            rewriter=Rewriter(DeepSeekSmallModel(client)),
            query_llm=DeepSeekQueryLlm(client),
            executor=SqliteReadonlyExecutor(db_path, row_limit=row_limit),
            vanna=vanna,
        ),
        brain=DeepSeekPlanner(client),
        ddl=config.ddl,
        documentation=config.documentation,
    )


def readonly_count(db_path: Path, sql: str) -> int:
    """另开一条只读连接重数一遍。和执行器的 _count 是两条独立通路。"""
    counted = TRAILING_LIMIT.sub("", sql.strip().rstrip(";").strip())
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM ({counted})").fetchone()
    finally:
        connection.close()
    return int(row[0])


def fetch_rows(db_path: Path, sql: str, limit: int) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        cursor = connection.execute(sql.strip().rstrip(";").strip())
        columns = tuple(item[0] for item in cursor.description or ())
        rows = [tuple("" if v is None else str(v) for v in row) for row in cursor.fetchmany(limit)]
    finally:
        connection.close()
    return columns, rows


def _structure_only(sql: str) -> str:
    """只留结构性 token：字符串字面量和注释都换成空格。

    一次交替扫完（见 `_STRUCT` 上方的注释）：字面量支排第一，保证字面量被整段消费，
    注释不会从字面量内部开始吃。这是启发式，不是完整 SQL 词法分析——真要严谨得上解析器，
    但判据只需要"别把字面量当名字、也别把后文吞掉"，够用。
    """
    return _STRUCT.sub(" ", sql)


def unknown_names(sql: str, *materials: str) -> list[str]:
    """SQL 里用到的、材料里找不到的**标识符**（表名/列名）。"模型编了个名字"的硬判据。

    必须先去掉字符串字面量和注释：字面量里是**筛选值**，不是名字。
    `WHERE email = 'alice@example.com'` 里 alice/example/com 会被 `_IDENT` 当成标识符
    抓出来，把一条正确的 SQL 判成 FAIL（实测，见 observability.md F6）。
    """
    haystack = " ".join(materials).lower()
    seen: list[str] = []
    for ident in _IDENT.findall(_structure_only(sql)):
        lowered = ident.lower()
        if lowered in SQL_WORDS or lowered in seen:
            continue
        if lowered not in haystack:
            seen.append(ident)
    return seen


def material_route(hits: Any) -> dict[str, int]:
    return {
        "ddl": len(getattr(hits, "ddl", "") or ""),
        "documentation": len(getattr(hits, "documentation", "") or ""),
        "question_sql": len(getattr(hits, "question_sql", "") or ""),
    }


def category_names(db_path: Path) -> list[str]:
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT category_name FROM xhs_creator_category"
        ).fetchall()
    finally:
        connection.close()
    return sorted(str(row[0]) for row in rows if row[0] is not None)


_TERM = re.compile(r"[\u4e00-\u9fff]+")
_PARTICLES = re.compile(r"[的是在与及了和，。？、,?!\s]+")
_NUMBER = re.compile(r"\d[\d,]*")


def questioned_terms(question: str) -> list[str]:
    """把问句切成 2 字以上的中文片段。"""
    terms: list[str] = []
    for chunk in _TERM.findall(question):
        for piece in _PARTICLES.split(chunk):
            if len(piece) >= 2 and piece not in terms:
                terms.append(piece)
    return terms


def windows(text: str, size: int = 2) -> list[str]:
    """所有 size 字窗口，去重保序：四字词要能拆出两字词，才可能对上枚举里的短名字。"""
    found: list[str] = []
    for chunk in _TERM.findall(text):
        for start in range(len(chunk) - size + 1):
            piece = chunk[start : start + size]
            if piece not in found:
                found.append(piece)
    return found


def category_probe(question: str, names: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """问句和 114 个类目名的机械对账。

    返回（完整命中的类目名，[(片段, 名字里含这个片段的名字们)])。
    第二个是给你挑同义词用的线索，不是说这些片段就是类目词。
    """
    exact = [name for name in names if name in question]
    near: list[tuple[str, list[str]]] = []
    for piece in list(questioned_terms(question)) + windows(question, 2):
        hits = [name for name in names if piece in name]
        if hits and piece not in [item[0] for item in near]:
            near.append((piece, hits))
    return exact, near


def numbers_in(text: str) -> list[str]:
    return _NUMBER.findall(text)


def and_conjuncts(sql: str) -> list[str]:
    """把 WHERE 里顶层用 AND 连起来的条件拆开。

    归因靠这个：模型少了一个条件时，用它生成条件子集的变体，看哪个体最像模型的数。
    括号里的 AND 不拆（那是同一条件内部）。
    """
    match = re.search(r"(?is)\bwhere\b(.*?)(\bgroup\b|\border\b|\blimit\b|\Z)", sql)
    if match is None:
        return []
    body = match.group(1)
    parts: list[str] = []
    depth = 0
    current = ""
    index = 0
    while index < len(body):
        char = body[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0 and body[index : index + 3].upper() == "AND":
            before = body[index - 1] if index else " "
            after = body[index + 3] if index + 3 < len(body) else " "
            if not before.isalnum() and not after.isalnum():
                parts.append(current.strip())
                current = ""
                index += 3
                continue
        current += char
        index += 1
    if current.strip():
        parts.append(current.strip())
    return [part for part in parts if part]


def variant_counts(db_path: Path, sql: str) -> list[tuple[str, int]]:
    """按条件子集生成 2^k 个变体各数一遍，按命中数降序。"""
    conditions = and_conjuncts(sql)
    if not conditions:
        return []
    table = _table_of(sql)
    if table is None:
        return []
    results: list[tuple[str, int]] = []
    for mask_index in range(1 << len(conditions)):
        chosen = [c for i, c in enumerate(conditions) if mask_index & (1 << i)]
        where = f" WHERE {' AND '.join(chosen)}" if chosen else ""
        counted = f"SELECT COUNT(*) FROM {table}{where}"
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        try:
            row = connection.execute(counted).fetchone()
        finally:
            connection.close()
        results.append((" ∧ ".join(chosen) or "（无条件）", int(row[0])))
    results.sort(key=lambda item: -item[1])
    return results


def _table_of(sql: str) -> str | None:
    """金标 SQL 里第一次出现的 FROM 表名。归因变体只用得到单表场景。"""
    match = re.search(r"(?is)\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)", sql)
    return match.group(1) if match else None


@dataclass
class Hop:
    """一条问法的每一跳，打印和判定都从这里取。"""

    utterance: str
    result: TurnResult
    gold: dict[str, Any]
    db_path: Path
    notes: list[str] = field(default_factory=list)
    checks: list[tuple[str, str, bool, str]] = field(default_factory=list)

    def add(self, label: str, verdict: str, ok: bool, detail: str = "") -> None:
        self.checks.append((label, verdict, ok, detail))

    def hard(self, label: str, ok: bool, detail: str = "") -> None:
        self.add(label, "FAIL", ok, detail)

    def soft(self, label: str, ok: bool = True, detail: str = "") -> None:
        """软判据不过只提醒，不算失败。"""
        self.add(label, "WARN", ok, detail)

    @property
    def failed(self) -> bool:
        return any(not ok and verdict == "FAIL" for _, verdict, ok, _ in self.checks)

    @property
    def warned(self) -> bool:
        return any(not ok and verdict == "WARN" for _, verdict, ok, _ in self.checks)


def judge(hop_input: dict[str, Any], db_path: Path, config: Config, result: TurnResult) -> Hop:
    hop = Hop(utterance=str(hop_input.get("原话") or ""), result=result, gold=hop_input, db_path=db_path)
    gold = hop.gold
    expected = str(gold.get("期望分支") or "").strip()

    hop.hard("回复非空", bool(result.reply.strip()),
             "空回复时用户什么也拿不到" if not result.reply.strip() else "")
    # 空回复已经在 src/turn.py 里兜住了（NO_REPLY_FALLBACK），所以「非空」这条现在
    # 必然过。真正要判的是主 Agent 有没有开口——兜底话术不是回答，别让它冒充成功。
    hop.hard("主 Agent 说了话", not result.reply_is_fallback,
             "reply 是兜底话术，主 Agent 本轮始终没开口" if result.reply_is_fallback else "")
    hop.hard("回复无 SQL", "select" not in result.reply.lower())

    if result.query_tool_call_count == 0:
        # 本轮回环 A：主 Agent 只说话。澄清是规格内的动作，不是缺口。
        hop.soft("本轮回环 A", True, "没调查数工具，只说话")
        found = numbers_in(result.reply)
        hop.soft(
            "回复里的数字",
            not found,
            f"{found} 出现在回复里。本轮没查库，这些数字不能是库里的精确数"
            if found
            else "没出现数字",
        )
        if expected:
            hop.hard("期望分支", False, f"金标期望 {expected}，实际只说话没查")
        return hop

    hop.hard("每一跳", bool(
        result.query_tool_arguments and result.rewritten_question and result.sql_text
    ), "当前问题 / 改写问题 / SQL")

    material = material_route(result.rag_hits) if result.rag_hits else {}
    if material:
        hop.soft(
            "材料三路",
            all(material.values()),
            " ".join(f"{key}={size}" for key, size in material.items())
            + ("" if all(material.values()) else "  ← 空的那一路就是没料，模型只能自己编名字"),
        )

    sql = result.sql_text or ""
    invented = unknown_names(sql, config.ddl, config.documentation)
    hop.hard("名字核对", not invented, f"材料里没有：{invented}" if invented else "表名列名都能在材料里找到")

    if result.matched_rows is not None and sql:
        standalone = readonly_count(db_path, sql)
        hop.hard(
            "基数交叉核对",
            standalone == result.matched_rows,
            f"独立重数 {standalone} vs 报告 {result.matched_rows}",
        )

    if expected:
        actual = result.readonly_returns[-1].kind if result.readonly_returns else "（没跑成）"
        hop.hard("期望分支", actual == expected, f"期望 {expected} 实得 {actual}")

    gold_count = gold.get("基数")
    if gold_count is not None and result.matched_rows is not None:
        same = int(gold_count) == result.matched_rows
        hop.hard("金标基数", same, f"金标 {gold_count} 实得 {result.matched_rows}")
        if not same:
            hop.notes.append(
                f"库侧按金标 SQL 重数是 {readonly_count(db_path, str(gold.get('金标SQL') or ''))}，"
                "若与金标基数不符即为金标过期"
            )
            hop.notes.extend(attribute(db_path, str(gold.get("金标SQL") or ""), result.matched_rows))

    for label, key in (("必含", "必含"), ("必不含", "必不含")):
        anchors = [str(item) for item in (gold.get(key) or [])]
        if not anchors:
            continue
        ok, detail = _anchor_check(db_path, sql, anchors, want=label == "必含")
        hop.hard(f"锚点{label}", ok, detail)

    for token in gold.get("必须提示") or []:
        present = str(token) in (result.query_tool_fillback or "")
        hop.hard(f"提示「{token}」", present)

    return hop


def _anchor_check(db_path: Path, sql: str, anchors: list[str], *, want: bool) -> tuple[bool, str]:
    """锚点核对：在模型 SQL 的整个结果集里找这几个 xhs_id，不受回填行数限制。"""
    if not sql:
        return False, "没有 SQL"
    try:
        columns, _ = fetch_rows(db_path, sql, 0)
    except sqlite3.Error as error:
        return False, f"SQL 跑不动：{error}"
    if "xhs_id" not in columns:
        return True, f"无法核对：SQL 没回 xhs_id（列是 {list(columns)}）"
    counted = TRAILING_LIMIT.sub("", sql.strip().rstrip(";").strip())
    placeholders = ", ".join("?" for _ in anchors)
    probe = f"SELECT xhs_id FROM ({counted}) AS t WHERE t.xhs_id IN ({placeholders})"
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        found = {str(row[0]) for row in connection.execute(probe, anchors).fetchall()}
    finally:
        connection.close()
    if want:
        return found == set(anchors), f"找到 {len(found)}/{len(anchors)}"
    return not found, f"越界命中 {sorted(found)}" if found else "一个都没越界"


def attribute(db_path: Path, gold_sql: str, actual: int | None) -> list[str]:
    """基数对不上时的归因：按条件子集生成变体，报最像的那个。"""
    if not gold_sql or actual is None:
        return []
    variants = variant_counts(db_path, gold_sql)
    if not variants:
        return []
    best = min(variants, key=lambda item: abs(item[1] - actual))
    conditions = and_conjuncts(gold_sql)
    kept = best[0].split(" ∧ ") if best[0] != "（无条件）" else []
    lost = [c for c in conditions if c not in kept]
    note = f"归因：最像的变体是「{best[0]}」（{best[1]}）"
    if lost:
        note += f"；模型少用的条件：{lost}"
    else:
        note += "；金标的条件模型都用上了，差异不在条件子集上（可能是写法或口径不同）"
    return [note]


def render(hop: Hop, *, show_rows: int = 6) -> str:
    result = hop.result
    lines: list[str] = []
    lines.append(f"问法      {mask_email(hop.utterance)}")
    # 这一行是给人读「主 Agent 把原话收成了什么」的，所以打**值**，不打整个 arguments 字典
    # （原来 json.dumps 整个字典，把最该读的那句话包在 `{"当前问题": "…"}` 里）。
    # 但**没这个键时不许静默**：那说明主 Agent 没按契约填，属于要看见的失败，
    # 这种情况整段打出来。多出来的键也照样附在后面，不丢信息。
    arguments = result.query_tool_arguments or {}
    if "当前问题" in arguments:
        collapsed = str(arguments["当前问题"]).strip()
        extra = {key: value for key, value in arguments.items() if key != "当前问题"}
        line = f"当前问题  {mask_email(collapsed)}"
        if extra:
            line += "   " + mask_email(json.dumps(extra, ensure_ascii=False))
        lines.append(line)
    else:
        lines.append(f"当前问题  {mask_email(json.dumps(arguments, ensure_ascii=False))}")
    lines.append(f"改写问题  {mask_email(result.rewritten_question or '')}")
    lines.append(f"需求说明  {mask_email(result.query_summary or '')}")
    lines.append(f"SQL       {mask_email(result.sql_text or '')}")
    lines.append(f"基数      {result.matched_rows}    只读调用 {result.readonly_call_count} 次")
    if result.rag_hits:
        material = material_route(result.rag_hits)
        lines.append("材料      " + "  ".join(f"{key} {size} 字" for key, size in material.items()))
    fillback = result.query_tool_fillback or ""
    if show_rows and "结果:" in fillback:
        head, _, body = fillback.partition("结果:")
        lines.append("回填头    " + mask_email(head.strip()).replace("\n", " | "))
        lines.append("回填行    " + mask(body.strip()[:show_rows * 120]).replace("\n", " | "))
    else:
        lines.append("回填      " + mask(fillback.replace("\n", " | ")[:400]))
    fallback_mark = "   ← 兜底话术，主 Agent 本轮没开口" if result.reply_is_fallback else ""
    lines.append(f"回复      {mask_email(result.reply or '')}{fallback_mark}")
    lines.append("判定")
    for label, verdict, ok, detail in hop.checks:
        mark = "✓" if ok else ("!" if verdict == "WARN" else "✗")
        lines.append(f"  [{mark}] {label:<12}{detail}")
    for note in hop.notes:
        lines.append(f"  ↳ {note}")
    verdict = "FAIL" if hop.failed else ("PARTIAL" if hop.warned else "PASS")
    lines.append(f"结论      {verdict}")
    # 出口再兜一遍：判定明细和归因注释里可能带 SQL 片段（比如条件里有个邮箱字面量），
    # 上面逐行打的码管不到它们。mask_email 是幂等的，重复打不会变形。
    return mask_email("\n".join(lines))


def describe_env() -> str:
    client = DeepSeekClient.from_env()
    return f"模型 {client.model}  端点 {client.base_url}  思考 {client.thinking}"


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
