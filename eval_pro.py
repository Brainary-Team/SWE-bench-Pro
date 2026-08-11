#!/usr/bin/env python3
"""在项目根目录调用官方评测脚本的薄包装：收补丁 + 切目录 + 透传。

存在的两个理由：

① `SWE-bench_Pro-os/swe_bench_pro_eval.py` 里
   `dockerfiles/{base,instance}_dockerfile/<iid>/Dockerfile` 是**相对 CWD** 读的
   （没有参数、没有 __file__ 锚点），所以它必须在官方仓库目录里跑。
   本包装把路径参数全转成绝对路径，再切到那个目录去调 —— 产出物留在项目根目录，
   官方仓库一个字节都不用动。

② 官方评测吃的是一个 patches JSON，得先用 helper_code/gather_patches.py 从
   推理产物里收一遍。这里直接 import 那个函数收好，省掉一个手工步骤。

用法（在项目根目录）：
  .venv/bin/python eval_pro.py \
      --dataset swebench_pro.jsonl \
      --run results/codex-pro \
      --output-dir results/eval-codex-pro \
      --workers 4

其余参数原样透传给官方脚本，例如 --redo / --block_network。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / "SWE-bench_Pro-os"

# 只 import，不写入 —— dont_write_bytecode 必须在 import 之前设，
# 否则会在纯净的官方仓库里落一个 helper_code/__pycache__。
sys.dont_write_bytecode = True
sys.path.insert(0, str(UPSTREAM / "helper_code"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="fetch_dataset.py 产出的 JSONL")
    ap.add_argument("--run", help="推理输出目录，自动从里面收补丁")
    ap.add_argument("--patches", help="直接给 patches JSON，给了就不再收")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--dockerhub-username", default="jefzda")
    ap.add_argument("--prefix", default="codex", help="评测产物的文件名前缀")
    ap.add_argument("--modal", action="store_true",
                    help="用 Modal 跑；默认本机 Docker（--use_local_docker）")
    args, passthrough = ap.parse_known_args()

    script = UPSTREAM / "swe_bench_pro_eval.py"
    if not script.is_file():
        sys.exit(f"找不到官方评测脚本：{script}（先跑 ./setup.sh 把它 clone 下来）")

    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.patches:
        patches = Path(args.patches).resolve()
    else:
        if not args.run:
            sys.exit("--run 和 --patches 至少给一个")
        from gather_patches import gather_patches_from_local  # noqa: E402
        recs = gather_patches_from_local(str(Path(args.run).resolve()), args.prefix)
        if not recs:
            sys.exit(f"{args.run} 里没收到任何补丁")
        n_empty = sum(1 for r in recs if not r["patch"].strip())
        patches = out / "patches.json"
        patches.write_text(json.dumps(recs, indent=2, ensure_ascii=False))
        print(f"[eval] 收到 {len(recs)} 条补丁（空 patch {n_empty} 条）→ {patches}", flush=True)

    cmd = [
        sys.executable, str(script),
        f"--raw_sample_path={Path(args.dataset).resolve()}",
        f"--patch_path={patches}",
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
