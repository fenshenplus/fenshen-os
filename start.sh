#!/usr/bin/env bash
# 分身 v1 · 桌面托管启动脚本
# 用法: bash start.sh   （或双击 启动分身.command 自动打开浏览器）
# 手机访问：浏览器打开 http://<桌面局域网IP>:8002/
# 端口 8002 规避 8000（choice-power 生产项目占用）
set -e
cd "$(dirname "$0")"

# 优先受管 venv（已含 fastapi/uvicorn，无需联网安装），否则回退系统 python3
MANAGED_PY="$HOME/.workbuddy/binaries/python/envs/default/bin/python3"
if [ -x "$MANAGED_PY" ]; then
  PY="$MANAGED_PY"
else
  PY="python3"
fi

# 端口预检：已在运行则友好提示并退出（避免 address already in use）
if curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1; then
  echo "分身已在运行 → http://localhost:8002/   （如需重启请先运行 停止分身.command）"
  exit 0
fi

echo "分身 v1 启动中… 访问 http://localhost:8002/"
exec "$PY" -m uvicorn backend.main:app --host 0.0.0.0 --port 8002
