#!/usr/bin/env python3
# 分身 v6.3 · 跨平台一键启动器
# 自动安装缺失依赖，启动 backend.main:app，health 后自动打开浏览器。
#
# 用法:
#   python start.py                         # 本机回环 8002
#   FENSHEN_ALLOW_LAN=1 python start.py     # 局域网模式（同一网络可访问，需令牌）
#
# 说明:
#   本启动器不依赖 WorkBuddy 受管 venv；在任意装了 Python 3.10+ 的机器上，
#   首次运行会自动 pip 安装 backend/requirements.txt 里的依赖，之后直接启动。
import os
import sys
import time
import sqlite3
import subprocess
import threading
import webbrowser
import urllib.request

PORT = 8002
ROOT = os.path.dirname(os.path.abspath(__file__))


def ensure_deps():
    req = os.path.join(ROOT, "backend", "requirements.txt")
    try:
        import fastapi  # noqa
        import uvicorn  # noqa
        import requests  # noqa
    except ImportError:
        print("[分身] 首次运行，安装依赖中…（需联网，约 1 分钟）")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req])


def lan_enabled():
    if os.environ.get("FENSHEN_ALLOW_LAN") == "1":
        return True
    db = os.path.join(ROOT, "data", "fenshen.db")
    if os.path.exists(db):
        try:
            r = sqlite3.connect(db).execute(
                "SELECT value FROM meta_settings WHERE key='lan_enabled'").fetchone()
            return bool(r and r[0] == "1")
        except Exception:
            pass
    return False


def open_browser():
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1)
            webbrowser.open(f"http://localhost:{PORT}/")
            return
        except Exception:
            time.sleep(0.5)


def main():
    ensure_deps()
    os.chdir(ROOT)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    host = "0.0.0.0" if lan_enabled() else "127.0.0.1"
    if host == "0.0.0.0":
        print("⚠️  局域网模式已开启：同一网络的设备可访问本机分身，访问需令牌（data/.auth_token），仅限可信网络。")
    print(f"分身 v6.3 启动中 → http://localhost:{PORT}/")
    threading.Thread(target=open_browser, daemon=True).start()
    import uvicorn
    try:
        uvicorn.run("backend.main:app", host=host, port=PORT, app_dir=ROOT, log_level="info")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
