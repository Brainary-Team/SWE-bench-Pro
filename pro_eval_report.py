#!/usr/bin/env python3
"""把 SWE-bench Pro 官方评测的产物，翻译成 SWE-bench Verified 那份 eval_report.json。

存在的理由：Pro 官方评测只吐一个 `eval_results.json`，形如 `{instance_id: true/false}` ——
连哪条测试挂了都不告诉你。而逐条测试的明细其实就躺在旁边的
`<output_dir>/<iid>/_output.json` 里（`{"tests": [{"name", "status"}]}`），
只是没人把两边缝起来。这个脚本就干这件事，缝成 Verified 的 schema，
于是 make_report.py 可以原样搬过来用，报告长得一模一样。

判定口径**照抄官方**（swe_bench_pro_eval.py:555-558）：
    passed = {t.name for t in output.tests if t.status == "PASSED"}
    resolved = (fail_to_pass | pass_to_pass) <= passed
注意是「按名字取并集再判包含」，所以同名测试出现多次时**只要有一次 PASSED 就算过**
（instance 的 Dockerfile 普遍开了 --reruns=3，重复行是常态）。按「最后一行」判会对不上账。

用法:
  python pro_eval_report.py \
      --dataset swebench_pro.jsonl \
      --run results/codex-pro \
      --eval results/eval-codex-pro \
      -o eval_pro.json
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

# fail_to_pass 里 731 条只有 9 条能被 json.loads 解开 —— 其余是 Python repr，
# 混着单双引号。必须用 ast.literal_eval，官方脚本用的是 eval()，等价。
def parse_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if not isinstance(v, str) or not v.strip():
        return []
    try:
        out = ast.literal_eval(v)
    except (ValueError, SyntaxError):
        return []
    return [str(x) for x in out] if isinstance(out, (list, tuple)) else []


def load_dataset(path: Path) -> dict[str, dict]:
    rows = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows[r["instance_id"]] = r
    return rows


def read_pred(run: Path, iid: str) -> dict:
    p = run / iid / f"{iid}.pred"
    if not p.is_file():
        print(f"[warn] {iid}: 没有 .pred，当空 patch 处理")
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        # 不能静默：读不出来会被当成空 patch，报告里表现成「模型什么都没写」，
        # 而真相是文件坏了。宁可吵一句。
        print(f"[warn] {iid}: .pred 不是合法 JSON（{e}），当空 patch 处理")
        return {}


def load_prefixes(evaldir: Path) -> dict[str, str]:
    """从评测目录的 patches.json 里读每条实例用的 prefix。

    prefix 决定评测产物叫 `<prefix>_output.json` 还是别的。官方支持多个模型共用一个
    output_dir（各自一个 prefix），所以同一个实例目录里可能躺着好几套结果。
    不认 prefix 直接 glob 取第一个，就会把别人家的成绩安到自己头上。
    """
    f = evaldir / "patches.json"
    if not f.is_file():
        return {}
    try:
        recs = json.loads(f.read_text())
    except json.JSONDecodeError:
        return {}
    return {r["instance_id"]: r.get("prefix", "")
            for r in recs if isinstance(r, dict) and r.get("instance_id")}


def find_output(evaldir: Path, iid: str, prefix: str | None) -> tuple[dict | None, float, Path | None]:
    """读逐条测试明细、评测耗时估计值、以及评测那边存的 patch 快照路径。

    耗时：官方评测不记时。`<prefix>_patch.diff` 是起容器**前**写的、`<prefix>_output.json`
    是回收结果时写的，两者的 mtime 差约等于这条评测容器的墙钟。注意它是**文件时间戳之差**，
    不是计时器：整个目录被 cp（非 -p）搬过之后这个数就没意义了。
    """
    d = evaldir / iid
    if not d.is_dir():
        return None, 0.0, None

    if prefix is None:
        outs, diffs = sorted(d.glob("*_output.json")), sorted(d.glob("*_patch.diff"))
        if len(outs) > 1:
            print(f"[warn] {iid}: 有 {len(outs)} 套评测产物却不知道该认哪个 prefix，"
                  f"取了 {outs[0].name}。给 --prefix 或保证 {evaldir}/patches.json 在")
    else:
        outs = [p for p in [d / f"{prefix}_output.json"] if p.is_file()]
        diffs = [p for p in [d / f"{prefix}_patch.diff"] if p.is_file()]
    if not outs:
        return None, 0.0, None
    try:
        data = json.loads(outs[0].read_text())
    except json.JSONDecodeError as e:
        print(f"[warn] {iid}: {outs[0].name} 不是合法 JSON（{e}）")
        return None, 0.0, None
    secs = 0.0
    if diffs:
        secs = max(0.0, round(outs[0].stat().st_mtime - diffs[0].stat().st_mtime, 1))
    return data, secs, (diffs[0] if diffs else None)


def build_instance(iid: str, row: dict, run: Path, evaldir: Path,
                   official: dict[str, bool] | None, prefix: str | None) -> tuple[dict, str]:
    pred = read_pred(run, iid)
    patch = pred.get("model_patch", "")
    f2p, p2p = parse_list(row.get("fail_to_pass")), parse_list(row.get("pass_to_pass"))

    # 键序与 Verified 的 local_eval.py 一致，纯粹为了两边的 JSON 能直接 diff。
    res = {
        "instance_id": iid,
        "repo": row.get("repo", ""),
        "base_commit": row.get("base_commit", ""),
        "image": row.get("dockerhub_tag", ""),
        "test_cmd": " ".join(parse_list(row.get("selected_test_files_to_run"))),
        "patch_chars": len(patch),
        "model_patch": patch,
        "fail_to_pass": f2p,
        "pass_to_pass": p2p,
    }

    if not patch.strip():
        # 与 Verified 的空 patch 分支逐字对齐：不进评测，也不编测试结果。
        if official and official.get(iid):
            print(f"[warn] {iid}: patch 是空的，官方却判 resolved —— 多半是 --run 和 --eval "
                  f"不是同一轮的产物")
        res.update(patch_applied=False, resolved=False, note="empty_patch", tests=[])
        res["model_name_or_path"] = pred.get("model_name_or_path", "")
        return res, "empty_patch"

    output, secs, snap = find_output(evaldir, iid, prefix)
    if output is None:
        # 评测容器没跑出 output.json：拉不到镜像、git apply 失败、跑测试崩了、解析器炸了 ——
        # 官方那边这几种情况一律记 false，分不开。这里统一记成「补丁没落地」，
        # 真正的原因得去 <eval>/<iid>/<prefix>_stdout.log、_stderr.log 和评测那轮的
        # 终端输出里看。
        res.update(patch_applied=False, resolved=bool((official or {}).get(iid, False)),
                   note="no_eval_output", tests=[], seconds=secs)
        res["model_name_or_path"] = pred.get("model_name_or_path", "")
        return res, "no_eval_output"

    # 评测那边起容器前会把收到的 patch 原样存一份快照。拿它和推理产物比字节，
    # 就能识破官方那个「静默续跑」：<prefix>_output.json 已存在时它连容器都不起，
    # 直接返回旧结果，既不看时间戳也不看 patch 变没变（swe_bench_pro_eval.py:165-175）。
    # 不比这一下，换了 agent 重跑一轮却复用旧 --output-dir，两轮会给出一模一样的分数。
    stale = False
    if snap is not None:
        try:
            stale = snap.read_text() != patch
        except OSError:
            stale = False
        if stale:
            print(f"[warn] {iid}: 评测存的 patch 与推理产物对不上 —— 这条大概率是"
                  f"旧结果被复用了（评测换个干净的 --output-dir，或加 --redo 重跑）")

    rows = output.get("tests") or []
    passed = {t.get("name") for t in rows if t.get("status") == "PASSED"}
    # 非 PASSED 的状态（FAILED / SKIPPED / ERROR）留最后一次出现的，只用于展示。
    other = {t.get("name"): t.get("status") for t in rows if t.get("status") != "PASSED"}

    tests = []
    for name in f2p + p2p:
        status = "PASSED" if name in passed else other.get(name, "MISSING")
        tests.append({
            "name": name,
            # Verified 的规则原样保留：pytest 的 `path::test` 取后半段；
            # Pro 的名字是 `file | title`（JS）或裸标识符（Go），没有 `::`，等于原样。
            "short": name.split("::", 1)[-1],
            "group": "FAIL_TO_PASS" if name in set(f2p) else "PASS_TO_PASS",
            "status": status,
            "detail": "",   # Pro 的 parser 只给状态，不给失败信息
        })

    resolved = (set(f2p) | set(p2p)) <= passed
    if official is not None and iid in official and official[iid] != resolved:
        print(f"[warn] {iid}: 官方判 {official[iid]}，本地重算 {resolved}，以官方为准")
        resolved = official[iid]

    res.update(
        # ⚠️ 口径与 Verified 不同，报告里那一列在 Pro 下读作「补丁确实进了这一轮评测」。
        # Verified 能从 harness 输出里看到 `git apply` 到底成没成功；Pro 不行 ——
        # 它的 entryscript 没有 set -e，git apply 的输出又只进容器 stdout，
        # 而容器是 detach+remove 起的，日志当场就没了。所以这里只能判两件事：
        # 评测出了结果，且它手上那份 patch 就是这一轮推理产的。
        patch_applied=not stale,
        resolved=bool(resolved),
        f2p_all_passed=all(t["status"] == "PASSED" for t in tests if t["group"] == "FAIL_TO_PASS"),
        p2p_all_passed=all(t["status"] == "PASSED" for t in tests if t["group"] == "PASS_TO_PASS"),
        seconds=secs,
        tests=tests,
    )
    if stale:
        res["note"] = "stale_eval_output"
    res["model_name_or_path"] = pred.get("model_name_or_path", "")
    return res, "stale_eval_output" if stale else "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="fetch_dataset.py 产出的 JSONL")
    ap.add_argument("--run", required=True, help="推理输出目录（含 preds.json / logs/ / <iid>/）")
    ap.add_argument("--eval", required=True, help="评测输出目录（含 eval_results.json）")
    ap.add_argument("--prefix", default=None,
                    help="评测产物的文件名前缀；留空自动从 <eval>/patches.json 读")
    ap.add_argument("--subset", default="pro")
    ap.add_argument("--split", default="test")
    ap.add_argument("-o", "--output", default="eval_pro.json")
    a = ap.parse_args()

    rows = load_dataset(Path(a.dataset))
    run, evaldir = Path(a.run), Path(a.eval)
    prefixes = {} if a.prefix is not None else load_prefixes(evaldir)

    official = None
    f = evaldir / "eval_results.json"
    if f.is_file():
        try:
            official = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            # 别因为一个坏文件把整份报告丢掉 —— 退回本地重算，吵一句就行。
            print(f"[warn] {f} 不是合法 JSON（{e}），resolved 全部按本地重算")
    else:
        print(f"[warn] 没有 {f}，resolved 全部按本地重算")

    # 报告的分母以「推理跑了哪些条」为准，不用 eval_results.json 的长度 ——
    # 官方脚本会把对不上号的实例在跑之前就丢掉，用它当分母会把成绩算高。
    iids = [d.name for d in sorted(run.iterdir())
            if d.is_dir() and d.name != "logs" and (d / f"{d.name}.pred").is_file()]
    if not iids:
        raise SystemExit(f"{run} 下没找到任何 <iid>/<iid>.pred")

    insts, notes = [], {}
    for iid in iids:
        if iid not in rows:
            print(f"[warn] {iid} 不在数据集里，跳过")
            continue
        pfx = a.prefix if a.prefix is not None else prefixes.get(iid)
        r, note = build_instance(iid, rows[iid], run, evaldir, official, pfx)
        insts.append(r)
        notes[note] = notes.get(note, 0) + 1

    preds_path = run / "preds.json"
    rep = {
        "subset": a.subset,
        "split": a.split,
        # 报告脚本按这个路径的**同级目录**去找 run_meta.json 和 logs/，别写成绝对路径以外的东西
        "predictions_path": str(preds_path),
        "model": insts[0].get("model_name_or_path", "") if insts else "",
        "total": len(insts),
        "resolved": sum(1 for r in insts if r["resolved"]),
        "tests_total": sum(len(r["tests"]) for r in insts),
        "tests_passed": sum(1 for r in insts for t in r["tests"] if t["status"] == "PASSED"),
        "instances": insts,
    }
    Path(a.output).write_text(json.dumps(rep, indent=2, ensure_ascii=False))

    pct = rep["resolved"] / rep["total"] * 100 if rep["total"] else 0.0
    print(f"已生成 {a.output}：Resolved {rep['resolved']}/{rep['total']}（{pct:.1f}%）· "
          f"测试 {rep['tests_passed']}/{rep['tests_total']} · "
          + " · ".join(f"{k} {v}" for k, v in sorted(notes.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
