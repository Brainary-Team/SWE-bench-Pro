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
  2. 工具面:code mode(exec()/wait)与 bash 等基础工具并存,靠 model_catalog_json。
     ⚠️ gpt-5.6-sol 在二进制内置模型目录里自带 tool_mode=code_mode_only,而模型元数据
     的优先级高于 features 开关(core/tools/mod.rs 的 requested_tool_mode 先读
     model_info.tool_mode)——所以只开 features.code_mode=true 时模型仍然只看得到
     exec()/wait 两个工具,shell/apply_patch 全被藏掉,bash 只能从 JS 里绕。唯一能改
     写这份元数据的杠杆是顶层配置 model_catalog_json(整体替换进程内模型目录)。本脚本
     按 --code-mode 从 brainary-codex-bin/models.json(与二进制同 commit 拷出)派生一份
     目录挂进容器:on 把 code_mode_only 改成 code_mode(exec() 与 bash/apply_patch 并存),
     off 改成 direct(纯基础工具,对照组),only 不动(官方元数据原样,只有 exec()/wait)。
  3. POA(Program of Agent):让模型在跑题过程中自己编写 JS 编排程序并执行,在程序里
     spawn/wait 子 agent。gpt-5.6-sol 元数据带 multi_agent_version=v2,但 v2 的
     collaboration__* 工具默认 non_code_mode_only=true → 只在直连工具面可见,code mode
     的 JS 里调不到。下发 features.multi_agent_v2.non_code_mode_only=false 后,exec()
     里的 JS 能直接调 tools.collaboration__spawn_agent 等(fork 对 CodeMode 消息走明文
     的补丁正是为这条路径打的)。子 agent 并发上限 --max-agents(v2 语义:含 root)。
     子 agent 的模型调用走同一个 provider/relay,出网钉死对它们同样生效。
     ⚠️ POA 与 --ephemeral 不共存:spawn_agent 默认 full-history fork 要读 parent
     线程落盘的 rollout,--ephemeral 不落盘 → 每次 spawn 必失败(collab spawn
     failed: no thread with id)。所以本脚本不加 --ephemeral(见 build_codex_cmd
     注释;早期带着它的 poa-* smoke,trace 里都只有 root 一个线程。brainary-*-probe
     那两轮 4 线程不是反证:探针提示词显式传了 fork_turns:"none",绕开了读盘路径)。
  4. 默认模型配置指向公司 relay:gpt-5.6-sol @ https://relay.lzbrainary.com/v1,
     reasoning effort high,wire_api=responses。web_search 照旧关死(服务端执行,
     搜得到上游真实 fix,机理见 run_codex_pro.build_codex_cmd 的注释)。
  5. 执行过程留痕:fork 的 rollout-trace(CODEX_ROLLOUT_TRACE_ROOT)落在 logs/<iid>.trace/。
     stdout 的 --json 事件流对 code mode 是「盲」的——exec() 单元不出现,JS 里编排的
     嵌套工具调用与直连调用无法区分(CommandExecutionSource 没有 CodeMode 变体)。
     trace bundle 才有全量:code_cell_started 带 JS 源码,tool_call_started 的
     requester.runtime_cell_id 把子工具挂回所在 exec 单元,inference 响应 payload 里有
     思考/消息。make_report.py 优先读 trace 渲染执行过程(LLM输出/工具/子工具/思考/信息
     五类标签),没有 trace 的旧日志退回 --json 流。默认裁掉 inference request payload
     (逐 turn 全量 prompt,O(turns²) 大小,报告用不上);--trace full 保留。
     token 消耗同理以 trace 为准(parse_trace_usage):stdout 的 turn.completed 只有
     root session 自己的账,子 agent 的消耗不在里面,而且 root turn 收尾失败(如 429)
     时整轮为 0;trace 聚合全部线程的 inference_completed,_meta 里 usage_source 自述
     用的哪套账,agent_threads 记录线程数(含 root)。

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
import shutil
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
# 派生出的模型目录在容器里的挂载点(model_catalog_json 要求绝对路径)
INNER_CATALOG = "/tmp/model_catalog.json"
# rollout-trace 落点(容器内)。fork 在 CODEX_ROLLOUT_TRACE_ROOT 下建
# trace-<uuid>-<thread_id>/{manifest.json, trace.jsonl, payloads/N.json},
# 子 agent 线程写进同一个 bundle(带 thread_id/agent_path 区分)。
INNER_TRACE = "/tmp/rollout-trace"

# --code-mode → 派生目录里 tool_mode 的目标值。only 不覆盖(None):
# 官方元数据对 gpt-5.6-sol 本来就是 code_mode_only,这就是 only 想要的。
TOOL_MODE_OVERRIDE = {"on": "code_mode", "only": None, "off": "direct"}


def build_model_catalog(args, models_json: Path) -> str | None:
    """按 --code-mode 从 vendored models.json 派生 model_catalog_json 的内容。

    动机见文件头第 2 点:模型元数据里的 tool_mode 压过 features 开关,gpt-5.6-sol
    自带 code_mode_only → 模型只看得到 exec()/wait。model_catalog_json 是唯一能
    覆盖这份元数据的配置。只改 tool_mode 一个字段,context_window /
    multi_agent_version=v2 / 模型指令模板等其余元数据原样保留(整份文件都是从
    构建二进制的同一 commit 拷出来的,serde 的 deny_unknown_fields 不会炸)。
    """
    target = TOOL_MODE_OVERRIDE[args.code_mode]
    if target is None:
        return None
    catalog = json.loads(models_json.read_text())
    for m in catalog.get("models", []):
        # 只动声明了 code_mode_only 的条目。tool_mode 缺省(None)的条目本来就由
        # features 开关决定,不需要覆盖;direct 的条目保持官方语义。
        if m.get("tool_mode") == "code_mode_only":
            m["tool_mode"] = target
    if args.model not in {m.get("slug") for m in catalog.get("models", [])}:
        print(f"⚠️ {args.model} 不在 {models_json.name} 里:tool_mode 覆盖对它不生效,"
              f"将走 fallback 元数据(tool_mode=None,由 features 开关决定)",
              file=sys.stderr)
    return json.dumps(catalog)


def build_codex_cmd(args) -> str:
    """与 run_codex_pro 同构;差异只有:入口叫 brainary-codex,多了工具面/POA 的配置下发。"""
    p = args.provider
    parts = [
        "brainary-codex", "exec",
        "--json",
        # ⚠️ 与 run_codex_pro 版差一个 --ephemeral,是特意去掉的,别「对齐」回来:
        # v2 的 collaboration__spawn_agent 默认 fork_turns="all"(full-history fork),
        # spawn 时要从 thread store 读 parent 线程落盘的历史(fork 源码:
        # agent/control/spawn.rs 的 spawn_forked_thread → read_stored_thread);
        # --ephemeral 不落盘 → ThreadNotFound → 每次 spawn 都报
        # "collab spawn failed: no thread with id: <root 自己的 id>",POA 只剩
        # 显式 fork_turns:"none" 一条活路(早期 brainary-*-probe 的 4 线程就是
        # 这么侥幸通过的),模型自然编排走默认值必败(A/B 实测:带 --ephemeral
        # 1 个 thread_started,去掉后 root+子 agent,子 agent 结果能被 wait 到)。
        # ephemeral 本来防的是往 ~/.codex/sessions 落盘,这里 CODEX_HOME=
        # /opt/codexhome 在容器里,docker run --rm 退出即销毁,去掉没有代价。
        "--skip-git-repo-check",
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
    if args.catalog_text is not None:
        # 覆盖进程内模型目录,把 gpt-5.6-sol 的 tool_mode 从 code_mode_only 改掉
        # (见 build_model_catalog)。这是模型能看到 bash 等基础工具的关键。
        parts += ["-c", f"model_catalog_json={INNER_CATALOG}"]
    if args.code_mode == "on":
        # features 开关只对「不在模型目录里」的模型生效(fallback 元数据 tool_mode=None);
        # 在目录里的模型(如 gpt-5.6-sol)由上面的 catalog 覆盖决定。两个都下发,
        # 换模型跑分时行为才一致。CodeModeHost 让 JS 跑在独立 host 进程
        # (不开 host 的话 thread manager 给 DisabledCodeModeSessionProvider,
        # code mode 会静默退回 Direct 工具面,等于白挂了 host 二进制)
        parts += ["-c", "features.code_mode=true", "-c", "features.code_mode_host=true"]
    elif args.code_mode == "only":
        # CodeModeOnly 隐含 CodeMode,模型只看得到 exec/wait,无静默回退
        parts += ["-c", "features.code_mode_only=true", "-c", "features.code_mode_host=true"]
    if args.code_mode != "off":
        # POA:v2 的 collaboration__* 工具默认 non_code_mode_only=true,只在直连
        # 工具面可见;关掉之后 exec() 里的 JS 才能 tools.collaboration__spawn_agent
        # 编排子 agent —— 「模型自己写 POA 程序并执行」靠的就是这一条。
        parts += ["-c", "features.multi_agent_v2.non_code_mode_only=false"]
        # v2 语义:并发线程数**含 root**,内置默认 4(=root+3 个子 agent)
        parts += ["-c", f"features.multi_agent_v2.max_concurrent_threads_per_session={args.max_agents}"]
    if args.reasoning_effort:
        parts += ["-c", f"model_reasoning_effort={shlex.quote(args.reasoning_effort)}"]
    parts += ["-m", shlex.quote(args.model), "-", "< /tmp/prompt.txt"]
    return " ".join(parts)


def prune_trace_requests(trace_dir: Path) -> None:
    """把 trace bundle 里 inference 的 request payload 删掉,只留报告要用的部分。

    request payload 是发给模型的完整请求体——每个 turn 带全量对话前缀,O(turns²)
    字符,几十个 turn 就是几十 MB/条。报告只消费 response payload(思考/消息)与
    tool 的 invocation/result payload。inference_failed / inference_cancelled 的
    partial_response_payload 是排障证据,留着。trace.jsonl 里的事件行原样保留:
    只删 payloads/ 下的文件,引用悬空由读方容错(make_report 按「文件存在才读」)。
    """
    for bundle in trace_dir.glob("trace-*"):
        tj = bundle / "trace.jsonl"
        if not tj.is_file():
            continue
        for line in tj.read_text(errors="replace").splitlines():
            try:
                p = json.loads(line).get("payload") or {}
            except json.JSONDecodeError:
                continue
            if p.get("type") not in ("inference_started", "compaction_request_started"):
                continue
            ref = p.get("request_payload") or {}
            rel = ref.get("path", "")
            # payload 路径是 bundle 相对路径("payloads/N.json"),不出 bundle 目录
            if rel.startswith("payloads/") and ".." not in rel:
                (bundle / rel).unlink(missing_ok=True)


def parse_trace_usage(trace_dir: Path) -> dict | None:
    """从 trace bundle 聚合**全部线程**的 token 消耗,替代 stdout 口径。

    stdout 的 turn.completed.usage 只有 root session 自己的账:TokenCount 事件
    读的是本 session 的状态,子 agent 线程各记各的,从不聚合(Verified 侧实测
    对账:root stdout 报的数与 trace 里 root 线程逐次推理之和分毫不差,子 agent
    的完全不在里面)。POA 一开子 agent,parse_usage 就必然漏记;root turn 收尾
    撞上 429(turn.failed)时更是整轮记 0——两个坑都在这里补上。

    账本来源:inference_completed → response payload 里每次推理都带 token_usage
    (字段与 USAGE_FIELDS 同名),覆盖所有线程。inference_failed 没有 usage
    (流断在计费信息之前),不计。--trace on 只裁 request payload,response
    原样保留,不影响这里;--trace off 没有 bundle → 返回 None,调用方退回
    stdout 口径(_meta 里 usage_source 字段自述用的哪套账)。

    turns 口径:root 线程 distinct codex_turn_id(做过推理的轮)——正常收尾时
    与「turn.completed 条数」一致,收尾失败时比它诚实(干了活就算)。
    另附 agent_threads:bundle 里的线程总数(含 root),POA 是否真的编排过
    子 agent,一眼可查。
    """
    usage = {k: 0 for k in base.USAGE_FIELDS}
    root_turns: set[str] = set()
    threads: set[str] = set()
    n_calls = 0
    for bundle in trace_dir.glob("trace-*"):
        tj = bundle / "trace.jsonl"
        if not tj.is_file():
            continue
        root_tid = None
        inferences = []
        for line in tj.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = e.get("payload") or {}
            if p.get("type") == "thread_started":
                threads.add(p.get("thread_id") or "")
                # v2 的 root 打 "/root";没有 multi-agent 时可能缺省
                if root_tid is None and p.get("agent_path") in (None, "", "/root"):
                    root_tid = p.get("thread_id")
            elif p.get("type") == "inference_completed":
                inferences.append(e)
        for e in inferences:
            rel = (e["payload"].get("response_payload") or {}).get("path", "")
            # payload 路径是 bundle 相对路径,防目录穿越(prune 同款纪律)
            if not rel.startswith("payloads/") or ".." in rel:
                continue
            f = bundle / rel
            if not f.is_file():
                continue
            try:
                tu = json.loads(f.read_text(errors="replace")).get("token_usage") or {}
            except json.JSONDecodeError:
                continue
            n_calls += 1
            for k in base.USAGE_FIELDS:
                if isinstance(tu.get(k), int):
                    usage[k] += tu[k]
            if e.get("thread_id") == root_tid and e.get("codex_turn_id"):
                root_turns.add(e["codex_turn_id"])
    if n_calls == 0:
        return None
    usage["turns"] = len(root_turns)
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    usage["agent_threads"] = len(threads)
    return usage


def dir_bytes(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0


def run_one(inst: dict, args, codex_bin: Path, outdir: Path, egr: dict | None = None) -> dict:
    """copy 自 run_codex_pro.run_one,只动五处:双二进制挂载、模型目录/trace 挂载、
    命令入口、trace 收尾、model_name_or_path。

    其余(剥历史 / 逐条出网探针 / 容器命名与超时清理 / .pred 落盘)逐行保持一致 ——
    那些行为都是踩坑踩出来的,见原文件各处注释。
    """
    iid = inst["instance_id"]
    img = base.get_dockerhub_image_uri(iid, args.dockerhub_username, inst.get("repo", ""))
    prompt = base.build_prompt(inst)

    logs_dir = outdir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # 执行过程留痕(见文件头第 5 点)。挂 rw 目录进容器,重跑先清掉旧 bundle,
    # 不然一条 instance 底下攒出多个 trace-*,报告不知道该读哪个。
    trace_dir: Path | None = None
    if args.trace != "off":
        # resolve():docker -v 只认绝对路径,相对路径会被当成 volume 名拒掉
        trace_dir = (logs_dir / f"{iid}.trace").resolve()
        shutil.rmtree(trace_dir, ignore_errors=True)
        trace_dir.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "prompt.txt").write_text(prompt)
        if args.catalog_text is not None:
            (td / "model_catalog.json").write_text(args.catalog_text)

        strip = base.strip_history_script(iid) + "\n" if args.strip_history else ""
        trace_env = f"export CODEX_ROLLOUT_TRACE_ROOT={INNER_TRACE}\n" if trace_dir else ""
        inner = f"""set -uo pipefail
mkdir -p /opt/codexhome
export CODEX_HOME=/opt/codexhome
{trace_env}{egress.probe_snippet(egr)}
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
               "-v", f"{td/'run.sh'}:/tmp/run.sh:ro"]
        if args.catalog_text is not None:
            cmd += ["-v", f"{td/'model_catalog.json'}:{INNER_CATALOG}:ro"]
        if trace_dir is not None:
            cmd += ["-v", f"{trace_dir}:{INNER_TRACE}"]
        cmd += ["-e", f"{base.INNER_ENV_KEY}={args.api_key}",
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

    trace_bytes = None
    trace_usage = None
    if trace_dir is not None:
        if args.trace == "on":
            prune_trace_requests(trace_dir)
        trace_bytes = dir_bytes(trace_dir)
        trace_usage = parse_trace_usage(trace_dir)

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
            "trace_bytes": trace_bytes,
            # usage 优先用 trace 的全量账(含子 agent、含收尾失败的 turn),
            # 退回 stdout 口径只发生在 --trace off 或 bundle 读不出来时。
            "usage_source": "trace" if trace_usage is not None else "stdout",
            **(trace_usage if trace_usage is not None else base.parse_usage(out)),
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
    meta["code_mode"] = args.code_mode           # on / only / off,这轮跑的工具面自描述
    meta["tools"] = {                             # 工具面细节,事后审计「模型到底看得到什么」
        # 派生目录写进模型元数据的 tool_mode(None=没覆盖,官方元数据原样)
        "tool_mode_override": TOOL_MODE_OVERRIDE[args.code_mode],
        # exec() 里的 JS 能否 spawn 子 agent(non_code_mode_only=false 是否已下发)
        "poa_in_code_mode": args.code_mode != "off",
        # v2 语义:含 root。off 模式不下发(collaboration 保持官方默认面)
        "max_agents": args.max_agents if args.code_mode != "off" else None,
        "models_json": args.binaries_sha.get("models.json"),
    }
    meta["binaries"] = args.binaries_sha
    meta["trace"] = args.trace                    # 执行过程留痕:on(裁request)/full/off
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
    ap.add_argument("--models-json", default="brainary-codex-bin/models.json",
                    help="二进制内置模型目录的副本（与二进制同 commit 拷出），"
                         "用来派生 model_catalog_json 覆盖 tool_mode")
    ap.add_argument("--code-mode", default="on", choices=["on", "only", "off"],
                    help="on=exec() 写 POA + bash 等基础工具并存（tool_mode 覆盖成 code_mode）；"
                         "only=模型只看得到 exec/wait（官方元数据对 gpt-5.6-sol 的原样行为）；"
                         "off=纯 Direct 工具面（tool_mode 覆盖成 direct，无 code mode，对照组）")
    ap.add_argument("--max-agents", type=int, default=6,
                    help="POA 子 agent 并发上限（multi_agent v2 语义：含 root，6=root+5；"
                         "二进制内置默认 4）。--code-mode off 时不下发")
    ap.add_argument("--trace", default="on", choices=["on", "full", "off"],
                    help="执行过程留痕到 logs/<iid>.trace/（make_report 用它渲染"
                         "exec 的 JS 源码与子工具嵌套）。on=默认，跑完裁掉逐 turn 的"
                         "inference request payload（O(turns²) 大小，报告用不上）；"
                         "full=全留（排障用）；off=不留痕")
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

    # 工具面覆盖:on/off 需要派生模型目录(only 用官方元数据,不需要)
    models_json = Path(args.models_json).resolve()
    if TOOL_MODE_OVERRIDE[args.code_mode] is not None and not models_json.is_file():
        sys.exit(f"找不到 {models_json}\n从构建二进制的 brainary-codex commit 拷出:"
                 f" codex-rs/models-manager/models.json → brainary-codex-bin/models.json\n"
                 f"(没有它就没法覆盖 tool_mode,gpt-5.6-sol 会退回 code_mode_only,"
                 f"模型只看得到 exec()/wait)")
    args.catalog_text = build_model_catalog(args, models_json)

    args.api_key = args.api_key or os.environ.get(args.env_key, "")
    if not args.api_key:
        sys.exit(f"没有 API key：加 --api-key sk-...，或 export {args.env_key}=sk-...")
    args.binaries_sha = {codex_bin.name: sha256_prefix(codex_bin),
                         host_bin.name: sha256_prefix(host_bin)}
    if args.catalog_text is not None:
        args.binaries_sha["models.json"] = sha256_prefix(models_json)

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

    ov = TOOL_MODE_OVERRIDE[args.code_mode]
    print(f"[run] {len(rows)}/{n_all} instances（跳过已完成 {skipped}）, "
          f"agent=brainary-codex code_mode={args.code_mode}"
          f"(tool_mode={ov or '官方元数据'}) "
          f"poa={'on,max_agents=%d' % args.max_agents if args.code_mode != 'off' else 'off'} "
          f"trace={args.trace}, model={args.model}, workers={args.workers}", flush=True)
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
