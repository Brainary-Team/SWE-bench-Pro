#!/usr/bin/env python3
"""把这一轮 SWE-bench Pro 的产出汇成 Markdown 报告（格式对齐 SWE-bench verified 那份）。

数据来自四处，各管一段，别混用：
  results/<run>/*/*.pred          每条实例的耗时/patch/Codex 自己的 token 账
  results/<eval>/eval_results.json 官方评测的 Resolved 判定
  logs/usage/usage.jsonl          record_proxy 抓的**上游真实 usage**，是记账的唯一真相
                                  （Codex 把 cache_creation 折进了 input_tokens，
                                   拿不到「缓存写入」这一档，而它按 1.25× 计价）
  docker system df -v             镜像分层，算磁盘

用法：.venv/bin/python make_report.py > REPORT-relay-opus5-pro30.md
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "SWE-bench_Pro-os" / "helper_code"))
from image_uri import get_dockerhub_image_uri  # noqa: E402

DATASET = ROOT / "pro30.jsonl"
FULL = ROOT / "swe_bench_pro_full.jsonl"
RUN = ROOT / "results" / "relay-opus5-pro30"
EVAL = ROOT / "results" / "eval-relay-opus5-pro30" / "eval_results.json"
USAGE = ROOT / "logs" / "usage" / "usage.jsonl"

N_FULL = 731          # SWE-bench Pro 全量条数
TIMEOUT_S = 2400      # 本轮的单实例超时；超过它的耗时一定被宿主机休眠污染了

# 各模型官方 API 价格，美元 / 百万 token。claude-opus-5 一栏经 claude-api skill 核对：
# 输入 $5、输出 $25，缓存写入 = 1.25×输入，缓存命中 = 0.1×输入。
PRICES = {
    "deepseek-v4-flash": (0.14, None, 0.0028, 0.28),
    "kimi-k3":           (3.00, None, 0.30, 15.00),
    "claude-opus-5":     (5.00, 6.25, 0.50, 25.00),
    "gpt-5.6-sol":       (5.00, 6.25, 0.50, 30.00),
}


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.open() if l.strip()]


def fmt(n) -> str:
    return f"{n:,}" if isinstance(n, int) else f"{n:,.1f}"


def relay_usage() -> dict:
    """上游真实 usage 合计 —— 唯一可信的记账口径。"""
    t = {"miss": 0, "write": 0, "read": 0, "out": 0, "n": 0, "err": 0}
    for line in USAGE.open():
        r = json.loads(line)
        t["n"] += 1
        if r["status"] != 200:
            t["err"] += 1
            continue
        t["miss"] += r["input_tokens"]
        t["write"] += r["cache_creation_input_tokens"]
        t["read"] += r["cache_read_input_tokens"]
        t["out"] += r["output_tokens"]
    return t


def disk_by_repo(rows: list[dict]) -> dict[str, tuple[int, float, float, float]]:
    """从 docker system df -v 抠出这 30 个镜像的 SIZE/SHARED/UNIQUE，按仓库聚合。"""
    want = {get_dockerhub_image_uri(r["instance_id"], "jefzda", r["repo"]): r["repo"]
            for r in rows}
    try:
        out = subprocess.check_output(["docker", "system", "df", "-v"], text=True)
    except Exception:
        return {}

    def gb(s: str) -> float:
        m = re.match(r"([\d.]+)([KMG]B)$", s)
        return float(m.group(1)) * {"KB": 1e-6, "MB": 1e-3, "GB": 1}[m.group(2)] if m else 0.0

    agg: dict[str, list] = defaultdict(list)
    for line in out.splitlines():
        if "jefzda/sweap-images" not in line:
            continue
        p = line.split()
        uri = f"{p[0]}:{p[1]}"
        if uri not in want:
            continue
        agg[want[uri]].append((gb(p[-4]), gb(p[-3]), gb(p[-2])))
    return {k: (len(v), sum(i[0] for i in v) / len(v),
                sum(i[1] for i in v) / len(v), sum(i[2] for i in v) / len(v))
            for k, v in agg.items()}


def main() -> int:
    rows = load_jsonl(DATASET)
    meta = {r["instance_id"]: r for r in rows}
    ev = json.load(EVAL.open())
    relay = relay_usage()

    recs = []
    for f in sorted(RUN.glob("*/*.pred")):
        d = json.load(f.open())
        iid = d["instance_id"]
        m = d["_meta"]
        # 断因要分清：srvtoolu_ 出现在日志里 = 被中转注入的 web_search 打断；
        # 没有它但有 [TIMEOUT] = 单纯跑超时。两者混成一个数会把结论说歪。
        log = (f.parent / f"{iid}.log").read_bytes().decode("utf-8", "replace")
        recs.append({
            "iid": iid,
            "repo": meta[iid]["repo"],
            "lang": meta[iid]["repo_language"],
            "sec": m["seconds"],
            "turns": m["turns"],
            "patch": m["patch_chars"],
            "inp": m["input_tokens"],
            "cached": m["cached_input_tokens"],
            "out": m["output_tokens"],
            "pull": m["pull_seconds"],
            "resolved": bool(ev.get(iid, False)),
            # turns==0 ⇒ Codex 没吐出任何 turn.completed，这条的 token 账是空的
            "broken": m["turns"] == 0,
            "link_broken": "srvtoolu_" in log,
            "hit_timeout": "[TIMEOUT]" in log,
            # 超过单实例超时的耗时只可能来自宿主机休眠（monotonic 冻结、wall clock 照走）
            "dirty_time": m["seconds"] is not None and m["seconds"] > TIMEOUT_S,
        })
    recs.sort(key=lambda r: (r["repo"], r["iid"]))

    n = len(recs)
    resolved = sum(r["resolved"] for r in recs)
    clean = [r for r in recs if not r["dirty_time"] and r["sec"] is not None]
    P = []                                        # 报告正文
    A = P.append

    A("# SWE-bench Pro（中转 + bridge + Codex CLI，claude-opus-5）\n")
    A("SWE-bench Pro 官网：https://github.com/scaleapi/SWE-bench_Pro-os\n")

    # ---------------------------------------------------------------- 1
    A("## 1. SWE-bench Pro 评测原理\n")
    A("和 Verified 一样的三步，但难度上了一个台阶：\n")
    A("1. 给 agent 提供 PR 前的代码快照 + issue 正文 + **Requirements** + **New interfaces**")
    A("2. agent 根据 issue 解决问题")
    A("3. 通过 `fail_to_pass` / `pass_to_pass` 判定 —— 全部通过才算 Resolved\n")
    A("与 Verified 的关键差别：\n")
    A("| 维度 | Verified | Pro |")
    A("| --- | --- | --- |")
    A("| 规模 | 500 条 / 12 个仓库 | 731 条 / 11 个仓库 |")
    A("| 语言 | 纯 Python | Go 38% / Python 36% / JS 23% / TS 3% |")
    A("| 仓库路径 | `/testbed` | `/app` |")
    A("| Prompt | 只有 issue 正文 | issue + Requirements + New interfaces（官方 scaffold）|")
    A("| 改动量 | 多为单文件小改 | 常跨多文件、多模块 |")
    A("| 镜像 | ~1.6 GB/环境层 | **1.4–15.7 GB/条**，webclients 单条就 15.7 GB |\n")
    A("局限性与 Verified 同源（只测测试覆盖得到的、可能被过拟合、训练数据泄漏风险、"
      "PASS_TO_PASS 覆盖有边界），此处不再重复。\n")

    # ---------------------------------------------------------------- 2
    A("## 2. 本次评测链路\n")
    A("```")
    A("Codex CLI (linux musl 二进制，挂进官方镜像的 /app 里跑)")
    A("      │  OpenAI Responses 协议 (wire_api=responses)")
    A("      ▼")
    A("bridge/bridge.py codex-on-anthropic  (LiteLLM + bridge_patch)")
    A("      │  Anthropic Messages 协议")
    A("      ▼")
    A("https://relay.lzbrainary.com  →  claude-opus-5")
    A("```\n")
    A(f"- 模型 `claude-opus-5`，thinking 预算 8k（Codex 的 `model_reasoning_effort` "
      f"到不了 Anthropic，只能在桥上钉）")
    A(f"- 并发 3 worker，单实例超时 {TIMEOUT_S}s")
    A("- 推理和评测**严格分家**：推理起自己的容器，评测由官方 `swe_bench_pro_eval.py` "
      "另起干净容器跑\n")
    A("### 上量前必须先补的两个坑\n")
    A("这条链路**开箱是跑不通的**，两个坑都只有抓包才看得见：\n")
    A("**坑一：第二轮必崩的 prefill 400。** Codex 把一轮 assistant 拆成 `message` + "
      "`function_call` 两个 item，LiteLLM 原样转成两条 chat assistant 消息；邻接补丁为满足 "
      "「tool 必须紧跟 assistant」把 tool 结果提上去，那条纯文本 assistant 就被挤到了数组**最后** "
      "→ Anthropic 当成 prefill 直接 400：\n")
    A("```")
    A("This model does not support assistant message prefill.")
    A("The conversation must end with a user message.")
    A("```\n")
    A("报错在撒谎（请求确实以 user+tool_result 结尾），且第一轮永远不炸 —— 冒烟全绿、"
      "一干活就废。修法：`merge_adjacent_assistants()` 先把连续 assistant 合并成一条。\n")
    A("**坑二：缓存命中率。** LiteLLM 从 Responses 转 Anthropic 时**一个 `cache_control` 都不加**，"
      "agent 每轮都按未命中价重付整段历史。LiteLLM 自带的 `cache_control_injection_points` "
      "在这条链路上无效（system 那条会被搬进顶层 `system`、断点半路丢失；落到 `tool` 消息时写的是"
      "消息级 cache_control，转成 `tool_result` 块时同样丢失 —— 实测 9 次请求只有 1 次真带上）。"
      "改成在**最终 Anthropic body** 上自己打三个断点：\n")
    A("| 断点 | 位置 | 作用 |")
    A("| --- | --- | --- |")
    A("| `system` | `system` 末块 | 每轮都一样，稳定命中 |")
    A("| `prev` | `messages[-3]` 末块 | **主力**。Anthropic 只在本次请求显式标了 cache_control 的位置查缓存；"
      "`last` 是本轮新产生的、从没写进过缓存，必然 miss |")
    A("| `last` | `messages[-1]` 末块 | 把本轮新增写进缓存，供下一轮的 `prev` 命中 |\n")
    A("命中率实测：**无断点 23% → 只标 system+last 65% → 三个断点 96%**（单实例冒烟），"
      f"30 条全量下来 **{100*relay['read']/(relay['miss']+relay['write']+relay['read']):.1f}%**。\n")

    # ---------------------------------------------------------------- 3
    A("## 3. 评测集分布\n")
    A("从全量 731 条里按「仓库均匀」抽 30 条：8 个大仓 ×3 + 3 个小仓 ×2。"
      "仓内按 `instance_id` 排序后**等距抽样**（不是取前 N 条，否则会扎堆在同一批 PR 上），"
      "并逐条 `docker manifest inspect` 校验 tag 存在（官方数据集里确有 404 的 tag）。\n")
    byrepo = defaultdict(lambda: [0, 0])
    bylang = defaultdict(lambda: [0, 0])
    for r in recs:
        byrepo[r["repo"]][0] += 1
        byrepo[r["repo"]][1] += r["resolved"]
        bylang[r["lang"]][0] += 1
        bylang[r["lang"]][1] += r["resolved"]
    full = load_jsonl(FULL)
    fullrepo = defaultdict(int)
    for r in full:
        fullrepo[r["repo"]] += 1
    A("| 仓库 | 语言 | 全量 | 本次抽样 | Resolved |")
    A("| --- | --- | ---: | ---: | ---: |")
    for repo, (c, ok) in sorted(byrepo.items(), key=lambda x: -fullrepo[x[0]]):
        lang = next(r["lang"] for r in recs if r["repo"] == repo)
        A(f"| `{repo}` | {lang} | {fullrepo[repo]} | {c} | {ok}/{c} |")
    A(f"| **合计** | | **{len(full)}** | **{n}** | **{resolved}/{n}** |\n")
    A("语言分布（抽样 vs 全量）：" + "、".join(
        f"{k} {v[0]}/{n}={100*v[0]/n:.0f}%（全量 {100*sum(1 for r in full if r['repo_language']==k)/len(full):.0f}%）"
        for k, v in sorted(bylang.items(), key=lambda x: -x[1][0])) + "\n")

    # ---------------------------------------------------------------- 4
    A("## 4. 结果\n")
    A(f"**Resolved {resolved}/{n} = {100*resolved/n:.1f}%**"
      f"（非空 patch {sum(1 for r in recs if r['patch'] > 0)}/{n}）\n")
    broken = [r for r in recs if r["broken"]]
    link = [r for r in recs if r["link_broken"]]
    tmo = [r for r in recs if r["hit_timeout"]]
    A(f"这 {n} 条里有 {len(broken)} 条**根本没跑完**，断因分两类，别混：\n")
    A(f"**a) 中转注入 `web_search` 打断（{len(link)} 条）。** 中转会往请求里注入服务端 "
      f"`web_search` 工具，模型一旦调用，返回的 `srvtoolu_…` 块 Codex round-trip 不回去，"
      f"下一轮直接 400：\n")
    A("```")
    A("messages.20.content.0: unexpected `tool_use_id` found in `tool_result` blocks:")
    A("srvtoolu_01ScBVTv2kdNjUj355HwdEeH. Each `tool_result` block must have a")
    A("corresponding `tool_use` block in the previous message.")
    A("```\n")
    A(f"这是**确定性**打断（{len(link)} 条日志里都有 `srvtoolu_`，`rc=1`），不是偶发。"
      f"这 {len(link)} 条全部判 ❌，但责任在链路不在模型。\n")
    A(f"**b) 跑到超时（{len(tmo)} 条）。** 与 `web_search` 无关，日志里没有 `srvtoolu_`。\n")
    valid = n - len(link)
    A(f"扣掉 a) 的 {len(link)} 条链路故障，有效样本 {valid} 条里 "
      f"Resolved {resolved}/{valid} = **{100*resolved/valid:.1f}%**。"
      f"两个数字都列出来：**{100*resolved/n:.1f}%** 是这条链路当下的真实交付率，"
      f"**{100*resolved/valid:.1f}%** 是修好注入问题后能期待的水平。\n")

    A("### 逐条明细\n")
    A("| instance | 语言 | 秒 | turns | 输入 | 缓存命中 | 命中率 | 输出 | patch | 结果 |")
    A("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |")
    for r in recs:
        hit = f"{100*r['cached']/r['inp']:.1f}%" if r["inp"] else "—"
        sec = "TIMEOUT" if r["sec"] is None else (
            f"{r['sec']:.0f}*" if r["dirty_time"] else f"{r['sec']:.0f}")
        short = r["iid"].replace("instance_", "")
        short = short[:38] + "…" if len(short) > 39 else short
        A(f"| `{short}` | {r['lang']} | {sec} | {r['turns']} | {fmt(r['inp'])} | "
          f"{fmt(r['cached'])} | {hit} | {fmt(r['out'])} | {r['patch']}B | "
          f"{'✅' if r['resolved'] else '❌'} |")
    A("")
    A("`*` = 耗时被宿主机休眠污染（见「已知问题」），token / patch / 判定不受影响；"
      "`turns=0` = 没跑完，Codex 的 token 账为空（真实消耗见 §5.2 中转那一行）。\n")

    # ---------------------------------------------------------------- 5
    A("## 5. 评测资源使用情况估计\n")
    A("### 5.1 磁盘使用估计\n")
    A("Pro 的镜像**没有 Verified 那种「80 种环境层大家共用」的结构** —— 实测同仓库不同实例之间"
      "共享层很少，绝大部分是每条独有的。所以不能照搬 Verified 的算法，只能按「每条独有」累加。\n")
    dk = disk_by_repo(rows)
    if dk:
        A("| 仓库 | n | 平均 SIZE | 平均 SHARED | 平均 UNIQUE（=实际增量） |")
        A("| --- | ---: | ---: | ---: | ---: |")
        for repo, (c, s, sh, u) in sorted(dk.items(), key=lambda x: -x[1][3]):
            A(f"| `{repo}` | {c} | {s:.2f} GB | {sh:.2f} GB | **{u:.2f} GB** |")
        tot_u = sum(v[3] * v[0] for v in dk.values())
        A(f"| **本次 30 条合计** | **{sum(v[0] for v in dk.values())}** | | | "
          f"**{tot_u:.1f} GB** |\n")
        # 按仓库均值 × 全量该仓条数外推
        est = sum(dk[r][3] * fullrepo[r] for r in dk if r in fullrepo)
        A(f"按每仓 UNIQUE 均值 × 全量该仓条数外推，**全量 731 条约 {est:.0f} GB**"
          f"（Verified 500 条只要 ~230 GB —— Pro 贵在镜像上，不在 token 上）。\n")
        A(f"> 单条最贵的是 `protonmail/webclients`：**{dk['protonmail/webclients'][3]:.1f} GB/条**，"
          f"全量 65 条就要 {dk['protonmail/webclients'][3]*65:.0f} GB。"
          f"磁盘紧张时优先跳过这个仓。\n")
        A("> 实测本次 30 条把 Docker 占用从 109.6 GB 推到 ~228 GB。跑全量前先确认有 "
          "**1 TB 以上**空闲，或者边跑边 `docker image rm`。\n")

    A("### 5.2 token 和费用估计\n")
    tot_in = relay["miss"] + relay["write"] + relay["read"]
    A(f"记账口径：**以中转上游抓到的 usage 为准**（`logs/usage/usage.jsonl`，"
      f"共 {relay['n']} 次上游请求，其中 {relay['err']} 次非 200）。"
      f"不用 Codex 自己那份 —— 它把 `cache_creation` 折进了 `input_tokens`，"
      f"拿不到「缓存写入」这一档，而这档按 **1.25×** 计价。\n")
    A(f"| 口径 | 未命中输入 | 缓存写入 | 缓存命中 | 输入合计 | 输出 |")
    A(f"| --- | ---: | ---: | ---: | ---: | ---: |")
    A(f"| 中转上游（真实）| {fmt(relay['miss'])} | {fmt(relay['write'])} | "
      f"{fmt(relay['read'])} | {fmt(tot_in)} | {fmt(relay['out'])} |")
    pin = sum(r["inp"] for r in recs)
    pout = sum(r["out"] for r in recs)
    A(f"| Codex `.pred` 合计 | \\* 折进输入 | \\* 折进输入 | {fmt(sum(r['cached'] for r in recs))} | "
      f"{fmt(pin)} | {fmt(pout)} |")
    A(f"| 差额 | | | | {fmt(tot_in-pin)} | {fmt(relay['out']-pout)} |\n")
    A(f"差额来自两处：没跑完的 {len(broken)} 条（Codex 没产出 `turn.completed`，账是空的，"
      f"但请求已经真金白银发出去了），以及一个**跑飞的孤儿容器**（见「已知问题」）。"
      f"**做预算时按中转那一行算**，Codex 那份会少算 14%。\n")
    A(f"**缓存命中率 {100*relay['read']/tot_in:.1f}%**，"
      f"实付输入只有 {fmt(relay['miss']+relay['write'])} token —— "
      f"这就是 §2 那三个断点的价值。\n")

    A("#### 本次 30 条的实际费用\n")
    A("| 模型 | 实付输入 | 缓存写入 | 缓存命中 | 输出 | 合计 |")
    A("| --- | ---: | ---: | ---: | ---: | ---: |")
    for name, (pi, pw, pr, po) in PRICES.items():
        w = relay["write"] / 1e6 * (pw if pw else pi)
        c = (relay["miss"] / 1e6 * pi, w, relay["read"] / 1e6 * pr, relay["out"] / 1e6 * po)
        mark = " ⬅ 本次" if name == "claude-opus-5" else ""
        A(f"| {name}{mark} | ${c[0]:.2f} | ${c[1]:.2f} | ${c[2]:.2f} | ${c[3]:.2f} | "
          f"**${sum(c):.2f}** |")
    A("")
    A("#### 外推全量 731 条\n")
    k = N_FULL / n
    A(f"按 30 条均值 × {N_FULL} 线性外推（token 数单位：M）：\n")
    A("| 模型 | 实付输入 | 缓存写入 | 缓存命中 | 输出 | 合计 |")
    A("| --- | ---: | ---: | ---: | ---: | ---: |")
    A(f"| **token 数** | {relay['miss']*k/1e6:.2f} M | {relay['write']*k/1e6:.2f} M | "
      f"{relay['read']*k/1e6:.1f} M | {relay['out']*k/1e6:.2f} M | "
      f"{(tot_in+relay['out'])*k/1e6:.1f} M |")
    for name, (pi, pw, pr, po) in PRICES.items():
        w = relay["write"] * k / 1e6 * (pw if pw else pi)
        c = (relay["miss"] * k / 1e6 * pi, w, relay["read"] * k / 1e6 * pr,
             relay["out"] * k / 1e6 * po)
        mark = " ⬅ 本次" if name == "claude-opus-5" else ""
        A(f"| {name}{mark} | ${c[0]:.2f} | ${c[1]:.2f} | ${c[2]:.2f} | ${c[3]:.2f} | "
          f"**${sum(c):.2f}** |")
    A("")
    A("> 这是按**官方 API 价**算的。走订阅/中转的实际单价通常更低，且实际 token 消耗"
      "受 thinking 预算、`effort`、超时设置影响，无法完全锁死。\n")

    A("### 5.3 时间\n")
    if clean:
        secs = sorted(r["sec"] for r in clean)
        med = secs[len(secs) // 2]
        A(f"只统计**未被休眠污染**的 {len(clean)} 条（`seconds` ≤ 单实例超时 {TIMEOUT_S}s）：\n")
        A(f"- 中位 **{med:.0f}s**，均值 {sum(secs)/len(secs):.0f}s，"
          f"最快 {secs[0]:.0f}s，最慢 {secs[-1]:.0f}s")
    pulls = [r["pull"] for r in recs if r["pull"] > 0]
    if pulls:
        cp = [p for p in pulls if p <= TIMEOUT_S]      # 同样要剔掉跨休眠的
        A(f"- 拉镜像 {len(pulls)} 次；剔掉 {len(pulls)-len(cp)} 次跨休眠的，"
          f"其余 {len(cp)} 次中位仅 {sorted(cp)[len(cp)//2]/60:.1f} 分钟，"
          f"但长尾很重（最慢 {max(cp)/60:.1f} 分钟，webclients 那种十几 GB 的）。"
          f"这部分**不计入解题耗时**，但要计入跑完全量的墙钟预算")
    A(f"- **解题耗时不含拉镜像**：`ensure_image()` 在计时开始前跑完并单独计入 `pull_seconds`；"
      f"报告里的「秒」取容器内 `AGENT_T0/T1` 时间戳之差，只包住 `codex exec`，"
      f"不含建容器、`git reset`、收尾 `git diff`（实测这些开销 1–12s）")
    A(f"- 3 worker 并发跑完 30 条的墙钟约 {55551.9/3600:.1f} 小时（含拉镜像和被污染的那几条）\n")

    # ---------------------------------------------------------------- 6
    A("## 6. 已知问题\n")
    A("| 问题 | 影响 | 状态 |")
    A("| --- | --- | --- |")
    A(f"| 中转注入服务端 `web_search`，`srvtoolu_…` 块 Codex round-trip 不回去 | "
      f"确定性打断 **{len(link)}/{n}** 条，全判 ❌ | ❌ 未修，需中转侧关掉注入 |")
    A("| 宿主机休眠导致计时失真：macOS 休眠时 `time.monotonic()` 冻结（所以 2400s 超时没触发）"
      "而 `time.time()` 和容器内 `date` 照走 | "
      f"{sum(1 for r in recs if r['dirty_time'])} 条耗时虚高，token/patch 不受影响 | "
      "✅ 已改用 monotonic 计时 + `caffeinate` 防休眠 |")
    A("| `subprocess.run(timeout=)` 杀的是 docker **客户端**，容器还在后台跑 | "
      "1 个孤儿容器多跑了 14 小时，一直在烧中转 token 并污染记账 | "
      "✅ 已改成 `--name` + 超时后 `docker rm -f` |")
    A("| 超时被杀的实例只有 `AGENT_T0` 没有 `T1` | "
      "旧代码会退回 `wall_seconds` 冒充解题耗时，把统计拉爆 | "
      "✅ 已改成留空并标 `timed_out` |")
    A("| Codex 报 `Model metadata for claude-opus-5 not found` | "
      "只是元数据缺失的告警，功能正常 | ⚠️ 可忽略 |")
    A("")
    A("## 7. 复现\n")
    A("```bash")
    A("# 0. 选 30 条均匀分布的实例（含镜像存在性校验）")
    A(".venv/bin/python select30.py")
    A("")
    A("# 1. 起桥（三个 cache 断点 + prefill 补丁）")
    A('RELAY_KEY="sk-..." bridge/.venv/bin/python bridge/bridge.py codex-on-anthropic \\')
    A("  --base-url https://relay.lzbrainary.com --model claude-opus-5 --env-key RELAY_KEY \\")
    A("  --max-tokens 32000 --thinking-budget 8000 --cache-points system,prev,last \\")
    A("  --host 0.0.0.0 --port 4000")
    A("")
    A("# 2. 跑 agent（先跑 --slice 0:1 验一条再放量）")
    A(".venv/bin/python run_codex_pro.py --dataset pro30.jsonl \\")
    A("  --model claude-opus-5 --provider bridge \\")
    A("  --base-url http://host.docker.internal:4000/v1 --api-key sk-bridge \\")
    A("  --codex-bin /path/to/codex-x86_64-unknown-linux-musl \\")
    A(f"  --workers 3 --timeout {TIMEOUT_S} -o results/relay-opus5-pro30")
    A("")
    A("# 3. 汇总 + 官方评测")
    A(".venv/bin/python SWE-bench_Pro-os/helper_code/gather_patches.py \\")
    A("  --directory results/relay-opus5-pro30 --prefix relay-opus5 \\")
    A("  --output relay_opus5_pro30_patches.json")
    A(".venv/bin/python eval_pro.py --dataset pro30.jsonl \\")
    A("  --patches relay_opus5_pro30_patches.json \\")
    A("  --output-dir results/eval-relay-opus5-pro30 --workers 3")
    A("```")

    print("\n".join(P))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
