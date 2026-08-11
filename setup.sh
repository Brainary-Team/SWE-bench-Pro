#!/usr/bin/env bash
# 一键准备 SWE-bench Pro 评测环境。
#
#   ./setup.sh                 # 虚拟环境 + 依赖 + 官方仓库 + Codex 二进制 + 自检
#   ./setup.sh --skip-codex    # 不要 Codex 二进制
#   ./setup.sh --skip-dataset  # 不拉数据集（731 条，约 24 MB）
#
# 幂等：重复跑不会重复下载，已就位的步骤直接跳过。
set -euo pipefail

cd "$(dirname "$0")"

PY_VERSION=3.12
CODEX_BIN=codex-bin/codex-x86_64-unknown-linux-musl
UPSTREAM=SWE-bench_Pro-os
UPSTREAM_URL=https://github.com/scaleapi/SWE-bench_Pro-os.git
# 钉死上游版本：run_scripts/ 和 dockerfiles/ 要跟数据集对得上，
# 上游一动可能就有实例的 parser 或 Dockerfile 对不上号，分数不可比。
UPSTREAM_REF=ca10a60a5fcae51e6948ffe1485d4153d421e6c5
DATASET=swebench_pro.jsonl
SKIP_CODEX=0
SKIP_DATASET=0

# macOS 自带 bash 3.2：用 while + $1，避开它在 set -u 下把空 "$@" 当未绑定的老 bug。
while [ $# -gt 0 ]; do
  arg=$1
  case "$arg" in
    --skip-codex)   SKIP_CODEX=1 ;;
    --skip-dataset) SKIP_DATASET=1 ;;
    -h|--help)      sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: ${arg}（用 --help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ── 1. 虚拟环境 ─────────────────────────────────────────────
step "创建虚拟环境 (.venv, Python $PY_VERSION)"
if command -v uv >/dev/null 2>&1; then
  [ -d .venv ] || uv venv --python "$PY_VERSION"
  PIP_INSTALL="uv pip install"
else
  warn "没装 uv，退回 python3 -m venv（装依赖会慢一些）"
  warn "想用 uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
  [ -d .venv ] || python3 -m venv .venv
  PIP_INSTALL=".venv/bin/pip install"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
ok "$(python --version)"

# ── 2. 依赖 ────────────────────────────────────────────────
step "安装 Python 依赖"
$PIP_INSTALL -r requirements.txt
ok "datasets / docker / pandas 就位"

# ── 3. 官方仓库 ─────────────────────────────────────────────
# 评测直接调它的 swe_bench_pro_eval.py，还要用它的 run_scripts/（每条实例的
# 跑测脚本 + 结果解析器）和 dockerfiles/。本仓库一个字节都不改它。
step "拉官方仓库 $UPSTREAM"
if [ -d "$UPSTREAM/.git" ]; then
  ok "已存在，跳过 clone（$(git -C "$UPSTREAM" rev-parse --short HEAD)）"
else
  git clone --quiet "$UPSTREAM_URL" "$UPSTREAM" || die "clone 失败"
  git -C "$UPSTREAM" checkout --quiet "$UPSTREAM_REF" || die "checkout $UPSTREAM_REF 失败"
  ok "$UPSTREAM @ $(git -C "$UPSTREAM" rev-parse --short HEAD)"
fi
[ -d "$UPSTREAM/run_scripts" ] || die "$UPSTREAM/run_scripts 不在，clone 不完整"
ok "run_scripts $(ls "$UPSTREAM/run_scripts" | wc -l | tr -d ' ') 个 · dockerfiles $(ls "$UPSTREAM/dockerfiles/base_dockerfile" | wc -l | tr -d ' ') 个"

# ── 4. Codex Linux 二进制 ───────────────────────────────────
# 官方镜像不可改，Codex 只能以静态二进制挂载进容器跑。
if [ "$SKIP_CODEX" = 1 ]; then
  step "跳过 Codex 二进制（--skip-codex）"
elif [ -x "$CODEX_BIN" ]; then
  step "Codex 二进制"
  ok "已存在，跳过下载：$CODEX_BIN"
else
  step "下载 Codex Linux x86_64 静态二进制"
  command -v npm >/dev/null 2>&1 || die "需要 npm（brew install node），或改用 --skip-codex"
  mkdir -p codex-bin
  (
    cd codex-bin
    npm i @openai/codex --os=linux --cpu=x64 --no-audit --no-fund
    cp node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex \
       codex-x86_64-unknown-linux-musl
    chmod +x codex-x86_64-unknown-linux-musl
    rm -rf node_modules package.json package-lock.json   # 留着白占 297 MB
  )
  file "$CODEX_BIN" | grep -q 'x86-64' || die "拿到的不是 x86_64 二进制，检查 npm 的 --os/--cpu 参数"
  ok "$CODEX_BIN"
fi

# ── 5. 数据集 ──────────────────────────────────────────────
step "拉数据集（HuggingFace ScaleAI/SWE-bench_Pro，731 条）"
if [ "$SKIP_DATASET" = 1 ]; then
  warn "跳过（--skip-dataset）"
elif [ -s "$DATASET" ]; then
  ok "已存在，跳过：$DATASET（$(wc -l < "$DATASET" | tr -d ' ') 条）"
else
  python fetch_dataset.py --output "$DATASET" || die "拉数据集失败（网络？）"
fi

# ── 6. Docker 自检 ─────────────────────────────────────────
step "检查 Docker 与 x86_64 模拟"
if ! command -v docker >/dev/null 2>&1; then
  warn "没装 Docker Desktop —— 推理和评测都跑不了，装完再跑一次本脚本"
elif ! docker info >/dev/null 2>&1; then
  warn "Docker 没启动：open -a Docker，等 docker info 不报错后重跑本脚本"
else
  ok "Docker $(docker info --format '{{.ServerVersion}}')"
  arch=$(docker run --rm --platform linux/amd64 alpine uname -m 2>/dev/null || echo FAIL)
  if [ "$arch" = "x86_64" ]; then
    ok "amd64 容器可用（uname -m = x86_64）"
    if [ "$SKIP_CODEX" != 1 ] && [ -x "$CODEX_BIN" ]; then
      ver=$(docker run --rm --platform linux/amd64 \
              -v "$PWD/$CODEX_BIN:/usr/local/bin/codex:ro" \
              alpine codex --version 2>/dev/null || echo FAIL)
      [ "$ver" = FAIL ] && warn "二进制挂进容器跑不起来，检查 $CODEX_BIN" || ok "容器内 $ver"
    fi
  else
    warn "amd64 容器起不来（得到：${arch}）"
    warn "Docker Desktop → Settings → General 勾上"
    warn "  ☑ Use Rosetta for x86_64/amd64 emulation on Apple Silicon"
    warn "不开 Rosetta 会退化成 QEMU 软件模拟，慢到不可用。"
  fi
fi

# ── 7. 磁盘提醒 ────────────────────────────────────────────
step "磁盘"
df -h . | tail -1 | awk '{print "  可用 " $4 "（Pro 的镜像是整仓依赖，单个好几个 G；"\
  "全量 731 条务必给命令加 --rm-image）"}'

cat <<'EOF'

──────────────────────────────────────────────
准备完成。每次开工先激活环境：

  source .venv/bin/activate

然后按 README 的「冒烟测试」跑 2 条验证链路。
──────────────────────────────────────────────
EOF
