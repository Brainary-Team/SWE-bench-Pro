#!/usr/bin/env python3
"""把 Codex 的 JSONL 日志还原成可读的执行过程。

run_codex_agent.py 给 codex exec 加了 --json（为了拿输入/输出 token 拆分），
代价是 logs/*.log 从人类可读变成了事件流。这个脚本负责反解析。

用法:
  python show_codex_run.py results/codex-tok/logs/astropy__astropy-12907.log
  python show_codex_run.py results/codex-tok            # 目录：列出所有条目
  python show_codex_run.py <log> --full                 # 不截断命令输出
  python show_codex_run.py <log> --commands             # 只看执行了哪些命令
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, YELLOW, RED, GREEN = "\033[36m", "\033[33m", "\033[31m", "\033[32m"


def events(path: Path):
    """日志里混着 shell 的 echo 标记和 git diff，只挑得出来的 JSON 行。"""
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def clip(text: str, limit: int) -> str:
    text = text.rstrip()
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    return f"{head}\n{DIM}… 还有 {len(text) - limit} 字符，加 --full 看全部{RESET}"


def show(path: Path, args) -> None:
    print(f"{BOLD}{path}{RESET}")
    n_cmd = 0
    for e in events(path):
        typ = e.get("type")

        if typ == "turn.completed":
            u = e.get("usage") or {}
            print(f"\n{DIM}{'─' * 70}{RESET}")
            print(f"{BOLD}turn 结束{RESET}  输入 {u.get('input_tokens', 0):,}"
                  f"（缓存 {u.get('cached_input_tokens', 0):,}）· "
                  f"输出 {u.get('output_tokens', 0):,}"
                  f"（推理 {u.get('reasoning_output_tokens', 0):,}）")
            continue

        if typ != "item.completed":
            continue
        it = e.get("item") or {}
        kind = it.get("type")

        if kind == "command_execution":
            n_cmd += 1
            rc = it.get("exit_code", "?")
            color = GREEN if rc == 0 else RED
            print(f"\n{CYAN}${RESET} {it.get('command', '')}")
            print(f"  {color}rc={rc}{RESET}")
            if not args.commands:
                out = it.get("aggregated_output", "")
                if out.strip():
                    body = clip(out, 0 if args.full else args.max_output)
                    print("\n".join("  " + ln for ln in body.splitlines()))

        elif kind == "agent_message":
            print(f"\n{YELLOW}▸ agent{RESET}")
            body = clip(it.get("text", ""), 0 if args.full else args.max_output)
            print("\n".join("  " + ln for ln in body.splitlines()))

        elif kind == "error":
            print(f"\n{RED}✕ {it.get('message', '')}{RESET}")

    print(f"\n{DIM}共 {n_cmd} 条命令{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="日志文件，或含 logs/ 的 run 目录")
    ap.add_argument("--full", action="store_true", help="不截断输出")
    ap.add_argument("--commands", action="store_true", help="只列命令，不带输出")
    ap.add_argument("-n", "--max-output", type=int, default=600, help="每段输出截断字符数")
    args = ap.parse_args()

    p = Path(args.target)
    if p.is_dir():
        logs = sorted((p / "logs" if (p / "logs").is_dir() else p).glob("*.log"))
        if not logs:
            print(f"{p} 下没有 .log", file=sys.stderr)
            return 1
        if len(logs) == 1:
            show(logs[0], args)
            return 0
        print("多个日志，指定其一：")
        for f in logs:
            print(f"  python {sys.argv[0]} {f}")
        return 0

    if not p.is_file():
        print(f"找不到 {p}", file=sys.stderr)
        return 1
    show(p, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
