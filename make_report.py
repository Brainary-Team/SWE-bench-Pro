#!/usr/bin/env python3
"""把 local_eval.py 产出的 eval_report.json 渲染成可视化 HTML 报告。

用法: python make_report.py eval_report.json -o report.html

布局：KPI 一行（结果 + 开销）→ 逐条 instance 表格（instance → PASS 情况）。
每行一个「N 步」按钮，展开该条的明细；执行过程收在一个默认折叠的
「执行过程」折叠框里，与「逐条测试」「patch」同级。

执行过程的步骤流只有五类标签，按「是谁产生的」划分：
  LLM输出  模型对外说的话（agent_message / assistant 文本）
  思考     模型的推理内容——有明文才记（gpt-5.6-sol 这类只回 encrypted_content
           的模型没有可展示的思考，整类自动消失）
  工具     模型发起的调用（bash/exec/apply_patch/collaboration__* 都算），
           卡片里是「参数 + 输出」；exec 的参数是整段 JS，默认折叠可展开
  子工具   exec() 的 JS 里编排的嵌套调用，折叠在所属 exec 工具卡下一层；
           连续的同参同果轮询（wait_agent/list_agents 热自旋）合并成一张卡记 ×N
  信息     模型以外的脚本信息：runner 标记（出网自检/CODEX_END rc/超时）、
           turn 边界与用量、agent 启停、错误（错误是信息的 critical 变体，标「错误」）

数据源两层，按可用性降级：
  1. logs/<iid>.trace/trace-*/（brainary-codex fork 的 rollout-trace bundle，
     run_brainary_codex --trace 落的）——唯一有 exec JS 源码、子工具↔exec 单元
     关联（requester.runtime_cell_id）、子 agent 线程（agent_path）的记录。
  2. logs/<iid>.log 里的 JSONL 事件流（Codex --json / Claude Code stream-json）
     ——没有 trace 的旧日志走这条，exec 单元在这层不可见，只有拍平的直连调用。
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

# 颜色取自 dataviz 参考调色板。状态标记恒为「图标 + 文字」，颜色不单独承载语义。
CSS = """
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; }
.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --plane: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --good: #0ca30c;
  --critical: #d03b3b;
  --warning: #b25000;
  --acc-blue: #3563cf;
  --acc-purple: #7e57d0;
  --acc-teal: #0c7f74;
  font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", sans-serif;
  background: var(--plane);
  color: var(--text-primary);
  min-height: 100vh;
  padding: 32px 24px 64px;
  -webkit-font-smoothing: antialiased;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --plane: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --grid: #2c2c2a; --baseline: #55544f; --border: rgba(255,255,255,0.10);
    --good: #21ba21; --critical: #e05d5d; --warning: #e08a3c;
    --acc-blue: #8aa8f2; --acc-purple: #b39df0; --acc-teal: #3ab5a8;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1: #1a1a19; --plane: #0d0d0d;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
  --grid: #2c2c2a; --baseline: #55544f; --border: rgba(255,255,255,0.10);
  --good: #21ba21; --critical: #e05d5d; --warning: #e08a3c;
  --acc-blue: #8aa8f2; --acc-purple: #b39df0; --acc-teal: #3ab5a8;
}
.wrap { max-width: 1180px; margin: 0 auto; }
header.top { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 22px; }
h1 { font-size: 19px; font-weight: 600; margin: 0 0 6px; letter-spacing: -0.01em; }
.sub { font-size: 13px; color: var(--text-secondary); margin: 0; }
.sub code { background: var(--grid); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
button.ghost {
  font: inherit; font-size: 12px; color: var(--text-secondary);
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 11px; cursor: pointer; min-height: 32px;
}
button.ghost:hover { background: var(--grid); }
button.xs { padding: 2px 9px; min-height: 24px; font-size: 11.5px;
            font-variant-numeric: tabular-nums; white-space: nowrap; }
button.xs.on { background: var(--grid); color: var(--text-primary); }

/* ---- KPI 行：结果 + 开销 ---- */
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); gap: 12px; margin-bottom: 22px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.tile .k { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.tile .v { font-size: 25px; font-weight: 600; line-height: 1.1; letter-spacing: -0.02em;
           font-variant-numeric: tabular-nums; }
.tile .n { font-size: 11.5px; color: var(--text-muted); margin-top: 5px; font-variant-numeric: tabular-nums; }

/* ---- 主表：一行一条 instance ---- */
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
        padding: 6px 14px; overflow-x: auto; }
table.main { width: 100%; border-collapse: collapse; font-size: 12.5px; font-variant-numeric: tabular-nums; }
table.main th, table.main td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
table.main tbody tr:last-child > td { border-bottom: none; }
table.main th { font-weight: 600; color: var(--text-secondary); font-size: 11.5px; white-space: nowrap; }
th.num, td.num { text-align: right; }
td.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
td .s { display: block; font-size: 10.5px; color: var(--text-muted); }
.st { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; font-weight: 600; font-size: 11.5px; }
.ok { color: var(--good); } .bad { color: var(--critical); } .mis { color: var(--warning); }
.chip { width: 15px; height: 15px; border-radius: 4px; display: inline-grid; place-items: center;
        font-size: 10px; font-weight: 700; color: #fff; flex: none; line-height: 1; }
.chip.pass { background: var(--good); }
.chip.fail { background: var(--critical); }
.chip.miss { background: var(--baseline); color: var(--text-primary); }

/* ---- 展开行：执行过程 ---- */
tr.exprow > td { background: var(--plane); padding: 14px 16px 18px; }
/* 关键：不让展开区的 nowrap 内容参与表格取宽 —— 否则长命令会把整张表撑开 */
.expwrap { contain: inline-size; }
.failbox { border-left: 3px solid var(--critical); background: var(--surface-1);
           padding: 10px 13px; border-radius: 0 6px 6px 0; margin-bottom: 12px; }
.failbox .t { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.failbox .m { font-size: 11.5px; color: var(--text-secondary); line-height: 1.55;
              font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-word; }
/* ---- 步骤卡片：标签 + 一行摘要，点开看全文 ---- */
.steps { display: flex; flex-direction: column; gap: 7px; }
.stp { border: 1px solid var(--border); border-radius: 8px; background: var(--surface-1);
       font-size: 12px; line-height: 1.5; }
.stp.bad { box-shadow: inset 3px 0 0 var(--critical); }
.stp.errc { border-color: color-mix(in srgb, var(--critical) 35%, var(--border));
            background: color-mix(in srgb, var(--critical) 6%, var(--surface-1)); }
.stp > summary, .stp > .hd { display: flex; align-items: center; gap: 9px; padding: 6px 11px; min-height: 33px; }
details.stp > summary { cursor: pointer; list-style: none; border-radius: 8px; }
details.stp > summary::-webkit-details-marker { display: none; }
details.stp > summary:hover { background: color-mix(in srgb, var(--grid) 45%, transparent); }
.chev { flex: none; width: 10px; font-size: 10px; color: var(--text-muted); }
summary .chev::before { content: "▸"; }
details[open] > summary .chev::before { content: "▾"; }
.tag { flex: none; font-size: 10px; font-weight: 700; letter-spacing: .02em;
       padding: 2px 8px; border-radius: 999px; line-height: 1.5;
       color: var(--tc, var(--text-secondary));
       background: color-mix(in srgb, var(--tc, var(--text-secondary)) 13%, transparent); }
.tag.tool  { --tc: var(--acc-blue); }
.tag.sub   { --tc: var(--acc-blue); opacity: .75; }
.tag.llm   { --tc: var(--acc-teal); }
.tag.think { --tc: var(--acc-purple); }
.tag.err   { --tc: var(--critical); }
.tag.agt   { --tc: var(--warning); font-weight: 600; }
.tname { flex: none; font-weight: 600; font-size: 11.5px;
         font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.sumtx { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
         white-space: nowrap; color: var(--text-primary); }
.sumtx.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.sumtx.mline { white-space: pre-wrap; overflow: visible; text-overflow: clip; word-break: break-word; }
.hr { flex: none; margin-left: auto; display: inline-flex; gap: 9px; align-items: baseline;
      font-size: 10.5px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.rc { font-weight: 700; font-family: ui-monospace, Menlo, monospace; }
.rep { font-weight: 700; color: var(--warning); }
.stp .bd { border-top: 1px solid var(--grid); padding: 9px 12px 11px; }
.stp .cap { font-size: 10.5px; color: var(--text-muted); margin: 9px 0 4px; }
.stp .cap:first-child { margin-top: 0; }
.stp pre { margin: 0; padding: 8px 10px; background: var(--plane);
           border: 1px solid var(--grid); border-radius: 6px; overflow-x: auto;
           font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
           font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--text-secondary); }
.stp .prose { white-space: pre-wrap; word-break: break-word; color: var(--text-primary); font-size: 12px; }
/* exec 的 JS 参数默认折叠：卡片体内再嵌一层 details */
details.argfold > summary { cursor: pointer; font-size: 10.5px; color: var(--text-muted);
                            margin: 9px 0 4px; list-style: none; }
details.argfold > summary::-webkit-details-marker { display: none; }
details.argfold > summary::before { content: "▸ "; }
details.argfold[open] > summary::before { content: "▾ "; }
details.argfold > summary:hover { color: var(--text-secondary); }
/* 子工具：折叠在工具卡体内的下一层，靠左细线标出层级 */
.substeps { display: flex; flex-direction: column; gap: 6px; margin-top: 4px;
            padding-left: 10px; border-left: 2px solid var(--grid); }
.turnrow { font-size: 11px; color: var(--text-muted); border-top: 1px dashed var(--grid);
           padding-top: 7px; margin-top: 3px; font-variant-numeric: tabular-nums; }
.inforow { display: flex; align-items: baseline; gap: 8px; font-size: 11px;
           color: var(--text-muted); padding: 1px 2px; font-variant-numeric: tabular-nums; }
.inforow .tag { font-size: 9.5px; padding: 1px 7px; }
.nolog { font-size: 12px; color: var(--text-muted); margin: 0; }
.exp-sec { margin-top: 12px; }
.exp-sec > summary { cursor: pointer; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
.exp-sec > .steps { margin-top: 8px; }
table.tlist { width: 100%; border-collapse: collapse; font-size: 11.5px; margin-top: 8px;
              font-variant-numeric: tabular-nums; }
table.tlist td { padding: 4px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
table.tlist td.mono { white-space: normal; word-break: break-all; }
pre.diff { margin: 8px 0 0; padding: 10px 12px; background: var(--surface-1); border: 1px solid var(--border);
           border-radius: 6px; overflow-x: auto; font-size: 11.5px; line-height: 1.5;
           font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre.diff .add { color: var(--good); } pre.diff .del { color: var(--critical); }
pre.diff .hunk { color: var(--text-muted); }
"""

JS = """
document.addEventListener('click', function (ev) {
  var b = ev.target.closest('[data-exp]');
  if (!b) return;
  var tr = document.getElementById(b.getAttribute('data-exp'));
  if (!tr) return;
  tr.hidden = !tr.hidden;
  b.classList.toggle('on', !tr.hidden);
});
document.getElementById('toggle-theme').addEventListener('click', function () {
  var el = document.documentElement;
  var dark = el.getAttribute('data-theme') === 'dark'
    || (!el.hasAttribute('data-theme') && matchMedia('(prefers-color-scheme: dark)').matches);
  el.setAttribute('data-theme', dark ? 'light' : 'dark');
});
"""

esc = html.escape


def chip(status: str) -> str:
    if status == "PASSED":
        return '<span class="chip pass" aria-hidden="true">✓</span>'
    if status == "MISSING":
        return '<span class="chip miss" aria-hidden="true">?</span>'
    return '<span class="chip fail" aria-hidden="true">✕</span>'


def fmt_secs(s: float) -> str:
    s = round(s)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def elide(text: str, limit: int) -> str:
    """中段截断：头尾都保留 —— 命令失败时关键信息（traceback）通常在尾部。"""
    text = text.rstrip()
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return (text[:head] + f"\n··· 省略 {len(text) - limit:,} 字符 ···\n"
            + text[-(limit - head):])


# ─────────────────────────── 日志 → 统一步骤流 ───────────────────────────
#
# 步骤模型（见文件头）——所有数据源都归一到这五类：
#   {"kind":"llm",   "text", "agent"?}
#   {"kind":"think", "text", "agent"?}
#   {"kind":"tool",  "name", "arg", "input"?, "out", "rc"?, "err"?, "secs"?,
#                    "repeat"?, "exec"?, "subtools"?: [同 tool 形状], "agent"?}
#   {"kind":"info",  "text", "flavor": "turn"|"err"|"script", "usage"?, "agent"?}
# agent 是子 agent 的路径名（root 不打标）；exec=True 的工具卡参数默认折叠。

# Claude Code 工具 → 取哪个入参当摘要（够认出这一步在干什么就行）
_TOOL_ARG = ("file_path", "path", "pattern", "query", "url", "command",
             "notebook_path", "prompt", "description")


def _claude_tool_arg(inp: dict) -> str:
    for k in _TOOL_ARG:
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v
    for v in inp.values():
        if isinstance(v, str) and v:
            return v
    return ""


def _tool(name: str, arg: str = "", input_: str = "", **kw) -> dict:
    return {"kind": "tool", "name": name, "arg": arg, "input": input_,
            "out": "", "rc": None, "err": False, **kw}


def _info(text: str, flavor: str = "script", **kw) -> dict:
    return {"kind": "info", "text": text, "flavor": flavor, **kw}


def parse_steps(text: str) -> list[dict]:
    """.log 全文（脚本标记 + JSONL 事件流）→ 统一步骤列表。

    JSON 行同一个循环里分发 Codex JSONL / Claude Code stream-json 两套词表，
    认事件不认 agent；非 JSON 行只认 runner 标记与 codex 的 stderr ERROR 行
    （→ 信息步骤），git diff 段整段跳过。此路径没有 trace，exec() 单元不可见
    ——JS 里编排的调用被拍平成普通工具步骤，与直连调用无法区分。
    """
    steps: list[dict] = []
    pending: dict[str, dict] = {}   # Claude: tool_use id → 待回填输出的步骤
    in_diff = False

    def err(msg: str) -> None:
        # error 事件和 turn.failed 常带同一条消息，连续重复只记一次
        if msg and not (steps and steps[-1]["kind"] == "info"
                        and steps[-1]["flavor"] == "err" and steps[-1]["text"] == msg):
            steps.append(_info(msg, "err"))

    n_turn = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            # ---- 脚本信息（runner 标记 / stderr）----
            if line.startswith("===DIFF_START==="):
                in_diff = True
            elif line.startswith("===DIFF_END==="):
                in_diff = False
            elif in_diff:
                pass
            elif line.startswith("===EGRESS_OK==="):
                steps.append(_info("出网自检通过（出网已钉死到模型端点）"))
            elif line.startswith("===EGRESS_FAIL"):
                err("出网自检失败——该条结果不可采信")
            elif line.startswith("===CODEX_START==="):
                steps.append(_info("agent 进程启动"))
            elif line.startswith("===CODEX_END rc="):
                rc = line.removeprefix("===CODEX_END rc=").rstrip("=")
                if rc == "0":
                    steps.append(_info("agent 进程退出 rc=0"))
                else:
                    err(f"agent 进程退出 rc={rc}")
            elif line == "[TIMEOUT]":
                err("agent 超时被杀（--timeout）")
            elif _STDERR_ERR_RE.match(line):
                err(line)
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = e.get("type")

        # ---- Codex JSONL 事件流 ----
        if t == "item.completed":
            it = e.get("item") or {}
            k = it.get("type")
            if k == "command_execution":
                cmd = it.get("command", "")
                steps.append(_tool("shell", strip_shell(cmd).splitlines()[0].strip()
                                   if cmd else "", cmd,
                                   out=it.get("aggregated_output", ""),
                                   rc=it.get("exit_code")))
            elif k == "agent_message":
                steps.append({"kind": "llm", "text": it.get("text", "")})
            elif k == "reasoning":
                steps.append({"kind": "think", "text": it.get("text", "")})
            elif k == "file_change":
                items = [f'{c.get("kind", "")} {c.get("path", "")}'
                         for c in it.get("changes", [])]
                steps.append(_tool("apply_patch", " · ".join(items),
                                   "\n".join(items)))
            elif k == "web_search":
                steps.append(_tool("web_search", it.get("query", "")))
            elif k == "todo_list":
                items = [(bool(i.get("completed")), i.get("text", ""))
                         for i in it.get("items", [])]
                done = sum(1 for d, _ in items if d)
                steps.append(_tool(
                    "update_plan", f'{done}/{len(items)} 完成：'
                    + "；".join(t_ for _, t_ in items),
                    "\n".join(f'{"☑" if d else "☐"} {t_}' for d, t_ in items)))
            elif k == "collab_tool_call":
                steps.append(_tool(f'collaboration.{it.get("tool", "?")}',
                                   it.get("prompt") or "",
                                   err=it.get("status") == "failed"))
            elif k == "mcp_tool_call":
                steps.append(_tool(f'{it.get("server", "?")}.{it.get("tool", "?")}',
                                   json.dumps(it.get("arguments"), ensure_ascii=False)[:200],
                                   err=it.get("status") == "failed"))
            elif k == "error":
                err(it.get("message", ""))
        elif t == "turn.completed":
            n_turn += 1
            steps.append(_info(f'turn {n_turn} 完成 · ' + _fmt_usage(e.get("usage") or {}),
                               "turn", usage=e.get("usage") or {}))
        elif t == "turn.failed":
            err((e.get("error") or {}).get("message", ""))
        elif t == "error":
            err(e.get("message", ""))

        # ---- Claude Code stream-json ----
        elif t == "assistant":
            for b in (e.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    steps.append({"kind": "llm", "text": b.get("text", "")})
                elif bt == "thinking":
                    steps.append({"kind": "think", "text": b.get("thinking", "")})
                elif bt == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") == "Bash":
                        s = _tool("Bash", (inp.get("command") or "").splitlines()[0].strip()
                                  if inp.get("command") else "", inp.get("command", ""))
                    else:
                        s = _tool(b.get("name", "?"), _claude_tool_arg(inp),
                                  json.dumps(inp, ensure_ascii=False, indent=1)
                                  if inp else "")
                    steps.append(s)
                    pending[b.get("id", "")] = s
        elif t == "user":
            for b in (e.get("message") or {}).get("content") or []:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue                     # 纯文本 user 事件是 prompt 回放，不渲染
                s = pending.pop(b.get("tool_use_id", ""), None)
                if s is None:
                    continue
                out = b.get("content")
                if isinstance(out, list):
                    out = "\n".join(x.get("text", "") for x in out
                                    if isinstance(x, dict) and x.get("type") == "text")
                elif not isinstance(out, str):
                    out = json.dumps(out, ensure_ascii=False)
                s["out"] = out or ""
                # stream-json 不回传 exit code，只有 is_error 标志
                if s["name"] == "Bash" and s["rc"] is None:
                    s["rc"] = 1 if b.get("is_error") else 0
                else:
                    s["err"] = bool(b.get("is_error"))
        elif t == "result":
            u = e.get("usage") or {}
            # 口径对齐 Codex：input = 三个互斥字段之和（同 run_claude_agent.parse_usage）
            usage = {"input_tokens": u.get("input_tokens", 0)
                         + u.get("cache_creation_input_tokens", 0)
                         + u.get("cache_read_input_tokens", 0),
                     "cached_input_tokens": u.get("cache_read_input_tokens", 0),
                     "output_tokens": u.get("output_tokens", 0),
                     "reasoning_output_tokens": 0}
            cost = e.get("total_cost_usd") or 0.0
            steps.append(_info(
                f'完成（{e.get("subtype", "done")}）· {e.get("num_turns", 0)} turns · '
                + _fmt_usage(usage) + (f' · ${cost:.4f}' if cost else ""),
                "turn", usage=usage))
    return steps


# codex 二进制打到 stderr 的错误行（docker 日志尾部），如
# 2026-08-18T15:15:01Z ERROR codex_core::tools::router: error=...
_STDERR_ERR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z?\s+ERROR\s")
# unified-exec 给模型的输出包装里的退出码行
_WRAP_RC_RE = re.compile(r"Process exited with code (\d+)")


# ──────────────── trace bundle（rollout-trace）→ 统一步骤流 ────────────────
#
# bundle 结构：trace.jsonl（事件封皮 {seq, wall_time_unix_ms, thread_id,
# codex_turn_id, payload:{type,...}}）+ payloads/N.json（大对象外挂文件）。
# 事件按 seq 单调，一个 bundle 装整棵 agent 树（子 agent 线程带 agent_path）。

def find_trace_bundle(logs_dir: Path | None, iid: str) -> Path | None:
    d = logs_dir / f"{iid}.trace" if logs_dir else None
    if not (d and d.is_dir()):
        return None
    cands = sorted(d.glob("trace-*/trace.jsonl"))
    return cands[0].parent if cands else None


def _payload(bundle: Path, ref: dict | None) -> dict | list | str | None:
    """payload 引用 → 反序列化对象。--trace on 会裁掉 inference request payload，
    文件不存在按 None 处理（引用悬空是预期，不是坏 bundle）。"""
    rel = (ref or {}).get("path", "")
    if not rel or ".." in rel:
        return None
    f = bundle / rel
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(errors="replace"))
    except json.JSONDecodeError:
        return None


def _result_text(payload) -> tuple[str, int | None, bool]:
    """tool result / cell response payload → (输出文本, exit_code, 是否错误)。

    已知三种形态（实测 gpt-5.6-sol @ 8f16802f3）：
      {"type":"code_mode_response","value":{exit_code,output,...}}   JS 里的嵌套调用
      {"type":"direct_response","response_item":{"output":...}}      直连调用
      {"response":{"Result":{content_items,error_text}}}             exec 单元返回值
    形态不认识就整个 dump 成 JSON——宁可难看不可丢信息。
    """
    if payload is None:
        return "", None, False
    if isinstance(payload, str):
        return payload, None, False
    if isinstance(payload, dict):
        if payload.get("type") == "code_mode_response":
            v = payload.get("value")
            if isinstance(v, dict):
                rc = v.get("exit_code")
                if "output" in v:        # shell 类：{exit_code, output, ...}
                    return str(v.get("output", "")), rc, bool(rc)
                # collab 类的 value 没有 output（{"task_name":...} / {"agents":[...]}
                # / {"message":"Wait completed.",...}），整个对象就是结果
                return json.dumps(v, ensure_ascii=False), rc, bool(rc)
            return str(v), None, False
        if payload.get("type") == "direct_response":
            it = payload.get("response_item") or {}
            out = it.get("output", "")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            return out, None, False
        if "response" in payload and isinstance(payload["response"], dict):
            for status, body in payload["response"].items():   # Result / Yielded / Error
                if not isinstance(body, dict):
                    return str(body), None, status == "Error"
                txt = "\n".join(c.get("text", "") for c in body.get("content_items") or []
                                if isinstance(c, dict))
                errtx = body.get("error_text")
                if errtx:
                    txt = (txt + "\n" if txt else "") + str(errtx)
                return txt, None, bool(errtx) or status == "Error"
    return json.dumps(payload, ensure_ascii=False), None, False


def _args_pretty(raw: str) -> tuple[str, str]:
    """工具 arguments（JSON 字符串）→ (一行摘要, 全文)。shell 类取 cmd 当摘要，
    apply_patch 的参数是 patch 原文（非 JSON），摘要取动了哪些文件。"""
    if (raw or "").lstrip().startswith("*** Begin Patch"):
        files = [ln.split(":", 1)[1].strip() for ln in raw.splitlines()
                 if ln.startswith("*** ") and ":" in ln]
        return " · ".join(files) or "*** Begin Patch", raw
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return (raw or "").splitlines()[0][:200] if raw else "", raw or ""
    if isinstance(d, dict):
        for k in ("cmd", "command", "task_name", "target", "path", "query"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v.splitlines()[0].strip(), json.dumps(d, ensure_ascii=False, indent=1)
        return json.dumps(d, ensure_ascii=False)[:200], json.dumps(d, ensure_ascii=False, indent=1)
    return str(d)[:200], raw


def _push_subtool(cell: dict, s: dict, lookback: int = 3) -> None:
    """子工具入列，合并热自旋：wait_agent/list_agents 这类轮询一秒能打十几次，
    同名同参同果的卡在最近 lookback 张里找到就 ×N 归并（wait/list 交替出现，
    只看前一张并不够）。合并的都是逐字节相同的无进展轮询，时间序无损失。"""
    subs = cell.setdefault("subtools", [])
    key = (s["name"], s["input"], s["out"], s["rc"])
    for prev in reversed(subs[-lookback:]):
        if (prev["name"], prev["input"], prev["out"], prev["rc"]) == key:
            prev["repeat"] = prev.get("repeat", 1) + 1
            return
    subs.append(s)


def parse_trace(bundle: Path) -> list[dict]:
    """rollout-trace bundle → 统一步骤列表（带 exec/子工具/子 agent 全量结构）。

    工具步骤两阶段装配：tool_call_started 建卡（含参数），tool_call_runtime_ended
    回填 shell 的 aggregated_output/exit_code，tool_call_ended 回填其余结果与
    时长——卡片进列表的位置是 started 的时刻，与实际时序一致。requester 是
    code_cell 的挂到所属 exec 卡的 subtools（合并热自旋），其余为顶层步骤。
    exec 单元只有 code_cell_* 事件（fork 不给它记 generic tool call），不会重卡。
    """
    steps: list[dict] = []
    threads: dict[str, str] = {}     # thread_id → agent 短名（root → ""）
    cells: dict[tuple, dict] = {}    # (thread_id, runtime_cell_id) → exec 工具步骤
    calls: dict[str, dict] = {}      # tool_call_id → 工具步骤
    t0: dict[str, float] = {}        # tool_call_id → started 的 wall ms
    turn_usage: dict[str, dict] = {}     # codex_turn_id → 累计 usage
    turn_no: dict[str, int] = {}     # thread_id → 已完成 turn 数

    def agent_of(e: dict) -> str:
        return threads.get(e.get("thread_id") or "", "")

    def add(step: dict, agent: str) -> None:
        if agent:
            step["agent"] = agent
        steps.append(step)

    for line in (bundle / "trace.jsonl").read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = e.get("payload") or {}
        t = p.get("type")

        if t == "thread_started":
            path = p.get("agent_path", "")
            name = "" if path in ("/root", "") else path.removeprefix("/root/")
            threads[p.get("thread_id", "")] = name
            if name:
                add(_info(f"子 agent 启动：{path}"), name)

        elif t == "code_cell_started":
            src = p.get("source_js", "")
            first = next((ln.strip() for ln in src.splitlines() if ln.strip()), "")
            cell = _tool("exec", first, src, exec=True, subtools=[])
            cells[(e.get("thread_id") or "", p.get("runtime_cell_id", ""))] = cell
            add(cell, agent_of(e))

        elif t in ("code_cell_initial_response", "code_cell_ended"):
            key = (e.get("thread_id") or "", p.get("runtime_cell_id", ""))
            cell = cells.get(key)
            if cell is not None:
                out, _, bad = _result_text(_payload(bundle, p.get("response_payload")))
                if out:
                    cell["out"] = out
                cell["err"] = cell["err"] or bad or p.get("status") == "failed"

        elif t == "tool_call_started":
            summ = p.get("summary") or {}
            name = summ.get("label") or (p.get("kind") or {}).get("type", "?")
            inv = _payload(bundle, p.get("invocation_payload"))
            raw_args = ""
            if isinstance(inv, dict):
                raw_args = ((inv.get("payload") or {}).get("arguments")
                            or summ.get("input_preview") or "")
            arg, full = _args_pretty(raw_args or summ.get("input_preview") or "")
            s = _tool(name, arg, full)
            cid = p.get("tool_call_id", "")
            calls[cid] = s
            t0[cid] = e.get("wall_time_unix_ms") or 0
            req = p.get("requester") or {}
            if req.get("type") == "code_cell":
                # 子工具此刻先不入列：热自旋合并要按「同参同果」判，结果要等
                # tool_call_ended 才有——从 ended 那头再挂进所属 exec 卡
                s["_cell"] = (e.get("thread_id") or "", req.get("runtime_cell_id", ""))
            else:
                add(s, agent_of(e))

        elif t == "tool_call_runtime_ended":
            s = calls.get(p.get("tool_call_id", ""))
            rp = _payload(bundle, p.get("runtime_payload"))
            if s is not None and isinstance(rp, dict):
                # ExecCommandEnd 形态：aggregated_output / exit_code / duration
                if not s["out"] and isinstance(rp.get("aggregated_output"), str):
                    s["out"] = rp["aggregated_output"]
                if s["rc"] is None and rp.get("exit_code") is not None:
                    s["rc"] = rp.get("exit_code")

        elif t == "tool_call_ended":
            cid = p.get("tool_call_id", "")
            s = calls.pop(cid, None)
            if s is None:
                continue
            out, rc, bad = _result_text(_payload(bundle, p.get("result_payload")))
            if not s["out"] and out:
                s["out"] = out
            if s["rc"] is None:
                s["rc"] = rc
            if s["rc"] is None:
                # unified-exec 的 yielded 会话结构化字段里没有 exit code，
                # 只在给模型看的包装文本里（"Process exited with code N"）
                m = _WRAP_RC_RE.search(s["out"])
                if m:
                    s["rc"] = int(m.group(1))
            s["err"] = s["err"] or bad or p.get("status") == "failed"
            ms0 = t0.pop(cid, 0)
            if ms0 and e.get("wall_time_unix_ms"):
                s["secs"] = (e["wall_time_unix_ms"] - ms0) / 1000
            key = s.pop("_cell", None)
            if key is not None:
                cell = cells.get(key)
                if cell is not None:
                    _push_subtool(cell, s)
                else:                     # cell 没记到（不该发生），别丢数据
                    add(s, agent_of(e))

        elif t == "inference_completed":
            rp = _payload(bundle, p.get("response_payload"))
            if not isinstance(rp, dict):
                continue
            tu = rp.get("token_usage") or {}
            acc = turn_usage.setdefault(e.get("codex_turn_id") or "", {})
            for k in ("input_tokens", "cached_input_tokens",
                      "output_tokens", "reasoning_output_tokens"):
                acc[k] = acc.get(k, 0) + (tu.get(k) or 0)
            agent = agent_of(e)
            for it in rp.get("output_items") or []:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "reasoning":
                    # 有明文 summary 才是可展示的思考；encrypted_content 只能跳过
                    txt = "\n".join(x for x in it.get("summary") or [] if x).strip()
                    if txt:
                        add({"kind": "think", "text": txt}, agent)
                elif it.get("type") == "message":
                    txt = "\n".join(c.get("text", "") for c in it.get("content") or []
                                    if isinstance(c, dict)).strip()
                    if txt:
                        add({"kind": "llm", "text": txt}, agent)
                # function_call / custom_tool_call 略过：tool_call_started /
                # code_cell_started 才带执行结果，凭它们建卡不重不漏

        elif t in ("inference_failed", "inference_cancelled"):
            add(_info(f'模型请求{"失败" if t == "inference_failed" else "被取消"}：'
                      f'{p.get("error") or p.get("reason") or ""}', "err"), agent_of(e))

        elif t == "codex_turn_ended":
            thr = e.get("thread_id") or ""
            turn_no[thr] = turn_no.get(thr, 0) + 1
            u = turn_usage.pop(e.get("codex_turn_id") or "", {})
            status = p.get("status", "completed")
            tail = "" if status == "completed" else f"（{status}）"
            add(_info(f'turn {turn_no[thr]} 完成{tail} · ' + _fmt_usage(u),
                      "turn", usage=u), agent_of(e))

        # rollout_* / thread_ended / protocol_event_observed / agent_result_observed
        # / tool_call_runtime_started：对报告没有增量信息，略过

    # 中断的 run 会留下 started 而无 ended 的调用：子工具补挂回所属 exec 卡
    # （顶层的在 started 时已入列），别让它们凭空消失。
    for s in calls.values():
        key = s.pop("_cell", None)
        if key is not None and cells.get(key) is not None:
            _push_subtool(cells[key], s)
    return steps


def _fmt_usage(u: dict) -> str:
    return (f'输入 {u.get("input_tokens", 0):,}（缓存 {u.get("cached_input_tokens", 0):,}）· '
            f'输出 {u.get("output_tokens", 0):,}（推理 {u.get("reasoning_output_tokens", 0):,}）')


# `/bin/bash -lc '...'` 这层包装是 runner 加的，摘要行里剥掉，只看真正的命令。
# 展开后的「参数」区仍是原文，剥壳只影响摘要。
_SHELL_RE = re.compile(r"^\s*(?:/usr)?(?:/bin/)?(?:ba|z|da)?sh\s+-l?c\s+(.*)$", re.S)


def strip_shell(cmd: str) -> str:
    m = _SHELL_RE.match(cmd or "")
    if not m:
        return cmd or ""
    inner = m.group(1).strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        q, inner = inner[0], inner[1:-1]
        inner = (inner.replace("'\\''", "'") if q == "'"
                 else inner.replace('\\"', '"').replace("\\$", "$").replace("\\\\", "\\"))
    elif inner[:1] in ("'", '"'):
        inner = inner[1:]        # 混合引号包不住时至少去掉开头那个，摘要别带壳
    return inner


def _agent_tag(s: dict) -> str:
    a = s.get("agent")
    return f'<span class="tag agt">@{esc(a)}</span>' if a else ""


def _fmt_step_secs(v: float) -> str:
    return f"{v:.1f}s" if v < 60 else fmt_secs(v)


def tool_card(s: dict, max_out: int, sub: bool = False) -> str:
    """工具/子工具卡：标签 + 工具名 + 参数摘要；体内「参数 → 输出 → 子工具」。

    exec 卡的参数是整段 JS，体内默认折叠（argfold）；其余工具参数直接摊开。
    子工具列表嵌在所属 exec 卡体内的下一层（substeps），子工具卡自身同构、
    不再有第三层。
    """
    name, arg = s.get("name") or "?", s.get("arg") or ""
    inp, out = (s.get("input") or "").rstrip(), (s.get("out") or "").rstrip()
    rc, bad = s.get("rc"), bool(s.get("err")) or s.get("rc") not in (None, 0)
    subs, rep = s.get("subtools") or [], s.get("repeat", 1)
    n_calls = sum(x.get("repeat", 1) for x in subs)

    right = ""
    if rep > 1:
        right += f'<span class="rep">×{rep}</span>'
    if rc is not None:
        right += f'<span class="rc {"bad" if rc else "ok"}">rc={rc}</span>'
    elif s.get("err"):
        right += '<span class="rc bad">error</span>'
    if s.get("secs") and s["secs"] >= 0.95:
        right += f'<span>{_fmt_step_secs(s["secs"])}</span>'
    if out:
        right += f'<span>{len(out):,} 字符</span>'
    if subs:
        right += f'<span>{n_calls} 子调用</span>'

    body = ""
    if s.get("exec"):
        body += (f'<details class="argfold"><summary>参数（JS）· {len(inp):,} 字符'
                 f'</summary><pre>{esc(elide(inp, 8000))}</pre></details>')
    elif inp and (inp != arg or "\n" in inp or len(inp) > 100):
        body += f'<div class="cap">参数</div><pre>{esc(elide(inp, 1200))}</pre>'
    if out:
        body += f'<div class="cap">输出</div><pre>{esc(elide(out, max_out))}</pre>'
    if subs:
        body += (f'<div class="cap">子工具 · {len(subs)} 种 / {n_calls} 次调用</div>'
                 '<div class="substeps">'
                 + "".join(tool_card(x, max_out, sub=True) for x in subs) + '</div>')

    head = ((f'<span class="tag sub">子工具</span>' if sub else '<span class="tag tool">工具</span>')
            + _agent_tag(s)
            + f'<span class="tname">{esc(name)}</span>'
            + f'<span class="sumtx mono">{esc(elide(arg, 200)) or "—"}</span>'
            + (f'<span class="hr">{right}</span>' if right else ""))
    cls = "stp bad" if bad else "stp"
    if body:
        return (f'<details class="{cls}"{" open" if bad and not sub else ""}>'
                f'<summary><span class="chev"></span>{head}</summary>'
                f'<div class="bd">{body}</div></details>')
    # 不可折叠的卡也放一个空 chev 占位，让标签跟可折叠卡对齐
    return f'<div class="{cls}"><div class="hd"><span class="chev"></span>{head}</div></div>'


def text_card(s: dict, max_out: int) -> str:
    """LLM输出 / 思考卡：单行摘要，长文折叠。"""
    think = s["kind"] == "think"
    txt = (s.get("text") or "").strip()
    lines = txt.splitlines()
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    long = len(lines) > 1 or len(first) > 110
    head = (('<span class="tag think">思考</span>' if think
             else '<span class="tag llm">LLM输出</span>')
            + _agent_tag(s)
            + f'<span class="sumtx">{esc(first) or "—"}</span>'
            + (f'<span class="hr">{len(txt):,} 字符</span>' if long else ""))
    if long:
        body = f'<div class="prose">{esc(elide(txt, max_out * 3 if think else 4000))}</div>'
        return (f'<details class="stp"><summary><span class="chev"></span>{head}</summary>'
                f'<div class="bd">{body}</div></details>')
    return f'<div class="stp"><div class="hd"><span class="chev"></span>{head}</div></div>'


def steps_html(steps: list[dict], max_out: int) -> str:
    parts = []
    for s in steps:
        k = s["kind"]
        if k == "tool":
            parts.append(tool_card(s, max_out))
        elif k in ("llm", "think"):
            if (s.get("text") or "").strip():   # 空 agent_message 不值一张卡
                parts.append(text_card(s, max_out))
        elif k == "info":
            fl = s.get("flavor")
            if fl == "err":
                parts.append('<div class="stp errc"><div class="hd"><span class="chev"></span>'
                             '<span class="tag err">错误</span>' + _agent_tag(s)
                             + f'<span class="sumtx mono mline">{esc(elide(s["text"], max_out))}'
                             '</span></div></div>')
            elif fl == "turn":
                parts.append(f'<div class="turnrow">{_agent_tag(s)}{esc(s["text"])}</div>')
            else:
                parts.append('<div class="inforow"><span class="tag">信息</span>'
                             + _agent_tag(s) + f'<span>{esc(s["text"])}</span></div>')
    return f'<div class="steps">{"".join(parts)}</div>'


def diff_html(patch: str) -> str:
    out = []
    for ln in patch.splitlines():
        cls = ""
        if ln.startswith("+") and not ln.startswith("+++"):
            cls = "add"
        elif ln.startswith("-") and not ln.startswith("---"):
            cls = "del"
        elif ln.startswith(("@@", "diff ", "index ", "+++", "---")):
            cls = "hunk"
        out.append(f'<span class="{cls}">{esc(ln)}</span>' if cls else esc(ln))
    return "\n".join(out)


# ─────────────────────────────── 数据装配 ───────────────────────────────

def load_meta(rep: dict, report_path: str, explicit: str = "") -> dict:
    """推理侧的 token/耗时由 agent runner 写在 preds.json 旁边的 run_meta.json。

    评测报告里没有这些数字（local_eval.py 只管判卷），所以按 predictions_path 去找。
    找不到就返回 {} —— mini-swe-agent 的 run 没有这个文件，报告要能正常降级。
    """
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if rep.get("predictions_path"):
        p = Path(rep["predictions_path"]).parent / "run_meta.json"
        cands += [p, Path(report_path).parent / p]
    for c in cands:
        if c.is_file():
            try:
                return json.loads(c.read_text())
            except json.JSONDecodeError:
                pass
    return {}


def find_logs_dir(rep: dict, report_path: str, explicit: str = "") -> Path | None:
    """agent 执行日志与 preds.json 同级，在 logs/ 下，一条 instance 一个 .log。"""
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if rep.get("predictions_path"):
        p = Path(rep["predictions_path"]).parent / "logs"
        cands += [p, Path(report_path).parent / p]
    return next((c for c in cands if c.is_dir()), None)


def group_cell(tests: list[dict], group: str) -> str:
    ts = [t for t in tests if t["group"] == group]
    if not ts:
        return '<td class="num">—</td>'
    ok = sum(1 for t in ts if t["status"] == "PASSED")
    miss = sum(1 for t in ts if t["status"] == "MISSING")
    cls = "ok" if ok == len(ts) else ("mis" if miss else "bad")
    return f'<td class="num"><span class="{cls}">{ok}/{len(ts)}</span></td>'


def render(rep: dict, meta: dict | None = None,
           logs_dir: Path | None = None, max_out: int = 600) -> str:
    meta = meta or {}
    minst, mtot = meta.get("instances", {}), meta.get("totals", {})
    total, resolved = rep["total"], rep["resolved"]
    pct = (resolved / total * 100) if total else 0.0

    f2p_all = [t for r in rep["instances"] for t in r["tests"] if t["group"] == "FAIL_TO_PASS"]
    p2p_all = [t for r in rep["instances"] for t in r["tests"] if t["group"] == "PASS_TO_PASS"]
    f2p_ok = sum(1 for t in f2p_all if t["status"] == "PASSED")
    p2p_ok = sum(1 for t in p2p_all if t["status"] == "PASSED")
    applied = sum(1 for r in rep["instances"] if r.get("patch_applied"))

    # ── KPI：结果两块 + 开销三块（无 run_meta.json 时开销块自动消失）──
    tiles = [
        f'<div class="tile"><div class="k">Resolved</div><div class="v">{pct:.0f}%</div>'
        f'<div class="n">{resolved}/{total} 条 · patch 应用 {applied}/{total}</div></div>',
        f'<div class="tile"><div class="k">测试通过</div>'
        f'<div class="v">{rep["tests_passed"]}/{rep["tests_total"]}</div>'
        f'<div class="n">F2P {f2p_ok}/{len(f2p_all)} · P2P {p2p_ok}/{len(p2p_all)}</div></div>',
    ]
    if mtot:
        n = len(minst) or 1
        cin, ccached = mtot.get("input_tokens", 0), mtot.get("cached_input_tokens", 0)
        hit = (ccached / cin * 100) if cin else 0.0
        cost = sum(m.get("total_cost_usd", 0) or 0 for m in minst.values())
        tiles += [
            f'<div class="tile"><div class="k">推理耗时</div>'
            f'<div class="v">{fmt_secs(mtot.get("seconds", 0))}</div>'
            f'<div class="n">平均 {fmt_secs(mtot.get("seconds", 0) / n)}/条 · '
            f'{mtot.get("turns", 0)} turns</div></div>',
            f'<div class="tile"><div class="k">输入 token</div><div class="v">{cin:,}</div>'
            f'<div class="n">缓存 {ccached:,} · {hit:.0f}%</div></div>',
            f'<div class="tile"><div class="k">输出 token</div>'
            f'<div class="v">{mtot.get("output_tokens", 0):,}</div>'
            f'<div class="n">推理 {mtot.get("reasoning_output_tokens", 0):,}'
            + (f' · ${cost:.2f}' if cost else "") + '</div></div>',
        ]

    # ── 主表：instance → PASS 情况，行尾「N 步」展开执行过程 ──
    has_meta = bool(minst)
    heads = ['Instance', '结果', '<th class="num">F2P</th>', '<th class="num">P2P</th>',
             '<th class="num">patch</th>']
    if has_meta:
        heads += ['<th class="num">推理</th>', '<th class="num">输入 tok</th>',
                  '<th class="num">输出 tok</th>']
    heads += ['<th class="num">评测</th>', '过程']
    thead = "".join(h if h.startswith("<th") else f"<th>{h}</th>" for h in heads)
    ncols = 7 + (3 if has_meta else 0)

    body = []
    for i, r in enumerate(rep["instances"]):
        ok = r["resolved"]
        st = ("PASSED", "RESOLVED") if ok else ("FAILED", "UNRESOLVED")
        row = [f'<td class="mono">{esc(r["instance_id"])}</td>',
               f'<td><span class="st {"ok" if ok else "bad"}">{chip(st[0])}{st[1]}</span></td>',
               group_cell(r["tests"], "FAIL_TO_PASS"),
               group_cell(r["tests"], "PASS_TO_PASS"),
               f'<td class="num">{r["patch_chars"]:,}'
               + ('' if r.get("patch_applied") else '<span class="s bad">应用失败</span>')
               + '</td>']
        m = minst.get(r["instance_id"], {})
        if has_meta:
            row += ([f'<td class="num">{fmt_secs(m.get("seconds", 0))}'
                     f'<span class="s">{m.get("turns", 0)} turns</span></td>',
                     f'<td class="num">{m.get("input_tokens", 0):,}'
                     f'<span class="s">缓存 {m.get("cached_input_tokens", 0):,}</span></td>',
                     f'<td class="num">{m.get("output_tokens", 0):,}'
                     f'<span class="s">推理 {m.get("reasoning_output_tokens", 0):,}</span></td>']
                    if m else ['<td class="num">—</td>'] * 3)
        row.append(f'<td class="num">{r.get("seconds", 0)}s</td>')

        # 执行过程：优先 trace bundle（有 exec/子工具/子 agent 结构），
        # 退回 .log 的事件流解析；没有日志时按钮退化成「明细」。
        # trace 模式下 .log 的脚本标记仍要：事件流部分跳过（trace 是它的超集），
        # 只留标记与 stderr → 挂在 trace 步骤流前后（标记本来就括着 codex 进程）。
        steps = []
        log = logs_dir / f'{r["instance_id"]}.log' if logs_dir else None
        log_text = log.read_text(errors="replace") if log and log.is_file() else ""
        bundle = find_trace_bundle(logs_dir, r["instance_id"])
        if bundle:
            marks = [s for s in parse_steps(log_text) if s["kind"] == "info"
                     and s.get("flavor") != "turn"]
            head = [s for s in marks if s["text"].startswith(("出网", "agent 进程启动"))]
            steps = head + parse_trace(bundle) + [s for s in marks if s not in head]
        elif log_text:
            steps = parse_steps(log_text)
        n_act = sum(1 for s in steps if s["kind"] == "tool")
        n_sub = sum(x.get("repeat", 1) for s in steps if s["kind"] == "tool"
                    for x in s.get("subtools") or [])
        row.append(f'<td><button class="ghost xs" data-exp="exp-{i}">'
                   + (f'{n_act} 步' if n_act else '明细') + '</button></td>')

        # 展开区：失败测试 → 执行过程（默认折叠）→ 逐条测试 → 最终 patch
        exp = []
        fails = [t for t in r["tests"] if t["status"] != "PASSED"]
        for t in fails:
            exp.append(f'<div class="failbox"><div class="t">{chip(t["status"])} '
                       f'{esc(t["short"])}（{t["group"]} · {t["status"]}）</div>'
                       + (f'<div class="m">{esc(t["detail"])}</div>' if t["detail"] else "")
                       + '</div>')
        if steps:
            # 与「逐条测试」「patch」同级的折叠框，默认收起
            label = f'执行过程 · {n_act} 步' + (f'（{n_sub} 次子工具调用）' if n_sub else "")
            exp.append(f'<details class="exp-sec"><summary>{label}</summary>'
                       + steps_html(steps, max_out) + '</details>')
        else:
            exp.append('<p class="nolog">无执行日志</p>')
        tlist = "".join(
            f'<tr><td><span class="st {"ok" if t["status"] == "PASSED" else "bad"}">'
            f'{chip(t["status"])}{t["status"]}</span></td>'
            f'<td>{"F2P" if t["group"] == "FAIL_TO_PASS" else "P2P"}</td>'
            f'<td class="mono">{esc(t["short"])}</td></tr>' for t in r["tests"])
        exp.append(f'<details class="exp-sec"><summary>逐条测试 · {len(r["tests"])} 条</summary>'
                   f'<table class="tlist">{tlist}</table></details>')
        if r.get("model_patch"):
            exp.append(f'<details class="exp-sec"><summary>patch · {r["patch_chars"]:,} 字符'
                       f'</summary><pre class="diff">{diff_html(r["model_patch"])}</pre></details>')

        body.append(f'<tr>{"".join(row)}</tr>'
                    f'<tr class="exprow" id="exp-{i}" hidden><td colspan="{ncols}">'
                    f'<div class="expwrap">{"".join(exp)}</div></td></tr>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SWE-bench 评测报告</title><style>{CSS}</style></head>
<body><div class="viz-root"><div class="wrap">

<header class="top"><div>
  <h1>SWE-bench 评测报告</h1>
  <p class="sub"><code>{rep['subset']} / {rep['split']}</code> ·
     <code>{esc(rep['model'] or 'n/a')}</code></p>
</div>
<button class="ghost" id="toggle-theme">切换深/浅色</button></header>

<div class="kpis">{''.join(tiles)}</div>

<div class="card">
<table class="main">
<thead><tr>{thead}</tr></thead>
<tbody>{''.join(body)}</tbody>
</table>
</div>

</div></div>
<script>{JS}</script>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?", default="eval_report.json")
    ap.add_argument("-o", "--output", default="report.html")
    ap.add_argument("--meta", default="", help="run_meta.json 路径（默认按 predictions_path 自动找）")
    ap.add_argument("--logs", default="", help="agent 日志目录（默认 preds.json 同级的 logs/）")
    ap.add_argument("-n", "--max-output", type=int, default=600,
                    help="执行过程里每段输出的截断字符数")
    a = ap.parse_args()
    rep = json.load(open(a.report))
    meta = load_meta(rep, a.report, a.meta)
    logs_dir = find_logs_dir(rep, a.report, a.logs)
    Path(a.output).write_text(render(rep, meta, logs_dir, a.max_output), encoding="utf-8")
    n_logs = sum(1 for r in rep["instances"]
                 if logs_dir and (logs_dir / f'{r["instance_id"]}.log').is_file())
    print(f"已生成 {a.output}（{rep['total']} 条 · 执行日志 {n_logs} 条"
          + ("" if meta.get("totals") else " · 未找到 run_meta.json，无推理开销") + "）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
