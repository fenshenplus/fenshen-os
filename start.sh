#!/usr/bin/env bash
# 分身 v1 · 桌面托管启动脚本
# 用法: bash start.sh   （或双击 启动分身.command 自动打开浏览器）
# 端口 8002 规避 8000（choice-power 生产项目占用）
#
# 手机 / 局域网访问：FENSHEN_ALLOW_LAN=1 bash start.sh
#   开启后需要令牌才能访问，令牌见 data/.auth_token
#   仅在可信网络下使用——分身能在这台电脑上执行命令和读写文件。
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

# 依赖自检：受管 venv 已含依赖；系统 python3 缺失时自动安装（首次约 1 分钟，需联网）
if [ "$PY" != "$MANAGED_PY" ]; then
  if ! "$PY" -c "import fastapi, uvicorn, requests" >/dev/null 2>&1; then
    echo "首次运行，正在安装依赖…（约 1 分钟，需联网）"
    "$PY" -m pip install -r backend/requirements.txt
  fi
fi

# 安全默认值：只绑本机回环地址。
# v3.9 之前这里写死 --host 0.0.0.0，等于在同一 WiFi 下开了个后门——
# 别人扫到 8002 端口就能调用能执行 shell 的接口，审查中已实测可接管整台电脑。
HOST="127.0.0.1"
# v5.5：设置页「局域网访问」一键开关（lan_enabled=1）同样启用，无需环境变量
if [ "$FENSHEN_ALLOW_LAN" != "1" ] && command -v sqlite3 >/dev/null 2>&1 && [ -f "data/fenshen.db" ]; then
  LAN_DB=$(sqlite3 data/fenshen.db "SELECT value FROM meta_settings WHERE key='lan_enabled'" 2>/dev/null)
  [ "$LAN_DB" = "1" ] && FENSHEN_ALLOW_LAN=1
fi
if [ "$FENSHEN_ALLOW_LAN" = "1" ]; then
  HOST="0.0.0.0"
  echo "⚠️  局域网模式已开启：同一网络的设备可访问本机分身。"
  echo "    访问需携带令牌，令牌见 data/.auth_token —— 请确保处于可信网络。"
fi

echo "分身 v1 启动中… 访问 http://localhost:8002/"
exec "$PY" -m uvicorn backend.main:app --host "$HOST" --port 8002
