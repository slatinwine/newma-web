#!/usr/bin/env python3
"""Newma Chat 一键启动器（跨平台，Windows 友好）。

替代 start.sh 的编排职责，一条命令拉起整套服务：

    python run.py            # 对外端口 3010
    python run.py 8080       # 自定义对外端口

职责：
1. 检查端口是否可用，被占用时给出可执行的建议；
2. 检查 newma CLI 是否安装，未安装时给出一键安装指引；
3. 修复 newma --api/--web 拼接 /v1/chat/completions 导致的 404
   （与 start.sh 相同，通过 OPENAI_ENDPOINT 指定完整端点）；
4. 启动内部 newma --web（仅绑 127.0.0.1），等待 /health 就绪；
5. 启动对外 Python 桥（server.py），退出时回收全部子进程。

Windows 用户可直接双击 start.bat（内容即 python run.py）。
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJ = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def port_free(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def spawn_newma(port, ws):
    exe = shutil.which(os.environ.get("NEWMA_BIN", "newma"))
    if not exe:
        print("❌ 未找到 newma CLI。请先安装：")
        print("     npm install -g newma-cli")
        print("   或用环境变量 NEWMA_BIN 指定可执行文件路径。")
        sys.exit(1)
    if exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", exe, "--web", "--web-port", str(port),
               "--web-host", "127.0.0.1", "-d", ws]
    else:
        cmd = [exe, "--web", "--web-port", str(port),
               "--web-host", "127.0.0.1", "-d", ws]
    kwargs = {"cwd": ws}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    # 输出落到临时文件：启动失败时打印尾部便于排障
    log = tempfile.NamedTemporaryFile(prefix="newma-web-", suffix=".log", delete=False)
    kwargs["stdout"] = log
    kwargs["stderr"] = subprocess.STDOUT
    proc = subprocess.Popen(cmd, **kwargs)
    return proc, log.name


def wait_health(port, proc, log_path, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"❌ newma 进程启动后立即退出（code {proc.returncode}）。日志尾部：")
            _tail(log_path)
            sys.exit(1)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.6)
    print(f"❌ newma 在 {timeout}s 内未就绪。日志尾部：")
    _tail(log_path)
    try:
        proc.kill()
    except Exception:
        pass
    sys.exit(1)


def _tail(path, n=15):
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        for ln in lines[-n:]:
            print("  | " + ln)
    except OSError:
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3010
    internal = int(os.environ.get("NEWMA_INTERNAL_PORT") or port + 1)

    if not port_free(port):
        print(f"❌ 端口 {port} 已被占用。换个端口试试：python run.py {port + 10}")
        sys.exit(1)
    if not port_free(internal):
        print(f"❌ 内部端口 {internal} 已被占用。换个端口试试：python run.py {port + 10}")
        sys.exit(1)

    # 修复 baseUrl 拼接 404（换模型服务商时改这里或设 OPENAI_ENDPOINT）
    os.environ.setdefault("OPENAI_ENDPOINT", DEFAULT_ENDPOINT)

    print("🚀 启动内部 newma --web …")
    proc, log = spawn_newma(internal, PROJ)
    wait_health(internal, proc, log)

    env_bridge = dict(os.environ, NEWMA_INTERNAL=f"127.0.0.1:{internal}")
    py = sys.executable or "python"
    kwargs = {"env": env_bridge, "cwd": PROJ}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    bridge = subprocess.Popen([py, os.path.join(PROJ, "server.py"), str(port)], **kwargs)

    print(f"🌐 Newma Chat: http://127.0.0.1:{port}/")
    print("   Ctrl+C 退出（同时回收 newma 与桥进程）")
    try:
        bridge.wait()
    except KeyboardInterrupt:
        print("\n👋 正在退出…")
    finally:
        for p in (bridge, proc):
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(0.5)
        for p in (bridge, proc):
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        try:
            os.unlink(log)
        except OSError:
            pass


if __name__ == "__main__":
    main()
