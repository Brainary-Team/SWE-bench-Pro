#!/usr/bin/env python3
"""把 HuggingFace 上的 SWE-bench Pro 拉成一份 JSONL，后面推理和评测都吃这一份。

为什么要落地成文件而不是每次现拉：官方评测脚本 `swe_bench_pro_eval.py` 只认
`--raw_sample_path`（CSV 或 JSONL），而且对列的**类型**很挑 —— 它拿
`eval(sample["fail_to_pass"])` 解析测试名单，所以那几列必须是「Python 字面量字符串」，
不能是真的 JSON 数组。HF 上那 16 列全是 string dtype，原样落盘就正好合规；
自己动手把它们转成 list 反而会让评测 `TypeError` 然后把整轮判成 0 分。

用法:
  python fetch_dataset.py --output swebench_pro.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# 官方公开集：731 条，只有 test 一个 split。
DATASET = "ScaleAI/SWE-bench_Pro"

# 评测脚本硬性依赖的列，缺一列就整轮静默判 false（异常被它的 bare except 吞掉）。
REQUIRED = ("instance_id", "repo", "base_commit", "fail_to_pass", "pass_to_pass",
            "before_repo_set_cmd", "selected_test_files_to_run")
# 推理侧建 prompt 要用的列（create_problem_statement 读这三个）。
PROMPT_COLS = ("problem_statement", "requirements", "interface")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=DATASET)
    ap.add_argument("--split", default="test")
    ap.add_argument("-o", "--output", default="swebench_pro.jsonl")
    a = ap.parse_args()

    from datasets import load_dataset
    ds = load_dataset(a.dataset, split=a.split)

    missing = [c for c in REQUIRED + PROMPT_COLS if c not in ds.column_names]
    if missing:
        raise SystemExit(f"数据集缺列：{missing}（拿到的是 {ds.column_names}）")

    out = Path(a.output)
    with out.open("w", encoding="utf-8") as f:
        for row in ds:
            # 原样写出，一个字段都不动 —— 列的字符串形态就是评测脚本的入参契约。
            f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")

    langs: dict[str, int] = {}
    for row in ds:
        langs[row.get("repo_language", "?")] = langs.get(row.get("repo_language", "?"), 0) + 1
    print(f"已写出 {out}（{len(ds)} 条 · "
          + " · ".join(f"{k} {v}" for k, v in sorted(langs.items(), key=lambda x: -x[1])) + "）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
