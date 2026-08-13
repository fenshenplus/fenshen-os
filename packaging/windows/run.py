#!/usr/bin/env python3
"""分身 · PyInstaller 打包入口（Windows/macOS exe）
启动后端并自动打开浏览器。局域网模式：环境变量 FENSHEN_ALLOW_LAN=1 或设置页 lan_enabled=1（v5.5）。"""
import os
import sqlite3
import sys
import threading
import webbrowser

PORT = 8002


def lan_enabled() -> bool:
    if os.environ.get("FENSHEN_ALLOW_LAN") == "1":
        return True
    # 设置页一键开关：读用户数据目录 DB（与 main.py 的 _MEI 数据目录一致）
    db = os.path.expanduser("~/.fenshen/fenshen.db")
    if os.path.exists(db):
        try:
            r = sqlite3.connect(db).execute(
                "SELECT value FROM meta_settings WHERE key='lan_enabled'").fetchone()
            return bool(r and r[0] == "1")
        except Exception:
            pass
    return False


def open_browser():
    webbrowser.open(f"http://127.0.0.1:{PORT}")


if __name__ == "__main__":
    threading.Timer(1.2, open_browser).start()
    import uvicorn
    host = "0.0.0.0" if lan_enabled() else "127.0.0.1"
    sys.exit(uvicorn.run("backend.main:app", host=host, port=PORT,
                         app_dir=os.path.dirname(os.path.abspath(__file__))))
