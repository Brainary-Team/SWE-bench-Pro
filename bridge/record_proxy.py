#!/usr/bin/env python3
"""录制型透明代理：坐在桥和真上游之间，把每个请求/响应落盘再转发。

只为排查协议转换问题用 —— 跑分时把 --base-url 直接指回真上游，别留着它。

  python bridge/record_proxy.py --upstream https://api.deepseek.com/v1 --port 4100 --out logs/rec
  # 然后桥的 --base-url 指向 http://127.0.0.1:4100/v1
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ARGS = None
SEQ = [0]


USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")


def _append_usage(outdir: Path, status: int, data: bytes) -> None:
    """从响应里抠出 usage 追加到 usage.jsonl。流式和非流式都吃。"""
    u: dict = {}
    text = data.decode("utf-8", errors="replace")
    if text.lstrip().startswith("{"):                      # 非流式
        try:
            u = (json.loads(text).get("usage") or {})
        except Exception:
            u = {}
    else:                                                   # SSE：message_start 给
        for line in text.splitlines():                      # 输入侧，message_delta
            if not line.startswith("data: "):               # 给最终 output_tokens
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            if ev.get("type") == "message_start":
                u.update(ev.get("message", {}).get("usage") or {})
            elif ev.get("type") == "message_delta":
                u.update(ev.get("usage") or {})
    rec = {"status": status, **{k: u.get(k, 0) for k in USAGE_KEYS}}
    with (outdir / "usage.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音默认访问日志
        pass

    def do_POST(self):
        n = SEQ[0] = SEQ[0] + 1
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        outdir = Path(ARGS.out)
        outdir.mkdir(parents=True, exist_ok=True)
        if not ARGS.usage_only:
            try:
                parsed = json.loads(body)
                (outdir / f"{n:03d}-req.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
            except Exception:
                (outdir / f"{n:03d}-req.bin").write_bytes(body)

        url = ARGS.upstream.rstrip("/") + self.path.replace("/v1", "", 1)
        req = urllib.request.Request(url, data=body, method="POST")
        # 转发除 hop-by-hop / 长度 / Host 之外的全部头。Anthropic 方向靠的是
        # x-api-key + anthropic-version + anthropic-beta，只放行 authorization
        # 会让上游 401，排查时白折腾一轮。
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection", "accept-encoding"):
                continue
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=ARGS.timeout) as r:
                data = r.read()
                status, hdrs = r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            data, status, hdrs = e.read(), e.code, dict(e.headers)

        if ARGS.usage_only:
            # 跑分时只留账：全量 body 一条几十 KB，几百次请求就是几百 MB，而报告
            # 需要的只是 usage。Codex 自己的 turn.completed 把 cache_creation 折进了
            # input_tokens，拿不到「缓存写入」这一档（计价 1.25×），只能在这儿抠。
            _append_usage(outdir, status, data)
        else:
            (outdir / f"{n:03d}-resp-{status}.txt").write_bytes(data[:200000])
        self.send_response(status)
        self.send_header("Content-Type", hdrs.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, default=4100)
    ap.add_argument("--out", default="logs/rec")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--usage-only", action="store_true",
                    help="只把每次响应的 usage 追加到 usage.jsonl，不落请求/响应 body。"
                         "跑分时用这个；调协议不兼容时才要全量 body")
    ARGS = ap.parse_args()
    print(f"[rec] {ARGS.port} -> {ARGS.upstream}, dumping to {ARGS.out}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), H).serve_forever()


if __name__ == "__main__":
    main()
