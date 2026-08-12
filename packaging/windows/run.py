#!/usr/bin/env python3
"""分身 · PyInstaller 打包入口（Windows/macOS exe）
启动后端并自动打开浏览器。"""
import os
import sys
import threading
import webbrowser

PORT = 8002


def open_browser():
    webbrowser.open(f"http://127.0.0.1:{PORT}")


if __name__ == "__main__":
    # 1.2s 后自动开浏览器（等 uvicorn 起来）
    threading.Timer(1.2, open_browser).start()
    import uvicorn
    sys.exit(uvicorn.run("backend.main:app", host="127.0.0.1", port=PORT,
                         app_dir=os.path.dirname(os.path.abspath(__file__))))
