#!/usr/bin/env python3
"""把出网收敛到一个钉死目的地的中继。在 relay 容器里跑，不在宿主机跑。

两种模式：

  plain  —— 纯 TCP 转发。用于目的地是**宿主机本地进程**的场景
             （bridge.py 那种 http://host.docker.internal:4001）。
             明文、无 TLS、无 SNI，目的地根本不出宿主机，天然钉死。

  sni    —— 转发前先读 TLS ClientHello，SNI ≠ 钉死主机名就直接断连。
             用于目的地是**远端 HTTPS**的场景（https://api.deepseek.com）。

为什么 sni 模式是必需的、光靠 socat 钉死目的地不够：
  宿主机若跑着 Clash/Surge 这类 TUN + fake-ip 代理（本机实测 Clash Verge，
  utun1024 / 198.18.0.0/15），它**按 SNI 路由，不看目的 IP**。实测同一个目的
  IP 换三个 SNI 就落到三个真实站点：

      198.18.0.110 + SNI example.com              → Example Domain
      198.18.0.110 + SNI github.com               → GitHub 首页 572 KB
      198.18.0.110 + SNI raw.githubusercontent.com → 真实 pytest README

  也就是说 agent 只要在 /etc/hosts 里把 github.com 指向 relay，就能拿到
  **真实 GitHub 内容**，证书还是 CN=github.com、verify ok —— 目的地钉死被
  彻底架空。SNI 一过滤，这条路就断了，且不依赖宿主机代理怎么配。

环境变量：MODE / PIN_HOST / PIN_PORT / LISTEN_PORT
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading

MODE = os.environ.get("MODE", "plain")
PIN_HOST = os.environ["PIN_HOST"]
PIN_PORT = int(os.environ["PIN_PORT"])
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", PIN_PORT))

# ClientHello 再大也就几 KB；给足上限，避免被慢速/超大握手拖住
MAX_HELLO = 16384
HELLO_TIMEOUT = 15


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def parse_sni(data: bytes) -> str | None:
    """从一条 TLS record 里抠出 SNI。抠不出来返回 None（→ 一律拒绝）。

    刻意写得保守：任何长度对不上、类型不认识的情况都返回 None。
    这里的失败方向必须是「拒绝」，不能是「放行」。
    """
    try:
        # TLS record: type(1) version(2) length(2)
        if len(data) < 5 or data[0] != 0x16:      # 0x16 = handshake
            return None
        p = 5
        if data[p] != 0x01:                        # 0x01 = ClientHello
            return None
        p += 4                                     # handshake type(1) + length(3)
        p += 2                                     # client_version
        p += 32                                    # random
        p += 1 + data[p]                           # session_id
        p += 2 + struct.unpack_from(">H", data, p)[0]   # cipher_suites
        p += 1 + data[p]                           # compression_methods
        if p + 2 > len(data):
            return None
        ext_end = p + 2 + struct.unpack_from(">H", data, p)[0]
        p += 2
        while p + 4 <= min(ext_end, len(data)):
            etype, elen = struct.unpack_from(">HH", data, p)
            p += 4
            if etype != 0x0000:                    # 0x0000 = server_name
                p += elen
                continue
            q = p + 2                              # server_name_list length
            if data[q] != 0:                       # name_type 0 = host_name
                return None
            q += 1
            nlen = struct.unpack_from(">H", data, q)[0]
            q += 2
            return data[q:q + nlen].decode("ascii").rstrip(".").lower()
    except Exception:
        return None
    return None


def read_hello(sock: socket.socket) -> bytes | None:
    """读完整一条 TLS record。读不满就返回 None。"""
    buf = b""
    sock.settimeout(HELLO_TIMEOUT)
    while len(buf) < 5:
        chunk = sock.recv(5 - len(buf))
        if not chunk:
            return None
        buf += chunk
        # 不是 handshake record 就立刻断（明文 HTTP 打过来是 'GET '=0x47）。
        # 不早断的话会拿垃圾长度傻等到超时，白占 15 秒和一个线程。
        if buf[0] != 0x16:
            return None
    total = 5 + struct.unpack_from(">H", buf, 3)[0]
    if total > MAX_HELLO:
        return None
    while len(buf) < total:
        chunk = sock.recv(total - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            b = src.recv(65536)
            if not b:
                break
            dst.sendall(b)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket) -> None:
    upstream = None
    try:
        head = b""
        if MODE == "sni":
            head = read_hello(client) or b""
            sni = parse_sni(head) if head else None
            if sni != PIN_HOST.lower():
                # 这行就是防作弊的现场证据，relay 日志里能直接审计
                log(f"DENY sni={sni!r} != pinned {PIN_HOST!r}")
                return
        client.settimeout(None)
        upstream = socket.create_connection((PIN_HOST, PIN_PORT), timeout=30)
        upstream.settimeout(None)
        if head:
            upstream.sendall(head)
        t = threading.Thread(target=pump, args=(client, upstream), daemon=True)
        t.start()
        pump(upstream, client)
        t.join(timeout=5)
    except Exception as exc:
        log(f"ERR {exc}")
    finally:
        for s in (client, upstream):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


def main() -> int:
    if MODE not in ("plain", "sni"):
        log(f"MODE 只能是 plain / sni，收到 {MODE!r}")
        return 1
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(128)
    log(f"relay up: mode={MODE} listen=:{LISTEN_PORT} pinned={PIN_HOST}:{PIN_PORT}")
    while True:
        try:
            conn, _ = srv.accept()
        except OSError as exc:
            log(f"accept 失败: {exc}")
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
