#!/usr/bin/env python3
"""把 local_eval.py 产出的 eval_report.json 渲染成可视化 HTML 报告。

用法: python make_report.py eval_report.json -o report.html

布局：KPI 一行（结果 + 开销）→ 逐条 instance 表格（instance → PASS 情况）。
每行一个「N 步」按钮，展开该条的执行过程 —— 从 logs/<iid>.log 解析，
同时认 Codex 的 JSONL 事件流和 Claude Code 的 stream-json，两种都归一成
统一的步骤流（命令/消息/思考/改文件/搜索/turn 边界），与 agent 架构无关。
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
.tag.cmd   { --tc: var(--acc-blue); }
.tag.mut   { --tc: var(--acc-teal); }
.tag.think { --tc: var(--acc-purple); }
.tag.srch  { --tc: var(--warning); }
.tag.err   { --tc: var(--critical); }
.sumtx { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
         white-space: nowrap; color: var(--text-primary); }
.sumtx.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.sumtx.mline { white-space: pre-wrap; overflow: visible; text-overflow: clip; word-break: break-word; }
.hr { flex: none; margin-left: auto; display: inline-flex; gap: 9px; align-items: baseline;
      font-size: 10.5px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.rc { font-weight: 700; font-family: ui-monospace, Menlo, monospace; }
.stp .bd { border-top: 1px solid var(--grid); padding: 9px 12px 11px; }
.stp .cap { font-size: 10.5px; color: var(--text-muted); margin: 9px 0 4px; }
.stp .cap:first-child { margin-top: 0; }
.stp pre { margin: 0; padding: 8px 10px; background: var(--plane);
           border: 1px solid var(--grid); border-radius: 6px; overflow-x: auto;
           font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
           font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--text-secondary); }
.stp .prose { white-space: pre-wrap; word-break: break-word; color: var(--text-primary); font-size: 12px; }
.turnrow { font-size: 11px; color: var(--text-muted); border-top: 1px dashed var(--grid);
           padding-top: 7px; margin-top: 3px; font-variant-numeric: tabular-nums; }
.nolog { font-size: 12px; color: var(--text-muted); margin: 0; }
.exp-sec { margin-top: 12px; }
.exp-sec > summary { cursor: pointer; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
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
# 步骤 kind：cmd（命令）/ tool（非 shell 工具）/ msg（agent 发言）/ think（思考）
#            / file（改文件）/ search / todo / turn（轮次边界+用量）/ err
# 两种日志格式都归一到这套 kind，渲染层不再关心 agent 是谁。

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


def parse_steps(text: str) -> list[dict]:
    """Codex JSONL / Claude Code stream-json → 统一步骤列表。

    非 JSON 行（shell 标记、git diff）直接跳过；两种事件词表混在同一个
    循环里分发，日志是哪家产的无所谓 —— 认事件不认 agent。
    """
    steps: list[dict] = []
    pending: dict[str, dict] = {}   # Claude: tool_use id → 待回填输出的步骤

    def err(msg: str) -> None:
        # error 事件和 turn.failed 常带同一条消息，连续重复只记一次
        if msg and not (steps and steps[-1]["kind"] == "err" and steps[-1]["text"] == msg):
            steps.append({"kind": "err", "text": msg})

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
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
                steps.append({"kind": "cmd", "cmd": it.get("command", ""),
                              "out": it.get("aggregated_output", ""),
                              "rc": it.get("exit_code")})
            elif k == "agent_message":
                steps.append({"kind": "msg", "text": it.get("text", "")})
            elif k == "reasoning":
                steps.append({"kind": "think", "text": it.get("text", "")})
            elif k == "file_change":
                steps.append({"kind": "file", "changes": [
                    (c.get("kind", ""), c.get("path", "")) for c in it.get("changes", [])]})
            elif k == "web_search":
                steps.append({"kind": "search", "q": it.get("query", "")})
            elif k == "todo_list":
                steps.append({"kind": "todo", "items": [
                    (bool(i.get("completed")), i.get("text", "")) for i in it.get("items", [])]})
            elif k == "error":
                err(it.get("message", ""))
        elif t == "turn.completed":
            steps.append({"kind": "turn", "usage": e.get("usage") or {}})
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
                    steps.append({"kind": "msg", "text": b.get("text", "")})
                elif bt == "thinking":
                    steps.append({"kind": "think", "text": b.get("thinking", "")})
                elif bt == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") == "Bash":
                        s = {"kind": "cmd", "cmd": inp.get("command", ""), "out": "", "rc": None}
                    else:
                        s = {"kind": "tool", "name": b.get("name", "?"),
                             "arg": _claude_tool_arg(inp), "out": ""}
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
                if s["kind"] == "cmd" and s["rc"] is None:
                    s["rc"] = 1 if b.get("is_error") else 0
                elif s["kind"] == "tool":
                    s["err"] = bool(b.get("is_error"))
        elif t == "result":
            u = e.get("usage") or {}
            # 口径对齐 Codex：input = 三个互斥字段之和（同 run_claude_agent.parse_usage）
            steps.append({"kind": "turn", "final": e.get("subtype", "done"),
                          "turns": e.get("num_turns", 0),
                          "cost": e.get("total_cost_usd") or 0.0,
                          "usage": {
                              "input_tokens": u.get("input_tokens", 0)
                                  + u.get("cache_creation_input_tokens", 0)
                                  + u.get("cache_read_input_tokens", 0),
                              "cached_input_tokens": u.get("cache_read_input_tokens", 0),
                              "output_tokens": u.get("output_tokens", 0),
                              "reasoning_output_tokens": 0}})
    return steps


def _fmt_usage(u: dict) -> str:
    return (f'输入 {u.get("input_tokens", 0):,}（缓存 {u.get("cached_input_tokens", 0):,}）· '
            f'输出 {u.get("output_tokens", 0):,}（推理 {u.get("reasoning_output_tokens", 0):,}）')


# `/bin/bash -lc '...'` 这层包装是 runner 加的，摘要行里剥掉，只看真正的命令。
# 展开后的「命令」区仍是原文，剥壳只影响摘要。
_SHELL_RE = re.compile(r"^\s*(?:/usr)?(?:/bin/)?(?:ba|z|da)?sh\s+-l?c\s+(.*)$", re.S)
_MUT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}   # Claude Code 的写类工具


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


def _card(tag_cls: str, tag: str, summary: str, right: str = "", body: str = "",
          open_: bool = False, bad: bool = False, mono: bool = True) -> str:
    """一步一张卡：标签 + 单行摘要（+右侧指标）；有 body 才可折叠。"""
    sumcls = "sumtx mono" if mono else "sumtx"
    head = (f'<span class="tag {tag_cls}">{esc(tag)}</span>'
            f'<span class="{sumcls}">{summary}</span>'
            + (f'<span class="hr">{right}</span>' if right else ""))
    cls = "stp bad" if bad else "stp"
    if body:
        return (f'<details class="{cls}"{" open" if open_ else ""}>'
                f'<summary><span class="chev"></span>{head}</summary>'
                f'<div class="bd">{body}</div></details>')
    # 不可折叠的卡也放一个空 chev 占位，让标签跟可折叠卡对齐
    return f'<div class="{cls}"><div class="hd"><span class="chev"></span>{head}</div></div>'


def steps_html(steps: list[dict], max_out: int) -> str:
    parts, n_turn = [], 0
    for s in steps:
        k = s["kind"]
        if k == "cmd":
            orig = s.get("cmd") or ""
            disp = strip_shell(orig)
            lines = disp.splitlines() or [""]
            first = lines[0].strip()
            rc, out = s.get("rc"), (s.get("out") or "").rstrip()
            bad = rc not in (None, 0)
            right = ('<span class="rc" style="color:var(--text-muted)">rc=?</span>' if rc is None
                     else f'<span class="rc {"bad" if bad else "ok"}">rc={rc}</span>')
            if len(lines) > 1:
                right += f'<span>+{len(lines) - 1} 行</span>'
            if out:
                right += f'<span>{len(out):,} 字符</span>'
            body = ""
            if len(lines) > 1 or len(first) > 100 or disp != orig:
                body += f'<div class="cap">命令</div><pre>{esc(elide(orig, 1200))}</pre>'
            if out:
                body += f'<div class="cap">输出</div><pre>{esc(elide(out, max_out))}</pre>'
            parts.append(_card("cmd", "命令", esc(first) or "—", right, body,
                               open_=bad, bad=bad))
        elif k == "tool":
            name = s.get("name") or "?"
            out, bad = (s.get("out") or "").rstrip(), bool(s.get("err"))
            right = ('<span class="rc bad">error</span>' if bad else "") \
                + (f'<span>{len(out):,} 字符</span>' if out else "")
            body = f'<div class="cap">输出</div><pre>{esc(elide(out, max_out))}</pre>' if out else ""
            parts.append(_card("mut" if name in _MUT_TOOLS else "cmd", name,
                               esc(elide(s.get("arg", ""), 200)), right, body,
                               open_=bad, bad=bad))
        elif k in ("msg", "think"):
            txt = (s.get("text") or "").strip()
            if not txt:
                continue        # 有的桥接端点会发空 agent_message，不值一张卡
            lines = txt.splitlines()
            first = next((ln.strip() for ln in lines if ln.strip()), "")
            long = len(lines) > 1 or len(first) > 110
            body = (f'<div class="prose">{esc(elide(txt, 4000 if k == "msg" else max_out * 3))}'
                    '</div>') if long else ""
            parts.append(_card("think" if k == "think" else "", "思考" if k == "think" else "消息",
                               esc(first) or "—", f'{len(txt):,} 字符' if long else "",
                               body, mono=False))
        elif k == "file":
            items = [f'{kind} {p}' for kind, p in s["changes"]]
            body = ("<pre>" + esc("\n".join(items)) + "</pre>") if len(items) > 1 else ""
            parts.append(_card("mut", "改文件", esc(" · ".join(items)) or "—",
                               f'{len(items)} 个文件' if len(items) > 1 else "", body))
        elif k == "search":
            parts.append(_card("srch", "搜索", esc(s.get("q") or ""), mono=False))
        elif k == "todo":
            items = s["items"]
            done = sum(1 for d, _ in items if d)
            body = "<pre>" + esc("\n".join(f'{"☑" if d else "☐"} {t}' for d, t in items)) + "</pre>"
            parts.append(_card("", "TODO", esc("；".join(t for _, t in items)),
                               f'{done}/{len(items)} 完成', body, mono=False))
        elif k == "turn":
            n_turn += 1
            if "final" in s:   # Claude Code 结尾 result：整个 run 的累计值
                label = (f'完成（{esc(str(s["final"]))}）· {s.get("turns", 0)} turns · '
                         + _fmt_usage(s["usage"])
                         + (f' · ${s["cost"]:.4f}' if s.get("cost") else ""))
            else:
                label = f'turn {n_turn} · ' + _fmt_usage(s["usage"])
            parts.append(f'<div class="turnrow">{label}</div>')
        elif k == "err":
            parts.append('<div class="stp errc"><div class="hd"><span class="chev"></span>'
                         '<span class="tag err">错误</span>'
                         f'<span class="sumtx mono mline">{esc(elide(s["text"], max_out))}'
                         '</span></div></div>')
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

        # 执行过程：logs/<iid>.log 解析成步骤流；没有日志时按钮退化成「明细」
        steps = []
        log = logs_dir / f'{r["instance_id"]}.log' if logs_dir else None
        if log and log.is_file():
            steps = parse_steps(log.read_text(errors="replace"))
        n_act = sum(1 for s in steps if s["kind"] in ("cmd", "tool", "search", "file"))
        row.append(f'<td><button class="ghost xs" data-exp="exp-{i}">'
                   + (f'{n_act} 步' if n_act else '明细') + '</button></td>')

        # 展开区：失败测试 → 步骤流 → 逐条测试 → 最终 patch
        exp = []
        fails = [t for t in r["tests"] if t["status"] != "PASSED"]
        for t in fails:
            exp.append(f'<div class="failbox"><div class="t">{chip(t["status"])} '
                       f'{esc(t["short"])}（{t["group"]} · {t["status"]}）</div>'
                       + (f'<div class="m">{esc(t["detail"])}</div>' if t["detail"] else "")
                       + '</div>')
        if steps:
            exp.append(steps_html(steps, max_out))
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
