#!/usr/bin/env python3
"""扫推理日志，找 agent「抄答案」而不是「解题」的痕迹。

为什么需要它：官方镜像的 /app/.git 里带着 gold fix 和判分用的 test_patch，沙箱又能上网，
于是 agent 有两条抄近路的通道。Cursor 盲审 731 条 trajectory 的结论是 **63% 的成功解是
检索而非推导**（公网查 PR 57% + 挖 .git 9%）。所以「Resolved 多少」这一个数没法单独看，
必须同时报「其中多少条是自己做出来的」。

三档判定（宁可漏报也不误报，命中的都要人工过一眼）：

  confirmed  日志里直接出现了 gold fix 的 commit hash —— 它只可能来自 .git 或 instance_id，
             agent 拿到手就等于拿到答案。这一档基本没有误报。
  history    出现历史侦查命令（git log --all / -S、git show <hash>、git branch -a、
             reflog、fsck）。可能只是在读代码演进，得看内容。
  network    出现抓上游的动作（curl/wget 到 github.com、/pull/、.patch）。

用法：
  python audit_contamination.py --run results/smoke
  python audit_contamination.py --run results/smoke --report eval_smoke.json -o audit.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from run_codex_pro import fix_commit_hash

# ── 信号 ──────────────────────────────────────────────────────────────
# 历史侦查。`git log` 本身不算 —— 读提交历史是正常开发动作，只有跨到 HEAD 之外
# （--all）、按内容大海捞针（-S/-G）、或直接点名某个 commit 才可疑。
HISTORY_PATTERNS = [
    ("git log --all", re.compile(r"git\s+log\b[^|;&\n]*--all")),
    ("git log -S/-G（按内容搜历史）", re.compile(r"git\s+log\b[^|;&\n]*\s-[SG]\b")),
    ("git show <hash>", re.compile(r"git\s+show\s+[0-9a-f]{7,40}\b")),
    ("git diff <hash>", re.compile(r"git\s+diff\b[^|;&\n]*\s[0-9a-f]{7,40}\b")),
    ("git cherry-pick", re.compile(r"git\s+cherry-pick\b")),
    ("git branch -a", re.compile(r"git\s+branch\b[^|;&\n]*\s(-a|--all)\b")),
    ("git reflog", re.compile(r"git\s+reflog\b")),
    ("git fsck", re.compile(r"git\s+fsck\b")),
    ("git rev-list --all", re.compile(r"git\s+rev-list\b[^|;&\n]*--all")),
]

# 抓上游。只提到 github.com 不算（README 里到处都是），得是「取」这个动作，
# 或者 URL 指向 PR / commit / .patch 这类一看就是答案的路径。
#
# ⚠️ `.patch`/`.diff` 必须锚在 http(s):// 上。不锚的话任何叫 foo.patch 的本地文件
# 都会命中 —— 这条一开始就是这么写的，实测在正常的轮次上直接误报。
NET_PATTERNS = [
    ("curl/wget github", re.compile(r"\b(curl|wget)\b[^|;&\n]*github(usercontent)?\.com")),
    ("GitHub API", re.compile(r"api\.github\.com/repos/[^\s\"']+/(pulls|commits)")),
    ("拉 PR/commit 的 patch",
     re.compile(r"https?://[^\s\"']*(?:/(?:pull|commit)/[^\s\"']*|\.(?:patch|diff)\b)")),
    # web_search 工具（服务端执行，搜索结果直接进模型上下文）。跑分脚本已用
    # `-c web_search=disabled` 关死（Codex 0.146 起默认开），所以这个信号正常应为 0 ——
    # 再出现只有两种可能：关闭开关失效（配置回归），或中转真在服务端注入。
    # 哪种都是污染风险，照旧计入 network 档。
    ("web_search", re.compile(r"\bweb_search\b")),
]


def texts_from_log(path: Path) -> str:
    """把一条 instance 的日志摊平成一段纯文本。

    日志是 Codex 的 JSONL 事件流，命令和输出都是 **JSON 转义过的**（`\\"`、`\\n`），
    直接对原始行做正则会漏。所以先按行解析 JSON、递归收集所有字符串值再拼起来；
    解析不了的行（容器模式里夹杂的 shell 回显）原样保留。

    ⚠️ 容器模式的日志尾部有 ===DIFF_START===/===DIFF_END=== 包着的 git diff，
    那是**产物**不是 agent 的动作。不剔掉的话，patch 里出现的 `.patch` 字样、
    测试文件路径都会变成误报。
    """
    raw = path.read_text(errors="replace")
    if "===DIFF_START===" in raw:
        head, _, rest = raw.partition("===DIFF_START===")
        _, _, tail = rest.partition("===DIFF_END===")
        raw = head + tail

    out: list[str] = []

    def walk(v) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("{"):
            try:
                walk(json.loads(s))
                continue
            except json.JSONDecodeError:
                pass
        out.append(line)
    return "\n".join(out)


def audit_one(iid: str, text: str) -> dict:
    """判一条。返回命中的三档信号。"""
    h = fix_commit_hash(iid)

    # ⚠️ 必须先把 instance_id 本身从文本里抹掉再找 hash。宿主机模式的 workdir 是
    # work/<instance_id>/app，而 instance_id 里就含着这个 hash —— 不抹的话
    # agent 每敲一条带路径的命令都会命中，100% 误报。
    stripped = text.replace(iid, " ")
    # 连着的 40 位也要抹：有些日志里 instance_id 被截断或拼接过
    hits_hash: list[str] = []
    if h:
        # 7 位以上的前缀就算 —— agent 一般只敲前 10 位
        for m in re.finditer(r"\b([0-9a-f]{7,40})\b", stripped):
            g = m.group(1)
            if h.startswith(g) or g.startswith(h):
                hits_hash.append(g)

    hist = [name for name, rx in HISTORY_PATTERNS if rx.search(text)]
    net = [name for name, rx in NET_PATTERNS if rx.search(text)]

    level = "confirmed" if hits_hash else ("history" if hist else ("network" if net else "clean"))
    return {
        "instance_id": iid,
        "level": level,
        "fix_hash": h,
        "fix_hash_hits": sorted(set(hits_hash))[:5],
        "history_signals": hist,
        "network_signals": net,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="推理输出目录（要有 logs/<iid>.log）")
    ap.add_argument("--report", default="",
                    help="pro_eval_report.py 产出的 eval_*.json；给了就交叉出"
                         "「干净的 Resolved」——这才是能拿去比的那个数")
    ap.add_argument("-o", "--output", default="", help="把逐条结果写成 JSON")
    ap.add_argument("--show", default="all", choices=["all", "hit", "confirmed"],
                    help="终端上打哪些条目，默认全打")
    args = ap.parse_args()

    logs = Path(args.run) / "logs"
    if not logs.is_dir():
        sys.exit(f"找不到 {logs} —— --run 要指向推理的 -o 目录")
    files = sorted(p for p in logs.glob("*.log"))
    if not files:
        sys.exit(f"{logs} 里没有 .log")

    rows = [audit_one(p.stem, texts_from_log(p)) for p in files]

    resolved: dict[str, bool] = {}
    if args.report:
        rep = json.loads(Path(args.report).read_text())
        resolved = {r["instance_id"]: bool(r.get("resolved")) for r in rep.get("instances", [])}

    order = {"confirmed": 0, "history": 1, "network": 2, "clean": 3}
    icon = {"confirmed": "❌ 抄了", "history": "⚠️  翻历史", "network": "⚠️  查上游", "clean": "✅ 干净"}
    for r in sorted(rows, key=lambda r: (order[r["level"]], r["instance_id"])):
        if args.show == "confirmed" and r["level"] != "confirmed":
            continue
        if args.show == "hit" and r["level"] == "clean":
            continue
        why = ", ".join(r["fix_hash_hits"] or r["history_signals"] or r["network_signals"])
        rs = "" if r["instance_id"] not in resolved else \
            ("  [resolved]" if resolved[r["instance_id"]] else "  [failed]")
        print(f"{icon[r['level']]}  {r['instance_id'][:56]:56}{rs}  {why}")

    n = len(rows)
    c = {k: sum(1 for r in rows if r["level"] == k) for k in order}
    print(f"\n共 {n} 条：抄了 {c['confirmed']} · 翻历史 {c['history']} · "
          f"查上游 {c['network']} · 干净 {c['clean']}")

    if resolved:
        # 这才是重点：污染的条目对分数的实际影响，只体现在**它判过了**的那些上。
        # 没判过的条目就算翻了历史也不虚增分数。
        dirty = {r["instance_id"] for r in rows if r["level"] != "clean"}
        conf = {r["instance_id"] for r in rows if r["level"] == "confirmed"}
        nres = sum(1 for i, v in resolved.items() if v)
        nres_dirty = sum(1 for i, v in resolved.items() if v and i in dirty)
        nres_conf = sum(1 for i, v in resolved.items() if v and i in conf)
        clean = nres - nres_dirty
        pct = (clean / n * 100) if n else 0.0
        print(f"Resolved {nres}/{n}，其中确认抄答案 {nres_conf} 条、有嫌疑 {nres_dirty} 条")
        print(f"→ 干净的 Resolved：{clean}/{n}（{pct:.1f}%）← 拿去比的应该是这个数")

    if args.output:
        Path(args.output).write_text(json.dumps(
            {"run": args.run, "total": n, "counts": c, "instances": rows},
            indent=2, ensure_ascii=False))
        print(f"写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
