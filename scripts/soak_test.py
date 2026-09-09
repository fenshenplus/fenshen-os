#!/usr/bin/env python3
"""分身 72h 内存/句柄泄漏长跑观测工具（P0-b）。

独立脚本，不依赖分身进程：周期性 GET /api/health，记录
uptime / rss_mb / max_rss_mb / open_fds / threads，
并在 RSS 持续单调上涨（疑似泄漏）时打印告警，写入 CSV 供事后分析。

用法：
  python3 scripts/soak_test.py --hours 72 --interval 60
  python3 scripts/soak_test.py --url http://127.0.0.1:8002/api/health --hours 3 --out /tmp/soak.csv
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request


def main():
    p = argparse.ArgumentParser(description="分身 72h 内存/句柄泄漏长跑观测")
    p.add_argument("--url", default="http://127.0.0.1:8002/api/health")
    p.add_argument("--hours", type=float, default=72.0, help="总运行时长（小时）")
    p.add_argument("--interval", type=float, default=60.0, help="采样间隔（秒）")
    p.add_argument("--out", default=os.path.join(os.path.expanduser("~"), ".fenshen", "soak.csv"))
    args = p.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    end = time.time() + args.hours * 3600
    prev_rss = None
    rising = 0

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "uptime_seconds", "rss_mb", "max_rss_mb", "open_fds", "threads", "alert"])
        f.flush()
        while time.time() < end:
            alert = ""
            try:
                with urllib.request.urlopen(args.url, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.URLError as e:
                print(f"[soak] 连接失败 {e}", flush=True)
                time.sleep(args.interval)
                continue

            rss = data.get("rss_mb", 0)
            # RSS 连续 5 次净上涨 → 疑似泄漏告警
            if prev_rss is not None and rss > prev_rss + 0.5:
                rising += 1
                if rising >= 5:
                    alert = f"RSS 连续 {rising} 次上涨 (prev={prev_rss} now={rss}) 疑似泄漏"
            else:
                rising = 0
            prev_rss = rss

            w.writerow([
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                data.get("uptime_seconds"), rss, data.get("max_rss_mb"),
                data.get("open_fds"), data.get("threads"), alert,
            ])
            f.flush()
            if alert:
                print(f"[soak] ALERT: {alert}", flush=True)
            time.sleep(args.interval)
    print("[soak] 完成", flush=True)


if __name__ == "__main__":
    main()
