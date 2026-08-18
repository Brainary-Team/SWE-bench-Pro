#!/usr/bin/env python3
"""用 Brainary Codex(brainary-codex fork 自建二进制)作为 agent 跑 SWE-bench Pro。

与 run_codex_pro.py 的区别只有「挂什么、开什么」,其余机器全部复用它的
(剥历史 / 出网收敛 / 容器命名与超时清理 / 墓碑与原子聚合,一行不重写):

  1. 挂两个 musl 静态二进制(brainary-codex-bin/):
       brainary-codex-x86_64-unknown-linux-musl                → /usr/local/bin/brainary-codex
       brainary-codex-code-mode-host-x86_64-unknown-linux-musl → /usr/local/bin/codex-code-mode-host
     ⚠️ host 在容器里的文件名必须是 codex-code-mode-host,一个字都不能差:codex 按
     「自身可执行文件同目录 + 固定文件名」查找 host(install-context 的 fallback 逻辑,
     没有环境变量可以覆盖)。挂错名字的症状不是报错,而是 code mode 静默不可用。
  2. 默认开启 code mode(features.code_mode + features.code_mode_host):JS 编排跑在
     独立 host 进程里。gpt-5.6-sol 不在内置模型目录,走 fallback 元数据(tool_mode=None),
     features 开关生效。
  3. 默认模型配置指向公司 relay:gpt-5.6-sol @ https://relay.lzbrainary.com/v1,
     reasoning effort high,wire_api=responses。

产物形状与 run_codex_pro.py 完全一致(.pred / logs/ / preds.json / run_meta.json),
评测(eval_pro.py)和报告(pro_eval_report.py + make_report.py)两段原样共用。

用法:
  .venv/bin/python run_brainary_codex.py --dataset swebench_pro.jsonl \
      --instances instance_ansible__ansible-... --api-key sk-... -o results/brainary-smoke
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import egress
import run_codex_pro as base

# 容器里两个二进制的落点。入口挂成 brainary-codex 以便与官方 codex 区分,
# host 的名字是查找协议的一部分,固定死。
INNER_CODEX = "/usr/local/bin/brainary-codex"
INNER_HOST = "/usr/local/bin/codex-code-mode-host"


def build_codex_cmd(args) -> str:
    """与 run_codex_pro 同构;差异只有:入口叫 brainary-codex,多了 code mode 开关。"""
    p = args.provider
    parts = [
        "brainary-codex", "exec",
        "--json",
        "--skip-git-repo-check", "--ephemeral",
        "-s", "danger-full-access",
        "-C", base.REPO_DIR,
        "-c", f"model_provider={shlex.quote(p)}",
        "-c", f"model_providers.{p}.name={shlex.quote(p)}",
        "-c", f"model_providers.{p}.base_url={shlex.quote(args.base_url)}",
        "-c", f"model_providers.{p}.env_key={base.INNER_ENV_KEY}",
        "-c", f"model_providers.{p}.wire_api=responses",
        # 防数据污染,机理见 run_codex_pro.build_codex_cmd 的注释
        "-c", "web_search=disabled",
        # code mode 是实验特性,把「实验特性已开启」横幅从事件流里压掉
        "-c", "suppress_unstable_features_warning=true",
    ]
    if args.code_mode == "on":
        # CodeMode 暴露 code mode 工具面,CodeModeHost 让会话跑在独立 host 进程
        # (不开 host 的话 thread manager 给 DisabledCodeModeSessionProvider,
        # code mode 会静默退回 Direct 工具面,等于白挂了 host 二进制)
        parts += ["-c", "features.code_mode=true", "-c", "features.code_mode_host=true"]
    elif args.code_mode == "only":
        # CodeModeOnly 隐含 CodeMode,模型只看得到 exec/wait,无静默回退
        parts += ["-c", "features.code_mode_only=true", "-c", "features.code_mode_host=true"]
    if args.reasoning_effort:
        parts += ["-c", f"model_reasoning_effort={shlex.quote(args.reasoning_effort)}"]
    parts += ["-m", shlex.quote(args.model), "-", "< /tmp/prompt.txt"]
    return " ".join(parts)


def run_one(inst: dict, args, codex_bin: Path, outdir: Path, egr: dict | None = None) -> dict:
    """copy 自 run_codex_pro.run_one,只动三处:双二进制挂载、命令入口、model_name_or_path。

    其余(剥历史 / 逐条出网探针 / 容器命名与超时清理 / .pred 落盘)逐行保持一致 ——
    那些行为都是踩坑踩出来的,见原文件各处注释。
    """
    iid = inst["instance_id"]
    img = base.get_dockerhub_image_uri(iid, args.dockerhub_username, inst.get("repo", ""))
    prompt = base.build_prompt(inst)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "prompt.txt").write_text(prompt)

        strip = base.strip_history_script(iid) + "\n" if args.strip_history else ""
        inner = f"""set -uo pipefail
mkdir -p /opt/codexhome
export CODEX_HOME=/opt/codexhome
{egress.probe_snippet(egr)}
cd {base.REPO_DIR}
git reset --hard {inst['base_commit']} >/dev/null 2>&1
{strip}echo "===AGENT_T0 $(date +%s.%N)==="
echo "===CODEX_START==="
{build_codex_cmd(args)}
echo "===CODEX_END rc=$?==="
echo "===AGENT_T1 $(date +%s.%N)==="
cd {base.REPO_DIR}
git add -A -- . {' '.join(base.EXCLUDES)} >/dev/null 2>&1
echo "===DIFF_START==="
git diff --cached
echo "===DIFF_END==="
"""
        (td / "run.sh").write_text(inner)

        # 容器必须起名字:超时杀的是 docker 客户端,容器要显式 rm -f(见原文件注释)
        cname = f"brainarypro-{iid[:48]}-{os.getpid()}"
        cmd = ["docker", "run", "--rm", "--name", cname, "--platform", args.platform,
               *egress.docker_run_args(egr),
               "-v", f"{codex_bin}:{INNER_CODEX}:ro",
               "-v", f"{args.host_bin}:{INNER_HOST}:ro",
               "-v", f"{td/'prompt.txt'}:/tmp/prompt.txt:ro",
               "-v", f"{td/'run.sh'}:/tmp/run.sh:ro",
               "-e", f"{base.INNER_ENV_KEY}={args.api_key}",
               "--entrypoint", "/bin/bash",
               img, "/tmp/run.sh"]

        pull_s = base.ensure_image(img, args.platform, args.pull_timeout)

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
    logs_dir = outdir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"{iid}.log").write_text(out)

    patch = ""
    if "===DIFF_START===" in out and "===DIFF_END===" in out:
        raw = out.split("===DIFF_START===", 1)[1].rsplit("===DIFF_END===", 1)[0]
        patch = raw.lstrip("\n")
        if patch and not patch.endswith("\n"):
            patch += "\n"

    stripped = args.strip_history and base.STRIP_OK in out
    if args.strip_history and base.STRIP_FAIL in out:
        print(f"[warn] {iid}  剥离后 gold fix 仍可读，这条的分数按污染算", flush=True)

    egr_ok, egr_why = egress.parse_probe(out)
    if not egr_ok:
        print(f"[warn] {iid}  出网自检没过（{egr_why}）—— 这条能上公网抄答案，不可采信",
              flush=True)

    agent_s = base.parse_agent_seconds(out)
    rec = {
        "instance_id": iid,
        "model_patch": patch,
        "model_name_or_path": f"brainary-codex/{args.model}",
        "_meta": {
            "seconds": round(agent_s, 1) if agent_s is not None else None,
            "timed_out": timed_out or agent_s is None,
            "wall_seconds": round(wall_s, 1),
            "pull_seconds": pull_s,
            "patch_chars": len(patch),
            "image": img,
            "history_stripped": stripped,
            "egress_ok": egr_ok,
            "egress_why": egr_why,
            **base.parse_usage(out),
        },
    }
    (inst_dir / f"{iid}.pred").write_text(json.dumps(rec, indent=2))

    if args.rm_image:
        subprocess.run(["docker", "rmi", "-f", img], capture_output=True)
    agent_txt = "TIMEOUT" if rec["_meta"]["seconds"] is None else f"{rec['_meta']['seconds']}s"
    print(f"[done] {iid}  patch={len(patch)}B  agent={agent_txt}  "
          f"turns={rec['_meta']['turns']}  out_tok={rec['_meta']['output_tokens']}", flush=True)
    return rec


def sha256_prefix(path: Path, n: int = 12) -> str:
    """记进 run_meta,让每轮跑分都能对上「是哪次构建的二进制」。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


_base_write_aggregates = base.write_aggregates


def write_aggregates(outdir: Path, args, egr: dict | None = None) -> dict:
    """先用 run_codex_pro 的聚合(原子写、扫盘全量、egress 台账一概沿用),
    再补上 brainary 的自描述字段。run_one_guarded 每条都会经由 base 模块调到这里。"""
    meta = _base_write_aggregates(outdir, args, egr)
    meta["agent"] = "brainary-codex"
    meta["code_mode"] = args.code_mode
    meta["binaries"] = args.binaries_sha
    base.write_json_atomic(outdir / "run_meta.json", meta)
    return meta


# 装回 base 模块:run_one_guarded(墓碑 + 兜底聚合)按模块全局引用这两个名字,
# 换掉之后它整套崩溃防护直接为 brainary 版工作,不用另抄一份。
base.run_one = run_one
base.write_aggregates = write_aggregates


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="JSONL，列同 HuggingFace 的 SWE-bench_Pro")
    ap.add_argument("--instances", nargs="*", default=None, help="只跑这些 instance_id")
    ap.add_argument("--slice", default=None, help="形如 0:2，按顺序切一段")
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--provider", default="brainary", help="配置段名，随便起，只是个标签")
    ap.add_argument("--base-url", default="https://relay.lzbrainary.com/v1",
                    help="端点，必须支持 Responses API；容器里访问得到才行")
    ap.add_argument("--api-key", default="", help="留空则读环境变量 BRAINARY_API_KEY")
    ap.add_argument("--env-key", default="BRAINARY_API_KEY",
                    help="不想让 key 出现在命令行/history 里，改从这个环境变量读")
    ap.add_argument("--reasoning-effort", default="high",
                    help="空＝不下发，用 Codex 内置默认（实测 none）；推理模型用 high")
    ap.add_argument("--codex-bin",
                    default="brainary-codex-bin/brainary-codex-x86_64-unknown-linux-musl")
    ap.add_argument("--host-bin",
                    default="brainary-codex-bin/"
                            "brainary-codex-code-mode-host-x86_64-unknown-linux-musl")
    ap.add_argument("--code-mode", default="on", choices=["on", "only", "off"],
                    help="on=code mode + 常规工具并存；only=模型只看得到 exec/wait；"
                         "off=纯 Direct 工具面（等价官方 codex 行为，用于对照）")
    ap.add_argument("--dockerhub-username", default="jefzda")
    ap.add_argument("--platform", default="linux/amd64", help="官方镜像只有 amd64")
    ap.add_argument("--timeout", type=int, default=1800, help="单实例容器总超时（秒）")
    ap.add_argument("--pull-timeout", type=int, default=3600)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--redo-existing", action="store_true",
                    help="默认跳过已有非空 patch 的条目（续跑）；带上就全部重跑")
    ap.add_argument("--rm-image", action="store_true", help="每条跑完删镜像省磁盘")
    ap.add_argument("--no-strip-history", dest="strip_history", action="store_false",
                    help="不剥离 git 历史。⚠️ 见 run_codex_pro.py 同名参数的警告")
    ap.add_argument("--egress", default="on", choices=["on", "off"],
                    help="出网收敛，语义同 run_codex_pro.py。off 的分数不可采信")
    ap.add_argument("--subset", default="pro", help="只写进 run_meta.json，报告表头显示")
    ap.add_argument("--split", default="test", help="同上")
    ap.add_argument("-o", "--output-dir", default="results/brainary-codex")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    codex_bin = Path(args.codex_bin).resolve()
    host_bin = Path(args.host_bin).resolve()
    missing = [p for p in (codex_bin, host_bin) if not p.is_file()]
    if missing:
        sys.exit("找不到二进制：\n  " + "\n  ".join(map(str, missing))
                 + "\n先在 brainary-codex 仓库用 musl-gcc 交叉构建，产物拷进 brainary-codex-bin/")
    args.host_bin = host_bin              # run_one 从 args 上取，签名保持与 base 一致
    args.api_key = args.api_key or os.environ.get(args.env_key, "")
    if not args.api_key:
        sys.exit(f"没有 API key：加 --api-key sk-...，或 export {args.env_key}=sk-...")
    args.binaries_sha = {codex_bin.name: sha256_prefix(codex_bin),
                         host_bin.name: sha256_prefix(host_bin)}

    rows = base.load_dataset_rows(args.dataset)
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

    n_all = len(rows)
    if not args.redo_existing:
        done = {iid for iid, r in base.read_preds(outdir).items()
                if r.get("model_patch", "").strip()}
        rows = [r for r in rows if r["instance_id"] not in done]
    skipped = n_all - len(rows)

    # 出网收敛:建网 + 起 relay + 真实镜像里过探针,不通过拒绝开跑(全部复用 base 的套路)
    egr = None
    if rows and args.egress == "on":
        try:
            egr = egress.ensure_egress(args.base_url)
        except egress.EgressError as exc:
            print(exc, file=sys.stderr)
            return 1
        img0 = base.get_dockerhub_image_uri(rows[0]["instance_id"], args.dockerhub_username,
                                            rows[0].get("repo", ""))
        base.ensure_image(img0, args.platform, args.pull_timeout)
        ok, why = egress.selftest(egr, img0)
        if not ok:
            print(f"===EGRESS_FAIL=== 出网自检没过（{why}）——防护没生效，拒绝开跑。\n"
                  f"  relay={egr['relay']} 钉死 {egr['label']}\n"
                  f"  看日志：docker logs {egr['relay']}\n"
                  f"  只是想跑通就加 --egress off（会记进 run_meta.json）。", file=sys.stderr)
            return 1
        print(f"[egress] {egr['label']} via {egr['relay']}（自检通过）", flush=True)
    elif args.egress == "off":
        print("⚠️ --egress off：agent 能自由出网，可直接抄 gold fix，分数不可采信。", flush=True)

    print(f"[run] {len(rows)}/{n_all} instances（跳过已完成 {skipped}）, "
          f"agent=brainary-codex code_mode={args.code_mode}, model={args.model}, "
          f"workers={args.workers}", flush=True)
    print(f"[run] 二进制: {args.binaries_sha}", flush=True)

    lock = threading.Lock()
    if rows:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(lambda r: base.run_one_guarded(r, args, codex_bin, outdir, lock, egr),
                        rows))

    meta = write_aggregates(outdir, args, egr)
    t = meta["totals"]
    n_ok = sum(1 for r in base.read_preds(outdir).values() if r.get("model_patch", "").strip())
    print(f"[done] 非空 patch {n_ok}/{len(meta['instances'])} · "
          f"{t['turns']} turns · 输入 {t['input_tokens']:,}（缓存 {t['cached_input_tokens']:,}）"
          f" · 输出 {t['output_tokens']:,} · 推理累计 {t['seconds'] / 3600:.1f} 小时")
    n_leak = meta["egress_failed_instances"]
    if n_leak:
        print(f"  ===EGRESS_FAIL=== {n_leak}/{len(meta['instances'])} 条出网自检没过，"
              f"这些结果不可采信（逐条看 run_meta.json 的 instances[*].egress_why）",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
