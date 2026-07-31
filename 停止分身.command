#!/usr/bin/env bash
# 分身 v1 · 停止服务（双击运行）
echo "正在停止分身(8002)…"
pkill -f "uvicorn.*8002" && echo "已停止" || echo "没有运行中的分身服务"
sleep 1
curl -s -m 2 -o /dev/null -w "端口状态: HTTP %{http_code}\n" http://127.0.0.1:8002/api/health 2>&1 || echo "端口已释放"
