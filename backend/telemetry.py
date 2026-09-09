"""分身可观测性层（P0-b · D5→L3 硬缺口补齐）。

纯标准库实现，零外部依赖。采集进程级运行时指标：
  - started_at      进程启动时刻（ISO8601 墙钟，UTC）
  - uptime_seconds  自启动以来的秒数
  - rss_mb          当前常驻内存（MB）
  - max_rss_mb      进程生命周期内观测到的峰值常驻内存（MB）
  - open_fds        当前打开的文件描述符数
  - threads         当前线程数

后台采样守护线程按 FENSHEN_TELEMETRY_INTERVAL（秒）把快照追加写入
~/.fenshen/telemetry.csv，供 72h 内存/句柄泄漏长跑观测。interval<=0 或不设 → 关闭采样。

⚠️ 本模块只采集、只落本机文件，绝不外发任何数据（与宪法③重大隐私默认保守一致）。
"""
import csv
import os
import sys
import threading
import time

# ── 采样状态（模块级单例）──
_START_MONO = time.monotonic()
_START_WALL = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
_lock = threading.Lock()
_max_rss_bytes = 0
_csv_path = os.path.join(os.path.expanduser("~"), ".fenshen", "telemetry.csv")
_interval = 0
_thread = None
_running = False


def _current_rss_bytes() -> int:
    """尽力而为的当前常驻内存（字节）。跨平台、纯 stdlib。"""
    # Linux: /proc/self/status 的 VmRSS
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024  # 内核给的是 KB
    except Exception:
        pass
    # macOS / 其他：psutil 可用则用之（仅读本进程 RSS）
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except Exception:
        pass
    # 最后兜底：ru_maxrss（峰值，单位平台相关）
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform.startswith("linux"):
            return r * 1024
        return r
    except Exception:
        return 0


def _open_fds() -> int:
    """当前打开的文件描述符数（尽力而为）。"""
    for path in ("/dev/fd", "/proc/self/fd"):
        try:
            return len([f for f in os.listdir(path) if f.isdigit()])
        except Exception:
            continue
    return 0


def snapshot() -> dict:
    """返回当前指标快照（dict）。"""
    global _max_rss_bytes
    rss = _current_rss_bytes()
    with _lock:
        if rss > _max_rss_bytes:
            _max_rss_bytes = rss
        max_rss = _max_rss_bytes
    return {
        "started_at": _START_WALL,
        "uptime_seconds": round(time.monotonic() - _START_MONO, 1),
        "rss_mb": round(rss / (1024 * 1024), 1),
        "max_rss_mb": round(max_rss / (1024 * 1024), 1),
        "open_fds": _open_fds(),
        "threads": threading.active_count(),
    }


def _sampler_loop(interval: int):
    # 首次写表头（仅当文件不存在）
    try:
        os.makedirs(os.path.dirname(_csv_path), exist_ok=True)
        if not os.path.exists(_csv_path):
            with open(_csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ts", "uptime_seconds", "rss_mb", "max_rss_mb", "open_fds", "threads"])
    except Exception:
        pass
    while True:
        with _lock:
            if not _running:
                break
        try:
            s = snapshot()
            with open(_csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    s["uptime_seconds"], s["rss_mb"], s["max_rss_mb"],
                    s["open_fds"], s["threads"],
                ])
        except Exception:
            pass
        time.sleep(interval)


def start(interval: int = None):
    """启动后台采样守护线程。interval 缺省读 FENSHEN_TELEMETRY_INTERVAL（秒）。<=0 → 不启动。"""
    global _interval, _thread, _running
    if interval is None:
        try:
            interval = int(os.environ.get("FENSHEN_TELEMETRY_INTERVAL", "0"))
        except Exception:
            interval = 0
    _interval = interval
    if interval <= 0:
        return False
    with _lock:
        if _running:
            return True
        _running = True
    _thread = threading.Thread(target=_sampler_loop, args=(interval,), daemon=True)
    _thread.start()
    return True


def stop():
    """停止采样守护线程。"""
    global _running
    with _lock:
        _running = False


def is_enabled() -> bool:
    with _lock:
        return _running and _interval > 0
