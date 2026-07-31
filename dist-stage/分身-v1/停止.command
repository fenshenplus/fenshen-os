#!/usr/bin/env bash
# 分身 v1 · 停止服务（双击运行）
pkill -f "uvicorn.*8002" && echo "已停止" || echo "没有运行中的分身"
