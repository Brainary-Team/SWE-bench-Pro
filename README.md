# SWE-bench Pro 评测流程

用 Codex CLI 作为 agent 跑 [SWE-bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)（公开集 731 条），
评测走官方 harness，报告与 SWE-bench Verified 那套**完全同款**。
agent 也可以直接跑在宿主机上（见[宿主机模式](#宿主机模式agent-跑在本机)），评测与报告两段完全共用。

## 执行流程

```
┌─ 阶段 A 推理（本地容器里跑 Codex）──────────────────────────┐
│  run_codex_pro.py    ──→ <run>/<iid>/<iid>.pred + logs/ + preds.json + run_meta.json
│                     换位置：run_codex_pro_host.py（Codex 跑在宿主机，产物同形状）
└──────────────────────────────────────────────────────────┘
┌─ 阶段 B 评测（官方 harness，另起干净容器）────────────────────┐
│  eval_pro.py         ──→ <eval>/eval_results.json + <iid>/codex_output.json
└──────────────────────────────────────────────────────────┘
┌─ 阶段 C 报告 ─────────────────────────────────────────────┐
│  pro_eval_report.py  ──→ eval_pro.json（Verified 的 schema）
│  make_report.py      ──→ report_pro.html
└──────────────────────────────────────────────────────────┘
```

两个阶段的容器**互不共用**：推理那个容器是给 agent 改代码用的，评测那个是干净起的，
agent 在推理容器里干了什么都不会污染评测。

## 仓库内容

| 文件 | 作用 |
|---|---|
| `setup.sh` | 一键装环境：虚拟环境 + 依赖 + 官方仓库 + Codex Linux 二进制 + 数据集 + Docker 自检 |
| `fetch_dataset.py` | 从 HuggingFace 把 731 条拉成 `swebench_pro.jsonl` |
| `run_codex_pro.py` | 阶段 A：起容器把 Codex 挂进去改代码，收尾 `git diff` 出 patch |
| `run_codex_pro_host.py` | 换位置：阶段 A 的 Codex 跑在宿主机，靠 `sbx` 桥进容器跑测试，不需要 Linux 二进制 |
| `eval_pro.py` | 阶段 B：收补丁 + 调官方 `swe_bench_pro_eval.py`（官方仓库一个字节不改） |
| `pro_eval_report.py` | 阶段 C-1：把官方评测产物翻译成 Verified 的 `eval_report.json` |
| `make_report.py` | 阶段 C-2：渲染 HTML 报告。**与 SWE-bench Verified 仓库里那份逐字节相同** |
| `audit_contamination.py` | 扫推理日志，找 agent「抄答案」而不是「解题」的痕迹，算出**干净的 Resolved** |
| `show_codex_run.py` | 运维：把 Codex 的 JSONL 日志还原成可读的执行过程 |
| `SWE-bench_Pro-os/` | 官方仓库（setup.sh 自动 clone，钉在 `ca10a60`）。**只读，不改** |

> **报告为什么能和 Verified 一模一样**：`make_report.py` 是从 Verified 仓库原样拷过来的
> （`shasum -a 256` 一致），一行没动。差异全部由 `pro_eval_report.py` 吸收 ——
> 它把 Pro 的评测产物拼成 Verified 那份 `eval_report.json` 的 schema，
> 再喂给同一个渲染器。所以两边报告的 CSS、表头、KPI、展开区结构都是同一份代码产出的。

---

## 一、拿到代码

```bash
git clone https://github.com/Brainary-Team/SWE-bench-Pro.git ~/swebench-pro
cd ~/swebench-pro
```

## 二、一键装环境

```bash
./setup.sh
```

干这么几件事（幂等，重复跑不会重复下载）：

1. 建 `.venv`（Python 3.12，有 `uv` 就用 `uv`），装 `requirements.txt`
2. clone 官方仓库 `SWE-bench_Pro-os` 并**钉到 `ca10a60`** ——
   它的 `run_scripts/`（每条实例的跑测脚本 + 结果解析器）和 `dockerfiles/` 要跟数据集对得上，
   上游一动分数就不可比
3. 用 npm 取 Codex 的 Linux x86_64 静态二进制到 `codex-bin/`
4. 拉数据集到 `swebench_pro.jsonl`（731 条）
5. 自检 Docker、amd64 容器、二进制能否挂进容器跑

### 前置：Docker + Rosetta

脚本会自检，但开关得你自己在 GUI 里点。打开 **Docker Desktop → Settings → General**，勾上：

> ☑️ **Use Rosetta for x86_64/amd64 emulation on Apple Silicon**

**Apply & Restart**。官方镜像**只有 x86_64 版本**，不开 Rosetta 会退化成 QEMU 软件模拟，慢到不可用。

```bash
docker run --rm --platform linux/amd64 alpine uname -m
# x86_64                      ← 必须是这个
file codex-bin/codex-x86_64-unknown-linux-musl
# ELF 64-bit LSB pie executable, x86-64, ... static-pie linked   ← 必须是这行
```

### ⚠️ 磁盘：这里跟 Verified 完全不是一个量级

Pro 的镜像是**整仓依赖装好**的，不是 SWE-bench 那种精简镜像。本机实测 32 个：

```
最小 1.53 GB · 中位 3.98 GB · 最大 20.6 GB · 平均 5.1 GB
按平均值外推 731 条 ≈ 3.7 TB
```

**结论：全量跑必须开 `--rm-image`，没有第二种选择。** 在 **Settings → Resources** 里
磁盘给到 **200 GB+**、内存 **8 GB+**，然后靠「跑一条删一条」滚动。

代价是同一个镜像会被拉两次（推理一次、评测一次）。想省一次就别在推理阶段删，
但那样得保证磁盘扛得住这一轮的累计量。

### 每次开工

```bash
cd ~/swebench-pro && source .venv/bin/activate
open -a Docker
docker info --format 'ok {{.ServerVersion}}'   # 等到不报错为止
```

## 三、验证模型端点（可跳过）

Codex 走 **Responses API**，不是 Chat Completions。端点不支持的话前面全白跑：

```bash
curl -s -X POST https://api.deepseek.com/v1/responses \
  -H "Authorization: Bearer sk-你的key" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","input":"hi"}' | head -c 120
# 必须看到 "object":"response","status":"completed"
```

DeepSeek 原生就有 `/v1/responses`，**直连即可**，不需要任何中转。

## 四、冒烟测试：2 条

三步的输入输出必须首尾相接：阶段 B 的 `--run` 要指向阶段 A 的 `-o`，阶段 C 两个目录都要给。

### A. 推理

```bash
# --dataset            fetch_dataset.py 产出的 JSONL
# --instances          指定 instance_id（空格分隔）；与 --slice 二选一
# --slice              形如 0:2，按顺序切一段
# --model              传给 codex -m，**不带 provider 前缀**
# --provider           配置段名，随便起，只是个标签
# --base-url           端点，必须支持 Responses API，且容器里访问得到
# --api-key            直接传 key
# --reasoning-effort   ''（Codex 内置默认，实测 none）；推理模型改 high
# --codex-bin          挂进容器的 Linux 二进制
# --workers            并发数，冒烟固定 1~2，出错好定位
# --timeout            单条容器总超时秒数（宿主机计时，卡的是整个 docker run：
#                      建容器 + agent + 收尾 git diff。拉镜像在计时之外）
# --pull-timeout       单个镜像预拉超时秒数
# --rm-image           开关（默认关）：每条跑完删镜像，全量必开
# --redo-existing      开关（默认关）：重跑已有非空 patch 的条目
# --no-strip-history      开关（默认**关**，即默认会剥离）：不剥 git 历史。
#                         ⚠️ 镜像的 .git 里就有 gold fix，不剥的话分数虚高，见「数据污染」一节
# （公网通道没有开关：web_search 已在 build_codex_cmd 里硬编码 disabled，同见那一节）
# 冒烟挑了两条 ansible（python，镜像 1.6 GB，是全集里最小的一档）。
# 想跑数据集头两条就把 --instances 换成 --slice 0:2 —— 但那两条镜像大得多。
python run_codex_pro.py \
  --dataset swebench_pro.jsonl \
  --instances \
    instance_ansible__ansible-fb144c44144f8bd3542e71f5db62b6d322c7bd85-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5 \
    instance_ansible__ansible-11c1777d56664b1acb56b387a1ad6aeadef1391d-v0f01c69f1e2528b935359cfe578530722bca2c59 \
  --model deepseek-v4-flash \
  --provider deepseek \
  --base-url https://api.deepseek.com/v1 \
  --api-key sk-你的key \
  --reasoning-effort '' \
  --codex-bin codex-bin/codex-x86_64-unknown-linux-musl \
  --workers 2 \
  --timeout 1800 \
  --pull-timeout 3600 \
  -o results/smoke
```

实测输出（deepseek-v4-flash，2026-08-11）：

```
[run] 2/2 instances（跳过已完成 0）, model=deepseek-v4-flash, workers=2
[done] instance_ansible__ansible-11c1777d...  patch=2270B  agent=80.3s  turns=1  out_tok=7329
[done] instance_ansible__ansible-fb144c44...  patch=3350B  agent=324.2s  turns=1  out_tok=10835
[done] 非空 patch 2/2 · 2 turns · 输入 1,285,016（缓存 1,237,888） · 输出 18,164 · 推理累计 0.1 小时
```

产出目录：

```
results/smoke/
├── preds.json                    # 汇总补丁，报告靠它定位同级的 meta 和日志
├── run_meta.json                 # token / 耗时台账（schema 与 Verified 一致）
├── logs/<iid>.log                # Codex 的 JSONL 事件流，报告的「过程」列解析它
└── <iid>/<iid>.pred              # 逐条补丁 + 用量，评测阶段吃这个
```

### B. 评测

```bash
# --dataset       必须和推理时是同一份
# --run           阶段 A 的 -o；脚本自动从里面收补丁
# --output-dir    评测产物目录
# --workers       并发数。评测比推理更吃 CPU 和内存，4 是稳妥值
# --prefix        评测产物的文件名前缀，默认 codex
# 未识别的参数原样透传给官方脚本，例如 --redo / --block_network
python eval_pro.py \
  --dataset swebench_pro.jsonl \
  --run results/smoke \
  --output-dir results/eval-smoke \
  --workers 2
```

结尾会打 `Overall accuracy:  1.0`。产出：

```
results/eval-smoke/
├── patches.json                        # 收上来的补丁，喂给官方脚本的入参
├── eval_results.json                   # 官方判卷：{instance_id: true/false}
└── <iid>/codex_output.json             # 逐条测试明细：{"tests":[{"name","status"}]}
        codex_stdout.log / codex_stderr.log / codex_patch.diff / codex_entryscript.sh
```

### C. 报告

两步：先把 Pro 的产物翻译成 Verified 的 schema，再用同一个渲染器出 HTML。

```bash
# --dataset  取 fail_to_pass / pass_to_pass 名单，用来对齐逐条测试
# --run      阶段 A 目录（拿 patch、model 名、以及报告要找的 logs/ 锚点）
# --eval     阶段 B 目录（拿判卷结果和逐条测试）
python pro_eval_report.py \
  --dataset swebench_pro.jsonl \
  --run results/smoke \
  --eval results/eval-smoke \
  -o eval_smoke.json

# 位置参数  eval_*.json
# --meta    run_meta.json 路径（留空按 predictions_path 同级自动找，显式给更稳）
# --output  HTML 输出路径
python make_report.py eval_smoke.json \
  --meta results/smoke/run_meta.json \
  --output report_smoke.html

open report_smoke.html
```

报告顶上一行 KPI，下面一行一条 instance，行尾「N 步」按钮展开该条的完整执行过程：

```
Resolved    100%       2/2 条 · patch 应用 2/2
测试通过    13/13      F2P 6/6 · P2P 7/7
推理耗时    6m44s      平均 3m22s/条 · 2 turns
输入 token  1,285,016  缓存 1,237,888 · 96%
输出 token  18,164     推理 9,352
```

## 五、全量推理（731 条）

```bash
# --slice / --instances 都不给 ＝ 跑数据集全部 731 条
# --workers 4           瓶颈在端点不在本机；端点扛得住就往上调
# --timeout 1800        单条 30 分钟
# --rm-image            ⚠️ 必开，否则 3.7 TB
tmux new -s swebench-pro
python run_codex_pro.py \
  --dataset swebench_pro.jsonl \
  --model deepseek-v4-flash \
  --provider deepseek \
  --base-url https://api.deepseek.com/v1 \
  --api-key sk-你的key \
  --reasoning-effort '' \
  --codex-bin codex-bin/codex-x86_64-unknown-linux-musl \
  --workers 4 \
  --timeout 1800 \
  --pull-timeout 3600 \
  --rm-image \
  -o results/codex-pro \
  2>&1 | tee -a logs-pro-infer.txt
```

### 中断与续跑

**推理支持断点续跑**：中断后重跑**完全相同的命令**即可，已有非空 patch 的条目会跳过。

- 空 patch 的条目**会重试**（上次没憋出来，值得再给一次机会）
- 想全部重跑加 `--redo-existing`
- `preds.json` / `run_meta.json` 每轮结束都按**扫盘**重算，所以续跑之后它们依然是全量的

### 查看进度（可跳过）

```bash
# 已完成条数（非空 patch）
python -c "
import json,glob
n=t=0
for f in glob.glob('results/codex-pro/*/*.pred'):
    d=json.load(open(f)); t+=1; n+= bool(d['model_patch'].strip())
print(f'{n}/{t} 有效 · 空 patch {t-n}')
"

# 累计 token 与耗时
python -c "
import json; t=json.load(open('results/codex-pro/run_meta.json'))['totals']
print(f\"{t['turns']} turns · 输入 {t['input_tokens']:,}（缓存 {t['cached_input_tokens']:,}）\"
      f\" · 输出 {t['output_tokens']:,} · 累计 {t['seconds']/3600:.1f} 小时\")
"

# 磁盘（开了 --rm-image 也要盯着，评测阶段还会再涨）
watch -n 60 'df -h / | tail -1; docker system df | head -3'
```

### 查看某条里 agent 干了什么（可跳过）

```bash
# --commands      命令只留命令行、不带输出（agent 的发言和报错照常打），扫一眼用这个最快
# --max-output N  每段输出截断字符数，默认 600
# --full          不截断
python show_codex_run.py results/codex-pro/logs/<iid>.log --commands
python show_codex_run.py results/codex-pro/logs/<iid>.log --max-output 600
```

## 六、全量评测

```bash
python eval_pro.py \
  --dataset swebench_pro.jsonl \
  --run results/codex-pro \
  --output-dir results/eval-codex-pro \
  --workers 4 \
  2>&1 | tee -a logs-pro-eval.txt
```

⚠️ **评测目录必须是干净的**。官方脚本看到 `<iid>/codex_output.json` 已存在就直接复用旧结果，
不校验时间戳、不校验 patch 有没有变。重跑要么换一个 `--output-dir`，要么加 `--redo`。

## 七、生成 HTML 报告

```bash
python pro_eval_report.py \
  --dataset swebench_pro.jsonl \
  --run results/codex-pro \
  --eval results/eval-codex-pro \
  -o eval_pro.json

python make_report.py eval_pro.json \
  --meta results/codex-pro/run_meta.json \
  --output report_pro.html

open report_pro.html
```

---

## 宿主机模式：agent 跑在本机

同一个 Codex，只换「agent 进程跑在哪」。用宿主机装的 `codex`（arm64 原生）——
不必备 Linux 静态二进制（约 297 MB），agent 自己的进程也不用在 Rosetta 模拟的
x86_64 容器里爬。**评测和报告两段一个字都不用改**：`eval_pro.py` 只认
`<run>/<iid>/<iid>.pred`，不关心 patch 是谁在哪生成的。

```
宿主机                                          容器 codexprohost-<iid>（常驻，官方镜像）
├─ codex exec（arm64 原生，配置全从命令行来）
├─ work/<iid>/app/ ──────bind mount──────▶ /app     依赖（ansible-test / go / node_modules）
│     agent 直接编辑这份                              只装在这儿；同一份文件，改完立刻生效
├─ work/<iid>/sbx ────── docker exec ────▶ 跑 repro / pytest
└─ git diff → <iid>.pred → eval_pro.py 另起干净容器评测
```

三条约束缺一不可：**仓库副本必须在宿主机**（agent 要能直接编辑）、**必须 bind-mount
回容器**（宿主机没有仓库的依赖）、**评测必须另起干净容器**（agent 对测试文件的任何篡改
都带不进评测）。

**前置**：`codex --version` 有输出即可。这条路用不到 `setup.sh` 拉的 `codex-bin/`，
也**不用** `codex login` —— 凭据走 `--api-key`，和容器模式同一套 provider 配置。

```bash
# --workroot          宿主机上放仓库副本的目录，每条一份（ansible 约 350 M）
# --sandbox           见下面「沙箱」一节，默认 workspace-write
# --no-network        开关（默认关）：⚠️ 关掉沙箱网络会连 docker.sock 一起挡掉，agent 跑不了测试
# --codex-home        留空＝<workroot>/.codex-home（全新空目录，不碰你的 ~/.codex）
# --keep              开关（默认关）：跑完不删常驻容器，便于事后 docker exec 进去看
# --rm-workdir        开关（默认关）：跑完删宿主机副本，跑全量时基本必开
# 没有 --codex-bin：agent 不进容器，不需要 Linux 二进制
# 其余参数与 run_codex_pro.py 同名同义
python run_codex_pro_host.py \
  --dataset swebench_pro.jsonl \
  --instances \
    instance_ansible__ansible-fb144c44144f8bd3542e71f5db62b6d322c7bd85-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5 \
    instance_ansible__ansible-11c1777d56664b1acb56b387a1ad6aeadef1391d-v0f01c69f1e2528b935359cfe578530722bca2c59 \
  --model deepseek-v4-flash \
  --provider deepseek \
  --base-url https://api.deepseek.com/v1 \
  --api-key sk-你的key \
  --reasoning-effort '' \
  --workers 2 --timeout 1800 \
  -o results/host-smoke

# 后面两段与容器模式完全一样，只是把 --run / --meta 换成这一轮的目录
python eval_pro.py --dataset swebench_pro.jsonl \
  --run results/host-smoke --output-dir results/host-smoke-eval --workers 2

python pro_eval_report.py --dataset swebench_pro.jsonl \
  --run results/host-smoke --eval results/host-smoke-eval -o eval_host_smoke.json
python make_report.py eval_host_smoke.json \
  --meta results/host-smoke/run_meta.json --output report_host_smoke.html
```

产物目录结构与容器模式**逐字段同形状**，`show_codex_run.py` 照常能读。
`run_meta.json` 里多一个 `agent_location: "host"` 用来和容器模式的产物区分；
容器模式的 `wall_seconds` 换成了 `prep_seconds`（导出 `/app` + 起容器的耗时），
`seconds` 只含 codex 进程本身。`model_name_or_path` 是 `codex-cli-host/<model>`
（容器模式是 `codex-cli/<model>`）。

### `sbx` 桥

宿主机没有仓库的依赖，不给条路 agent 会拿本机 `python` 一路撞墙。所以每条会在
`work/<iid>/sbx` 生成一个一行脚本，prompt 里明确告诉 agent「要跑东西就调它」：

```bash
work/<iid>/sbx 'python -m pytest test/units/xxx_test.py -x -q'
# → docker exec -w /app codexprohost-<iid> bash -c '...'
```

它还会把命令里的宿主机路径替换成容器里的 `/app` —— agent 天然会拿眼前看到的绝对路径
拼命令，而那个路径在容器里不存在。草稿文件放 `work/<iid>/app/.agent_scratch/`，
该目录从 patch 和 `git status` 里双重排除。

实测 agent 确实在用这座桥，而不是纯静态改代码：写 `.agent_scratch/check_tty_ify.py`
→ `sbx 'python .agent_scratch/check_tty_ify.py'` → `sbx 'PYTHONPATH=/app/lib python -m
pytest ...'` → 迭代。

### 沙箱

macOS 上 Codex 用 Seatbelt。Seatbelt 把 unix domain socket 算在 `network-outbound` 里，
所以**不开网络就连不上 docker.sock**，agent 也就跑不了测试。默认 `workspace-write`
+ `network_access=true`：agent 能写工作区、能调 docker，但写不了宿主机 `~/`。

> ⚠️ 默认档里 agent 拿得到 docker socket，理论上能 `docker run -v /:/host` 绕出去。
> 自家 benchmark 机器可以接受，**别在共享机器上这么跑**。

脚本固定加了 `--ignore-user-config`（不读 `~/.codex/config.toml` 里的 notify 钩子、
插件、marketplace）、`-c project_doc_max_bytes=0`（不读全局和仓库里的 `AGENTS.md`），
并且默认把 `CODEX_HOME` 指到一个全新空目录、把 `OPENAI_API_KEY` 从环境里摘掉。
跑分只吃命令行上给的 model / effort / provider，保证可复现。

### 实测数据

`deepseek-v4-flash` / `effort=default` / `-w 2` / 两条 ansible → **2/2 resolved，13/13 测试通过**：

| Instance | agent | 准备 | turns | 输入（缓存） | 输出（推理） | patch | 评测 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ansible-11c1777d | 54.9s | 5.7s | 1 | 320,661 (305,920) | 4,683 (825) | 2270B | ✅ F2P 1/1 |
| ansible-fb144c44 | 104.9s | 4.3s | 1 | 751,242 (726,400) | 10,940 (4,777) | 7261B | ✅ F2P 5/5 · P2P 7/7 |

准备阶段（导出 305 M 的 `/app` + `git reset` + 起容器）5 秒上下，可以忽略。

### 注意

- **⚠️ 数据污染**：镜像的 `/app/.git` 里就有 gold fix，冒烟这两条里有一条 agent
  真的抄了。这是 Pro 镜像本身的问题，容器模式一样中招，详见
  [镜像里带着答案](#数据污染镜像里带着答案)。宿主机模式**额外**多送一份便利：
  `instance_id` 出现在 workdir 路径里，而它本身就含 fix commit 的 hash。
  另外沙箱网络是全开的，agent 也能直接 `curl` 上游 PR。
- **磁盘**：每条一份宿主机仓库副本 + 一个常驻容器。Pro 的仓库比 Verified 大得多
  （JS/TS 仓库带 `node_modules`，可以到几个 G），跑全量务必加 `--rm-workdir`。
- **并发**：x86_64 镜像在 arm64 上走 Rosetta，`-w` 开太高反而慢；已实测到 `-w 2`。
- **已验证范围**：只在两条 ansible（python）上端到端跑通。Go / JS / TS 仓库的
  bind-mount 表现未逐一验证 —— 尤其是 `node_modules` 在 macOS virtiofs 上的读写
  可能很慢，首次跑建议先单条试。
- **macOS 大小写不敏感**：仓库里若有仅大小写不同的同名文件，导出到宿主机会互相覆盖。
  脚本会在导出后打「工作区就不干净」的警告，撞上就改用容器模式跑那一条。

---

## 数据污染：镜像里带着答案

**这是 Pro 数据集本身的问题，不是本仓库哪个脚本引入的，容器模式和宿主机模式一样中招。**

### 现象

官方镜像的 `/app/.git` 是一份**完整 clone**，不是截断到 `base_commit` 的浅历史。
gold fix 那个 commit 就在里面，而且从 `refs/heads/*`、`refs/tags/*` 正常可达 ——
推理前那句 `git reset --hard <base_commit>` 只挪了 HEAD，**不删对象、不删 refs**。

而且 `instance_id` 本身就带着那个 commit 的 hash：

```
instance_ansible__ansible-11c1777d56664b1acb56b387a1ad6aeadef1391d-v0f01c69f1e...
                          └────────── gold fix 的 commit hash ──────────┘
   base_commit = e1daaae42af1a4e465edbdad4bb3c6dd7e7110d5   ← 另一个，是解题起点
```

### 实测（本地 32 个镜像，覆盖 8 个仓库 / 四种语言）

| 检查项 | 结果 |
| --- | --- |
| `instance_id` 里含 40 位 hash | **731/731**（全集，纯结构检查） |
| 那个 hash ≠ `base_commit` | 731/731 |
| `git reset --hard <base_commit>` 之后，该 commit 仍从 refs 可达 | **32/32** |
| `git show <hash>` 的文件集 == 数据集的 `patch` + `test_patch` | 28/32 |
| 剩下 4 条 | 是 merge commit，`git show` 默认不列文件；`git diff <hash>^ <hash>` 照样给出同一份 |

也就是**实测范围内 32/32 都能拿到答案**。

### agent 拿到的是什么

`git show <hash>` 一条命令，同时给出两样东西：

1. **`patch`** —— 源码侧的标准答案，逐行 diff。
2. **`test_patch`** —— 判分用的那批测试的源码。这个更要命：它等于把
   `fail_to_pass` 的断言原文摊开，agent 不用猜「什么算通过」，可以直接对着断言写。

冒烟里 `ansible-11c1777d` 那条就是这么过的。日志里的路径是：

```
git log --all --oneline -S get_locally_reachable_ips -- lib/.../linux.py   ← 拿题面里的符号名做 pickaxe 搜索
git show 11c1777d56 --stat && git show 11c1777d56 -- lib/.../linux.py
▸ agent: "This is the exact upstream commit this task is based on (11c1777d56)."
```

注意它是**靠内容搜索**找到的，没用 `instance_id`。所以容器模式（agent 看不到
`instance_id`，仓库固定挂在 `/app`）挡不住这条路径 —— 只要 fix commit 从 refs 可达，
`git log -S`／`git log --all` 就能翻出来。宿主机模式只是**额外**多送一份便利：
workdir 路径里带 `instance_id`，agent 连搜都不用搜，直接 `git show <那个 hash>`。

### 影响

- **绝对分数偏高，且偏高多少不可知** —— 取决于模型有多"想到"去翻历史。
  这类分数不能拿去和论文里的数字比。
- **两种模式之间仍然可比**（同一个洞、同一个量级），同一模型不同 effort 之间也可比。
- **不同模型之间会被扭曲**：爱翻 git 历史的模型白捡分。这是最需要警惕的一条 ——
  它会把"谁更会用工具"记成"谁更会解题"。

> **2026-08-12 起默认加固**（下一节）：git 历史默认剥离、web_search 硬编码关闭。
> 也就是说：**用默认参数跑出来的分数，与本文档更早版本里记的那些数不可比** ——
> 早那些是污染分。`--no-strip-history` 只能还原 git 通道；web_search 没有开关，
> 旧的污染分在现版本上**无法精确复现**（真要复现得 checkout 旧 commit）。
> 每一轮的状态记在 `run_meta.json` 的 `strip_history` / `web_search` / `n_stripped` 里，
> 产物是自描述的。

### 怎么堵（默认全开：剥历史开关 + web_search 硬编码关闭）

两条通道要分开治，机理完全不同。

#### ① git 通道 —— 能彻底堵，已堵

`--strip-history`（默认开，`--no-strip-history` 关掉）。在 `git reset --hard <base_commit>`
**之后**、跑 agent **之前**执行：

```bash
git remote remove origin 2>/dev/null || true
git for-each-ref --format='delete %(refname)' refs/heads refs/remotes refs/tags | git update-ref --stdin
rm -f .git/FETCH_HEAD .git/ORIG_HEAD
git reflog expire --expire=now --all
git gc --prune=now
git cat-file -e <fix hash>   # 断言：还读得到就报 STRIP_FAIL
```

命令取自上游未合的 PR [#94](https://github.com/scaleapi/SWE-bench_Pro-os/pull/94)，末尾那句断言是我们自己加的
—— fix 的 hash 从 `instance_id` 里就能拿到，直接问「那个 commit 还读不读得到」，
比 PR #94 按提交日期数未来提交的启发式硬。

**三个必须注意的点：**

1. **顺序不能反。** 镜像里 HEAD 停在造数据集时的日期截断点，那是 base_commit 之后很远的
   地方。先 strip 的话 `gc` 会把「HEAD 可达」的东西全留下 —— 包括 gold fix，等于白剥。
2. **`gc --prune=now` 一行都不能少。** 前四行只删指针，对象还在 pack 里，
   `git show <hash>` 照样读得出来。实测：删光 refs 后 fix **仍可读**，gc 之后才真读不到。
3. **不必用 `--aggressive`。** 实测普通 gc 已经够，而 `--aggressive` 会慢一个量级。

代价实测 **1~3 秒**（go / js / python / ts 各验一个，最大的 teleport `.git` 1.1G→102M），
而且 **base_commit 之前的历史一条不少** —— `git log` / `git blame` 照常可用，
agent 该有的工具没被削。宿主机模式下这一步在**导出之前**于容器里做，顺带少拷几百 MB 到几个 G。

> 评测**不受影响**：官方 harness 自己重新起干净容器、自己 `git reset --hard <base_commit>`，
> 用的是重新拉的镜像。Cursor 那套还得「打分时把历史还回去」，我们的两段容器本来就分家。

#### ② 公网通道 —— 主犯是 Codex 自带的 `web_search`，已关死

主力泄漏源**不是**厂商偷偷注入，是 **Codex 0.146 起把 web_search 改成了默认开启**：
不传 `--search` 它也会把 `{"type":"web_search"}` 声明进请求的 `tools` 数组
（full-access 沙箱下还默认升为 `live`）。DeepSeek 的 `/v1/responses` 按文档语义执行这个
「客户端声明的服务端工具」——搜索在**服务端**完成，结果直接进模型上下文，
不以任何可见事件回传。未加固那轮，agent 就是靠它搜到
`https://patch-diff.githubusercontent.com/raw/ansible/ansible/pull/79018.diff` 的，正是这条题的 gold PR。

诊断过程记一笔，因为**第一版判错了**，错误结论还在本节早期版本里挂过：

1. 表象：从没传 `--search`，日志里却有 `web_search` 事件 → 第一反应「厂商服务端注入，
   沙箱和出网代理都拦不住」。
2. 反证：DeepSeek 官方文档明确 web_search 是 **opt-in**（`tools` 里不声明就不搜），与「注入」矛盾。
3. 实锤：起本地假 API 收 Codex 的请求体 —— 全新 `CODEX_HOME`、没传 `--search`，
   `tools` 里赫然躺着 `web_search`。宿主机 0.146.0 与容器挂载的 musl 0.146.1 行为一致。

修法（已硬编码进**两份** `build_codex_cmd`，容器版和宿主版各一处，不设开关）：

```
-c web_search=disabled
```

- 抓包验证：加上后 `web_search` 从 `tools` 里消失，两个二进制都验过。
- ⚠️ 旧键 `tools.web_search=false` **无效** —— 压不过新版默认值，必须用顶层新键。
- 两种模式的 `CODEX_HOME` 都是全新的（宿主机还带 `--ignore-user-config`），
  CLI 旗标是唯一配置来源，不存在被本地 config.toml 盖掉的路径。
- 早期版本在 prompt 里加过一段「别去查上游」的反作弊话术兜这条通道，**现已删除**：
  工具直接从请求里消失，模型看不到也调不了，比「求模型别用」（按 Poolside 实测只能
  「可测量下降，不能根除」）原子得多。

残余风险，`audit_contamination.py` 继续兜着：

- agent 仍能自己 `curl` github（两种模式网络都通）—— audit 的 `network` 档盯这条；
  要物理堵死得上出网白名单（容器模式可做：internal 网络 + 只放行 API 的 sidecar 代理）。
- 若某个中转真在**服务端**注入搜索，客户端配置管不到。所以 audit 的 `web_search`
  信号关掉后照常保留：再出现，要么这行配置失效（Codex 升级改了键义），要么中转在注入
  —— 哪种都得停下来查。

### 审计：算出「干净的 Resolved」

`Resolved 多少` 这一个数没法单独看，得同时报「其中多少条是自己做出来的」：

```bash
python audit_contamination.py --run results/smoke --report eval_smoke.json
```

三档判定（宁可漏报也不误报，命中的都该人工过一眼）：

| 档 | 判据 | 误报率 |
| --- | --- | --- |
| `confirmed` 抄了 | 日志里出现 gold fix 的 commit hash | 基本没有 —— 那个 hash 只可能来自 `.git` 或 `instance_id` |
| `history` 翻历史 | `git log --all` / `-S`、`git show <hash>`、`git branch -a`、`reflog`、`fsck` | 中：也可能只是在读代码演进 |
| `network` 查上游 | `curl`/`wget` github、GitHub API、PR/commit 的 `.patch` URL、`web_search` | 中 |

> ⚠️ 脚本里有两处坑是踩出来的，改的时候别踩回去：**找 hash 前必须先把 `instance_id`
> 从文本里抹掉**（宿主机模式的 workdir 路径里就含着它，不抹 100% 误报）；
> **容器模式日志尾部 `===DIFF_START===` 包着的 git diff 要剔掉**（那是产物不是动作）。

### 加固前后实测（两条 ansible，deepseek-v4-flash）

| | Resolved | 审计 | patch 大小 | agent 耗时 |
| --- | --- | --- | --- | --- |
| 两条通道全开 | **2/2** | 抄了 2 条 | 2270B / 7261B | 55s / 105s |
| 只堵 git，公网通道全开 | 2/2 | 抄了 1、查上游 1（**改走公网**） | 2270B / 7261B | 143s / 128s |
| 堵 git + 反作弊 prompt* | **1/2** | **干净 2 条** | 2527B / 1554B | 177s / 106s |

> \* 第三行跑的时候公网通道靠的是 prompt 里一段「别去查上游」的话术 —— 当时还没定位到
> 泄漏源是 Codex 默认声明的 web_search。现在那段 prompt 已删，换成 `-c web_search=disabled`
> 直接把工具从请求里移除（见上节），约束只强不弱；表中数据未用新机制重跑，但结论不受影响。

读法：

- git 通道堵上后 agent **立刻改走公网**，分数一点没掉 —— 这就是为什么只堵一半等于没堵。
- 公网也堵上之后，`ansible-11c1777d` 那条**做不出来了**。它之前一直在抄，100% 是虚的。
- 干净轮的 patch 大小和污染轮**完全不同**（1554B vs 7261B），是真自己写的。
- 代价：agent 耗时和输出 token 都涨了约一倍 —— 它得真解题了。

n=2 只能说明方向。Cursor 在 731 条上的量级是 Opus 4.8 Max **87.1% → 73.0%**。

---

## 与 SWE-bench Verified 的差别

同一套流程搬到 Pro，下面这些地方**不一样**，踩过的都在这儿。

### 数据与镜像

| | Verified | Pro |
|---|---|---|
| 条数 | 500 | 731（go 280 / python 266 / js 165 / ts 20） |
| 语言 | 全 Python | 四种语言混合 |
| 仓库路径 | `/testbed` | `/app` |
| 镜像里的 git 历史 | 官方已加固（`git log --all ^HEAD` = 0，游离对象 0） | **没加固**，gold fix 可读；本仓库自己剥（见[数据污染](#数据污染镜像里带着答案)） |
| `instance_id` | `sympy__sympy-23534`，是 **PR 编号** | 直接带 **40 位 fix commit hash**，等于把答案的门牌号写在门口 |
| 镜像 | `swebench/sweb.eval.x86_64.*`，精简 | `jefzda/sweap-images:*`，整仓依赖，平均 5.1 GB |
| 测试名格式 | `path::test`（pytest） | `file \| title`（JS）、裸标识符（Go）、`path::test`（Python） |
| 评测粒度 | 逐条测试 + 失败原因 | 逐条测试，**没有失败原因** |

### 报告里几个字段的口径

- **`评测` 列的耗时**是估的。官方 harness 不记单条评测时间，这里取
  `codex_output.json` 与 `codex_patch.diff` 的 mtime 差 —— 前者是回收结果时写的、
  后者是起容器前写的，差值约等于评测容器的墙钟。
- **失败测试展开后没有错误信息**。Pro 的 parser 只吐 `{name, status}`，
  没有 pytest 那种 short summary。要看原因去 `results/<eval>/<iid>/codex_stdout.log`。
- **「patch 应用」这一列在 Pro 里读作「补丁确实进了这一轮评测」**，不是「`git apply` 成功」。
  Verified 能从 harness 的输出里直接看到 apply 成没成功；Pro 看不到 ——
  它的 entryscript 没有 `set -e`，`git apply` 的输出只进容器 stdout，
  而容器是 `detach + remove` 起的，日志当场就没了。所以这里只判两件事：
  评测出了 `codex_output.json`，且它存的 patch 快照与推理产物**逐字节相同**。
  不满足就记应用失败，并在 `eval_pro.json` 里写 `note`：`empty_patch`（模型没产出补丁，
  全量跑里这一档通常是大头）、`no_eval_output`（评测没出结果）、
  `stale_eval_output`（复用了旧结果）。注意 KPI 的分母是**全部条目**，
  所以 `patch 应用 300/731` 里那 431 条绝大多数是空 patch，不是「apply 失败」。
  真正的原因去 `codex_stdout.log` / `codex_stderr.log` 和评测那轮的终端输出里翻。
  另外，判定为 `stale_eval_output` 的条目**一律记作未解决** —— 那份成绩属于上一轮的补丁，
  不能算这一轮的（口径同 Verified：`resolved = applied and …`）。
- **`推理` 列对超时的条目显示的是墙钟**，不是解题耗时。容器被超时杀掉时只有起始打点、
  没有结束打点，拿不到净耗时。记 0 会让这条从 KPI 里凭空消失（烧了 30 分钟却显示没花时间），
  所以退回墙钟，口径与 Verified 一致；`run_meta.json` 里的
  `seconds_measured_in_container: false` 标着这条是估的。
- **判定口径照抄官方**：`(fail_to_pass | pass_to_pass) ⊆ {status == PASSED 的测试名}`。
  注意是按名字取并集再判包含 —— 同名测试出现多次时**只要有一次 PASSED 就算过**
  （instance 的 Dockerfile 普遍开了 `--reruns=3`，重复行是常态）。
  按「最后一行」判会和官方对不上账。`pro_eval_report.py` 两边都算，
  不一致时打 warning 并**以官方为准**。

### 数据集里的坑（都已在脚本里处理，改代码前先看这段）

1. **`fail_to_pass` 不是 JSON**。731 条里只有 9 条能被 `json.loads` 解开，其余是
   Python `repr()`，单双引号混着。必须用 `ast.literal_eval`（官方用的是 `eval`，等价）。
2. **列名大小写**。官方评测脚本读的是**小写** `fail_to_pass` / `pass_to_pass`，
   而它自己仓库里带的 `helper_code/sweap_eval_full_v2.jsonl` 是**大写**的。
   喂错了会触发 `KeyError` → 被它的 `except Exception` 吞掉 → 整轮干干净净地判 0 分，
   一条 traceback 都没有。**只能用 `fetch_dataset.py` 拉的那份**。
3. **那几列必须是字符串**。官方脚本用 `eval()` 解析它们，喂真的 JSON 数组会
   `TypeError: eval() arg 1 must be a string`，同样被吞成 0 分。
   所以 `fetch_dataset.py` 原样落盘，一个字段都不转。
4. **题面是双层 JSON 编码的**。731 条里有 328 条的 `problem_statement` / `requirements` /
   `interface` 存的是 JSON 字符串字面量：首尾带真的双引号、换行是字面的两个字符 `\n`。
   直接塞进 prompt，模型看到的是一坨转义符。`run_codex_pro.py` 里的 `_unwrap()`
   只在「首尾是双引号且能解出字符串」时剥一层，另外 403 条纯文本原样放行。
5. **372 条的 `pass_to_pass` 是空的**（go 257/280、python 59/266、js 37/165、ts 19/20）。
   算 P2P 通过率时分母会是 0，报告里显示成 `—`。
6. **`run_scripts/` 有 1000 个目录，但公开集只有 731 条**。多出来的 269 个不在公开集里，
   拿它数实例数会数多。

### 官方 harness 的两个硬约束

- **必须在 `SWE-bench_Pro-os/` 目录里跑**。它读
  `dockerfiles/{base,instance}_dockerfile/<iid>/Dockerfile` 用的是**相对 CWD** 的裸路径，
  没有参数也没有 `__file__` 锚点。`eval_pro.py` 就是为这个存在的：把所有路径参数转成绝对路径，
  再 `cwd=SWE-bench_Pro-os` 去调，产出物留在项目根目录，官方仓库一个字节都不动。
- **它不打 `test_patch`**。测试文件是靠 `before_repo_set_cmd` 的**最后一行**
  （通常是 `git checkout <fix_sha> -- <测试文件>`）捞回来的，而且这一步在
  `git apply 模型补丁` **之后**执行 —— 所以 agent 对测试文件的任何改动都会被覆盖掉。
  推理侧 `run_codex_pro.py` 也在 `git add` 时用 `:(exclude,glob)` 把常见的测试文件命名
  （`test_*` / `*_test.go` / `*.test.ts` / `tests/` / `__tests__/` …）挡在 patch 之外。
  这层是尽力而为的白名单，不保证覆盖所有仓库的命名习惯 —— 真正兜底的是评测那边的强制 checkout。

  > `:(exclude)` **必须带 `glob`**。不带的话 git 用的是不加 `FNM_PATHNAME` 的 fnmatch，
  > `*` 会跨 `/` 匹配，`:(exclude)*test_*` 就变成「路径里任意位置含 `test_` 就排除」——
  > `src/latest_news.go`（la·test_·news）会被无声地从 patch 里剔掉，
  > 表现成模型明明改了却判不过。

---

## 完整命令速查

```bash
# ── 一次性安装 ──
git clone https://github.com/Brainary-Team/SWE-bench-Pro.git ~/swebench-pro
cd ~/swebench-pro && ./setup.sh
# Docker Desktop：勾 Rosetta；磁盘 200 GB+；内存 8 GB+

# ── 每次开工 ──
cd ~/swebench-pro && source .venv/bin/activate
open -a Docker
docker run --rm --platform linux/amd64 alpine uname -m     # 应输出 x86_64

# ── 这一轮的旋钮（改这里，下面命令不用动）──
K=sk-你的key                                        # 不想留 history 就单独 export
MODEL=deepseek-v4-flash                            # 不带 provider 前缀
PROVIDER=deepseek                                  # 配置段名，随便起
BASE_URL=https://api.deepseek.com/v1               # 必须支持 Responses API
EFFORT=''                                          # ''|none|minimal|low|medium|high
CODEX_BIN=codex-bin/codex-x86_64-unknown-linux-musl
DATA=swebench_pro.jsonl
TIMEOUT=1800                                       # 单条 agent 超时秒数
PULL_TIMEOUT=3600

# ❶ 冒烟 2 条（--slice 0:2 就是数据集头两条；挑小镜像用 --instances）
python run_codex_pro.py --dataset "$DATA" --slice 0:2 \
  --model "$MODEL" --provider "$PROVIDER" --base-url "$BASE_URL" --api-key "$K" \
  --reasoning-effort "$EFFORT" --codex-bin "$CODEX_BIN" \
  --workers 2 --timeout "$TIMEOUT" --pull-timeout "$PULL_TIMEOUT" \
  -o results/smoke

python eval_pro.py --dataset "$DATA" --run results/smoke \
  --output-dir results/eval-smoke --workers 2

python pro_eval_report.py --dataset "$DATA" \
  --run results/smoke --eval results/eval-smoke -o eval_smoke.json
python make_report.py eval_smoke.json \
  --meta results/smoke/run_meta.json --output report_smoke.html

# ❷ 小批 20 条（想顺带验证端点扛不扛得住并发）
python run_codex_pro.py --dataset "$DATA" --slice 0:20 \
  --model "$MODEL" --provider "$PROVIDER" --base-url "$BASE_URL" --api-key "$K" \
  --reasoning-effort "$EFFORT" --codex-bin "$CODEX_BIN" \
  --workers 4 --timeout "$TIMEOUT" --pull-timeout "$PULL_TIMEOUT" \
  --rm-image -o results/pro-20

# ❸ 全量 731（tmux 里跑；中断重跑同命令即续；--rm-image 必开）
tmux new -s swebench-pro
python run_codex_pro.py --dataset "$DATA" \
  --model "$MODEL" --provider "$PROVIDER" --base-url "$BASE_URL" --api-key "$K" \
  --reasoning-effort "$EFFORT" --codex-bin "$CODEX_BIN" \
  --workers 4 --timeout "$TIMEOUT" --pull-timeout "$PULL_TIMEOUT" \
  --rm-image -o results/codex-pro \
  2>&1 | tee -a logs-pro-infer.txt

# ❹ 评测（--dataset 必须与推理一致；目录必须干净）
python eval_pro.py --dataset "$DATA" --run results/codex-pro \
  --output-dir results/eval-codex-pro --workers 4 \
  2>&1 | tee -a logs-pro-eval.txt

# ❺ 报告（重新评测后必须重跑这两步）
python pro_eval_report.py --dataset "$DATA" \
  --run results/codex-pro --eval results/eval-codex-pro -o eval_pro.json
python make_report.py eval_pro.json \
  --meta results/codex-pro/run_meta.json --output report_pro.html
open report_pro.html

# ❻ 宿主机模式 —— 同一个 Codex，agent 改在本机跑，不用 $CODEX_BIN
#    产物同形状，❹❺ 两段把 --run/--meta 换成这一轮的目录即可
python run_codex_pro_host.py --dataset "$DATA" --slice 0:2 \
  --model "$MODEL" --provider "$PROVIDER" --base-url "$BASE_URL" --api-key "$K" \
  --reasoning-effort "$EFFORT" \
  --workers 2 --timeout "$TIMEOUT" --pull-timeout "$PULL_TIMEOUT" \
  --workroot work -o results/host-smoke          # 跑全量再加 --rm-workdir --rm-image

python eval_pro.py --dataset "$DATA" --run results/host-smoke \
  --output-dir results/host-smoke-eval --workers 2
python pro_eval_report.py --dataset "$DATA" \
  --run results/host-smoke --eval results/host-smoke-eval -o eval_host_smoke.json
python make_report.py eval_host_smoke.json \
  --meta results/host-smoke/run_meta.json --output report_host_smoke.html

# ❼ 审计：这批分里有多少是抄的（每轮跑完都该看一眼）
python audit_contamination.py --run results/codex-pro --report eval_pro.json -o audit_pro.json

# ── 运维 ──
python show_codex_run.py results/codex-pro/logs/<iid>.log --commands --max-output 600
watch -n 60 'df -h / | tail -1; docker system df | head -3'
docker image prune -a -f
```

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 评测跑完 accuracy 0.00%，一条 traceback 都没有 | 数据集喂错了。官方脚本读小写 `fail_to_pass` 且要求它是字符串，异常全被吞。用 `fetch_dataset.py` 拉的那份 |
| `Warning: Instance ... not found in raw sample data` | `--dataset` 和推理时用的不是同一份 |
| 评测秒结束、结果和上次一模一样 | 命中了官方脚本的静默续跑：`<iid>/codex_output.json` 已存在就复用。换 `--output-dir` 或加 `--redo` |
| 报告里 KPI 只有两块（没有 token/耗时） | 没找到 `run_meta.json`。显式给 `--meta`；命令行会打「未找到 run_meta.json」 |
| 报告「过程」列全是「明细」，展不出步骤 | 没找到 `logs/<iid>.log`。确认 `predictions_path` 指的是 `<run>/preds.json` |
| `agent=TIMEOUT`，patch 是空的 | 容器被 `--timeout` 杀了。注意超时后脚本会显式 `docker rm -f`：不显式杀的话 `subprocess` 只杀 docker 客户端，容器会在后台接着跑、接着烧 token |
| 磁盘瞬间见底 | Pro 镜像平均 5.1 GB。`--rm-image` 必开；评测阶段还会再拉一遍 |
| `docker run` 报 `no matching manifest` | 漏了 `--platform linux/amd64`（镜像只有 amd64，Docker 默认按本机 arm64 找）。跟 Rosetta 没关系 —— Rosetta 只管跑得快不快，不管拉不拉得到 |
| 推理正常但 patch 恒为空 | 端点不支持 Responses API。用第三节的 curl 验一下 |
| 宿主机模式：agent 说 `python` 报 ModuleNotFoundError | 它在宿主机跑而不是走 `sbx` 桥。宿主机没有仓库依赖，正常现象；看日志确认它后来改用 `sbx` 了没有 |
| 宿主机模式：agent 说 docker `permission denied` | 沙箱网络被关了（`--no-network`）。Seatbelt 把 unix socket 算作网络，关了就连不上 docker.sock |
| 宿主机模式：patch 里混进复现脚本 | 草稿应落在 `work/<iid>/app/.agent_scratch/`，该目录已从 patch 和 `git status` 双重排除；混进来说明 agent 写到别处了 |
| 宿主机模式：导出后就报「工作区不干净」 | macOS 的 APFS 默认大小写不敏感，仓库里仅大小写不同的同名文件会互相覆盖。这条 instance 的 patch 会带噪声，换用容器模式跑 |
| 宿主机模式：`git reset --hard <base_commit> 失败` | 镜像里的 `/app` 和数据集的 `base_commit` 对不上。patch 基线与评测那边不一致，出来的 diff 大概率打不上，这条得单独查 |
