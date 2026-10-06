#!/usr/bin/env bash
# 启动 Newma Chat：内部 newma --web (3011) + 对外 Python 桥 (3010)
# 用法: ./start.sh [对外端口]   (默认 3010)
set -e
PORT="${1:-3010}"
INTERNAL=$((PORT + 1))
DIR="$(cd "$(dirname "$0")" && pwd)"

# 修复 newma --api/--web 拼接 /v1/chat/completions 导致的 404：
# settings.json 的 baseUrl 以 /v4 结尾，需用 OPENAI_ENDPOINT 指定完整端点
export OPENAI_ENDPOINT="${OPENAI_ENDPOINT:-https://open.bigmodel.cn/api/paas/v4/chat/completions}"

cleanup() {
  [ -n "${NEWMA_PID:-}" ] && kill "$NEWMA_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$DIR"

# 1) 内部 newma web（只绑本机回环）
newma --web --web-port "$INTERNAL" --web-host 127.0.0.1 -d "$DIR" &
NEWMA_PID=$!

# 等 newma web 就绪
for i in $(seq 1 30); do
  curl -sf "http://127.0.0.1:${INTERNAL}/health" >/dev/null 2>&1 && break
  sleep 1
done

# 2) 对外桥接服务器
echo "🌐 Newma Chat: http://127.0.0.1:${PORT}/"
python3 "$DIR/server.py" "$PORT" &
BRIDGE_PID=$!
wait "$BRIDGE_PID"
