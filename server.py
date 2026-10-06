#!/usr/bin/env python3
"""Newma Chat 桥接服务器（仅用标准库）。

职责：
- 伺服 public/index.html（对外端口，默认 3010）
- /api/local/skills   读取 ~/.kode/skills 与项目 .kode/skills 的 SKILL.md front-matter
- /api/local/plugins  读取项目 .kode/plugins 与 newma 内置插件清单
- /health /api/status /api/execute /api/clear 反代到内部 newma --web（默认 3011）
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
NEWMA_HOST = os.environ.get("NEWMA_INTERNAL", "127.0.0.1:3011")
HOME_SKILLS = os.path.join(os.path.expanduser("~"), ".kode", "skills")
PROJ_SKILLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kode", "skills")
PROJ_PLUGINS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kode", "plugins")

# newma 内置插件（src/loop/plugins/，随 CLI 安装，无法从外部目录枚举）
BUILTIN_PLUGINS = [
    {"name": "core-plugin", "desc": "核心命令插件：基础命令注册"},
    {"name": "mode-commands-plugin", "desc": "模式切换命令：/chat /plan /do 等"},
    {"name": "plan-mode-plugin", "desc": "规划模式：先规划再执行任务"},
    {"name": "do-mode-plugin", "desc": "执行模式：直接执行任务"},
    {"name": "intent-integration-plugin", "desc": "意图识别：自动把简单问题导向聊天、复杂任务导向规划"},
    {"name": "memo-cli-plugin", "desc": "备忘录命令：/memo 记录与检索"},
    {"name": "event-source-commands", "desc": "事件源命令"},
]

FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def parse_front_matter(text):
    m = FRONT_MATTER.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def list_skills():
    skills = []
    seen = set()

    def add_from(path, md_path, source):
        if path in seen:
            return
        try:
            meta = parse_front_matter(open(md_path, encoding="utf-8", errors="replace").read(20000))
        except OSError:
            return
        seen.add(path)
        skills.append({
            "name": meta.get("name") or os.path.basename(path),
            "dir": os.path.relpath(path, source),
            "description": meta.get("description", ""),
            "source": "project" if source == PROJ_SKILLS else "user",
        })

    for root in (PROJ_SKILLS, HOME_SKILLS):
        if not os.path.isdir(root):
            continue
        root_index = os.path.join(root, "SKILL.md")
        if os.path.isfile(root_index):
            add_from(root, root_index, root)
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            f = os.path.join(d, "SKILL.md")
            if os.path.isfile(f):
                add_from(d, f, root)
    return skills


def list_plugins():
    plugins = []
    if os.path.isdir(PROJ_PLUGINS):
        for name in sorted(os.listdir(PROJ_PLUGINS)):
            p = os.path.join(PROJ_PLUGINS, name)
            desc = ""
            if os.path.isfile(p) and name.endswith(".md"):
                meta = parse_front_matter(open(p, encoding="utf-8", errors="replace").read(20000))
                desc = meta.get("description", "")
                name = name[:-3]
            elif os.path.isdir(p):
                readme = os.path.join(p, "README.md")
                if os.path.isfile(readme):
                    with open(readme, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            line = line.strip().lstrip("# ").strip()
                            if line:
                                desc = line
                                break
            plugins.append({"name": name, "desc": desc, "kind": "project"})
    return plugins


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        url = f"http://{NEWMA_HOST}{self.path}"
        req = urllib.request.Request(url, data=data, method=self.command)
        for h in ("Content-Type",):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json({"error": str(e)}, 502)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/local/skills":
            return self._json({"skills": list_skills()})
        if path == "/api/local/plugins":
            return self._json({"plugins": list_plugins(), "builtin": BUILTIN_PLUGINS})
        if path in ("/health", "/api/status"):
            return self._proxy()
        if path in ("/", "/index.html"):
            f = os.path.join(PUBLIC_DIR, "index.html")
            if os.path.isfile(f):
                body = open(f, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "public/index.html not found"}, 404)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._proxy()
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3010
    print(f"🌉 Newma Chat bridge: http://127.0.0.1:{port}/  (newma web 内部 {NEWMA_HOST})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
