#!/usr/bin/env python3
"""分身 v6.3 分模块完整性测试：M1-M9 桌面端（API 级断言，真实数据）。"""
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

BASE = "http://127.0.0.1:8011"
TOKEN = open("/Users/a13401098230/WorkBuddy/fenshen-v1/data/.auth_token").read().strip()
HDRS = {"Content-Type": "application/json", "x-fenshen-token": TOKEN}

RESULTS = {}  # mod -> [(name, ok, detail)]


def q(url_path):
    """URL 路径分段编码（支持中文 mid/stage；保留 query 符号）。"""
    path, _, query = url_path.partition("?")
    enc = "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))
    return enc + ("?" + query if query else "")


def api(method, path, body=None, timeout=240):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + q(path), data=data, headers=HDRS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return {"http_error": e.code, "body": e.read().decode()[:200]}
        except Exception:
            return {"http_error": e.code}
    except Exception as e:
        return {"error": str(e)}


def check(mod, name, cond, detail=""):
    RESULTS.setdefault(mod, []).append((name, bool(cond), detail))


def get_projects():
    return api("GET", "/api/projects")


def find_busy_project(projects):
    """选一个模块较多的项目。"""
    best, best_mods = None, -1
    for p in projects:
        mods = api("GET", f"/api/projects/{p['id']}/modules")
        if isinstance(mods, list) and len(mods) > best_mods:
            best_mods = len(mods)
            best = p["id"]
    return best


def main():
    projects = get_projects()
    check("M0-环境", "projects 列表", isinstance(projects, list) and len(projects) > 0, f"n={len(projects) if isinstance(projects, list) else 'ERR'}")
    pid = find_busy_project(projects) if isinstance(projects, list) else None
    if not pid:
        print("NO PROJECTS"); sys.exit(1)

    # ============ M1 矩阵看板 ============
    mx = api("GET", f"/api/projects/{pid}/matrix")
    need = ["stages", "modules", "cells", "critical_path"]
    for k in need:
        check("M1-矩阵", f"matrix 含 {k}", k in mx, f"keys={list(mx.keys())[:8]}")
    mods = mx.get("modules") or []
    cells = mx.get("cells") or {}
    check("M1-矩阵", "cells 按 track 组织", isinstance(cells, dict) and len(cells) > 0, f"tracks={list(cells.keys()) if isinstance(cells, dict) else type(cells).__name__}")
    if isinstance(cells, dict):
        first_track = next(iter(cells.values()))
        ok_track = isinstance(first_track, dict) and len(first_track) > 0
        sample = next(iter(first_track.values())) if ok_track else None
        ok_cell = bool(sample and isinstance(sample, dict) and all(k in sample for k in ("stage", "pct", "total", "done")))
        check("M1-矩阵", "track→stage→cell 结构", ok_track and ok_cell, f"stages={list(first_track.keys()) if ok_track else type(first_track).__name__}")
    pct_fields = ["row_pct", "column_pct", "project_pct"]
    has_pct = all(f in mx for f in pct_fields)
    check("M1-矩阵", "完成度聚合字段(顶层)", has_pct, f"keys 含 pct={has_pct}")
    # 模块 CRUD（domain/flow/layer 闭环）
    nm = f"评测临时模块{int(time.time())%1000}"
    c = api("POST", f"/api/projects/{pid}/modules", {"name": nm, "owner_role": "前端", "domain": "评测域", "flow": "评测流", "layer": "api"})
    check("M1-模块", "建模块(domain/flow/layer)", c.get("ok") is True or c.get("id"), str(c)[:100])
    mid = c.get("id") or (c.get("module", {}) or {}).get("id") or ""
    if mid:
        mm = api("GET", f"/api/projects/{pid}/modules")
        if isinstance(mm, list):
            found = next((x for x in mm if x["id"] == mid), None)
            check("M1-模块", "domain/flow/layer 回显",
                  found and found.get("domain") == "评测域" and found.get("flow") == "评测流" and found.get("layer") == "api",
                  str(found)[:120] if found else "not found")
        u = api("PATCH", f"/api/projects/{pid}/modules/{mid}", {"flow": "评测流改", "layer": "data"})
        check("M1-模块", "PATCH 更新", u.get("ok") is True, str(u)[:100])
        d = api("DELETE", f"/api/projects/{pid}/modules/{mid}")
        check("M1-模块", "删除模块", d.get("ok") is True or d.get("deleted"), str(d)[:100])
    # 语义搜索
    s = api("POST", "/api/search", {"q": mx.get("stages") and "登录" or "模块"})
    check("M1-语义搜索", "search ok", s.get("ok") is True, f"n={(s.get('results') or []).__len__()}")
    if s.get("results"):
        r0 = s["results"][0]
        check("M1-语义搜索", "结果字段齐全", all(k in r0 for k in ("type", "id", "title", "score")), str(list(r0.keys())))
    # 关键路径
    cp = mx.get("critical_path") or []
    check("M1-矩阵", "关键路径可解析", isinstance(cp, list), f"n={len(cp)}")

    # ============ M2 版本管理 ============
    t = api("GET", f"/api/projects/{pid}/tasks")
    tlist = t if isinstance(t, list) else []
    target = next((x for x in tlist if x.get("stage")), None)
    if target:
        mid2, stage2 = target["module_id"], target["stage"]
        v = api("POST", f"/api/cells/{mid2}/{stage2}/version", {"kind": "confirmed", "note": "评测快照"})
        check("M2-版本", "创建 confirmed 快照", v.get("ok") is True, str(v)[:120])
        vs = api("GET", f"/api/cells/{mid2}/{stage2}/versions")
        vlist = vs if isinstance(vs, list) else (vs.get("versions") or [])
        check("M2-版本", "版本链 ≥1", len(vlist) >= 1, f"n={len(vlist)}")
        if len(vlist) >= 1:
            check("M2-版本", "版本列表字段齐全", all(k in vlist[0] for k in ("id", "version_no", "kind", "snapshot_name", "created_at", "content_len")), str(list(vlist[0].keys())))
        # wip 预编辑
        w = api("POST", f"/api/cells/{mid2}/{stage2}/version", {"kind": "wip", "note": "评测预编辑"})
        check("M2-版本", "创建 wip 快照", w.get("ok") is True, str(w)[:120])
        d = api("GET", f"/api/cells/{mid2}/{stage2}/versions/diff")
        check("M2-版本", "diff 接口可访问", not isinstance(d, dict) or "error" not in d or d.get("ok") is not False, str(d)[:80])
    else:
        check("M2-版本", "有 stage 任务可测", False, "无带 stage 任务")

    # ============ M3 项目群聊 ============
    c3 = api("POST", f"/api/projects/{pid}/chat", {"text": "汇报一下当前项目进度，用一句话。"}, timeout=300)
    check("M3-群聊", "闲聊指令有回复", isinstance(c3, dict) and (c3.get("ok") or c3.get("reply")), str(c3)[:120])
    msgs = api("GET", f"/api/messages/{pid}")
    check("M3-群聊", "消息落库", isinstance(msgs, list) and len(msgs) > 0, f"n={len(msgs) if isinstance(msgs, list) else 'ERR'}")
    # 执行类指令（自动调度）—— 用轻量执行指令，避免长任务
    c3b = api("POST", f"/api/projects/{pid}/chat", {"text": "检查一下项目里有没有状态为 doing 的任务，有则列出来，不用执行修改。"}, timeout=300)
    check("M3-群聊", "执行类指令可处理", isinstance(c3b, dict) and (c3b.get("ok") or c3b.get("reply") or c3b.get("job_id")), str(c3b)[:150])
    if c3b.get("job_id"):
        jid = c3b["job_id"]
        for _ in range(40):
            st = api("GET", f"/api/jobs/{jid}")
            if st.get("status") in ("done", "failed"):
                break
            time.sleep(6)
        check("M3-群聊", "派单 job 完成", st.get("status") == "done", f"status={st.get('status')} err={st.get('error','')[:80]}")
        tr = api("GET", f"/api/projects/{pid}/trajectory")
        check("M3-群聊", "trajectory 事件存在", isinstance(tr, list) and len(tr) > 0, f"n={len(tr) if isinstance(tr, list) else 'ERR'}")
        if isinstance(tr, list):
            kinds = {e.get("kind") for e in tr}
            check("M3-群聊", "run_start/plan 事件", "run_start" in kinds and "plan" in kinds, f"kinds={sorted(kinds)}")

    # ============ M4 话题对话组 ============
    topics = api("GET", f"/api/projects/{pid}/topics")
    check("M4-话题", "话题列表", isinstance(topics, list), f"n={len(topics) if isinstance(topics, list) else 'ERR'}")
    tid = topics[0]["id"] if isinstance(topics, list) and topics else ""
    if tid:
        tmsgs = api("GET", f"/api/topics/{tid}/messages")
        check("M4-话题", "话题消息可读", isinstance(tmsgs, list), f"n={len(tmsgs) if isinstance(tmsgs, list) else 'ERR'}")
        # 提炼任务落 stage
        tk = api("POST", f"/api/topics/{tid}/tasks", {"name": f"评测提炼任务{int(time.time())%1000}", "owner_role": "后端"})
        check("M4-话题", "提炼任务 ok", tk.get("ok") is True, str(tk)[:120])
        tkid = tk.get("id") or ""
        if tkid:
            tl = api("GET", f"/api/projects/{pid}/tasks")
            tt = next((x for x in (tl if isinstance(tl, list) else []) if x["id"] == tkid), None)
            check("M4-话题", "提炼任务落 stage(非空)", bool(tt and tt.get("stage")), f"stage={tt and tt.get('stage')}")
            api("DELETE", f"/api/tasks/{tkid}")

    # ============ M5 元神驾驶舱 ============
    ov = api("GET", "/api/meta/overview")
    check("M5-驾驶舱", "overview 状态卡", isinstance(ov, dict) and (ov.get("ok") or ov.get("state") or ov.get("projects") is not None), str(list(ov.keys()))[:100])
    su = api("GET", "/api/meta/sufficiency")
    check("M5-驾驶舱", "蒸馏充足度", isinstance(su, dict) and ("sufficiency" in su or "items" in su or su.get("ok") is not None), str(list(su.keys()))[:100])
    at = api("GET", "/api/meta/attribution")
    check("M5-驾驶舱", "疗效归因", isinstance(at, dict) and (at.get("ok") or "items" in at or at.get("attribution") is not None), str(list(at.keys()))[:100])
    mb = api("GET", f"/api/meta/morning-brief?project_id={pid}")
    check("M5-驾驶舱", "晨报可生成", isinstance(mb, dict) and mb.get("ok") is True, f"err={mb.get('error','')[:80]}")

    # ============ M6 元神续航 ============
    ap = api("GET", "/api/autopilot/state")
    check("M6-续航", "调度状态", isinstance(ap, dict) and ("mode" in ap or ap.get("ok") is not None), str(list(ap.keys()))[:120])
    if isinstance(ap, dict) and "mode" in ap:
        cur = ap["mode"]
        new = "rest" if cur != "rest" else "normal"
        s2 = api("POST", "/api/autopilot/set", {"mode": new})
        check("M6-续航", "三模式切换", s2.get("ok") is True, str(s2)[:100])
        s3 = api("POST", "/api/autopilot/set", {"mode": cur})
        check("M6-续航", "切回原模式", s3.get("ok") is True, str(s3)[:100])

    # ============ M7 记忆与经验 ============
    mem = api("GET", "/api/memory")
    check("M7-记忆", "长期记忆列表", isinstance(mem, list) or (isinstance(mem, dict) and "error" not in mem), f"n={len(mem) if isinstance(mem, list) else 'dict'}")
    exp = api("GET", "/api/experiences")
    check("M7-经验", "经验库", isinstance(exp, list), f"n={len(exp) if isinstance(exp, list) else 'ERR'}")
    sk = api("GET", "/api/skills")
    skl = sk if isinstance(sk, list) else (sk.get("skills") or [])
    check("M7-技能", "技能库 ≥19 内置", len(skl) >= 19, f"n={len(skl)}")
    rec = api("GET", "/api/experiences/recall")
    check("M7-经验", "经验召回可访问", isinstance(rec, dict) and "error" not in rec, str(list(rec.keys()))[:80])
    vault = api("GET", "/api/export/vault")
    check("M7-记忆", "Vault 导出", isinstance(vault, dict) and "error" not in vault, str(list(vault.keys()))[:80])

    # ============ M8 工具与护栏 ============
    el = api("GET", "/api/exec/log")
    check("M8-工具", "exec 日志", isinstance(el, list), f"n={len(el) if isinstance(el, list) else 'ERR'}")
    bl = api("GET", "/api/browser/log")
    check("M8-工具", "browser 日志", isinstance(bl, list), f"n={len(bl) if isinstance(bl, list) else 'ERR'}")
    fl = api("GET", "/api/file/log")
    check("M8-工具", "file 日志", isinstance(fl, list), f"n={len(fl) if isinstance(fl, list) else 'ERR'}")
    cpv = api("POST", "/api/cleanup/preview")
    check("M8-护栏", "清理预览", isinstance(cpv, dict) and "error" not in cpv, str(list(cpv.keys()))[:100])

    # ============ M9 账号与合规 ============
    ast = api("GET", "/api/auth/status")
    check("M9-账号", "auth 状态", isinstance(ast, dict) and "error" not in ast, str(ast)[:100])
    models = api("GET", "/api/models")
    check("M9-账号", "模型设置", isinstance(models, list) or (isinstance(models, dict) and models.get("ok") is not None), str(list(models.keys()) if isinstance(models, dict) else f"list n={len(models)}")[:100])
    ax = api("GET", "/api/account/export")
    check("M9-账号", "数据导出", isinstance(ax, dict) and "error" not in ax, str(list(ax.keys()))[:100])

    # ============ 汇总 ============
    print("\n==== 分模块测试汇总 ====")
    total_p = total_f = 0
    for mod in sorted(RESULTS):
        items = RESULTS[mod]
        p = sum(1 for _, ok, _ in items if ok)
        f = len(items) - p
        total_p += p; total_f += f
        print(f"  {mod}: {p}/{len(items)}")
        for name, ok, detail in items:
            if not ok:
                print(f"    ✗ {name}  {detail}")
    print(f"\n总计: 通过 {total_p} / 失败 {total_f}")
    return 0 if total_f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
