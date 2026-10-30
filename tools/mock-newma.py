#!/usr/bin/env python3
"""Mock newma --web：仅用于本地联调 / E2E 测试（不随产品分发）。

实现 newma --web 的最小协议子集：
- GET  /health       -> 200 {"ok":true}
- POST /api/execute  -> 排队一轮固定回复事件
- GET  /api/status   -> 逐行吐出 outputBuffer（JSON 事件），消费即清除
- POST /api/clear    -> 清空缓冲

用法：python tools/mock-newma.py [端口]   （默认 3011）
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
BUFFER = []


def queue_turn(requirement):
    LOCK.acquire()
    try:
        BUFFER.append(json.dumps({"type": "output", "content": f"收到：{requirement}"},
                                  ensure_ascii=False))
        BUFFER.append(json.dumps({"type": "output",
                                  "content": "这是 mock newma 的固定回复。\n\nA) 选项一\nB) 选项二"},
                                 ensure_ascii=False))
        BUFFER.append(json.dumps({"type": "output", "content": "\n✅ Done"},
                                 ensure_ascii=False))
    finally:
        LOCK.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json({"ok": True})
        if self.path.startswith("/api/status"):
            LOCK.acquire()
            try:
                lines, BUFFER[:] = BUFFER[:], []
            finally:
                LOCK.release()
            body = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path.startswith("/api/execute"):
            try:
                req = json.loads(raw.decode("utf-8")).get("requirement", "")
            except Exception:
                req = ""
            queue_turn(req)
            return self._json({"ok": True})
        if self.path.startswith("/api/clear"):
            LOCK.acquire()
            try:
                BUFFER[:] = []
            finally:
                LOCK.release()
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3011
    print(f"mock newma --web on 127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
