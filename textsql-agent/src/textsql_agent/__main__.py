"""本地演示页，不是规格里的产品 UI。

本文件在 src/ 里，所以一个业务词都不能有（ADR 0002）：库路径、DDL、文档
一律从命令行参数进来，内容住在文件里。

两种模式：
  不给 --db   规则规划者 + 未接线查数工具。页面直说「未接线」，不编假数。
  给了 --db   装配真组件（真模型客户端 / 真只读执行器 / 真库），页面上把
              每一跳摆出来：当前问题 → 改写问题 → SQL → 命中基数 → 回填。
"""

from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from textsql_agent.brain import MainAgentBrain
from textsql_agent.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    DeepSeekPlanner,
    DeepSeekQueryLlm,
    DeepSeekSmallModel,
)
from textsql_agent.planner import Planner
from textsql_agent.query_tool import (
    QueryPipeline,
    ReadonlySqlQueryPipeline,
    UnwiredQueryPipeline,
)
from textsql_agent.rag import InMemoryVanna
from textsql_agent.rewrite import Rewriter
from textsql_agent.session import Session
from textsql_agent.slots import ProgramSlots
from textsql_agent.sqlite_executor import SqliteReadonlyExecutor
from textsql_agent.turn import TurnResult, run_turn

HOST = "127.0.0.1"
PORT = 8765

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Text-SQL 问答</title>
  <style>
    :root { --bg:#0c1016; --ink:#e8eef5; --muted:#8f9eae; --card:#141a22; }
    * { box-sizing: border-box; }
    body {
      margin: 0; color: var(--ink); background: var(--bg);
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      min-height: 100vh; display: flex; flex-direction: column;
    }
    header { padding: 20px 20px 8px; max-width: 720px; margin: 0 auto; width: 100%; }
    h1 { margin: 0 0 6px; font-size: 1.25rem; }
    .lead { color: var(--muted); font-size: .9rem; line-height: 1.5; margin: 0; }
    #log {
      flex: 1; overflow: auto; padding: 12px 20px 16px;
      max-width: 720px; margin: 0 auto; width: 100%;
    }
    .msg { margin: 0 0 12px; padding: 10px 12px; border-radius: 12px; line-height: 1.5; }
    .user { background: #1d3a55; }
    .agent { background: var(--card); border: 1px solid rgba(255,255,255,.08); }
    .meta { color: var(--muted); font-size: .75rem; margin-top: 6px; }
    .hops {
      margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,.08);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .76rem;
    }
    .hops .row { display: flex; gap: 8px; align-items: baseline; margin: 2px 0; }
    .hops .k { color: var(--muted); flex: 0 0 5.5em; }
    .hops .v { white-space: pre-wrap; word-break: break-word; }
    form {
      display: flex; gap: 8px; padding: 12px 20px 20px;
      max-width: 720px; margin: 0 auto; width: 100%;
    }
    input {
      flex: 1; border-radius: 10px; border: 1px solid rgba(255,255,255,.16);
      background: var(--card); color: var(--ink); padding: 10px 12px; font-size: 1rem;
    }
    button {
      border: 0; border-radius: 10px; background: #3d8a6a; color: white;
      padding: 0 16px; font-size: 1rem; cursor: pointer;
    }
  </style>
</head>
<body>
  <header>
    <h1>Text-SQL 问答</h1>
    <p class="lead">{{LEAD}}</p>
  </header>
  <div id="log"></div>
  <form id="ask">
    <input id="utterance" name="utterance" autocomplete="off" placeholder="输入一句用户话" />
    <button type="submit">发送</button>
  </form>
  <script>
    const log = document.getElementById("log");
    const form = document.getElementById("ask");
    const input = document.getElementById("utterance");
    // 一律走 textContent：回复、SQL、回填都是模型和库里的字，不能当 HTML 插进去。
    function add(role, text, meta) {
      const div = document.createElement("div");
      div.className = "msg " + role;
      div.textContent = text;
      if (meta) {
        const m = document.createElement("div");
        m.className = "meta";
        m.textContent = meta;
        div.appendChild(m);
      }
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
      return div;
    }
    function addHops(parent, hops) {
      if (!hops || !hops.length) return;
      const box = document.createElement("div");
      box.className = "hops";
      for (const pair of hops) {
        if (!pair[1]) continue;
        const row = document.createElement("div");
        row.className = "row";
        const key = document.createElement("span");
        key.className = "k";
        key.textContent = pair[0];
        const value = document.createElement("span");
        value.className = "v";
        value.textContent = pair[1];
        row.appendChild(key);
        row.appendChild(value);
        box.appendChild(row);
      }
      if (box.childElementCount) parent.appendChild(box);
      log.scrollTop = log.scrollHeight;
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const utterance = input.value.trim();
      if (!utterance) return;
      input.value = "";
      add("user", utterance);
      const response = await fetch("/turn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ utterance }),
      });
      // 服务端把失败也回成 JSON（{"error": …}）。这里必须认，不能让
      // 「点了发送、页面上什么都没发生」发生——那分不清是坏了还是没查到。
      let data;
      try {
        data = await response.json();
      } catch (error) {
        add("agent", "没跑成：服务端没有回可解析的内容", "HTTP " + response.status);
        return;
      }
      if (data.error) {
        add("agent", "没跑成：" + data.error, "HTTP " + response.status);
        return;
      }
      const bits = [data.query_tool_call_count ? "环 B 查数" : "环 A 对话"];
      if (!data.hops || !data.hops.length) {
        if (data.rewritten_question) bits.push("改写：" + data.rewritten_question);
        if (data.last_sql) bits.push("上次 SQL：" + data.last_sql);
      }
      addHops(add("agent", data.reply, bits.join(" · ")), data.hops);
    });
  </script>
</body>
</html>
"""


def hops_of(result: TurnResult) -> list[list[str]]:
    """把这一轮的可观察量按跳摆好。

    全是 B 环自己产出的量（QueryToolOutcome → TurnResult），不另开观察通路。
    空值不在这里滤掉，交给页面按需跳过——判据留在测试夹具里，这一层只负责摆事实。
    """
    hops: list[list[str]] = []
    arguments = result.query_tool_arguments or {}
    if "当前问题" in arguments:
        hops.append(["当前问题", str(arguments["当前问题"])])
    hops.append(["改写问题", result.rewritten_question or ""])
    hops.append(["需求说明", result.query_summary or ""])
    hops.append(["SQL", result.sql_text or ""])
    if result.rag_hits is not None:
        hits = result.rag_hits
        hops.append(
            [
                "材料",
                f"ddl {len(hits.ddl)} 字 · 文档 {len(hits.documentation)} 字"
                f" · 问题-SQL {len(hits.question_sql)} 字",
            ]
        )
    if result.query_tool_call_count:
        # 本轮真查了库才报这两个量。环 A 没查库时不出现——否则「—」既表示"没查"
        # 又表示"查了但失败用尽"，同一个符号两种意思，等于没报。
        hops.append(["命中基数", "—" if result.matched_rows is None else str(result.matched_rows)])
        hops.append(["只读调用", str(result.readonly_call_count)])
    if result.query_tool_fillback:
        hops.append(["回填", result.query_tool_fillback])
    hops.append(["环别", "环 B 查数" if result.query_tool_call_count else "环 A 对话"])
    if result.reply_is_fallback:
        hops.append(["提示", "这一轮的回复是兜底话术，主 Agent 没开口"])
    return hops


def require_file(path: Path) -> Path:
    """起服务之前就把路径验掉。

    不验的话，服务会照常起来、页面照常 200、还宣称"真组件已接线"，
    然后每一次查询都炸在打不开库上——一个静默的坏服务。
    """
    if not path.is_file():
        print(f"找不到文件：{path}")
        raise SystemExit(2)
    return path


def build_wired(
    db: Path, ddl: Path | None, docs: Path | None, row_limit: int
) -> tuple[MainAgentBrain, QueryPipeline]:
    """装配真管线。四个适配器只依赖窄 Protocol，所以这里换的就是它们。"""
    client = DeepSeekClient.from_env()
    vanna = InMemoryVanna()
    if ddl is not None:
        vanna.train(ddl=ddl.read_text(encoding="utf-8"))
    if docs is not None:
        vanna.train(documentation=docs.read_text(encoding="utf-8"))
    return (
        DeepSeekPlanner(client),
        ReadonlySqlQueryPipeline(
            rewriter=Rewriter(DeepSeekSmallModel(client)),
            query_llm=DeepSeekQueryLlm(client),
            executor=SqliteReadonlyExecutor(db, row_limit=row_limit),
            vanna=vanna,
        ),
    )


class ChatApp:
    def __init__(
        self, brain: MainAgentBrain, pipeline: QueryPipeline, *, wired: bool
    ) -> None:
        self._brain = brain
        self._pipeline = pipeline
        self._wired = wired
        self._session: Session | None = None
        self._slots = ProgramSlots()

    def handle_turn(self, utterance: str) -> dict[str, object]:
        result = run_turn(
            utterance,
            brain=self._brain,
            session=self._session,
            slots=self._slots,
            query_pipeline=self._pipeline,
        )
        self._session = result.session
        self._slots = ProgramSlots(
            last_sql=result.last_sql,
            query_summary=result.query_summary,
        )
        return {
            "reply": result.reply,
            "query_tool_call_count": result.query_tool_call_count,
            "rewritten_question": result.rewritten_question,
            "last_sql": result.last_sql,
            "hops": hops_of(result) if self._wired else [],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Text-SQL 本地演示页")
    parser.add_argument("--db", type=Path, help="库文件。给了就装配真管线，不给就用未接线版")
    parser.add_argument("--ddl", type=Path, help="DDL 文本，喂检索的第一路材料")
    parser.add_argument("--docs", type=Path, help="文档文本，喂检索的第二路材料")
    parser.add_argument("--row-limit", type=int, default=200, help="执行器取行上限")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if args.db is not None:
        db = require_file(args.db)
        ddl = require_file(args.ddl) if args.ddl is not None else None
        docs = require_file(args.docs) if args.docs is not None else None
        try:
            brain, pipeline = build_wired(db, ddl, docs, args.row_limit)
        except DeepSeekError as error:
            print(f"没接上模型：{error}")
            print("先导出 OPENAI_API_KEY（或 DEEPSEEK_API_KEY）再起。")
            raise SystemExit(2) from error
        app = ChatApp(brain, pipeline, wired=True)
        lead = (
            f"真组件已接线：真模型客户端 + 真只读执行器 + {db}。"
            "每轮把当前问题 / 改写问题 / SQL / 命中基数 / 回填都摆出来。"
        )
    else:
        app = ChatApp(Planner(), UnwiredQueryPipeline(), wired=False)
        lead = (
            "当前是规则规划者 + 未接线查数工具，不是真实大模型和数据库。"
            "加 --db 换成真管线。"
        )
    # lead 里带了命令行原样传进来的路径，插进 HTML 前先转义（`--db '<img …>'` 那种自伤）。
    page = PAGE.replace("{{LEAD}}", html.escape(lead))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlparse(self.path).path != "/":
                self.send_error(404)
                return
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, code: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/turn":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                self.send_json(400, {"error": f"请求体不是合法 JSON：{error}"})
                return
            if not isinstance(payload, dict):
                self.send_json(400, {"error": "请求体必须是一个 JSON 对象"})
                return
            utterance = str(payload.get("utterance", "")).strip()
            if not utterance:
                self.send_json(400, {"error": "缺少 utterance"})
                return
            try:
                self.send_json(200, app.handle_turn(utterance))
            except Exception as error:
                # 这一层兜住一切：炸了也必须回一条能读的消息，不能静默断连——
                # 用户看到「点了发送、什么都没发生」是最坏的失败形态，
                # 分不清是系统坏了、没查到，还是没人回答（同 turn.py 的 NO_REPLY_FALLBACK）。
                # 另：send_error 的 reason phrase 不能带中文，http.server 默认
                # HTTP/1.0 用 latin-1 编响应行，中文那里会 UnicodeEncodeError。
                self.send_json(500, {"error": f"{type(error).__name__}: {error}"})

        def log_message(self, format: str, *args: object) -> None:
            print("[%s] %s" % (self.log_date_time_string(), format % args))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Text-SQL 问答已启动：http://{args.host}:{args.port}", flush=True)
    print(lead, flush=True)
    print("Ctrl+C 停止。", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
