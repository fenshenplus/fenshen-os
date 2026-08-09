#!/usr/bin/env bash
# 分身 v1 · 一键启动（双击运行）
# 双击本文件即可在本地启动分身，并自动打开浏览器
cd "$(cd "$(dirname "$0")" && pwd)"

PY="$HOME/.workbuddy/binaries/python/envs/default/bin/python3"
[ -x "$PY" ] || PY="python3"

# 已运行则直接打开浏览器
if curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1; then
  echo "分身已在运行，打开浏览器…"
else
  echo "分身启动中… (日志: /tmp/fenshen-v1.log)"
  # 只绑本机：分身能在这台电脑执行命令，绝不能让局域网里的其他设备直接打进来
  HOST="127.0.0.1"
  [ "$FENSHEN_ALLOW_LAN" = "1" ] && HOST="0.0.0.0" && echo "⚠️  局域网模式已开启，访问需令牌（data/.auth_token）"
  nohup "$PY" -m uvicorn backend.main:app --host "$HOST" --port 8002 > /tmp/fenshen-v1.log 2>&1 &
  # 等待端口可用
  for i in $(seq 1 15); do
    curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1 && break
    sleep 1
  done
fi

open "http://localhost:8002/"
echo "已打开 http://localhost:8002/   （关闭此终端窗口不影响服务运行）"
