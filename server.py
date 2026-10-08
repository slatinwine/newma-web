#!/usr/bin/env python3
"""Newma Chat 桥接服务器（仅用标准库）。

职责：
- 伺服 public/index.html（对外端口，默认 3010）
- /api/local/skills   读取 ~/.kode/skills 与项目 .kode/skills 的 SKILL.md front-matter
- /api/local/plugins  读取项目 .kode/plugins 与 newma 内置插件清单
- /health /api/status /api/execute /api/clear 反代到内部 newma --web（默认 3011）
- 会话工作区：请求带 ws 参数（GET）或 workspace 字段（POST JSON）时，
  按目录懒启动独立 newma --web 实例并路由过去；空闲 30 分钟自动回收。
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
NEWMA_HOST = os.environ.get("NEWMA_INTERNAL", "127.0.0.1:3011")
NEWMA_BIN = os.environ.get("NEWMA_BIN", "newma")
HOME_SKILLS = os.path.join(os.path.expanduser("~"), ".kode", "skills")
PROJ_SKILLS = os.path.join(PROJ_DIR, ".kode", "skills")
PROJ_PLUGINS = os.path.join(PROJ_DIR, ".kode", "plugins")

# 工作区实例参数
SPAWN_TIMEOUT = 60      # 等 /health 就绪的上限（秒）
IDLE_REAP = 30 * 60     # 空闲实例回收（秒）

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


class WorkspaceManager:
    """按目录懒启动 / 复用 / 回收独立的 newma --web 实例。

    每个会话可绑定一个工作区目录；绑定后该会话的请求路由到在
    该目录下启动的专属实例（等价于 newma --web -d <目录>）。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.instances = {}   # abspath -> {port, proc, last_used}

    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _spawn(self, ws):
        port = self._free_port()
        exe = shutil.which(NEWMA_BIN) or NEWMA_BIN
        if exe.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c", exe, "--web", "--web-port", str(port),
                   "--web-host", "127.0.0.1", "-d", ws]
        else:
            cmd = [exe, "--web", "--web-port", str(port),
                   "--web-host", "127.0.0.1", "-d", ws]
        kwargs = {"cwd": ws, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as e:
            raise RuntimeError(f"无法启动 newma（{NEWMA_BIN}）：{e}")
        deadline = time.time() + SPAWN_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"newma 进程启动后立即退出（code {proc.returncode}）")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status == 200:
                        return {"port": port, "proc": proc, "last_used": time.time()}
            except Exception:
                time.sleep(0.6)
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(f"newma 启动超时（{SPAWN_TIMEOUT}s 内未就绪）")

    def ensure(self, ws):
        """返回该目录的存活实例，没有则启动。"""
        ws = os.path.abspath(ws)
        if not os.path.isdir(ws):
            raise RuntimeError(f"工作区目录不存在：{ws}")
        with self.lock:
            inst = self.instances.get(ws)
            if inst and inst["proc"].poll() is None:
                inst["last_used"] = time.time()
                return inst
            if inst:
                try:
                    inst["proc"].kill()
                except Exception:
                    pass
                del self.instances[ws]
            inst = self._spawn(ws)
            self.instances[ws] = inst
            return inst

    def get(self, ws):
        """只查找已运行的实例，不启动。"""
        ws = os.path.abspath(ws)
        inst = self.instances.get(ws)
        if inst and inst["proc"].poll() is None:
            inst["last_used"] = time.time()
            return inst
        return None

    def list(self):
        return [
            {"workspace": ws, "port": i["port"]}
            for ws, i in self.instances.items()
            if i["proc"].poll() is None
        ]

    def reap(self):
        with self.lock:
            for ws, inst in list(self.instances.items()):
                if time.time() - inst["last_used"] > IDLE_REAP:
                    try:
                        inst["proc"].kill()
                    except Exception:
                        pass
                    del self.instances[ws]

    def forget(self, ws):
        """实例进程已退出（如 /api/stop），从表里移除以便下次重新拉起。"""
        with self.lock:
            ws = os.path.abspath(ws)
            inst = self.instances.pop(ws, None)
            return inst is not None

    def kill_all(self):
        with self.lock:
            for inst in self.instances.values():
                try:
                    inst["proc"].kill()
                except Exception:
                    pass
            self.instances.clear()


WS = WorkspaceManager()


def _reaper():
    while True:
        time.sleep(60)
        try:
            WS.reap()
        except Exception:
            pass


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

    def _forward(self, host, data=None):
        url = f"http://{host}{self.path}"
        req = urllib.request.Request(url, data=data, method=self.command)
        if data and self.headers.get("Content-Type"):
            req.add_header("Content-Type", self.headers["Content-Type"])
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

    def _proxy(self):
        """反代到 newma。POST JSON body 里的 workspace 字段、GET 上的
        ?ws= 查询参数会把请求路由到对应工作区的独立实例。"""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else None
        data, ws = raw, ""
        if raw:
            try:
                obj = json.loads(raw.decode("utf-8"))
                if isinstance(obj, dict):
                    ws = str(obj.pop("workspace", "") or "").strip()
                    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            except Exception:
                data = raw
        if not ws:
            parsed = urlparse(self.path)
            ws = (parse_qs(parsed.query).get("ws") or [""])[0].strip()
        if ws:
            try:
                inst = WS.ensure(ws)
            except Exception as e:
                self._json({"error": f"工作区启动失败：{e}"}, 502)
                return
            # /api/stop 会结束该实例进程；转发成功后从表里移除，下次自动重启
            stopped = urlparse(self.path).path == "/api/stop"
            host = f"127.0.0.1:{inst['port']}"
            if stopped:
                WS.forget(ws)
            return self._forward(host, data)
        self._forward(NEWMA_HOST, data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        ws = (parse_qs(parsed.query).get("ws") or [""])[0].strip()
        if path == "/api/local/skills":
            return self._json({"skills": list_skills()})
        if path == "/api/local/plugins":
            return self._json({"plugins": list_plugins(), "builtin": BUILTIN_PLUGINS})
        if path == "/api/workspaces":
            return self._json({"workspaces": WS.list(), "default": PROJ_DIR})
        if path in ("/health", "/api/status"):
            if ws:
                inst = WS.get(ws)
                if not inst:
                    return self._json({"error": "workspace not running"}, 503)
                return self._forward(f"127.0.0.1:{inst['port']}")
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
        if self.path.startswith("/api/workspace/ensure"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                obj = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                obj = {}
            ws = str(obj.get("workspace") or "").strip()
            if not ws:
                return self._json({"error": "缺少 workspace 字段"}, 400)
            try:
                inst = WS.ensure(ws)
            except Exception as e:
                return self._json({"error": str(e)}, 502)
            return self._json({"ok": True, "port": inst["port"], "workspace": os.path.abspath(ws)})
        if self.path.startswith("/api/"):
            return self._proxy()
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    import atexit
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3010
    atexit.register(WS.kill_all)   # 退出时回收所有工作区实例
    threading.Thread(target=_reaper, daemon=True).start()
    print(f"🌉 Newma Chat bridge: http://127.0.0.1:{port}/  (newma web 内部 {NEWMA_HOST}，工作区实例按需启动)")
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WS.kill_all()
