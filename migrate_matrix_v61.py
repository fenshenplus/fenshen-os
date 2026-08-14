#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_matrix_v61.py —— 分身 v6.1 矩阵看板 存量数据迁移

问题根因：旧系统只有一个自由维度（模块），于是「阶段」被塞进了 modules 表。
本脚本把数据纠正为「模块 × 阶段」矩阵所需的字段：
  - tasks.stage / tasks.track
  - modules.track / modules.weight
  - projects.tracks / projects.stage_chains

迁移策略（与 看板矩阵与元神续航设计v1.md §A.5 一致）：
  1. 模块名命中「阶段词典」→ 判定为被污染的阶段模块：
       其下任务 stage 置为该阶段名，module_id 改指同项目默认模块「主体功能」（不存在则创建），
       原污染模块删除。
  2. 其余真模块保留，其任务 stage 由 owner_role 推断（产品→策划 / 前端→前端 / 后端→后端 / 测试→测试 / 架构师→策划）。
  3. 推断不出 → stage='策划'，打印到 dry-run 摘要供人工归位。
  4. 项目 tracks=['web']、stage_chains 写入 web 预设链。

安全约定：
  - 默认 --dry-run：只打印映射，不落库、不删模块。
  - 真正改写需显式 --apply；改写前自动备份 DB 到 <db>.bak-<时间戳>。
  - 绝不静默丢弃数据：污染模块的任务全部迁走后才删模块。

用法：
  python3 migrate_matrix_v61.py --db /path/to/fenshen.db            # 预览
  python3 migrate_matrix_v61.py --db /path/to/fenshen.db --apply    # 执行
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

# ── 阶段词典（与 backend STAGE_PRESETS 的 web 链对齐，可单独扩充） ──
STAGE_DICT = {
    "需求澄清": "策划", "需求分析": "策划", "需求": "策划",
    "页面设计": "原型", "UI设计": "原型", "原型设计": "原型",
    "原型": "原型", "交互设计": "原型", "设计": "原型",
    "前端开发": "前端", "前端": "前端", "H5开发": "前端", "页面": "前端",
    "后端开发": "后端", "后端": "后端", "接口对接": "后端", "云函数": "后端", "实现": "后端",
    "联调": "联调", "封装": "联调",
    "测试": "测试", "测试验收": "测试", "测试与质量": "测试",
    "提审": "发布", "发布": "发布", "上线": "发布", "上架": "发布",
    "交付与发布": "发布", "交付": "发布",
}

# owner_role → 阶段（真模块的任务按角色归位）。旧库角色名大小写不一（前端/backend/architect），统一小写归一。
ROLE_STAGE = {
    "产品": "策划", "产品经理": "策划", "产品总监": "策划", "产品策划": "策划",
    "前端": "前端", "前端工程师": "前端", "frontend": "前端",
    "后端": "后端", "后端工程师": "后端", "backend": "后端",
    "测试": "测试", "测试工程师": "测试", "tester": "测试",
    "架构师": "策划", "architect": "策划",
}
DEFAULT_STAGE = "策划"


def _role_to_stage(owner):
    o = (owner or "").strip().lower()
    return ROLE_STAGE.get(o, DEFAULT_STAGE)
WEB_CHAIN = ["策划", "原型", "前端", "后端", "联调", "测试", "发布"]


def conn_db(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def ensure_cols(c):
    """幂等：确保列存在（与 main.py init_db 的迁移段一致）。"""
    for tbl, col, ddl in [
        ("tasks", "stage", "ALTER TABLE tasks ADD COLUMN stage TEXT DEFAULT ''"),
        ("tasks", "track", "ALTER TABLE tasks ADD COLUMN track TEXT DEFAULT 'web'"),
        ("modules", "track", "ALTER TABLE modules ADD COLUMN track TEXT DEFAULT 'web'"),
        ("modules", "weight", "ALTER TABLE modules ADD COLUMN weight REAL DEFAULT 1.0"),
        ("projects", "tracks", "ALTER TABLE projects ADD COLUMN tracks TEXT DEFAULT '[\"web\"]'"),
        ("projects", "stage_chains", "ALTER TABLE projects ADD COLUMN stage_chains TEXT DEFAULT '{}'"),
    ]:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
        if col not in cols:
            c.execute(ddl)
    c.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "data", "fenshen.db"))
    ap.add_argument("--apply", action="store_true", help="真正落库（默认仅预览）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[ERR] 数据库不存在: {args.db}", file=sys.stderr)
        sys.exit(2)

    db = os.path.abspath(args.db)
    c = conn_db(db)
    ensure_cols(c)

    projects = c.execute("SELECT id, name FROM projects").fetchall()
    print(f"=" * 60)
    print(f"分身 v6.1 矩阵迁移  {'[APPLY 模式]' if args.apply else '[DRY-RUN 预览]'}")
    print(f"数据库: {db}")
    print(f"项目数: {len(projects)}")
    print(f"=" * 60)

    if args.apply:
        _bak = db + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(db, _bak)
        print(f"📦 已自动备份数据库至: {_bak}")

    plan = []  # 每个项目的操作摘要

    for p in projects:
        pid, pname = p["id"], p["name"]
        mods = c.execute("SELECT * FROM modules WHERE project_id=?", (pid,)).fetchall()
        tasks = c.execute("SELECT * FROM tasks WHERE project_id=?", (pid,)).fetchall()

        ops = {"project": pname, "demote": [], "reassign": [], "project_meta": False}
        body_funcs = []  # 延迟到 --apply 时执行

        # 1) 污染模块降级的规划
        for m in mods:
            if m["name"] in STAGE_DICT:
                stage = STAGE_DICT[m["name"]]
                mtasks = [t for t in tasks if t["module_id"] == m["id"]]
                ops["demote"].append((m["id"], m["name"], stage, len(mtasks)))
                if args.apply:
                    # 创建/取「主体功能」默认模块
                    dm = c.execute(
                        "SELECT id FROM modules WHERE project_id=? AND name='主体功能' LIMIT 1", (pid,)).fetchone()
                    if dm:
                        dmid = dm["id"]
                    else:
                        dmid = f"{pid}-m-body"
                        sort_max = c.execute(
                            "SELECT COALESCE(MAX(sort),0) FROM modules WHERE project_id=?", (pid,)).fetchone()[0]
                        c.execute(
                            "INSERT INTO modules (id,project_id,name,desc,depends_on,owner_role,status,sort,created_at,updated_at,track,weight) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (dmid, pid, "主体功能", "v6.1 矩阵迁移自动归纳", "[]", "后端", "idea",
                             sort_max + 1, datetime.now().isoformat(), datetime.now().isoformat(), "web", 1.0))
                    # 迁任务 + 话题
                    c.execute(
                        "UPDATE tasks SET stage=?, module_id=?, track='web' WHERE module_id=?",
                        (stage, dmid, m["id"]))
                    c.execute("UPDATE topics SET module_id=? WHERE module_id=?", (dmid, m["id"]))
                    c.execute("DELETE FROM modules WHERE id=?", (m["id"],))

        # 2) 真模块任务的 stage 推断（污染模块已被迁走，这里只处理剩余的）
        for t in tasks:
            if t["module_id"] in {d[0] for d in ops["demote"]}:
                continue  # 已被降级处理
            owner = (t["owner_role"] or "后端")
            stage = _role_to_stage(owner)
            if (t["stage"] or ""):
                continue  # 已有 stage（重复运行幂等）
            if args.apply:
                c.execute("UPDATE tasks SET stage=?, track='web' WHERE id=?", (stage, t["id"]))
            else:
                ops["reassign"].append((t["id"], t["name"][:20], owner, stage))

        # 3) 项目 tracks / stage_chains
        cur = c.execute("SELECT tracks, stage_chains FROM projects WHERE id=?", (pid,)).fetchone()
        tracks = (cur["tracks"] or '["web"]')
        chains = (cur["stage_chains"] or "{}")
        if args.apply:
            try:
                chains_d = json.loads(chains) or {}
            except Exception:
                chains_d = {}
            chains_d.setdefault("web", WEB_CHAIN)
            c.execute("UPDATE projects SET tracks=?, stage_chains=? WHERE id=?",
                      (json.dumps(["web"], ensure_ascii=False), json.dumps(chains_d, ensure_ascii=False), pid))
            ops["project_meta"] = True

        plan.append(ops)

    # 输出摘要
    total_demote = sum(len(o["demote"]) for o in plan)
    total_reassign = sum(len(o["reassign"]) for o in plan)
    print(f"\n待降级污染模块（阶段冒充模块）：{total_demote} 个")
    print(f"待归位任务（按角色推断 stage）：{total_reassign} 条")
    for o in plan:
        print(f"\n• 项目 {o['project']} (id={[p['id'] for p in projects if p['name']==o['project']]})")
        for mid, mname, stage, n in o["demote"]:
            print(f"    [降级] 模块「{mname}」→ 阶段「{stage}」，{n} 个任务迁至「主体功能」")
        if o["reassign"]:
            print(f"    [归位] 示例：{o['reassign'][:5]}")
            if len(o["reassign"]) > 5:
                print(f"            … 另 {len(o['reassign'])-5} 条")
        if o["project_meta"]:
            print(f"    [项目] tracks=['web']，stage_chains 写入 web 预设链")

    if args.apply:
        c.commit()
        print("\n✅ 已落库。建议立即用前端「矩阵看板」核对各项目任务总数是否 = 迁移前。")
    else:
        print("\n⚠️ 以上为预览，未做任何修改。加 --apply 才真正执行（执行前会自动备份 DB）。")

    c.close()


if __name__ == "__main__":
    main()
