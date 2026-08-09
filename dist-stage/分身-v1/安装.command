#!/usr/bin/env bash
# 分身 v4.0 · 安装并启动（双击运行）
# 首次需联网安装运行依赖；之后完全离线可用
cd "$(cd "$(dirname "$0")" && pwd)"
echo "分身 v4.0 安装中…"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装：brew install python 或 Xcode 命令行工具（xcode-select --install）"
  exit 1
fi

# 建独立运行环境，不污染系统
if [ ! -d .venv ]; then
  echo "创建运行环境(.venv)…"
  python3 -m venv .venv
fi
. .venv/bin/activate
echo "安装依赖(首次需联网)…"
pip install -q -r requirements-dist.txt

# 已在运行则直接打开浏览器
if curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1; then
  echo "分身已在运行，打开浏览器…"
else
  # 安全默认：只绑本机回环地址。分身能在本机执行命令、读写文件，
  # 绝不能让局域网里的其他设备直接打进来（v3.9 之前 0.0.0.0 等于开了后门）。
  HOST="127.0.0.1"
  if [ "$FENSHEN_ALLOW_LAN" = "1" ]; then
    HOST="0.0.0.0"
    echo "⚠️  局域网模式已开启：同一网络可访问本机分身，访问需携带令牌（data/.auth_token），仅限可信网络。"
  fi
  echo "启动服务…"
  nohup .venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port 8002 > /tmp/fenshen-v4.0.log 2>&1 &
  for i in $(seq 1 20); do
    curl -s -m 1 http://127.0.0.1:8002/api/health >/dev/null 2>&1 && break
    sleep 1
  done
fi
open "http://localhost:8002/"
echo "完成 → http://localhost:8002/（端口 8002）"
