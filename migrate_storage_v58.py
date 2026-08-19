#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.8 P1 双维度存储：旧项目全量迁移脚本（幂等、可重跑）。

把 v5.6 及之前落在 ~/Desktop/<项目名> 的成果，迁入新双维度结构：
  ~/.fenshen/projects/<pid>/
    ├── public/        项目公共文件夹（旧 ~/Desktop/<项目名> 内容搬这里）
    ├── members/       每个群聊成员单独文件夹（当前为空，待使用）
    └── modules/<mid>/ 按看板模块组织的成果文件夹

用法：
  python3 migrate_storage_v58.py            # dry-run：只打印迁移计划，不改动
  python3 migrate_storage_v58.py --apply     # 真正执行迁移
"""
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "fenshen.db")
HOME = os.path.expanduser("~")
APPLY = "--apply" in sys.argv


def main():
    if not os.path.exists(DB):
        print(f"ℹ️ 未找到数据库 {DB}，无需迁移。")
        return
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    # 确保新列存在（脚本可独立运行，不依赖后端已 init_db）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "storage_root" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN storage_root TEXT DEFAULT ''")
    if "design_standard" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN design_standard TEXT DEFAULT ''")
    projects = conn.execute("SELECT * FROM projects").fetchall()
    print(f"{'【执行迁移】' if APPLY else '【预览(dry-run)】'} 共 {len(projects)} 个项目\n")

    moved_files = 0
    for p in projects:
        pid = p["id"]
        name = (p["name"] or "未命名项目").strip().strip("/")
        try:
            old_sr = (p["storage_root"] or "").strip()
        except (KeyError, IndexError):
            old_sr = ""
        new_sr = os.path.join(HOME, ".fenshen", "projects", pid)

        # 已迁移过（storage_root 已是新结构）→ 仅确保目录树存在
        already = bool(old_sr) and os.path.isdir(old_sr)
        if already:
            new_sr = old_sr

        os.makedirs(os.path.join(new_sr, "public"), exist_ok=True)
        os.makedirs(os.path.join(new_sr, "members"), exist_ok=True)
        for m in conn.execute("SELECT id FROM modules WHERE project_id=?", (pid,)).fetchall():
            os.makedirs(os.path.join(new_sr, "modules", m["id"]), exist_ok=True)

        # 迁移 ~/Desktop/<name> 内容 → public/
        old_dir = os.path.join(HOME, "Desktop", name)
        plan = []
        if not already and os.path.isdir(old_dir):
            items = sorted(os.listdir(old_dir))
            for it in items:
                src = os.path.join(old_dir, it)
                dst = os.path.join(new_sr, "public", it)
                if os.path.exists(dst):
                    plan.append(f"    跳过(已存在): {it}")
                    continue
                plan.append(f"    迁移: {src} → {dst}")
                moved_files += 1
                if APPLY:
                    shutil.move(src, dst)
            # 旧目录空了就删（仅当全部移走且目录为空）
            if APPLY and old_dir.startswith(os.path.join(HOME, "Desktop")):
                try:
                    if not os.listdir(old_dir):
                        os.rmdir(old_dir)
                        plan.append(f"    删除空旧目录: {old_dir}")
                except Exception as e:
                    plan.append(f"    旧目录保留(非空/删除失败): {e}")

        # 回填 storage_root
        if APPLY and not already:
            conn.execute("UPDATE projects SET storage_root=? WHERE id=?", (new_sr, pid))

        print(f"● 项目 {pid} «{name}»")
        print(f"    storage_root → {new_sr}")
        if plan:
            print("\n".join(plan))
        else:
            print("    无 ~/Desktop 内容需迁移（目录树已就绪）")
        print()

    if APPLY:
        conn.commit()
        print(f"✅ 迁移完成：{moved_files} 个文件/目录已迁入双维度结构。")
    else:
        print(f"预览：将迁移 {moved_files} 个文件/目录。加 --apply 真正执行。")
    conn.close()


if __name__ == "__main__":
    main()
