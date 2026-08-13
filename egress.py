#!/usr/bin/env python3
"""出网收敛：让 agent 容器只能连到一个钉死的目的地（模型 API），别的哪都去不了。

为什么要有这东西（Pro 这边是**实测抓到过**的，不是假想）
  agent 在容器里是 root，`-s danger-full-access`，网络又是通的（要连 API）。于是它可以：
      curl https://github.com/<repo>/commit/<fix_hash>.patch   ← fix_hash 从 instance_id 白送
      curl https://patch-diff.githubusercontent.com/raw/<repo>/pull/<n>.diff
      curl https://api.github.com/repos/<repo>/pulls/<n>/files
      git ls-remote / pip download / go mod download
  results/smoke 那轮的日志里，agent 干的就是第一条 —— 直接按 instance_id 里的 40 位 hash
  拉 upstream.patch。而且 `.patch` 里**连判分用的测试改动一起给**，等于把答案和答案的
  判据一并抄走。

  ⚠️ 和 run_codex_pro.py 里的 strip_history 不是一回事，两者不能互相替代：
     strip_history 堵的是「从镜像自带的 .git 里挖」，egress 堵的是「从公网抄」。
     实测 results/host-strip 那轮就是剥了 .git 之后，agent 原地改走公网，分数照拿。

机制（三层，缺一不可）
  1. docker network create --internal sweb_iso
     无 NAT、无路由出去、DNS 转发也死。agent 容器只挂这一张网。
  2. relay 容器：一头接 sweb_iso（内），一头接默认 bridge（外），
     目的地在启动时钉死（见 sni_relay.py）。
  3. agent 容器：--network sweb_iso --add-host <api_host>:<relay 在 iso 里的 IP>

不依赖 agent 配合：网络命名空间里只有 relay 一个可达对端。比 HTTPS_PROXY
环境变量强 —— 那玩意 agent 一句 env -u 就摘了。

⚠️ 为什么不能用「域名黑名单」那种拦法
  实测日志里 agent 找到过 gitlab.wolfspyre.io 上的 ansible 镜像仓库。公网上这种镜像
  站有的是，黑名单永远补不全。只有「白名单只剩一个目的地」这一种拦法是收敛的。

⚠️ 为什么不能只用 socat 钉死目的 IP（本机实测，见 sni_relay.py 顶部）
  宿主机的 Clash/Surge 类 TUN 代理**按 SNI 路由，不看目的 IP**。只钉 IP 的话，
  agent 在 /etc/hosts 里把 github.com 指向 relay 就能拿到真实 GitHub 内容
  （实测 200 / 真实 pytest README / 证书 CN=github.com verify ok）。
  所以远端 HTTPS 目的地必须走 sni 模式，由 relay 自己校验 SNI。

⚠️ 覆盖不到 web_search —— 那是**服务端**执行的，走的就是 API 通道本身，relay
  看不见也拦不住。必须另外用 -c web_search=disabled 关掉（run_codex_pro.py 里已硬编码）。
  两件事不能互相替代：日志里那些 `#ws_call_id=` 就是 web_search 找到 PR 号，
  然后 agent 再用 curl 去把 diff 拉下来 —— 两条通道是接力的，得各堵各的。

⚠️ 代价：agent 装不了任何包（pip/apt/go mod 全废）。Pro 的镜像是整仓依赖装好的，
  可接受。但不能因此放行 PyPI/proxy.golang.org —— 那和 GitHub 是同一条泄漏通道。

与 SWE-bench-Verified 的关系
  sni_relay.py 与那边**逐字节相同**（有意为之）：relay 容器的指纹里含脚本正文，
  内容一致两边就复用同一个 relay 容器，而不是互相 rm -f 把对方跑到一半的 relay 干掉。
  改这个文件时请两边同步改。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

NETWORK = "sweb_iso"
RELAY_IMAGE = "python:3.13-alpine"
RELAY_SCRIPT = Path(__file__).resolve().parent / "sni_relay.py"

# 目的地在宿主机自己身上时走 plain 模式：明文、无 SNI、流量根本不出宿主机，
# 天然钉死（本机四种伪造手法实测全部落回本地进程，拿不到外网内容）。
HOST_LOCAL = {"host.docker.internal", "localhost", "127.0.0.1", "::1",
              "gateway.docker.internal", "docker.for.mac.host.internal"}

EGRESS_OK = "===EGRESS_OK==="
EGRESS_FAIL = "===EGRESS_FAIL"
_FAIL_RE = re.compile(r"===EGRESS_FAIL ([^=]*)===")


class EgressError(RuntimeError):
    pass


def _docker(*args: str, check: bool = True) -> str:
    p = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise EgressError(f"docker {' '.join(args)} 失败: {p.stderr.strip()}")
    return p.stdout.strip()


def parse_target(base_url: str) -> tuple[str, int, str]:
    """--base-url → (host, port, mode)。mode 决定 relay 怎么把关。"""
    u = urlparse(base_url if "://" in base_url else f"https://{base_url}")
    host, scheme = (u.hostname or ""), (u.scheme or "https")
    if not host:
        raise EgressError(f"解析不出主机名: {base_url!r}")
    port = u.port or (443 if scheme == "https" else 80)
    if host in HOST_LOCAL:
        return host, port, "plain"
    if scheme == "https":
        return host, port, "sni"
    # 远端明文 HTTP：没有 SNI 可校验，Clash 之类可能按 Host 头路由，钉不住。
    raise EgressError(
        f"远端明文 HTTP 端点无法钉死出网: {base_url}\n"
        f"  改用 https:// 端点，或先起 bridge.py 走 "
        f"http://host.docker.internal:<port>/v1（本地明文，已验证无洞），\n"
        f"  或显式 --egress off 放弃隔离（会记进 run_meta.json）。")


def _relay_name(host: str, port: int) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", host)
    return f"sweb_relay_{safe}_{port}"


def _ensure_network() -> None:
    if _docker("network", "ls", "--filter", f"name=^{NETWORK}$", "--format", "{{.Name}}"):
        return
    _docker("network", "create", "--internal", NETWORK)


def ensure_egress(base_url: str) -> dict:
    """建网 + 起/复用 relay，返回给 docker run 用的信息。幂等。"""
    if not RELAY_SCRIPT.exists():
        raise EgressError(f"找不到 {RELAY_SCRIPT}")
    host, port, mode = parse_target(base_url)
    name = _relay_name(host, port)
    # 脚本内容进指纹：改了 relay 逻辑必须重建容器，否则会拿旧代码把关
    stamp = hashlib.sha256(
        f"{mode}|{host}|{port}|{RELAY_SCRIPT.read_text()}".encode()).hexdigest()[:16]

    _ensure_network()

    cur = _docker("ps", "-a", "--filter", f"name=^{name}$",
                  "--format", "{{.State}}\t{{.Label \"sweb.stamp\"}}", check=False)
    state, _, got = cur.partition("\t")
    if state != "running" or got != stamp:
        if cur:
            _docker("rm", "-f", name, check=False)
        _docker("run", "-d", "--name", name, "--network", "bridge", "--restart", "no",
                "--label", f"sweb.stamp={stamp}",
                "--label", f"sweb.pin={host}:{port}",
                "-v", f"{RELAY_SCRIPT}:/sni_relay.py:ro",
                "-e", f"MODE={mode}", "-e", f"PIN_HOST={host}",
                "-e", f"PIN_PORT={port}", "-e", f"LISTEN_PORT={port}",
                RELAY_IMAGE, "python3", "/sni_relay.py")
        _docker("network", "connect", NETWORK, name)

    ip = _docker("inspect", "-f",
                 '{{(index .NetworkSettings.Networks "' + NETWORK + '").IPAddress}}', name)
    if not ip:
        raise EgressError(f"取不到 relay {name} 在 {NETWORK} 里的 IP")
    return {"network": NETWORK, "host": host, "port": port, "mode": mode,
            "relay": name, "relay_ip": ip, "label": f"pinned:{host}:{port}({mode})"}


def docker_run_args(info: dict | None) -> list[str]:
    """插进 docker run 的参数。info 为 None（--egress off）时返回空。"""
    if not info:
        return []
    return ["--network", info["network"],
            "--add-host", f"{info['host']}:{info['relay_ip']}"]


# ── 容器内探针 ────────────────────────────────────────────────────────────
# 探针取真实内容里的固定串，且这些串**不可能出现在请求路径里** —— 否则任何回显
# path 的服务端都会把探针骗成「泄漏」（Verified 那边实测踩过这个坑）。
PROBES = [("/pytest-dev/pytest/main/LICENSE", "Holger Krekel"),
          ("/python/cpython/main/LICENSE", "PYTHON SOFTWARE FOUNDATION LICENSE")]

# 伪造 SNI/Host 去打 relay，看能不能拿到**真实 GitHub 内容**。
#
# ⚠️ 为什么不像 Verified 那样只用 curl：Pro 的镜像是各语言各仓库自带的，**不保证有
#    curl**。本机 32 个镜像实测，qutebrowser 那套 curl/wget 都没有。而 curl 缺席时
#    Verified 那个探针里的 `_c` 是空串、永远 != 200，于是**静默判过** —— 正好命中
#    它自己文档里警告的「没跑到探针也算失败」那种情况，却报成了 PASS。
#    所以这里改成 python3 → curl → openssl 三选一，且三个都没有时显式报 noprobe（=失败），
#    绝不静默放过。python3 版直接在 TLS 层伪造 SNI，比 curl --resolve 更贴近真实攻击面。
_PY_PROBE = r'''
import socket, ssl, sys
scheme, ip, port, host, path, marker = sys.argv[1:7]
try:
    s = socket.create_connection((ip, int(port)), timeout=10)
    if scheme == "https":
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        c.check_hostname = False           # 泄漏时证书是真的 CN=github.com，校验拦不住
        c.verify_mode = ssl.CERT_NONE      # 关掉只增不减检出力
        s = c.wrap_socket(s, server_hostname=host)   # ← 这里就是伪造 SNI
    s.settimeout(10)
    s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: p\r\n"
               "Connection: close\r\n\r\n" % (path, host)).encode())
    buf = b""
    while len(buf) < 262144:
        b = s.recv(65536)
        if not b:
            break
        buf += b
    s.close()
except Exception:
    sys.exit(1)
# 必须同时满足「200」和「真实内容里的固定串」，少一个都不算泄漏。
# 状态行只看 " 200"，别写死 HTTP/1.1 —— 1.0 和各种网关的回法都要认。
sys.exit(0 if b" 200" in buf[:64].split(b"\r\n", 1)[0] and marker.encode() in buf else 1)
'''


def probe_snippet(info: dict | None) -> str:
    """容器内自检，放在 agent 启动**之前**跑。

    ⚠️ 光查 DNS 是不够的：Verified 那边实测 SNI 绕过泄漏发生时，
       `getent hosts github.com` 照样失败（=看起来通过），分数照样悄悄涨回去。
       所以这里必须真的去打一次 relay，看能不能拿到**真实 GitHub 内容**。
    """
    if not info:
        return 'echo "===EGRESS_OFF==="'
    ip, port = info["relay_ip"], info["port"]
    scheme = "http" if info["mode"] == "plain" else "https"
    # 两个端口都试：relay 监听端口，以及 agent 最可能直接去敲的 443
    calls = "\n".join(f"_probe {scheme} {port} {p!r} {m!r}\n_probe https 443 {p!r} {m!r}"
                      for p, m in PROBES)
    return f'''
_ef=""
# ① DNS 必须是黑的：curl/git/pip 走的都是它
getent hosts github.com >/dev/null 2>&1 && _ef="dns"

# ② 挑一个可用的探测工具。三个都没有 → noprobe（记失败，不静默放过）
cat > /tmp/.eprobe.py <<'__EPROBE_PY__'
{_PY_PROBE}
__EPROBE_PY__
_tool=""
command -v python3 >/dev/null 2>&1 && _tool=python3
[ -z "$_tool" ] && command -v curl    >/dev/null 2>&1 && _tool=curl
[ -z "$_tool" ] && command -v openssl >/dev/null 2>&1 && _tool=openssl
[ -z "$_tool" ] && _ef="${{_ef:+$_ef,}}noprobe"

# $1=scheme $2=port $3=path $4=marker，命中真实内容就记一条 leak
_probe() {{
  [ -z "$_tool" ] && return 0
  _hit=1
  case "$_tool" in
    python3)
      python3 /tmp/.eprobe.py "$1" {ip} "$2" raw.githubusercontent.com "$3" "$4" && _hit=0 ;;
    curl)
      # -k 只增不减检出力：泄漏时证书是真的 CN=github.com、verify ok，靠证书校验拦不住
      _c=$(curl -sk -o /tmp/.eout -w '%{{http_code}}' --max-time 10 \\
           --resolve "raw.githubusercontent.com:$2:{ip}" \\
           "$1://raw.githubusercontent.com:$2$3" 2>/dev/null)
      [ "$_c" = "200" ] && grep -qF "$4" /tmp/.eout 2>/dev/null && _hit=0
      rm -f /tmp/.eout ;;
    openssl)
      [ "$1" = "https" ] || return 0
      printf 'GET %s HTTP/1.1\\r\\nHost: raw.githubusercontent.com\\r\\nConnection: close\\r\\n\\r\\n' "$3" \\
        | openssl s_client -quiet -verify_quiet -servername raw.githubusercontent.com \\
          -connect {ip}:"$2" 2>/dev/null | grep -qF "$4" && _hit=0 ;;
  esac
  [ "$_hit" = "0" ] && _ef="${{_ef:+$_ef,}}leak:$1/$2"
  return 0
}}
{calls}
rm -f /tmp/.eprobe.py
if [ -n "$_ef" ]; then echo "{EGRESS_FAIL} $_ef==="; else echo "{EGRESS_OK}"; fi
'''.strip()


def parse_probe(out: str) -> tuple[bool, str]:
    """(是否通过, 原因)。没跑到探针也算失败 —— 静默缺失就是最坏的情况。"""
    m = _FAIL_RE.search(out)
    if m:
        return False, m.group(1).strip()
    if EGRESS_OK in out:
        return True, ""
    if "===EGRESS_OFF===" in out:
        return True, "off"
    return False, "no-probe"


def selftest(info: dict | None, image: str) -> tuple[bool, str]:
    """跑分前在真实镜像里过一遍探针。不通过就别开跑。"""
    if not info:
        return True, "off"
    script = probe_snippet(info)
    # Pro 的镜像 ENTRYPOINT 是 bash，这里显式覆盖，和 run_codex_pro.py 保持一致
    p = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64",
                        *docker_run_args(info), "--entrypoint", "/bin/bash",
                        image, "-c", script],
                       capture_output=True, text=True, timeout=300)
    return parse_probe(p.stdout + p.stderr)


def main() -> int:
    """手动验一把：python egress.py <base_url> [镜像]"""
    if len(sys.argv) < 2:
        print(__doc__)
        print("用法: python egress.py <base_url> [镜像]", file=sys.stderr)
        return 1
    try:
        info = ensure_egress(sys.argv[1])
    except EgressError as exc:                # 配置问题，别甩 traceback
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(info, indent=2, ensure_ascii=False))
    img = sys.argv[2] if len(sys.argv) > 2 else None
    if img:
        ok, why = selftest(info, img)
        print(f"自检: {'PASS' if ok else 'FAIL ' + why}")
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
