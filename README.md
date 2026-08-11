# SWE-bench Pro 评测流程

用 Codex CLI 作为 agent 跑 [SWE-bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)（公开集 731 条），
评测走官方 harness，报告与 SWE-bench Verified 那套**完全同款**。

## 执行流程

```
┌─ 阶段 A 推理（本地容器里跑 Codex）──────────────────────────┐
│  run_codex_pro.py    ──→ <run>/<iid>/<iid>.pred + logs/ + preds.json + run_meta.json
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
| `eval_pro.py` | 阶段 B：收补丁 + 调官方 `swe_bench_pro_eval.py`（官方仓库一个字节不改） |
| `pro_eval_report.py` | 阶段 C-1：把官方评测产物翻译成 Verified 的 `eval_report.json` |
| `make_report.py` | 阶段 C-2：渲染 HTML 报告。**与 SWE-bench Verified 仓库里那份逐字节相同** |
| `show_codex_run.py` | 运维：把 Codex 的 JSONL 日志还原成可读的执行过程 |
| `SWE-bench_Pro-os/` | 官方仓库（setup.sh 自动 clone，钉在 `ca10a60`）。**只读，不改** |

> **报告为什么能和 Verified 一模一样**：`make_report.py` 是从 Verified 仓库原样拷过来的
> （`shasum -a 256` 一致），一行没动。差异全部由 `pro_eval_report.py` 吸收 ——
> 它把 Pro 的评测产物拼成 Verified 那份 `eval_report.json` 的 schema，
> 再喂给同一个渲染器。所以两边报告的 CSS、表头、KPI、展开区结构都是同一份代码产出的。

---

## 一、拿到代码

```bash
git clone <你的仓库地址> ~/swebench-pro
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
# --timeout            单条 agent 超时秒数（容器内计时，不含拉镜像）
# --pull-timeout       单个镜像预拉超时秒数
# --rm-image           开关（默认关）：每条跑完删镜像，全量必开
# --redo-existing      开关（默认关）：重跑已有非空 patch 的条目
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
# --commands      只列执行过的命令（不带输出），扫一眼用这个最快
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

## 与 SWE-bench Verified 的差别

同一套流程搬到 Pro，下面这些地方**不一样**，踩过的都在这儿。

### 数据与镜像

| | Verified | Pro |
|---|---|---|
| 条数 | 500 | 731（go 280 / python 266 / js 165 / ts 20） |
| 语言 | 全 Python | 四种语言混合 |
| 仓库路径 | `/testbed` | `/app` |
| 镜像 | `swebench/sweb.eval.x86_64.*`，精简 | `jefzda/sweap-images:*`，整仓依赖，平均 5.1 GB |
| 测试名格式 | `path::test`（pytest） | `file \| title`（JS）、裸标识符（Go）、`path::test`（Python） |
| 评测粒度 | 逐条测试 + 失败原因 | 逐条测试，**没有失败原因** |

### 报告里几个字段的口径

- **`评测` 列的耗时**是估的。官方 harness 不记单条评测时间，这里取
  `codex_output.json` 与 `codex_patch.diff` 的 mtime 差 —— 前者是回收结果时写的、
  后者是起容器前写的，差值约等于评测容器的墙钟。
- **失败测试展开后没有错误信息**。Pro 的 parser 只吐 `{name, status}`，
  没有 pytest 那种 short summary。要看原因去 `results/<eval>/<iid>/codex_stdout.log`。
- **「patch 应用失败」在 Pro 里含义更宽**。官方 `eval_results.json` 只有 true/false，
  拉不到镜像、`git apply` 失败、测试进程崩了、解析器炸了，一律记 false，分不开。
  这里的判据是「评测有没有产出 `codex_output.json`」：没有就记应用失败，
  并在 `eval_pro.json` 里写 `note: "no_eval_output"`。真正的原因得去
  `codex_stdout.log` / `codex_stderr.log` 和评测那轮的终端输出里翻。
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
5. **372 条的 `pass_to_pass` 是空的**（go 257/280、ts 19/20）。
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
  推理侧 `run_codex_pro.py` 也在 `git add` 时用 `:(exclude)` 把测试文件挡在 patch 之外，
  两头都堵上了。

---

## 完整命令速查

```bash
# ── 一次性安装 ──
git clone <你的仓库地址> ~/swebench-pro
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
| `docker run` 报 `no matching manifest` | 没开 Rosetta，或漏了 `--platform linux/amd64` |
| 推理正常但 patch 恒为空 | 端点不支持 Responses API。用第三节的 curl 验一下 |
