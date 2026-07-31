#!/usr/bin/env bash
# 分身 v1 · 启动（双击运行）
cd "$(cd "$(dirname "$0")" && pwd)"
if curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1; then
  echo "分身已在运行"; open "http://localhost:8002/"; exit 0
fi
. .venv/bin/activate 2>/dev/null || true
nohup .venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8002 > /tmp/fenshen-v1.log 2>&1 &
for i in $(seq 1 20); do
  curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1 && break
  sleep 1
done
open "http://localhost:8002/"
echo "已启动 → http://localhost:8002/"
