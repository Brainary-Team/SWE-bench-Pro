#!/usr/bin/env python3
"""用**宿主机**的 Codex CLI 跑 SWE-bench Pro 的推理阶段，产出与容器版逐字节同形状的产物。

与 run_codex_pro.py 的区别只有一处：codex 进程跑在哪。

  run_codex_pro.py       把 codex 的 Linux 二进制挂进官方镜像，agent 在容器里跑
  run_codex_pro_host.py  直接用宿主机装的 codex（arm64 原生），agent 在宿主机跑

为什么要这一份：不必准备 linux-musl 静态二进制（约 297 MB），也不必让 agent 在
Rosetta 模拟的 x86_64 容器里跑（它自己的进程会慢一截）。评测和报告两段一个字都不用改 ——
eval_pro.py 只认 <run>/<iid>/<iid>.pred，不关心 patch 是谁在哪生成的。

架构（铁律不变）：agent 用的容器与评测用的容器是两个，评测那个由官方 harness 自己起。

  ① 从镜像里把 /app 原样导出到宿主机 work/<iid>/app —— agent 编辑的是这份
  ② 起一条常驻容器，把这份宿主机目录 bind-mount 回 /app
     → 宿主机的编辑在容器里立刻可见，同一份文件，不需要来回同步
  ③ codex exec 在宿主机跑，-C 指向那个目录
  ④ agent 要跑测试就调 work/<iid>/sbx '<cmd>'，它 docker exec 进容器执行 ——
     依赖（ansible-test / go / node_modules …）只装在容器里，宿主机没有
  ⑤ 收 patch 用宿主机的 git（容器里 /app 属主是宿主机 uid 501，root 跑 git 会报
     dubious ownership，所以统一在宿主机侧收）

用法：
  python run_codex_pro_host.py --dataset swebench_pro.jsonl --slice 0:2 \
      --model deepseek-v4-flash --provider deepseek \
      --base-url https://api.deepseek.com/v1 --api-key sk-... \
      -o results/host-smoke
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 从容器版复用：PROMPT 是跑分对比的前提（两种模式必须逐字一致），其余几个是与
# 「agent 在哪跑」无关的公共件，重写只会引入分叉。import 它还会顺带把官方仓库的
# helper_code 挂进 sys.path（run_codex_pro 模块级干的），create_problem_statement 才拿得到。
from run_codex_pro import (
    EXCLUDES,
    INNER_ENV_KEY,
    PROMPT,
    REPO_DIR,
    STRIP_FAIL,
    STRIP_OK,
    TOTAL_FIELDS,
    USAGE_FIELDS,
    _unwrap,
    ensure_image,
    load_dataset_rows,
    parse_usage,
    read_preds,
    strip_history_script,
    with_no_cheat,
    write_json_atomic,
)
from create_problem_statement import create_problem_statement  # noqa: E402
from image_uri import get_dockerhub_image_uri  # noqa: E402

# agent 的草稿目录。放在工作区里面（sandbox 只让写工作区），但从 patch 里排除 ——
# 免得复现脚本被 git add -A 卷进 model_patch。
SCRATCH = ".agent_scratch"

# 容器版的 EXCLUDES 是拼进 shell 命令行用的，每条外面裹了一层单引号；
# 宿主机这边直接给 git 传 argv，不过 shell，得把那层壳剥掉。
# 只在这里派生、不另抄一份：那张表里 `glob` magic 的坑（见容器版注释）只能有一个出处。
PATCH_EXCLUDES = [e.strip("'") for e in EXCLUDES] + [f":(exclude,glob){SCRATCH}/**"]

# 插在 PROMPT 的 "## Workflow" 之前。宿主机没有仓库的依赖，不说清楚 agent
# 会用宿主机 python 一路撞墙。
RUNNING_CODE = """## Running code
This machine does NOT have the project's dependencies installed. Plain `python`,
`pytest`, `go test`, `npm test` etc. will fail here. To run anything against the
project's real environment — the same Linux environment your change will be graded
in — use:

    {sbx} '<shell command>'

It runs the command inside the project's prepared container, in the repository root.
The container mounts this very directory, so your edits are visible there immediately.
Examples:

    {sbx} 'python -c "import sys; print(sys.version)"'
    {sbx} 'python {scratch}/repro.py'
    {sbx} 'ls -la && git status'

Paths: the repository is {workdir} on this machine and {repo_dir} inside the
container; {sbx} rewrites the former to the latter for you, so either form works.

Put scratch files (reproduction scripts, notes) in {workdir}/{scratch}/ — that
directory is excluded from the final patch.

"""

# 宿主机路径 → 容器路径的替换是刻意加的：agent 天然会拿它眼前看到的绝对路径去拼命令，
# 而那个路径在容器里不存在。workdir 是一条又长又唯一的绝对路径，误伤概率可以忽略。
#
# ⚠️ 两处引号都是必须的，且**只能**是这个组合（macOS 是 bash 3.2，实测过）：
#   · 路径先进变量：`${cmd//pat/rep}` 的 pat 与 rep 是在**展开之前**按字面的 `/` 切开的，
#     把路径直接拼进模板，第一个 `/Users` 的斜杠就成了分隔符，pattern 变成空串。
#   · pattern 侧 `"$hostdir"` 要加引号，让展开出来的斜杠不参与切分。
#   · replacement 侧 `$repodir` **不能**加引号 —— bash 3.2 会把那对引号原样留在结果里，
#     `grep '<hostdir>'` 会被替换成 `grep '"/app"'`，语义就变了。
SBX = """#!/bin/bash
# 把命令送进本 instance 的常驻容器执行（依赖只装在那儿）。
hostdir={workdir_q}
repodir={repo_dir_q}
cmd="$*"
cmd="${{cmd//"$hostdir"/$repodir}}"
exec docker exec -w "$repodir" {container} bash -c "$cmd"
"""


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _sub(text: str, old: str, new: str) -> str:
    """替换且要求命中 —— 容器版 PROMPT 一改，这里立刻炸，而不是悄悄跑出一份分叉的 prompt。"""
    if old not in text:
        raise RuntimeError(f"run_codex_pro.PROMPT 里找不到 {old!r}："
                           "容器版 prompt 改了，宿主版的派生规则要跟着改")
    return text.replace(old, new)


def build_prompt(inst: dict, workdir: Path, sbx: Path, no_cheat: bool = True) -> str:
    """从容器版 PROMPT 派生，只多插一段「怎么跑命令」，题面拼法与容器版完全一致。

    不另写一份的原因：跑分对比的前提是任务描述逐字一致。`{repo_dir}` 本来就是占位符，
    填宿主机路径即可，不需要像 Verified 那版那样做字符串替换。

    两段外挂都插在 "## Workflow" 之前，先 NO_CHEAT 后 RUNNING_CODE ——
    容器版只有前者，共用同一个插入点，两边的措辞和顺序才对得上。
    """
    row = {**inst, **{k: _unwrap(inst.get(k, "") or "")
                      for k in ("problem_statement", "requirements", "interface")}}
    t = _sub(with_no_cheat(PROMPT, no_cheat), "## Workflow",
             RUNNING_CODE.format(sbx=sbx, workdir=workdir, scratch=SCRATCH,
                                 repo_dir=REPO_DIR) + "## Workflow")
    return t.format(problem=create_problem_statement(row), repo_dir=str(workdir))


def export_repo(img: str, platform: str, dest: Path, prep: str) -> str:
    """把镜像里的 {REPO_DIR} 导出到宿主机 dest，返回 prep 在容器里的输出。

    prep（对齐 base_commit + 可选的剥离历史）在**导出之前**于容器内执行。顺序不能反：
    teleport 那类仓库 .git 有 1.1 GB，剥完只剩 102 MB —— 先剥能少拷 1 GB，
    在 macOS 的 virtiofs 上这是分钟级的差别。

    用 docker cp（走 tar 流，保留权限/符号链接），不用 docker run 里 cp 到 bind mount
    —— 后者在 macOS 上慢一个量级。
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cid = run(["docker", "run", "-d", "--platform", platform,
               "--entrypoint", "/bin/bash", img, "-c", "sleep infinity"]).stdout.strip()
    if not cid:
        raise RuntimeError(f"起导出容器失败：{img}")
    try:
        p = run(["docker", "exec", "-w", REPO_DIR, cid, "bash", "-c", prep])
        out = p.stdout + p.stderr
        # `docker cp src/. dst` 的语义是「把 src 的内容放进 dst」，dst 必须已存在
        dest.mkdir(parents=True)
        p = run(["docker", "cp", f"{cid}:{REPO_DIR}/.", str(dest)])
        if p.returncode != 0:
            raise RuntimeError(f"docker cp 失败：{p.stderr[:400]}")
    finally:
        run(["docker", "rm", "-f", cid])

    if not (dest / ".git").is_dir():
        raise RuntimeError(f"{dest} 里没有 .git，收不到 patch")
    return out


def build_prep(iid: str, base_commit: str, strip: bool) -> str:
    """导出前在容器里跑的那段：对齐 base_commit，然后（可选）剥离 git 历史。

    ⚠️ reset 必须在 strip 之前。镜像里 HEAD 停在造数据集时的日期截断点，那是
    base_commit **之后**很远的地方；先 strip 的话 gc 会把「HEAD 可达」的东西全留下 ——
    包括 gold fix，等于白剥。
    """
    s = f"git reset --hard {base_commit}\n"
    return s + strip_history_script(iid) if strip else s


def prepare_workdir(dest: Path, base_commit: str) -> None:
    """导出之后在宿主机侧的收尾：核对基线 + 备好草稿目录。

    reset 在容器里已经做过一次，这里再做一次是**当校验用**的（幂等）——
    对不上 base_commit 意味着 patch 的基线跟评测那边不是同一个，出来的 diff
    大概率打不上，得当场喊出来而不是等评测阶段莫名其妙全挂。
    """
    p = run(["git", "-C", str(dest), "reset", "--hard", base_commit])
    if p.returncode != 0:
        print(f"  ⚠️  git reset --hard {base_commit[:12]} 失败：{p.stderr.strip()[:200]}",
              file=sys.stderr)

    # macOS 默认大小写不敏感，仓库里若有仅大小写不同的同名文件，拷过来会互相覆盖。
    # 症状是 git status 冒出几个 deleted，早点报出来比让 patch 变脏强。
    dirty = run(["git", "-C", str(dest), "status", "--porcelain"]).stdout
    if dirty.strip():
        lines = dirty.strip().splitlines()
        print(f"  ⚠️  导出后工作区就不干净（{len(lines)} 项），patch 可能带噪声："
              f"{lines[0].strip()}", file=sys.stderr)

    (dest / SCRATCH).mkdir(exist_ok=True)
    # 让草稿目录连 git status 都不出现（双保险，PATCH_EXCLUDES 已经挡了一次）
    with (dest / ".git" / "info" / "exclude").open("a") as f:
        f.write(f"\n{SCRATCH}/\n")


def start_container(img: str, platform: str, workdir: Path, name: str) -> None:
    """起常驻容器，把宿主机 workdir bind-mount 到 {REPO_DIR}。"""
    run(["docker", "rm", "-f", name])
    # 镜像默认 ENTRYPOINT 就是 bash，不覆盖的话 `sleep infinity` 会被当成脚本名。
    p = run(["docker", "run", "-d", "--name", name, "--platform", platform,
             "-v", f"{workdir}:{REPO_DIR}", "-w", REPO_DIR,
             "--entrypoint", "/bin/bash", img, "-c", "sleep infinity"])
    if p.returncode != 0:
        # 起失败也可能留下一个 created 状态的壳，占着名字让续跑起不来，顺手收掉。
        # 调用方的 try/finally 是从这个函数**之后**才铺开的，兜不到这里。
        run(["docker", "rm", "-f", name])
        raise RuntimeError(f"启动容器失败：{p.stderr[:400]}")
    # /app 属主是宿主机的 uid 501，容器里是 root → git 会拒绝操作。
    # agent 可能在容器里跑 git（如 git status），先放行。
    run(["docker", "exec", name, "git", "config", "--global",
         "--add", "safe.directory", REPO_DIR])


def build_codex_cmd(args, workdir: Path) -> list[str]:
    """与容器版 build_codex_cmd 同参，只是走 argv 不走 shell，外加两个宿主机专属开关。

    --ignore-user-config：不读 ~/.codex/config.toml（那里有 notify 钩子、插件、
    marketplace）。跑分要的是可复现，不是本地手感。
    wire_api 必须是 responses —— Codex 0.146+ 已移除 chat 支持。
    """
    p = args.provider
    cmd = ["codex", "exec",
           # --json：stdout 变 JSONL 事件流，turn.completed 里带 token 拆分
           "--json",
           "--ignore-user-config",
           "--ephemeral",                # 不往 CODEX_HOME/sessions 落盘
           "--skip-git-repo-check",
           # --ignore-user-config 管不到 AGENTS.md。全局的和仓库里的都得关掉，
           # 否则跑分被一份不受控的文档影响，而且很难发现。
           "-c", "project_doc_max_bytes=0",
           "-c", f"model_provider={p}",
           "-c", f"model_providers.{p}.name={p}",
           "-c", f"model_providers.{p}.base_url={args.base_url}",
           "-c", f"model_providers.{p}.env_key={INNER_ENV_KEY}",
           "-c", f"model_providers.{p}.wire_api=responses",
           "-s", args.sandbox,
           "-C", str(workdir)]
    if args.sandbox == "workspace-write" and args.network:
        # 不开这个 Seatbelt 会连 docker.sock 一起挡掉，agent 就跑不了测试（实测）
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    if args.reasoning_effort:
        cmd += ["-c", f"model_reasoning_effort={args.reasoning_effort}"]
    cmd += ["-m", args.model, "-"]        # prompt 从 stdin 读，避免超长参数
    return cmd


def collect_patch(workdir: Path) -> str:
    """在宿主机侧收 patch。用 --cached 是为了能带上新增文件。"""
    run(["git", "-C", str(workdir), "add", "-A", "--", ".", *PATCH_EXCLUDES])
    raw = run(["git", "-C", str(workdir), "diff", "--cached"]).stdout
    # ⚠️ 绝对不能 .strip()：unified diff 的空白上下文行就是「一个空格」，
    # strip 会把结尾那行吃掉 → git apply 报 "corrupt patch at line N"
    patch = raw.lstrip("\n")
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return patch


def container_name(iid: str) -> str:
    """instance_id 长到 130 字符，截断会撞名；补 8 位 hash 保证唯一。"""
    h = hashlib.md5(iid.encode()).hexdigest()[:8]
    return f"codexprohost-{iid[:40]}-{h}"


def run_one(inst: dict, args, root: Path, outdir: Path) -> dict:
    iid = inst["instance_id"]
    img = get_dockerhub_image_uri(iid, args.dockerhub_username, inst.get("repo", ""))
    workdir = (root / iid / "app").resolve()
    sbx = (root / iid / "sbx").resolve()
    cname = container_name(iid)

    # ⚠️ 拉镜像必须在计时开始**之前**：pull 是环境成本，不是模型解题耗时。
    pull_s = ensure_image(img, args.platform, args.pull_timeout)

    # 用 monotonic 不用 time.time()：macOS 休眠时 time.time() 照走、monotonic 冻结，
    # 而 subprocess 的 timeout 内部就是 monotonic，混用会写出自相矛盾的数。
    t_prep = time.monotonic()
    strip_out = export_repo(img, args.platform, workdir,
                            build_prep(iid, inst["base_commit"], args.strip_history))
    # 只在容器确实回报了 STRIP_OK 时才记 True。宁可少报，也不能在报告里写着
    # 「已加固」而实际没堵上。
    stripped = args.strip_history and STRIP_OK in strip_out
    if args.strip_history and STRIP_FAIL in strip_out:
        print(f"  ⚠️  {iid} 剥离后 gold fix 仍可读，这条的分数按污染算", file=sys.stderr)
    prepare_workdir(workdir, inst["base_commit"])
    sbx.write_text(SBX.format(container=cname, workdir_q=shlex.quote(str(workdir)),
                              repo_dir_q=shlex.quote(REPO_DIR)))
    sbx.chmod(0o755)
    start_container(img, args.platform, workdir, cname)
    prep_s = round(time.monotonic() - t_prep, 1)

    try:
        prompt = build_prompt(inst, workdir, sbx, args.no_cheat)
        (root / iid / "prompt.txt").write_text(prompt)   # 实际下发的 prompt，便于复现

        env = dict(os.environ)
        env[INNER_ENV_KEY] = args.api_key
        env["CODEX_HOME"] = str(args.codex_home)
        # 有这个变量时 Codex 可能优先拿它去打官方端点，跑分就串了台
        env.pop("OPENAI_API_KEY", None)

        t0 = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(build_codex_cmd(args, workdir), input=prompt,
                                  capture_output=True, text=True,
                                  timeout=args.timeout, env=env)
            out, err, rc = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
            err, rc, timed_out = "[TIMEOUT]", -1, True
        agent_s = round(time.monotonic() - t0, 1)

        # 存成 .log 而不是 .jsonl：内容确实是 JSONL 事件流，但 show_codex_run.py 和
        # make_report.py 都认 logs/<iid>.log，跟容器模式保持同一形状，下游一个字不用改。
        logs_dir = outdir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / f"{iid}.log").write_text(out)
        if err.strip():
            (logs_dir / f"{iid}.stderr").write_text(err)

        patch = collect_patch(workdir)
    finally:
        if not args.keep:                    # 中途炸了也别把容器留在那儿
            run(["docker", "rm", "-f", cname])

    rec = {
        "instance_id": iid,
        "model_patch": patch,
        # 与容器版的 codex-cli/<model> 区分开，报告里一眼能看出这轮 agent 跑在哪
        "model_name_or_path": f"codex-cli-host/{args.model}",
        "_meta": {
            # seconds 只含 codex 进程本身：导出 /app + 起容器记在 prep_seconds，
            # 拉镜像记在 pull_seconds，与容器版的「容器内打点」同口径
            "seconds": agent_s,
            "timed_out": timed_out,
            "prep_seconds": prep_s,
            "pull_seconds": pull_s,
            "exit_code": rc,
            "patch_chars": len(patch),
            "image": img,
            "history_stripped": stripped,
            **parse_usage(out),
        },
    }
    inst_dir = outdir / iid
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / f"{iid}.pred").write_text(json.dumps(rec, indent=2, ensure_ascii=False))

    if args.rm_workdir:
        shutil.rmtree(workdir.parent, ignore_errors=True)
    # 磁盘紧就跑完一条删一条。Pro 的镜像是整仓依赖装好的，动辄好几个 G ——
    # 但评测阶段还要再拉一次，别在只跑一轮时开。
    if args.rm_image:
        run(["docker", "rmi", "-f", img])

    m = rec["_meta"]
    print(f"[done] {iid}  patch={len(patch)}B  agent={agent_s}s（准备 {prep_s}s）  "
          f"turns={m['turns']}  out_tok={m['output_tokens']}"
          + ("  ⚠️TIMEOUT" if timed_out else ""), flush=True)
    return rec


def run_one_guarded(inst: dict, args, root: Path, outdir: Path, lock) -> dict | None:
    """把 run_one 包起来：单条炸了不许带走整轮。理由与容器版同名函数逐字一致 ——
    ThreadPoolExecutor.map 的异常是取结果时才抛的，一抛就没人写收尾汇总了。"""
    iid = inst["instance_id"]
    rec = None
    t0 = time.monotonic()
    try:
        rec = run_one(inst, args, root, outdir)
    except Exception as e:  # noqa: BLE001 —— 就是要兜住所有意外
        print(f"[error] {iid}  {type(e).__name__}: {e}", flush=True)
        pred = outdir / iid / f"{iid}.pred"
        # ⚠️ 只在还没有成果时才写这条墓碑。run_one 是**先写 .pred 再做收尾**的
        # （--rm-workdir / --rm-image / 那句 print），收尾抛异常时盘上已有好好的 patch，
        # 覆盖掉等于把烧了几十万 token 的成果抹掉。
        keep = False
        if pred.is_file():
            try:
                keep = bool(json.loads(pred.read_text()).get("model_patch", "").strip())
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                keep = False
        if keep:
            print(f"[error] {iid}  已有非空 patch，保留原 .pred 不覆盖", flush=True)
        else:
            pred.parent.mkdir(parents=True, exist_ok=True)
            pred.write_text(json.dumps({
                "instance_id": iid,
                "model_patch": "",
                "model_name_or_path": f"codex-cli-host/{args.model}",
                # seconds 记真实耗时而不是 0：记 0 会让这条从「推理耗时」KPI 里凭空消失
                "_meta": {"seconds": round(time.monotonic() - t0, 1), "timed_out": True,
                          "prep_seconds": 0.0, "pull_seconds": 0.0, "exit_code": -1,
                          "patch_chars": 0, "image": "",
                          "error": f"{type(e).__name__}: {e}",
                          **{k: 0 for k in USAGE_FIELDS}, "turns": 0, "total_tokens": 0},
            }, indent=2, ensure_ascii=False))

    # ⚠️ 这一段自己也要兜住：它每条都跑一次，磁盘写满 / .pred 缺字段都会在这里抛，
    # 抛出去照样穿过 map 把整轮带走，那前面那层 try 就白加了。
    try:
        with lock:
            write_aggregates(outdir, args)
    except Exception as e:  # noqa: BLE001
        print(f"[error] {iid}  汇总失败（不影响继续跑）：{type(e).__name__}: {e}", flush=True)
    return rec


# 容器版有「容器墙上时间」，宿主版没有，换成 prep_seconds（导出 /app + 起容器）。
# 其余字段与容器版逐字对齐 —— 报告脚本是同一份，字段名对不上只是少显示几块 KPI，
# 不报错，很难发现。
HOST_TOTAL_FIELDS = tuple(f for f in TOTAL_FIELDS if f != "wall_seconds") + ("prep_seconds",)


def write_aggregates(outdir: Path, args) -> dict:
    """扫盘产出 preds.json + run_meta.json，形状与容器版一致。

    扫盘而不是只汇总本轮：中断续跑之后产物依然是**全量**的。
    """
    recs = read_preds(outdir)
    # 用 .get：.pred 少字段也只是这一条缺内容，不能让整轮汇总抛 KeyError 崩掉。
    preds = {iid: {"instance_id": iid,
                   "model_patch": r.get("model_patch", ""),
                   "model_name_or_path": r.get("model_name_or_path", "")}
             for iid, r in recs.items()}
    write_json_atomic(outdir / "preds.json", preds)

    inst = {iid: dict(r.get("_meta") or {}) for iid, r in recs.items()}
    totals = {k: sum(m.get(k, 0) or 0 for m in inst.values()) for k in HOST_TOTAL_FIELDS}
    for k in ("seconds", "prep_seconds", "pull_seconds"):
        totals[k] = round(totals[k], 1)
    totals["n_error"] = sum(1 for m in inst.values() if m.get("exit_code"))

    write_json_atomic(outdir / "run_meta.json", {
        "model": args.model,
        "provider": args.provider,
        "base_url": args.base_url,
        "agent": "codex-cli",
        "agent_location": "host",          # 与容器模式的产物区分开
        "sandbox": args.sandbox + (" +network" if args.network else ""),
        "reasoning_effort": args.reasoning_effort or "default",
        # 这一轮有没有堵住「从 .git 抄答案」。逐条实际结果在 instances[*].history_stripped，
        # 两者对不上就说明有条目剥离失败了。
        "strip_history": args.strip_history,
        "anticheat_prompt": args.no_cheat,
        "n_stripped": sum(1 for m in inst.values() if m.get("history_stripped")),
        "subset": args.subset,
        "split": args.split,
        "totals": totals,
        "instances": inst,
    })
    return {"totals": totals, "instances": inst}


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="JSONL，列同 HuggingFace 的 SWE-bench_Pro")
    ap.add_argument("--instances", nargs="*", default=None, help="只跑这些 instance_id")
    ap.add_argument("--slice", default=None, help="形如 0:2，按顺序切一段")
    ap.add_argument("--model", required=True, help="传给 codex -m。脚本带 --ignore-user-config，"
                                                   "~/.codex/config.toml 的 model 不生效")
    ap.add_argument("--provider", default="deepseek", help="配置段名，随便起，只是个标签")
    ap.add_argument("--base-url", required=True, help="端点，必须支持 Responses API")
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--reasoning-effort", default="",
                    help="空＝不下发，用 Codex 内置默认（实测 none）；推理模型改 high")
    ap.add_argument("--codex-home", default="",
                    help="Codex 的状态目录，留空＝<workroot>/.codex-home（全新空目录，"
                         "不碰你的 ~/.codex）")
    ap.add_argument("--sandbox", default="workspace-write",
                    choices=["read-only", "workspace-write", "danger-full-access"])
    ap.add_argument("--no-network", dest="network", action="store_false",
                    help="不给沙箱开 network_access。⚠️ Seatbelt 把 unix socket 也算网络，"
                         "关了会连 docker.sock 一起挡掉，agent 将无法跑测试")
    ap.add_argument("--workroot", default="work", help="宿主机上放仓库副本的目录，每条一份")
    ap.add_argument("--dockerhub-username", default="jefzda")
    ap.add_argument("--platform", default="linux/amd64", help="官方镜像只有 amd64")
    ap.add_argument("--timeout", type=int, default=1800, help="单条 agent 超时秒数")
    ap.add_argument("--pull-timeout", type=int, default=3600)
    ap.add_argument("--workers", type=int, default=1,
                    help="并发数。每条占一个常驻容器 + 一份仓库副本")
    ap.add_argument("--redo-existing", action="store_true",
                    help="默认跳过已有非空 patch 的条目（续跑）；带上就全部重跑")
    ap.add_argument("--keep", action="store_true", help="跑完不删常驻容器，便于事后进去看")
    ap.add_argument("--rm-workdir", action="store_true", help="跑完删掉宿主机上的仓库副本")
    ap.add_argument("--rm-image", action="store_true", help="每条跑完删镜像省磁盘")
    ap.add_argument("--no-strip-history", dest="strip_history", action="store_false",
                    help="不剥离 git 历史。⚠️ 官方镜像的 .git 里就有 gold fix 和判分用的"
                         "测试源码，agent 一条 `git show` 就抄得到，分数会虚高且各模型虚高"
                         "程度不同。只在复现 2026-08-12 之前跑的旧分数时才关")
    ap.add_argument("--no-anticheat-prompt", dest="no_cheat", action="store_false",
                    help="prompt 里不加「别去查上游」那段。⚠️ 模型厂商可能服务端注入 "
                         "web_search（DeepSeek 实测会），沙箱和出网代理都拦不住，"
                         "这段是唯一能碰那条通道的杠杆")
    ap.add_argument("--subset", default="pro", help="只写进 run_meta.json，报告表头显示")
    ap.add_argument("--split", default="test", help="同上")
    ap.add_argument("-o", "--output-dir", required=True)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        sys.exit("--workers 至少为 1")
    if not shutil.which("codex"):
        sys.exit("PATH 里没有 codex：npm i -g @openai/codex")

    root = Path(args.workroot).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # 默认给一个全新的空 CODEX_HOME：跑分只吃命令行上给的 model / effort / provider，
    # 不受 ~/.codex 里的 config.toml、AGENTS.md、插件影响。
    args.codex_home = Path(args.codex_home).expanduser().resolve() if args.codex_home \
        else root / ".codex-home"
    args.codex_home.mkdir(parents=True, exist_ok=True)

    rows = load_dataset_rows(args.dataset)
    if args.instances:
        want = set(args.instances)
        rows = [r for r in rows if r["instance_id"] in want]
        if missing := want - {r["instance_id"] for r in rows}:
            sys.exit(f"不在 {args.dataset} 里：{sorted(missing)}")
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
    print(f"[run] {len(rows)}/{n_all} instances（跳过已完成 {n_all - len(rows)}）, "
          f"agent 在宿主机, model={args.model}, "
          f"effort={args.reasoning_effort or 'default'}, workers={args.workers}", flush=True)

    lock = threading.Lock()
    if rows:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(lambda r: run_one_guarded(r, args, root, outdir, lock), rows))

    meta = write_aggregates(outdir, args)
    t = meta["totals"]
    n_ok = sum(1 for r in read_preds(outdir).values() if r.get("model_patch", "").strip())
    err = f" · 非零退出 {t['n_error']} 条" if t["n_error"] else ""
    print(f"[done] 非空 patch {n_ok}/{len(meta['instances'])} · {t['turns']} turns · "
          f"输入 {t['input_tokens']:,}（缓存 {t['cached_input_tokens']:,}） · "
          f"输出 {t['output_tokens']:,} · 推理累计 {t['seconds'] / 3600:.2f} 小时"
          f"（准备 {t['prep_seconds']}s，拉镜像 {t['pull_seconds']}s 未计入）{err}")
    print(f"下一步: python eval_pro.py --dataset {args.dataset} --run {outdir} "
          f"--output-dir {outdir}-eval --workers 2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
