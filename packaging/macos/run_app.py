#!/usr/bin/env python3
# 分身 v6.4 · macOS 桌面助手入口（PyInstaller 打进 .app 的 MacOS/分身）
# 原生桌面窗口（PyWebview 内嵌前端），不再是浏览器标签页。
# 启动 uvicorn 服务（127.0.0.1 或 LAN 0.0.0.0）→ 权限引导（首次/未授权时）→ 桌面窗口加载主界面。
# 移动端照常可经 /ws 中继连接（引擎仍在 8002 监听）。
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import uvicorn
import backend.main as _m  # 触发 backend 包导入（PyInstaller 已 --add-data backend:backend）

PORT = 8002

# ── macOS 本机权限检测（首次安装即引导授权）──────────────────────────
def _ax_trusted() -> bool:
    """辅助功能：AXIsProcessTrusted。"""
    try:
        lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return False


def _disk_accessible() -> bool:
    """完全磁盘访问：探测受保护目录（~/.fenshen 数据目录 + 系统保护路径）。"""
    try:
        os.makedirs(os.path.expanduser("~/.fenshen"), exist_ok=True)
        # 受保护探测：读 macOS 保护目录
        probe = os.path.expanduser("~/Library/Safari")
        if os.path.isdir(probe):
            os.listdir(probe)
        return True
    except Exception:
        return False


def _permissions() -> dict:
    return {"accessibility": _ax_trusted(), "disk": _disk_accessible()}


def _open_system_settings(pane: str = None):
    """打开系统设置对应权限面板。"""
    url = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    if pane == "disk":
        url = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    try:
        subprocess.Popen(["open", url])
    except Exception:
        pass


# ── PyWebview JS API（引导页与主界面共用）────────────────────────────
class _Api:
    def check_permissions(self):
        return json.dumps(_permissions())

    def open_settings(self, pane: str = "accessibility"):
        _open_system_settings(pane)
        return "ok"

    def goto_main(self):
        for w in _windows():
            try:
                w.load_url(f"http://127.0.0.1:{PORT}/")
            except Exception:
                pass
        return "ok"

    def quit(self):
        for w in _windows():
            try:
                w.destroy()
            except Exception:
                pass
        return "ok"


_windows_ref = []


def _windows():
    return list(_windows_ref)


def _start_server() -> threading.Thread:
    host = "127.0.0.1"
    if os.environ.get("FENSHEN_ALLOW_LAN") == "1":
        host = "0.0.0.0"
        print("⚠️  局域网模式已开启：同一网络的设备可访问本机分身（移动端经 /ws 中继，需令牌），仅限可信网络。")
    t = threading.Thread(target=lambda: uvicorn.run(_m.app, host=host, port=PORT, log_level="warning"),
                         daemon=True)
    t.start()
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1)
            return t
        except Exception:
            time.sleep(0.5)
    return t


def main():
    import webview  # 延迟导入：源码模式（python run_app.py）无 GUI 时仍可跑服务

    _start_server()
    api = _Api()
    perms = _permissions()
    # 首次/未授权 → 引导页（file://）；已授权 → 直接主界面
    if perms["accessibility"] and perms["disk"]:
        url = f"http://127.0.0.1:{PORT}/"
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        url = "file://" + os.path.join(base, "permission_guide.html")
    window = webview.create_window(
        "分身 · AI 助手",
        url,
        js_api=api,
        width=1280,
        height=860,
        min_size=(980, 660),
        background_color="#0f0c28",
    )
    _windows_ref.append(window)
    window.events.closed += lambda: os._exit(0)
    webview.start()


if __name__ == "__main__":
    main()
