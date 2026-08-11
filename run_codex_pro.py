#!/usr/bin/env python3
"""用 Codex CLI 作为 agent 跑 SWE-bench Pro，产出 .pred 文件。

架构（和 swe_bench_pro_eval.py 严格分家）：
  推理：这里起容器，把 Codex 的 Linux 二进制挂进官方镜像，让它在 /app 里改代码，
        收尾 git diff 出 patch。
  评测：swe_bench_pro_eval.py 自己起另一个干净容器跑测试。两边不共用容器。

Codex 0.146+ 只认 wire_api=responses —— 端点必须支持 Responses API，不是 Chat Completions。
DeepSeek 原生就有 /v1/responses，直连即可：

  python run_codex_pro.py --dataset swebench_pro.jsonl --model deepseek-v4-flash \
      --provider deepseek --base-url https://api.deepseek.com/v1 --api-key sk-... \
      --codex-bin codex-bin/codex-x86_64-unknown-linux-musl -o results/codex-pro

产出（一个 run 目录里齐活，后面两个阶段都从这儿取数）：
  <output_dir>/<instance_id>/<instance_id>.pred   逐条补丁 + 用量，喂给 eval_pro.py
  <output_dir>/logs/<instance_id>.log             Codex 的 JSONL 事件流，喂给 make_report.py
  <output_dir>/preds.json                         汇总补丁，报告用它定位同级的 meta 和日志
  <output_dir>/run_meta.json                      token/耗时台账，schema 与 Verified 那套一致
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 官方仓库保持原样不动，只从它里面 import helper（不写入、不修改）。
# dont_write_bytecode：不然 import 会在官方仓库里落一个 helper_code/__pycache__。
UPSTREAM = Path(__file__).resolve().parent / "SWE-bench_Pro-os"
sys.dont_write_bytecode = True
sys.path.insert(0, str(UPSTREAM / "helper_code"))
from create_problem_statement import create_problem_statement  # noqa: E402
from image_uri import get_dockerhub_image_uri  # noqa: E402

# SWE-bench Pro 的镜像里仓库在 /app（不是 SWE-bench 的 /testbed）。
REPO_DIR = "/app"

# 容器里固定用这个名字取 key，值由 --api-key 注入；宿主机叫什么与容器无关。
INNER_ENV_KEY = "CODEX_API_KEY"

PROMPT = """<pr_description>
{problem}
</pr_description>

You are a software engineer working in the repository at {repo_dir}.
Implement the change described above.

## Boundaries
- MODIFY: regular source files under {repo_dir}
- DO NOT MODIFY: any test file, or CI/config files

## Workflow
1. Locate and read the relevant source files.
2. Implement the change so it satisfies the requirements and the described interfaces.
3. Keep the fix general and consistent with the surrounding code style.
4. Consider edge cases.

Leave your changes uncommitted in the working tree. Do not run `git commit`.
"""

USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens")

# patch 里要挡掉的东西：测试文件（评测阶段本来就会强制覆盖，留着只会让 patch 变脏）
# 和 agent 自己的草稿/构建产物。
#
# ⚠️ 每条都必须带 `glob` magic。不带的话 git pathspec 用的是 fnmatch **不加 FNM_PATHNAME**，
# `*` 会跨 `/` 匹配，于是 `:(exclude)*test_*` 这种写法是「路径里任意位置含 test_ 就排除」——
# `src/latest_news.go` 里的 "la|test_|news" 就中招，真源码被无声地从 patch 里删掉，
# 表现成模型明明改了却判不过。加了 glob 之后 `*` 不跨 `/`，`**/` 才表示任意层级。
EXCLUDES = [
    "':(exclude,glob)**/test_*'", "':(exclude,glob)**/*_test.go'",
    "':(exclude,glob)**/*_test.py'", "':(exclude,glob)**/*_test.js'",
    "':(exclude,glob)**/*.test.*'", "':(exclude,glob)**/*.spec.*'",
    "':(exclude,glob)**/test/**'", "':(exclude,glob)**/tests/**'",
    "':(exclude,glob)**/testing/**'", "':(exclude,glob)**/__tests__/**'",
    "':(exclude,glob)**/*.pyc'", "':(exclude,glob)**/__pycache__/**'",
    "':(exclude,glob)**/*.egg-info/**'", "':(exclude,glob)**/node_modules/**'",
    "':(exclude,glob)**/*.log'",
]


def _unwrap(s: str) -> str:
    """数据集里 731 条有 328 条的文本字段是**双层 JSON 编码**的，得剥一层。

    那 328 条的 problem_statement 存的是一个 JSON 字符串字面量：首尾各带一个真的双引号，
    换行是字面的两个字符 `\\n`。直接塞进 prompt，模型看到的就是一坨转义符而不是段落。
    另外 403 条是正常纯文本，所以不能无条件解码 —— 只在「首尾是双引号且能 json 解出字符串」
    时剥，其余原样返回。
    """
    if len(s) > 1 and s[0] == '"' and s[-1] == '"':
        try:
            v = json.loads(s)
            if isinstance(v, str):
                return v
        except json.JSONDecodeError:
            pass
    return s


def build_prompt(inst: dict) -> str:
    """题面用官方 helper 拼（problem_statement + Requirements + New interfaces），
    但三个字段先各自剥壳，免得 45% 的题目带着字面 \\n 进 prompt。"""
    row = {**inst, **{k: _unwrap(inst.get(k, "") or "")
                      for k in ("problem_statement", "requirements", "interface")}}
    return PROMPT.format(problem=create_problem_statement(row), repo_dir=REPO_DIR)


def build_codex_cmd(args) -> str:
    """wire_api 必须是 responses —— Codex 0.146+ 已移除 chat 支持，差额由桥来补。"""
    p = args.provider
    parts = [
        "codex", "exec",
        # --json：stdout 变 JSONL 事件流，turn.completed 里带 token 拆分；不加只有汇总数
        "--json",
        "--skip-git-repo-check", "--ephemeral",
        "-s", "danger-full-access",
        "-C", REPO_DIR,
        "-c", f"model_provider={shlex.quote(p)}",
        "-c", f"model_providers.{p}.name={shlex.quote(p)}",
        "-c", f"model_providers.{p}.base_url={shlex.quote(args.base_url)}",
        "-c", f"model_providers.{p}.env_key={INNER_ENV_KEY}",
        "-c", f"model_providers.{p}.wire_api=responses",
    ]
    # 容器里的 CODEX_HOME 是全新的，宿主机 ~/.codex/config.toml 一概不生效。
    # 不显式下发就是 Codex 的内置默认（实测 none）。
    if args.reasoning_effort:
        parts += ["-c", f"model_reasoning_effort={shlex.quote(args.reasoning_effort)}"]
    parts += ["-m", shlex.quote(args.model), "-", "< /tmp/prompt.txt"]
    return " ".join(parts)


def parse_usage(out: str) -> dict:
    """累加所有 turn.completed 的 usage —— 一次任务是多轮，只取最后一条会漏。

    只扫 CODEX_START/END 区段，免得把 git diff 里像 JSON 的行当成事件。
    """
    usage = {k: 0 for k in USAGE_FIELDS}
    turns = 0
    if "===CODEX_START===" in out:
        out = out.split("===CODEX_START===", 1)[1]
    if "===CODEX_END" in out:
        out = out.split("===CODEX_END", 1)[0]
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{") or "turn.completed" not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "turn.completed":
            continue
        turns += 1
        for k, v in (ev.get("usage") or {}).items():
            if k in usage and isinstance(v, int):
                usage[k] += v
    usage["turns"] = turns
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


AGENT_T = re.compile(r"^===AGENT_T([01]) ([0-9.]+)===$", re.M)


def parse_agent_seconds(out: str) -> float | None:
    """容器内打的两个时间戳之差 = agent 净耗时（不含拉镜像/建容器/收尾 diff）。"""
    ts: dict[str, float] = {}
    for which, val in AGENT_T.findall(out):
        try:
            ts.setdefault(which, float(val))
        except ValueError:
            return None
    if "0" in ts and "1" in ts and ts["1"] >= ts["0"]:
        return ts["1"] - ts["0"]
    return None


def ensure_image(img: str, platform: str, timeout: int) -> float:
    """镜像不在本地就先拉，返回拉取秒数。把拉取时间从 agent 耗时里摘出去。"""
    if subprocess.run(["docker", "image", "inspect", img],
                      capture_output=True).returncode == 0:
        return 0.0
    t = time.time()
    try:
        subprocess.run(["docker", "pull", "--platform", platform, img],
                       capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass  # 拉失败交给后面的 docker run 报错，这里只负责计时
    return round(time.time() - t, 1)


def run_one(inst: dict, args, codex_bin: Path, outdir: Path) -> dict:
    iid = inst["instance_id"]
    img = get_dockerhub_image_uri(iid, args.dockerhub_username, inst.get("repo", ""))
    prompt = build_prompt(inst)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "prompt.txt").write_text(prompt)

        # git add -N 让新建的源文件也进 diff；EXCLUDES 把测试/草稿挡在外面。
        inner = f"""set -uo pipefail
mkdir -p /opt/codexhome
export CODEX_HOME=/opt/codexhome
cd {REPO_DIR}
git reset --hard {inst['base_commit']} >/dev/null 2>&1
echo "===AGENT_T0 $(date +%s.%N)==="
echo "===CODEX_START==="
{build_codex_cmd(args)}
echo "===CODEX_END rc=$?==="
echo "===AGENT_T1 $(date +%s.%N)==="
cd {REPO_DIR}
git add -A -- . {' '.join(EXCLUDES)} >/dev/null 2>&1
echo "===DIFF_START==="
git diff --cached
echo "===DIFF_END==="
"""
        (td / "run.sh").write_text(inner)

        # ⚠️ 必须给容器起名字。subprocess 的 timeout 杀掉的是 **docker 客户端**，
        # 容器自己还在后台跑（--rm 只保证退出时清理，不保证被杀）。2026-08-10 那轮
        # 就漏了一个：实例早已记成 [done]，容器却又跑了 14 小时，一直在烧中转的 token，
        # 还把 usage 记账搅浑。超时后必须显式 docker rm -f。
        cname = f"codexpro-{iid[:50]}-{os.getpid()}"
        cmd = ["docker", "run", "--rm", "--name", cname, "--platform", args.platform,
               "-v", f"{codex_bin}:/usr/local/bin/codex:ro",
               "-v", f"{td/'prompt.txt'}:/tmp/prompt.txt:ro",
               "-v", f"{td/'run.sh'}:/tmp/run.sh:ro",
               "-e", f"{INNER_ENV_KEY}={args.api_key}",
               # 镜像默认 ENTRYPOINT 就是 bash，显式覆盖避免嵌套 bash
               "--entrypoint", "/bin/bash",
               img, "/tmp/run.sh"]

        # ⚠️ 拉镜像必须在计时开始**之前**：pull 是环境成本，不是模型解题耗时。
        # 实测有实例 pull 花了 9006s 而解题只有 8846s，混进去就完全没法看了。
        pull_s = ensure_image(img, args.platform, args.pull_timeout)

        # 用 monotonic 不用 time.time()：macOS 休眠时 time.time() 照走、monotonic 冻结，
        # 而 subprocess 的 timeout 内部就是 monotonic。混用的话「墙钟 27516s 但 2400s
        # 的超时没触发」这种自相矛盾的数会写进报告里（2026-08-10 那轮就是这么翻的车）。
        t0 = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=args.timeout, stdin=subprocess.DEVNULL)
            out = proc.stdout + "\n" + proc.stderr
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") + "\n[TIMEOUT]"
            timed_out = True
            subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
        wall_s = time.monotonic() - t0

    inst_dir = outdir / iid
    inst_dir.mkdir(parents=True, exist_ok=True)
    # 日志摊平放 logs/ 而不是各自的实例目录：报告那边按 preds.json 同级的 logs/<iid>.log 找，
    # 与 Verified 那套完全一致，换掉 agent 也不用改报告。
    logs_dir = outdir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"{iid}.log").write_text(out)

    patch = ""
    if "===DIFF_START===" in out and "===DIFF_END===" in out:
        raw = out.split("===DIFF_START===", 1)[1].rsplit("===DIFF_END===", 1)[0]
        # ⚠️ 不能 .strip()：unified diff 的空白上下文行就是「一个空格」，
        # strip 会吃掉行尾，hunk 声明的行数对不上 → git apply "corrupt patch"。
        patch = raw.lstrip("\n")
        if patch and not patch.endswith("\n"):
            patch += "\n"

    agent_s = parse_agent_seconds(out)
    # 容器被超时杀掉时只有 T0 没有 T1，agent_s 是 None。**这里**如实留 None，
    # 不拿 wall_seconds 冒充解题耗时 —— 那是「跑到超时」的长度，不是「解出来用了多久」。
    # 报告要的那份数在 write_aggregates 里另外补（退回 wall + 打 measured 标记）。
    rec = {
        "instance_id": iid,
        "model_patch": patch,
        "model_name_or_path": f"codex-cli/{args.model}",
        "_meta": {
            "seconds": round(agent_s, 1) if agent_s is not None else None,
            "timed_out": timed_out or agent_s is None,
            "wall_seconds": round(wall_s, 1),
            "pull_seconds": pull_s,
            "patch_chars": len(patch),
            "image": img,
            **parse_usage(out),
        },
    }
    (inst_dir / f"{iid}.pred").write_text(json.dumps(rec, indent=2))

    # 磁盘紧就跑完一条删一条。Pro 的镜像是整仓依赖装好的，动辄好几个 G，
    # 731 条全留在本地放不下 —— 但评测阶段还要再拉一次，别在只跑一轮时开。
    if args.rm_image:
        subprocess.run(["docker", "rmi", "-f", img], capture_output=True)
    agent_txt = "TIMEOUT" if rec["_meta"]["seconds"] is None else f"{rec['_meta']['seconds']}s"
    print(f"[done] {iid}  patch={len(patch)}B  agent={agent_txt}  "
          f"turns={rec['_meta']['turns']}  out_tok={rec['_meta']['output_tokens']}", flush=True)
    return rec


def run_one_guarded(inst: dict, args, codex_bin: Path, outdir: Path, lock) -> dict | None:
    """把 run_one 包起来：单条炸了不许带走整轮。

    ⚠️ ThreadPoolExecutor.map 的异常是在**取结果**时才抛的，一旦抛出，后面所有条目
    连结果都取不到，收尾的 write_aggregates 也执行不到 —— 跑了十几个小时的 731 条
    会因为第 400 条 docker 抽风而一份 run_meta.json 都不落地。所以这里兜底：
    出错就记一条空 patch 的 .pred（下轮续跑会自动重试它），然后接着跑。

    每条跑完顺手重写一次聚合，这样中途 Ctrl-C / 断电，preds.json 和 run_meta.json
    也是当时为止的全量快照，报告能直接出。写的时候加锁，避免并发下写出半截 JSON。
    """
    iid = inst["instance_id"]
    try:
        rec = run_one(inst, args, codex_bin, outdir)
    except Exception as e:  # noqa: BLE001 —— 就是要兜住所有意外
        rec = None
        print(f"[error] {iid}  {type(e).__name__}: {e}", flush=True)
        inst_dir = outdir / iid
        inst_dir.mkdir(parents=True, exist_ok=True)
        (inst_dir / f"{iid}.pred").write_text(json.dumps({
            "instance_id": iid,
            "model_patch": "",
            "model_name_or_path": f"codex-cli/{args.model}",
            "_meta": {"seconds": None, "timed_out": True, "wall_seconds": 0.0,
                      "pull_seconds": 0.0, "patch_chars": 0, "image": "",
                      "error": f"{type(e).__name__}: {e}",
                      **{k: 0 for k in USAGE_FIELDS}, "turns": 0, "total_tokens": 0},
        }, indent=2, ensure_ascii=False))
    with lock:
        write_aggregates(outdir, args)
    return rec


# run_meta.json 里 totals 要汇总的字段，与 Verified 的 run_codex_agent.py 逐字对齐 ——
# 报告脚本是同一份，字段名对不上就只是少显示几块 KPI，不会报错，很难发现。
TOTAL_FIELDS = ("seconds", "wall_seconds", "pull_seconds", "turns", "total_tokens", *USAGE_FIELDS)


def read_preds(outdir: Path) -> dict[str, dict]:
    """把 run 目录下所有 <iid>/<iid>.pred 读回来，按 instance_id 建表。

    续跑要靠它认已完成的条目，收尾汇总也要靠它 —— 这样中断重跑之后
    preds.json / run_meta.json 仍然是**全量**的，而不是只有这一轮新跑的那几条。
    """
    recs: dict[str, dict] = {}
    for d in sorted(outdir.iterdir()) if outdir.is_dir() else []:
        if not d.is_dir() or d.name == "logs":
            continue
        p = d / f"{d.name}.pred"
        if not p.is_file():
            continue
        try:
            r = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("instance_id"):
            recs[r["instance_id"]] = r
    return recs


def write_aggregates(outdir: Path, args) -> dict:
    """扫盘产出 preds.json + run_meta.json。

    preds.json 只是给报告当锚点用（它按 predictions_path 的同级目录找 run_meta.json
    和 logs/），评测那条路走的是 .pred 目录，两边不互相依赖。
    """
    recs = read_preds(outdir)
    preds = {iid: {k: r[k] for k in ("instance_id", "model_patch", "model_name_or_path")}
             for iid, r in recs.items()}
    (outdir / "preds.json").write_text(json.dumps(preds, indent=2, ensure_ascii=False))

    inst = {}
    for iid, r in recs.items():
        m = dict(r.get("_meta") or {})
        # .pred 里的 seconds 可能是 None（容器被超时杀掉，只有 T0 没有 T1）。
        # 报告里 fmt_secs 会 round(None) 直接 TypeError，必须落成数。
        # 退回 wall_seconds 而不是 0 —— 口径与 Verified 的 run_codex_agent.py 一致：
        # 记 0 会让这条从「推理耗时」KPI 里凭空消失，明明烧了 30 分钟却显示没花时间，
        # 比偏大更误导。是不是容器内实测的，由 seconds_measured_in_container 记着。
        secs = m.get("seconds")
        m["seconds"] = round(m.get("wall_seconds", 0.0) or 0.0, 1) if secs is None else secs
        m["seconds_measured_in_container"] = secs is not None
        inst[iid] = m

    totals = {k: sum(m.get(k, 0) or 0 for m in inst.values()) for k in TOTAL_FIELDS}
    for k in ("seconds", "wall_seconds", "pull_seconds"):
        totals[k] = round(totals[k], 1)

    meta = {
        "model": args.model,
        "provider": args.provider,
        "base_url": args.base_url,
        "agent": "codex-cli",
        "reasoning_effort": args.reasoning_effort or "default",
        "subset": args.subset,
        "split": args.split,
        "totals": totals,
        "instances": inst,
    }
    (outdir / "run_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return meta


def load_dataset_rows(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="JSONL，列同 HuggingFace 的 SWE-bench_Pro")
    ap.add_argument("--instances", nargs="*", default=None, help="只跑这些 instance_id")
    ap.add_argument("--slice", default=None, help="形如 0:2，按顺序切一段")
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default="deepseek", help="配置段名，随便起，只是个标签")
    ap.add_argument("--base-url", required=True,
                    help="端点，必须支持 Responses API；容器里访问得到才行")
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--reasoning-effort", default="",
                    help="空＝不下发，用 Codex 内置默认（实测 none）；推理模型改 high")
    ap.add_argument("--codex-bin", required=True, help="Codex 的 linux x86_64 静态二进制")
    ap.add_argument("--dockerhub-username", default="jefzda")
    ap.add_argument("--platform", default="linux/amd64", help="官方镜像只有 amd64")
    ap.add_argument("--timeout", type=int, default=1800, help="单实例容器总超时（秒）")
    ap.add_argument("--pull-timeout", type=int, default=3600)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--redo-existing", action="store_true",
                    help="默认跳过已有非空 patch 的条目（续跑）；带上就全部重跑")
    ap.add_argument("--rm-image", action="store_true", help="每条跑完删镜像省磁盘")
    ap.add_argument("--subset", default="pro", help="只写进 run_meta.json，报告表头显示")
    ap.add_argument("--split", default="test", help="同上")
    ap.add_argument("-o", "--output-dir", required=True)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    codex_bin = Path(args.codex_bin).resolve()
    if not codex_bin.is_file():
        sys.exit(f"找不到 Codex 二进制：{codex_bin}")

    rows = load_dataset_rows(args.dataset)
    if args.instances:
        want = set(args.instances)
        rows = [r for r in rows if r["instance_id"] in want]
    if args.slice:
        a, b = args.slice.split(":")
        rows = rows[int(a) if a else None:int(b) if b else None]
    if not rows:
        sys.exit("没有要跑的实例")

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    # 续跑：已经跑出**非空** patch 的条目跳过。空 patch 的会重试 ——
    # 上次没憋出东西，值得再给一次机会。想全量重来就 --redo-existing。
    n_all = len(rows)
    if not args.redo_existing:
        done = {iid for iid, r in read_preds(outdir).items() if r.get("model_patch", "").strip()}
        rows = [r for r in rows if r["instance_id"] not in done]
    skipped = n_all - len(rows)

    print(f"[run] {len(rows)}/{n_all} instances（跳过已完成 {skipped}）, "
          f"model={args.model}, workers={args.workers}", flush=True)

    lock = threading.Lock()
    if rows:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(lambda r: run_one_guarded(r, args, codex_bin, outdir, lock), rows))

    # 扫盘汇总，而不是只汇总本轮 —— 中断续跑之后产物依然是全量的。
    meta = write_aggregates(outdir, args)
    t = meta["totals"]
    n_ok = sum(1 for r in read_preds(outdir).values() if r["model_patch"].strip())
    print(f"[done] 非空 patch {n_ok}/{len(meta['instances'])} · "
          f"{t['turns']} turns · 输入 {t['input_tokens']:,}（缓存 {t['cached_input_tokens']:,}）"
          f" · 输出 {t['output_tokens']:,} · 推理累计 {t['seconds'] / 3600:.1f} 小时")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
