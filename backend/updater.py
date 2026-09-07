"""
分身自动更新器（方案 A：自动检查 + 横幅 + 一键下载打开 DMG）。

设计要点
--------
- 版本真源为 backend/version.py 的 SEMVER。
- 远程 latest.json 由发版流程顺手写，托管在 S4 下载站 /dl/latest.json
  （与正式 DMG 同目录，零额外服务端成本）。
- 因产品尚未 Apple 签名，不做静默替换：download_and_open 仅下载 DMG 并用
  系统 `open` 挂载，由用户在 Finder 里把分身.app 拖进 /Applications 覆盖旧版
  （Gatekeeper 仍可能要求右键打开一次，属已知限制）。
- 所有网络调用均吞掉异常，绝不阻塞主流程 / 启动。
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from backend.version import SEMVER, RELEASE

# ── 配置 ───────────────────────────────────────────────────────────
LATEST_JSON_URL = "https://fenshen.plus/dl/latest.json"
_CHECK_INTERVAL_SECONDS = 6 * 3600  # 每 6 小时后台自检一次
_DOWNLOAD_TIMEOUT = 180


def _parse_semver(v: str):
    """把 'x.y.z' 解析成可比三元组，非标准串回退 (0,0,0)。"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())


def fetch_latest(timeout: int = 10):
    """拉取 latest.json；任何失败返回 None（不影响主流程）。"""
    try:
        req = urllib.request.Request(
            LATEST_JSON_URL,
            headers={"User-Agent": f"Fenshen-Updater/{SEMVER}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001 - 更新检查失败必须静默
        print(f"[updater] fetch_latest 失败: {e}")
        return None


def get_update_status():
    """比较本机 SEMVER 与 latest.json，返回结构化状态字典。"""
    latest = fetch_latest()
    if not latest:
        return {
            "available": False,
            "current": SEMVER,
            "release": RELEASE,
            "latest": None,
            "url": None,
            "sha256": None,
            "notes": "",
            "error": "无法获取更新信息（稍后重试）",
        }
    latest_ver = str(latest.get("semver") or latest.get("version") or "")
    has_update = _parse_semver(latest_ver) > _parse_semver(SEMVER)
    return {
        "available": has_update,
        "current": SEMVER,
        "release": RELEASE,
        "latest": latest_ver or None,
        "url": latest.get("dmg") or latest.get("url"),
        "sha256": latest.get("sha256"),
        "notes": latest.get("notes", ""),
        "error": None,
    }


def download_and_open(timeout: int = _DOWNLOAD_TIMEOUT):
    """下载 DMG 并用系统 open 挂载，返回状态字典。"""
    status = get_update_status()
    url = status.get("url")
    if not url:
        return {"ok": False, "error": "未找到安装包下载地址"}
    if not status.get("available"):
        return {"ok": False, "error": "当前已是最新版本"}
    try:
        tmp_dir = tempfile.gettempdir()
        path = os.path.join(tmp_dir, "分身-update.dmg")
        # 安全编码：latest.json 里若写的是含中文的原始 URL，urlretrieve 会失败。
        # safe 保留 ':' '/' 等 URL 结构字符与 '%'（已有转义不被二次编码）。
        safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
        print(f"[updater] 下载安装包: {safe_url} → {path}")
        urllib.request.urlretrieve(safe_url, path)
        # 完整性校验：防止下载被截断/损坏（latest.json 提供 sha256 时才校验）
        want = (status.get("sha256") or "").strip().lower()
        if want:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != want:
                return {"ok": False,
                        "error": f"安装包校验失败（期望 {want[:16]}… 实际 {h.hexdigest()[:16]}…），已中止"}
        # 打开/挂载安装包，由用户拖拽覆盖
        subprocess_open(path)
        return {
            "ok": True,
            "path": path,
            "note": "已打开安装包，请在弹窗中把「分身」拖入 Applications 覆盖旧版",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def subprocess_open(path: str):
    """跨平台打开文件/挂载 DMG。

    ⚠️ 必须用 platform.system()：os.uname() 是 Unix-only，Windows 上会抛
    AttributeError，导致 else 分支永远走不到（Windows 打不开安装包）。
    """
    import platform
    import subprocess

    if platform.system() == "Darwin":
        subprocess.Popen(["open", path])  # 挂载 DMG 并弹 Finder，用户拖拽覆盖
    elif platform.system() == "Linux":
        subprocess.Popen(["xdg-open", path])
    else:  # Windows
        os.startfile(path)  # noqa: S606 - 仅在 Windows 分支执行


def start_updater():
    """后台 daemon 线程：启动即查一次，之后每 N 小时自检（仅打印日志）。

    前端横幅由用户在 App 打开时主动触发 /api/meta/update/check，无需本线程推送。
    """

    def _loop():
        try:
            st = get_update_status()
            if st.get("available"):
                print(f"[updater] 发现新版本 {st.get('latest')}（当前 {SEMVER}）")
        except Exception:  # noqa: BLE001
            pass
        while True:
            time.sleep(_CHECK_INTERVAL_SECONDS)
            try:
                st = get_update_status()
                if st.get("available"):
                    print(f"[updater] 发现新版本 {st.get('latest')}（当前 {SEMVER}）")
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_loop, daemon=True).start()
