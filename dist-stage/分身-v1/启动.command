#!/usr/bin/env bash
# 分身 v4.0 · 启动（双击运行）
cd "$(cd "$(dirname "$0")" && pwd)"
if ! [ -d .venv ]; then
  echo "尚未安装依赖，请先双击「安装.command」"
  exit 1
fi
if curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1; then
  echo "分身已在运行"; open "http://localhost:8002/"; exit 0
fi
. .venv/bin/activate 2>/dev/null || true
# 安全默认：只绑本机 127.0.0.1。局域网访问需 FENSHEN_ALLOW_LAN=1 且带令牌。
HOST="127.0.0.1"
if [ "$FENSHEN_ALLOW_LAN" != "1" ] && command -v sqlite3 >/dev/null 2>&1 && [ -f "data/fenshen.db" ]; then
  LAN_DB=$(sqlite3 data/fenshen.db "SELECT value FROM meta_settings WHERE key='lan_enabled'" 2>/dev/null)
  [ "$LAN_DB" = "1" ] && FENSHEN_ALLOW_LAN=1
fi
if [ "$FENSHEN_ALLOW_LAN" = "1" ]; then
  HOST="0.0.0.0"
  echo "⚠️  局域网模式已开启，访问需令牌（data/.auth_token），仅限可信网络。"
fi
nohup .venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port 8002 > /tmp/fenshen-v4.0.log 2>&1 &
for i in $(seq 1 20); do
  curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1 && break
  sleep 1
done
open "http://localhost:8002/"
echo "已启动 → http://localhost:8002/（端口 8002）"
