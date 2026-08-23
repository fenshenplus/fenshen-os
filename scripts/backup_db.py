#!/usr/bin/env python3
"""分身数据库每日自动备份（v6.4 推广就绪度补齐）。

用 sqlite3.backup() 拿到一致快照，写入 ~/.fenshen/backups/，
按文件数轮转（保留最近 30 份）。由 launchd 每日触发，无需令牌。
"""
import os
import sys
import sqlite3
import datetime

DB = os.path.join(os.path.expanduser("~"), ".fenshen", "fenshen.db")
OUTDIR = os.path.join(os.path.dirname(DB), "backups")
KEEP = 30


def main():
    if not os.path.exists(DB):
        print("no db to backup:", DB)
        return 0
    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(OUTDIR, "fenshen.db.bak-%s" % stamp)
    try:
        src = sqlite3.connect(DB)
        dst = sqlite3.connect(path)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    except Exception as e:
        print("backup failed:", e)
        return 1
    size = os.path.getsize(path)
    # 轮转：仅保留最近 KEEP 份
    try:
        baks = sorted(
            (os.path.join(OUTDIR, f) for f in os.listdir(OUTDIR)
             if f.startswith("fenshen.db.bak-")),
            key=os.path.getmtime, reverse=True,
        )
        for old in baks[KEEP:]:
            os.remove(old)
    except Exception:
        pass
    print("backup ok: %s (%d bytes)" % (path, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
