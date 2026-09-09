"""分身可观测性层 · 确定性单测（不依赖 LLM / 网络 / 主程序）。

验证 P0-b 可观测性层 backend/telemetry.py 的核心不变量：
  - snapshot() 返回 6 个约定字段且类型合理、峰值 >= 当前；
  - start(interval=0) 不启动（is_enabled False）；
  - start(interval>0) 真正起守护线程并按间隔写 CSV（表头 + 数据行）；
  - max_rss 单调递增跟踪峰值。
运行：python -m pytest tests/test_telemetry.py -q
      （或 python tests/test_telemetry.py）
"""
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import telemetry  # noqa: E402


def test_snapshot_keys_and_types():
    s = telemetry.snapshot()
    for k in ("started_at", "uptime_seconds", "rss_mb", "max_rss_mb", "open_fds", "threads"):
        assert k in s, f"缺少字段 {k}"
    assert isinstance(s["uptime_seconds"], (int, float))
    assert isinstance(s["open_fds"], int)
    assert isinstance(s["threads"], int)
    assert s["rss_mb"] >= 0
    # 峰值是观测到的历史最大，应 >= 当前（允许浮点误差）
    assert s["max_rss_mb"] >= s["rss_mb"] - 0.1


def test_start_disabled_by_zero():
    telemetry.stop()
    ok = telemetry.start(interval=0)
    assert ok is False
    assert telemetry.is_enabled() is False


def test_start_enabled_writes_csv(tmp_path, monkeypatch):
    telemetry.stop()
    csv_path = tmp_path / "telemetry.csv"
    monkeypatch.setattr(telemetry, "_csv_path", str(csv_path))
    ok = telemetry.start(interval=1)
    assert ok is True
    try:
        time.sleep(1.5)  # 等至少一次采样
        assert csv_path.exists()
        rows = list(csv.reader(open(csv_path, encoding="utf-8")))
        assert len(rows) >= 2, "应至少有表头 + 一行数据"
        assert rows[0] == ["ts", "uptime_seconds", "rss_mb", "max_rss_mb", "open_fds", "threads"]
        # 数据行可解析为数值
        assert float(rows[1][2]) >= 0
    finally:
        telemetry.stop()


def test_max_rss_tracks_peak():
    s1 = telemetry.snapshot()
    buf = "x" * (10 * 1024 * 1024)  # 制造 ~10MB 占用
    s2 = telemetry.snapshot()
    del buf
    assert s2["max_rss_mb"] >= s1["max_rss_mb"] - 0.1
