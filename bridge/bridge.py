#!/usr/bin/env python3
"""协议桥：让 Claude Code 跑 OpenAI 协议的模型，让 Codex 跑 Anthropic 协议的模型。

这是**扩展功能**，主流程（run_codex_agent.py / run_claude_agent.py / local_eval.py /
make_report.py）一个字都不改 —— 桥只是个本地 HTTP 端点，把 `--base-url` 指过来就行。

两个方向：

  claude-on-openai   对外说 Anthropic 协议（POST /v1/messages），对内说 OpenAI 协议
                     （POST /chat/completions）。给 run_claude_agent.py 用。

  codex-on-anthropic 对外说 OpenAI Responses 协议（POST /v1/responses），对内说
                     Anthropic 协议（POST /v1/messages）。给 run_codex_agent.py /
                     run_codex_host.py 用（Codex 0.146+ 只认 wire_api=responses）。

内核用 LiteLLM Proxy —— 不自己写 schema 转换。两个方向它都原生支持，本文件只做三件事：
把命令行参数翻译成 LiteLLM 的 config.yaml、起进程、把踩过的坑钉死在配置里。

用法（先跑 ./bridge/setup.sh 装环境）：

  bridge/.venv/bin/python bridge/bridge.py claude-on-openai \
      --base-url https://api.deepseek.com/v1 --model deepseek-chat --api-key sk-xxx

  bridge/.venv/bin/python bridge/bridge.py codex-on-anthropic \
      --base-url https://api.anthropic.com --model claude-sonnet-4-5-20250929 --api-key sk-ant-xxx

起来之后桥自己会打印下游该怎么接（--print-usage 单独再打一遍）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 桥自己的门钥匙。下游 CLI 拿这个当 api-key，真正的上游 key 只活在桥进程里，
# 不会进容器、不会进 preds.json、不会进 shell history。
DEFAULT_MASTER_KEY = "sk-bridge"

MODES = {
    # mode: (LiteLLM 侧的 provider 前缀, 对外门面, 下游是谁)
    "claude-on-openai": ("openai", "/v1/messages (Anthropic)", "Claude Code"),
    "codex-on-anthropic": ("anthropic", "/v1/responses (OpenAI Responses)", "Codex CLI"),
    "codex-on-openai": ("openai", "/v1/responses (OpenAI Responses)", "Codex CLI"),
}


def build_config(args) -> dict:
    """生成 LiteLLM 的 config.yaml 内容。

    model_list 放两条是有原因的：
      1. 精确名 —— 下游传什么 model 就叫什么，preds.json 里的 model_name_or_path
         才能记下真实模型名，而不是一个 "bridge" 之类的占位符。
      2. "*" 兜底 —— Claude Code 除了主模型还会打 haiku 档位的后台请求（标题、
         小工具调用），run_claude_agent.py 虽然把四个档位别名都 pin 死了，但
         宿主机直跑 / 手动 export 的场景 pin 不全，漏一个就是 400。兜底这条
         把任何未知模型名都收到同一个上游模型上。
    """
    provider, _, _ = MODES[args.mode]
    params = {
        "model": f"{provider}/{args.model}",
        "api_key": args.api_key,
    }
    if args.base_url:
        params["api_base"] = args.base_url
    if args.mode in ("claude-on-openai", "codex-on-openai") and args.upstream_api == "chat":
        # ⚠️ 关键的一脚。LiteLLM 的 /v1/messages 与 /v1/responses 两个门面，对 openai/
        # 前缀的模型**默认都走上游的 /responses**，而绝大多数第三方 OpenAI 兼容端点
        # （DeepSeek / Kimi / GLM / Qwen / vLLM / Ollama）只有 /chat/completions，
        # 不加这个就是 404 或者「返回体解析失败」的 APIError，报错完全看不出原因。
        # 打开后走 litellm/responses/litellm_completion_transformation 那套
        # Responses→ChatCompletions 转换（main.py 里 `responses_api_provider_config is
        # None or use_chat_completions_api is True` 那个分支）。
        params["use_chat_completions_api"] = True
    if args.mode == "codex-on-openai" and args.max_tokens > 0:
        # 与 codex-on-anthropic 同理：Codex 的 Responses 请求不带 max_output_tokens。
        # OpenAI 协议的 max_tokens 虽非必填，但多数第三方端点有个偏小的默认值
        # （DeepSeek 实测 4096），大 patch 会被腰斩成残缺 diff，这里显式抬高。
        params["max_tokens"] = args.max_tokens
    if args.mode == "codex-on-anthropic" and args.max_tokens > 0:
        # ⚠️ 也是必须的一脚。Codex 的 Responses 请求**不带** max_output_tokens，
        # 而 Anthropic 的 /v1/messages 把 max_tokens 列为必填 —— LiteLLM 只好补一个
        # 默认值 4096（实测抓包确认）。跑 SWE-bench 时一个大 patch 轻松超过 4096，
        # 结果就是输出被腰斩、diff 残缺、还查不出原因。这里必须显式抬高。
        params["max_tokens"] = args.max_tokens
    if args.mode == "codex-on-anthropic" and args.thinking_budget > 0:
        # Anthropic 的 extended thinking 得显式开。Codex 传下来的
        # model_reasoning_effort 到不了 Anthropic 那边，只能在桥上钉。
        params["thinking"] = {"type": "enabled", "budget_tokens": args.thinking_budget}
    # ⚠️ prompt cache 是这条链路上钱的大头，但**不在这里配** —— LiteLLM 自带的
    # cache_control_injection_points 作用在 chat 消息上，断点到不了 Anthropic 的
    # system 块和 tool_result 块（实测 9 次请求只有 1 次真的带上了断点）。改由
    # bridge_patch.inject_cache_control 在最终的 Anthropic body 上打，位置通过
    # BRIDGE_CACHE_POINTS 环境变量下发。详见 bridge_patch.py 补丁三。

    cfg = {
        "model_list": [
            {"model_name": args.model, "litellm_params": dict(params)},
            {"model_name": "*", "litellm_params": dict(params)},
        ],
        "litellm_settings": {
            # ⚠️ 必开。两个方向都会带过来对面协议没有的参数：Codex 会发
            # store / include / prompt_cache_key / text.verbosity，Claude Code 会发
            # thinking / top_k。不 drop 的话上游直接 400，而且报错信息很难看懂。
            "drop_params": True,
            "num_retries": args.num_retries,
            "request_timeout": args.request_timeout,
        },
        "general_settings": {
            "master_key": args.master_key,
        },
    }
    return cfg


def usage_text(args) -> str:
    provider, surface, downstream = MODES[args.mode]
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '127.0.0.1') else args.host}:{args.port}"
    in_container = f"http://host.docker.internal:{args.port}"
    lines = [
        "",
        "=" * 72,
        f"协议桥：{args.mode}",
        f"  对外门面   {surface}",
        f"  对内上游   {provider} :: {args.model} @ {args.base_url or '(provider 默认端点)'}",
        f"  监听       {args.host}:{args.port}",
        f"  下游 key   {args.master_key}   ← 下游 CLI 用这个，不是上游真 key",
        "-" * 72,
    ]
    if args.mode == "claude-on-openai":
        lines += [
            f"给 {downstream} 用（宿主机直跑）：",
            f"  --base-url {url} --api-key {args.master_key} --model {args.model}",
            "",
            "给 run_claude_agent.py 用（agent 在容器里，必须走 host.docker.internal，",
            "且桥要 --host 0.0.0.0 才能被容器访问）：",
            "  python run_claude_agent.py --subset verified --slice 0:2 \\",
            f"    --model {args.model} \\",
            f"    --base-url {in_container} \\",
            f"    --api-key {args.master_key} \\",
            "    --auth-style bearer \\",
            "    -o results/bridge-smoke/preds.json",
        ]
    else:
        lines += [
            f"给 {downstream} 用（宿主机直跑，run_codex_host.py 走 ~/.codex 登录态，",
            "要用桥得改用 run_codex_agent.py 或手动 codex exec）：",
            f"  codex exec -c model_provider=bridge \\",
            f"    -c model_providers.bridge.name=bridge \\",
            f"    -c model_providers.bridge.base_url={url}/v1 \\",
            f"    -c model_providers.bridge.env_key=CODEX_API_KEY \\",
            f"    -c model_providers.bridge.wire_api=responses \\",
            f"    -m {args.model} 'hi'",
            "",
            "给 run_codex_agent.py 用（agent 在容器里，同样走 host.docker.internal）：",
            "  python run_codex_agent.py --subset verified --slice 0:2 \\",
            f"    --model {args.model} --provider bridge \\",
            f"    --base-url {in_container}/v1 \\",
            f"    --api-key {args.master_key} \\",
            "    --codex-bin codex-bin/codex-x86_64-unknown-linux-musl \\",
            "    -o results/bridge-smoke/preds.json",
        ]
    lines += ["=" * 72, ""]
    return "\n".join(lines)


# 不直接调 `litellm` 那个可执行文件，而是自己 bootstrap：因为要在 proxy 起来之前
# 打上 bridge_patch（LiteLLM 会把 assistant 的 text 块排到 tool_use 之后，Anthropic
# 当成 prefill 直接 400 —— 详见 bridge_patch.py 的模块注释）。
# python -c 时 sys.argv 是 ["-c", *后面的参数]，click 取 argv[1:]，正好是 litellm 的参数。
BOOTSTRAP = (
    "import sys; sys.path.insert(0, {here!r});"
    "import bridge_patch; bridge_patch.apply();"
    "from litellm.proxy.proxy_cli import run_server; run_server()"
)


def proxy_cmd(args: list[str]) -> list[str]:
    """起 LiteLLM proxy 的命令（带补丁）。用当前解释器 —— 也就是 bridge/.venv。"""
    try:
        import litellm  # noqa: F401
    except ImportError:
        sys.exit("当前解释器里没有 litellm。先跑 ./bridge/setup.sh，"
                 "然后用 bridge/.venv/bin/python 跑本脚本。")
    return [sys.executable, "-c", BOOTSTRAP.format(here=str(HERE))] + args


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=list(MODES),
                    help="claude-on-openai：Claude Code 跑 OpenAI 协议模型；"
                         "codex-on-anthropic：Codex 跑 Anthropic 协议模型；"
                         "codex-on-openai：Codex 跑只有 /chat/completions 的 OpenAI 协议模型")
    ap.add_argument("--base-url", default="",
                    help="上游端点。*-on-openai 填 OpenAI 兼容地址（到 /v1 为止）；"
                         "codex-on-anthropic 填 Anthropic 兼容地址（不含 /v1）。留空＝用 provider 官方端点")
    ap.add_argument("--model", required=True, help="上游真实模型名；下游也用这个名字点它")
    ap.add_argument("--api-key", default="", help="上游 key。不想留 history 就换 --env-key")
    ap.add_argument("--env-key", default="",
                    help="从这个环境变量取上游 key，优先级低于 --api-key")
    ap.add_argument("--master-key", default=DEFAULT_MASTER_KEY,
                    help=f"桥自己的门钥匙，下游 CLI 用它当 api-key（默认 {DEFAULT_MASTER_KEY}）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址。agent 跑在容器里时必须 0.0.0.0，否则容器连不上")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--upstream-api", default="chat", choices=["chat", "responses"],
                    help="仅 *-on-openai：上游用哪个 OpenAI 端点。"
                         "chat＝/chat/completions（默认，兼容面最广）；"
                         "responses＝/responses（只有官方 OpenAI 等少数端点支持）")
    ap.add_argument("--max-tokens", type=int, default=32000,
                    help="仅 codex-on-anthropic / codex-on-openai：给上游补的 max_tokens。"
                         "Codex 自己不发这个字段，不补的话会被上游的小默认值（Anthropic 侧 "
                         "LiteLLM 兜底 4096，DeepSeek 实测 4096）截断，大 patch 出残缺 diff。"
                         "别超过目标模型的单次输出上限；0＝不补")
    ap.add_argument("--thinking-budget", type=int, default=0,
                    help="仅 codex-on-anthropic：开 Anthropic extended thinking 并给出预算 token；"
                         "0＝不开。Codex 的 model_reasoning_effort 传不到 Anthropic，只能在这儿钉")
    ap.add_argument("--cache-points", default="system,prev,last",
                    help="仅 codex-on-anthropic：给上游注入 cache_control 断点，逗号分隔，"
                         "可选 system / prev / last（默认三个都注）。LiteLLM 默认一个都不注，"
                         "长 agent 轮次会按未命中价重付整段上下文。空字符串＝关掉")
    ap.add_argument("--num-retries", type=int, default=2)
    ap.add_argument("--request-timeout", type=int, default=1800,
                    help="单次上游请求超时秒数；agent 长任务别设太小")
    ap.add_argument("--detailed-debug", action="store_true",
                    help="打印每次请求/响应的完整 body，调协议不兼容时开")
    ap.add_argument("--config-out", default="",
                    help="把生成的 LiteLLM config.yaml 落到这个路径（默认写临时文件）")
    ap.add_argument("--print-usage", action="store_true",
                    help="只打印下游接法然后退出，不起服务")
    args = ap.parse_args()

    args.api_key = args.api_key or (os.environ.get(args.env_key, "") if args.env_key else "")
    if not args.api_key and not args.print_usage:
        hint = f"，或 export {args.env_key}=..." if args.env_key else ""
        sys.exit(f"没有上游 API key：加 --api-key ...{hint}")

    if args.print_usage:
        print(usage_text(args))
        return 0

    import yaml  # litellm 自带，不额外声明依赖

    cfg = build_config(args)
    if args.config_out:
        cfg_path = Path(args.config_out)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        cfg_path = Path(tempfile.mkdtemp(prefix="bridge-")) / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))

    print(usage_text(args), flush=True)
    print(f"[bridge] LiteLLM config: {cfg_path}", flush=True)

    proxy_args = ["--config", str(cfg_path), "--host", args.host, "--port", str(args.port)]
    if args.detailed_debug:
        proxy_args.append("--detailed_debug")
    cmd = proxy_cmd(proxy_args)

    env = dict(os.environ)
    # 别让宿主机上已有的 ANTHROPIC_* / OPENAI_* 泄进桥进程，把上游打到别处去。
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        env.pop(k, None)
    # LiteLLM 无 DB 模式下会抱怨这些，关掉噪音
    env.setdefault("LITELLM_MODE", "PRODUCTION")
    env.setdefault("DISABLE_SCHEMA_UPDATE", "True")
    # 补丁三读这个决定往哪儿打 prompt cache 断点（仅 Anthropic 方向有意义）
    env["BRIDGE_CACHE_POINTS"] = args.cache_points if args.mode == "codex-on-anthropic" else ""

    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
