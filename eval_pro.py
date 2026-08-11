#!/usr/bin/env python3
"""在项目根目录调用官方评测脚本的薄包装。

存在的唯一理由：`SWE-bench_Pro-os/swe_bench_pro_eval.py` 里
`dockerfiles/{base,instance}_dockerfile/<iid>/Dockerfile` 是**相对 CWD** 读的，
所以它必须在官方仓库目录里跑。本包装把路径参数转成绝对路径，再切到那个目录去调，
这样产出物可以留在项目根目录，官方仓库一个字节都不用动。

用法（在项目根目录）：
  .venv/bin/python eval_pro.py \
      --dataset smoke2.jsonl \
      --patches codex_ds_direct_patches.json \
      --output-dir results/eval-codex-ds-direct

其余参数原样透传给官方脚本，例如 --redo / --block_network。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / "SWE-bench_Pro-os"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="JSONL/CSV，列同 HuggingFace 的 SWE-bench_Pro")
    ap.add_argument("--patches", required=True, help="gather_patches.py 产出的 JSON")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--dockerhub-username", default="jefzda")
    ap.add_argument("--modal", action="store_true",
                    help="用 Modal 跑；默认本机 Docker（--use_local_docker）")
    args, passthrough = ap.parse_known_args()

    script = UPSTREAM / "swe_bench_pro_eval.py"
    if not script.is_file():
        sys.exit(f"找不到官方评测脚本：{script}（先 git clone SWE-bench_Pro-os）")

    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(script),
        f"--raw_sample_path={Path(args.dataset).resolve()}",
        f"--patch_path={Path(args.patches).resolve()}",
        f"--output_dir={out}",
        # scripts_dir 相对官方仓库；给绝对路径省得再受 CWD 影响
        f"--scripts_dir={UPSTREAM / 'run_scripts'}",
        f"--num_workers={args.workers}",
        f"--dockerhub_username={args.dockerhub_username}",
    ]
    if not args.modal:
        cmd.append("--use_local_docker")
    cmd += passthrough

    print(f"[eval] cwd={UPSTREAM}\n[eval] {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, cwd=UPSTREAM)


if __name__ == "__main__":
    raise SystemExit(main())
