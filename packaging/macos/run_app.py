#!/usr/bin/env python3
# 分身 v5.8 · macOS 应用入口（PyInstaller 打进 .app 的 MacOS/分身）
# 启动 uvicorn 服务 backend.main:app，待 health 后自动打开浏览器。
import os
import sys
import time
import webbrowser
import threading
import urllib.request

import uvicorn
import backend.main as _m  # 触发 backend 包导入（PyInstaller 已 --add-data backend:backend）

PORT = 8002


def _wait_and_open():
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1)
            webbrowser.open(f"http://localhost:{PORT}/")
            return
        except Exception:
            time.sleep(0.5)


def main():
    host = "127.0.0.1"
    if os.environ.get("FENSHEN_ALLOW_LAN") == "1":
        host = "0.0.0.0"
        print("⚠️  局域网模式已开启：同一网络的设备可访问本机分身，访问需令牌（~/.fenshen/.auth_token），仅限可信网络。")
    threading.Thread(target=_wait_and_open, daemon=True).start()
    try:
        uvicorn.run(_m.app, host=host, port=PORT, log_level="info")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
