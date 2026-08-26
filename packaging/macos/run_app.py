#!/usr/bin/env python3
# 分身 v6.4 · macOS 桌面助手入口（PyInstaller 打进 .app 的 MacOS/分身）
# 原生桌面窗口（PyWebview 内嵌前端），不再是浏览器标签页。
# 启动 uvicorn 服务（127.0.0.1 或 LAN 0.0.0.0）→ 权限引导（首次/未授权时）→ 桌面窗口加载主界面。
# 移动端照常可经 /ws 中继连接（引擎仍在 8002 监听）。
import base64
import ctypes
import json
import os
import subprocess
import sys
import tempfile
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
        # 受保护探测：读 macOS 保护目录（Safari 历史等；不存在则试另一个保护路径）
        probe = os.path.expanduser("~/Library/Safari")
        if os.path.isdir(probe):
            os.listdir(probe)
            return True
        probe2 = os.path.expanduser("~/Library/Mail")
        if os.path.isdir(probe2):
            os.listdir(probe2)
            return True
        return False  # 保护目录都缺失 → 无法确认有完全磁盘访问，视为未授权
    except Exception:
        return False


# 重装/升级后 macOS TCC 旧授权失配（未签名应用按 inode 绑定）的逃生舱：
# 用户已授权过但仍检测不到时，点「跳过授权直接进入」，写标志文件记住，下次不再弹引导。
_PERM_SKIP_FILE = os.path.expanduser("~/.fenshen/.perm_skipped")


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

    def skip_permissions(self):
        """逃生舱：用户确认已授权/愿意稍后授权 → 记住跳过，直接进主界面。"""
        try:
            os.makedirs(os.path.dirname(_PERM_SKIP_FILE), exist_ok=True)
            with open(_PERM_SKIP_FILE, "w") as f:
                f.write("1")
        except Exception:
            pass
        self.goto_main()
        return "ok"

    def goto_main(self):
        for w in _windows():
            try:
                w.load_url(f"http://127.0.0.1:{PORT}/")
            except Exception:
                pass
        return "ok"

    def quit(self):
        _menu_quit()
        return "ok"

    def screenshot(self, mode: str = "region"):
        """原生截图（仅桌面端）：mode=region 交互选区，full 全屏。返回 base64 JSON。"""
        if sys.platform != "darwin":
            return json.dumps({"ok": False, "error": "截图仅支持 macOS 桌面端"})
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            if mode == "full":
                subprocess.run(["screencapture", "-x", path], check=True, timeout=20)
            else:
                # 交互选区：用户框选区域；取消则 screencapture 退出非 0 且不写文件
                r = subprocess.run(["screencapture", "-i", "-r", path], capture_output=True, timeout=30)
                if r.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
                    return json.dumps({"ok": False, "error": "已取消截图"})
            # 适度压缩，避免 base64 过大撑爆消息体
            try:
                from PIL import Image
                im = Image.open(path)
                if max(im.size) > 2400:
                    im.thumbnail((2400, 2400))
                buf = tempfile.mkstemp(suffix=".png")[1]
                im.save(buf, "PNG", optimize=True)
                os.replace(buf, path)
            except Exception:
                pass
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            return json.dumps({"ok": True, "name": "screenshot.png", "mime": "image/png", "data": data})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})
        finally:
            try:
                os.remove(path)
            except Exception:
                pass


_windows_ref = []

# 退出意图标记：菜单「退出」置 True；其他关闭/终止事件按「隐藏到后台」处理。
quit_requested = False


def _windows():
    return list(_windows_ref)


def _on_closing():
    """窗口关闭/退出事件钩子。
    - 正常关闭（红钮/Cmd+Q）：不真正退出，隐藏到后台（仿微信/WorkBuddy 静默驻留）。
    - 菜单「退出」已置 quit_requested：放行，真正退出进程。
    返回 False 取消关闭（由 pywebview should_close 解读），True 允许关闭。
    """
    global quit_requested
    if quit_requested:
        return True
    for w in _windows():
        try:
            w.minimize()  # 收进 Dock 后台（仿微信/WorkBuddy，最小化而非退出）
        except Exception:
            pass
    return False


def _menu_show():
    for w in _windows():
        try:
            w.restore()
        except Exception:
            pass
        try:
            w.show()
        except Exception:
            pass


def _menu_quit():
    """真正退出：标记意图 → 销毁窗口 → 兜底强制退出。"""
    global quit_requested
    quit_requested = True
    for w in _windows():
        try:
            w.destroy()
        except Exception:
            pass
    # 兜底：确保进程退出（webview 事件循环未必自然返回）
    threading.Timer(1.5, lambda: os._exit(0)).start()


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
    from webview.menu import MenuAction  # 菜单项构造器位于 webview.menu 子模块

    _start_server()
    api = _Api()
    perms = _permissions()
    # 首次/未授权 → 引导页（file://）；已授权或用户点过「跳过」 → 直接主界面
    if (perms["accessibility"] and perms["disk"]) or os.path.exists(_PERM_SKIP_FILE):
        url = f"http://127.0.0.1:{PORT}/"
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        url = "file://" + os.path.join(base, "permission_guide.html")
    # 系统菜单：放入「分身」App 菜单（标题 __app__ 表示挂到应用菜单内）
    menu = webview.Menu(
        "__app__",
        [
            MenuAction("显示分身", _menu_show),
            MenuAction("退出", _menu_quit),
        ],
    )
    window = webview.create_window(
        "分身 · AI 助手",
        url,
        js_api=api,
        width=1280,
        height=860,
        min_size=(980, 660),
        background_color="#0f0c28",
        menu=[menu],
    )
    _windows_ref.append(window)
    # 关闭窗口 → 隐藏到后台（不再退出进程，避免 launchd 重启导致反复弹出）
    window.events.closing += _on_closing
    webview.start()


if __name__ == "__main__":
    main()
