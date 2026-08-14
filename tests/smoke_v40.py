#!/usr/bin/env python3
"""分身 v4.0 回归冒烟测试

覆盖：① 全部 GET 接口可用性 ② P0 安全防护有效性 ③ 关键写链路（项目/模块/任务/状态流转）
用法：python tests/smoke_v40.py [--base http://127.0.0.1:8002]
退出码：0 = 全通过；1 = 有失败。
"""
import argparse
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8002"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", ".auth_token")
PASS, FAIL = [], []


def token() -> str:
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    return ""


TOK = token()


def call(method: str, path: str, body=None, headers=None, timeout=20):
    """返回 (status_code, parsed_body_or_text)。"""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("x-fenshen-token", TOK)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return -1, str(e)


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"{'  PASS' if ok else '! FAIL'}  {name}{('  — ' + detail) if detail else ''}")


# ── 1. GET 接口可用性 ─────────────────────────────────────────
GETS = [
    "/api/health", "/api/projects", "/api/templates", "/api/roles", "/api/resources",
    "/api/meta/files", "/api/models", "/api/models/usage", "/api/exec/log",
    "/api/browser/log", "/api/file/log", "/api/memory", "/api/cleanup/preview",
    "/api/context", "/api/phases", "/api/skills", "/api/experiences", "/api/reviews",
    "/api/meta/overview", "/api/meta/patrol", "/api/meta/settings", "/api/meta/profile",
]


def test_gets():
    print("\n── 1. GET 接口可用性 ──")
    for p in GETS:
        code, _ = call("GET", p)
        check(f"GET {p}", code == 200, f"HTTP {code}")


# ── 2. P0 安全防护 ────────────────────────────────────────────
def test_security():
    print("\n── 2. P0 安全防护 ──")
    # 2.1 无 token 必须 401
    req = urllib.request.Request(BASE + "/api/projects")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            check("无 token 访问 /api/projects 应 401", False, f"实际 {r.status}")
    except urllib.error.HTTPError as e:
        check("无 token 访问 /api/projects 应 401", e.code == 401, f"HTTP {e.code}")

    # 2.2 伪造 Host（DNS rebinding）必须 403
    code, _ = call("GET", "/api/projects", headers={"Host": "evil.example.com"})
    check("伪造 Host 应 403", code == 403, f"HTTP {code}")

    # 2.3 跨站 Origin（CSRF）必须 403
    code, _ = call("POST", "/api/cleanup", {"scope": "temp"},
                   headers={"Origin": "http://evil.example.com"})
    check("跨站 Origin 应 403", code == 403, f"HTTP {code}")

    # 2.4 cleanup 缺 scope 必须 400
    code, b = call("POST", "/api/cleanup", {})
    check("cleanup 缺 scope 应 400", code == 400, f"HTTP {code}")

    # 2.5 cleanup 非法 scope 必须 400
    code, _ = call("POST", "/api/cleanup", {"scope": "everything"})
    check("cleanup 非法 scope 应 400", code == 400, f"HTTP {code}")

    # 2.6 空 body / 坏 JSON 应 400（不是 500）
    req = urllib.request.Request(BASE + "/api/cleanup", data=b"", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-fenshen-token", TOK)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            check("坏 JSON 应 400", False, f"实际 {r.status}")
    except urllib.error.HTTPError as e:
        check("坏 JSON 应 400（非 500）", e.code == 400, f"HTTP {e.code}")


# ── 3. exec 真实性 ───────────────────────────────────────────
def test_exec():
    print("\n── 3. /api/exec 真实性 ──")
    code, b = call("POST", "/api/exec", {"command": "echo fenshen-v40-smoke"}, timeout=40)
    ok = code == 200 and isinstance(b, dict) and b.get("ok") is True \
        and b.get("exit_code") == 0 and "fenshen-v40-smoke" in str(b.get("output", ""))
    check("exec 成功命令 ok=true 且有真实输出", ok, str(b)[:120])

    code, b = call("POST", "/api/exec", {"command": "ls /definitely/not/exist/xyz"}, timeout=40)
    ok = code == 200 and isinstance(b, dict) and b.get("ok") is False
    check("exec 失败命令 ok=false（修复恒真）", ok, str(b)[:120])

    # 危险命令：客户端自带 confirm 也不得放行（服务端弹窗，超时 fail-closed）
    # v5.1：approval_timeout≤3 时服务端直接拒绝（不弹窗），测试确定且无打扰。
    _, cur = call("GET", "/api/meta/settings", timeout=20)
    old_to = cur.get("approval_timeout", 90) if isinstance(cur, dict) else 90
    call("POST", "/api/meta/settings", {"approval_timeout": 2}, timeout=20)
    try:
        code, b = call("POST", "/api/exec",
                       {"command": "rm -rf /tmp/fenshen-smoke-target", "confirm": True}, timeout=60)
        blocked = isinstance(b, dict) and (b.get("ok") is False or b.get("blocked"))
        check("危险命令带 confirm 仍被服务端拦截", blocked, str(b)[:140])
    finally:
        call("POST", "/api/meta/settings", {"approval_timeout": int(old_to)}, timeout=20)


# ── 4. 关键写链路 + 任务状态流转（v4.0 能派修复）──────────────
def test_task_flow():
    print("\n── 4. 关键写链路 / 任务状态流转 ──")
    code, b = call("POST", "/api/projects", {"name": "冒烟测试项目_v40", "goal": "回归验证"})
    pid = b.get("id") if isinstance(b, dict) else None
    check("创建项目", code == 200 and bool(pid), str(b)[:100])
    if not pid:
        return

    code, b = call("POST", f"/api/projects/{pid}/modules", {"name": "冒烟模块", "owner_role": "backend"})
    mid = b.get("id") if isinstance(b, dict) else None
    check("创建模块", code == 200 and bool(mid), str(b)[:100])

    code, b = call("POST", f"/api/projects/{pid}/topics",
                   {"name": "冒烟话题", "module_id": mid or ""})
    tpid = b.get("id") if isinstance(b, dict) else None
    check("创建话题", code == 200 and bool(tpid), str(b)[:100])
    if not tpid:
        return

    code, b = call("POST", f"/api/topics/{tpid}/tasks",
                   {"name": "冒烟任务", "owner_role": "backend"})
    tid = b.get("task_id") if isinstance(b, dict) else None
    check("话题提炼为任务（R2 任务必有来源）", code == 200 and bool(tid), str(b)[:100])
    if not tid:
        return

    # 元神调度：todo → doing 自动流转
    code, b = call("POST", "/api/meta/dispatch", {"task_id": tid, "to_role": "frontend"})
    ok = code == 200 and isinstance(b, dict) and b.get("status") == "doing"
    check("meta/dispatch 派单后状态自动 → doing", ok, str(b)[:120])

    # 手动流转 doing → done + 沉淀
    code, b = call("POST", f"/api/tasks/{tid}/move", {"to": "done"})
    ok = code == 200 and isinstance(b, dict) and b.get("status") == "done"
    check("任务 move → done", ok, str(b)[:120])

    # 校验落库
    code, b = call("GET", f"/api/projects/{pid}/tasks")
    got = [t for t in (b if isinstance(b, list) else []) if t.get("id") == tid]
    check("任务状态已落库为 done", bool(got) and got[0].get("status") == "done",
          str(got)[:120])

    # 清场
    call("DELETE", f"/api/tasks/{tid}")
    call("DELETE", f"/api/projects/{pid}")
    check("清理测试数据", True)


def test_standards_board():
    """批次 A：项目完成标准 + 聚合详情 + 看板↔群聊话题自动绑定。"""
    print("\n── 5. 批次 A：完成标准 / 聚合详情 / 看板↔群聊绑定 ──")
    STD = "微信登录可用 / 微信支付回调落单 / 测试≥80%"
    code, b = call("POST", "/api/projects",
                   {"name": "批次A测试", "goal": "知识付费小程序",
                    "standards": STD,
                    "modules": [{"name": "用户系统", "owner_role": "backend"},
                                {"name": "交易订单", "owner_role": "backend"}]})
    pid = b.get("id") if isinstance(b, dict) else None
    check("批次A：创建带完成标准的项目", code == 200 and bool(pid), str(b)[:100])
    if not pid:
        return

    # 聚合详情接口（看板/群聊联动一次性取齐）
    code, d = call("GET", f"/api/projects/{pid}")
    ok = code == 200 and isinstance(d, dict)
    check("批次A：GET /api/projects/{pid} 聚合详情", ok, f"HTTP {code}")
    if ok:
        check("批次A：返回完成标准 standards", d.get("standards") == STD, str(d.get("standards"))[:60])
        mods = d.get("modules") or []
        topics = d.get("topics") or []
        check("批次A：每模块自动建默认话题（断链修复）",
              len(mods) == 2 and len(topics) == 2,
              f"模块 {len(mods)} / 话题 {len(topics)}")
        # 每个模块都应有可绑定的话题 → 看板卡片点开能跳群聊
        mod_ids = {m["id"] for m in mods}
        bound = {t["module_id"] for t in topics if t.get("module_id")}
        check("批次A：话题一一覆盖模块", mod_ids <= bound, f"未覆盖: {mod_ids - bound}")

    # 清场
    call("DELETE", f"/api/projects/{pid}")
    check("批次A：清理测试数据", True)


def test_batch_b():
    """批次 B：任务完成标准 / 元神搭建开场 / 工具分级 / 护栏默认值。"""
    print("\n── 6. 批次 B：done_criteria / bootstrap / 工具分级 / 护栏 ──")
    # P2-4 护栏默认值（v4.1：approval_mode 默认 danger）
    code, h = call("GET", "/api/health")
    check("批次B：approval_mode 默认 danger", isinstance(h, dict) and h.get("approval_mode") == "danger",
          str(h.get("approval_mode")))
    check("批次B：release 版本 v5.5", isinstance(h, dict) and h.get("release") == "v5.5",
          str(h.get("release")) + "/" + str(h.get("version")))

    # ── v5.6 借鉴 DeepSeek Harness：工作区限定 + PTC 批处理 ──
    from backend.main import _write_file_tool, ROLE_TOOLS, _TOOL_WORKDIR, _project_workdir
    _TOOL_WORKDIR.set(_project_workdir("冒烟-工作区"))
    r_ws = _write_file_tool("~/Desktop/冒烟-越界.txt", "x")
    check("v5.6：工作区越界写被拦截", "越界" in r_ws and "工作区" in r_ws, r_ws[:60])
    r_ok = _write_file_tool(os.path.join(_project_workdir("冒烟-工作区"), "a.txt"), "ok")
    check("v5.6：工作区内写放行", "[已写入]" in r_ok, r_ok[:60])
    _TOOL_WORKDIR.set("")
    r_plain = _write_file_tool("~/Desktop/冒烟-普通.txt", "y")
    check("v5.6：无工作区上下文（元神）原行为", "[已写入]" in r_plain, r_plain[:60])
    _role_tool_names = [t.get("function", {}).get("name") for t in ROLE_TOOLS]
    check("v5.6：角色工具集含 run_batch（PTC）", "run_batch" in _role_tool_names, ", ".join(_role_tool_names))

    # P2-2 元神搭建基础设施：创建项目（带 roles）→ 群聊应有 bootstrap 开场消息
    code, b = call("POST", "/api/projects",
                   {"name": "批次B测试", "goal": "登录+支付 MVP", "standards": "登录可用",
                    "roles": ["architect", "backend"],
                    "modules": [{"name": "登录", "owner_role": "后端"}]})
    pid = b.get("id") if isinstance(b, dict) else None
    check("批次B：创建项目（含 roles）", code == 200 and bool(pid), str(b)[:100])
    if not pid:
        return
    code, msgs = call("GET", f"/api/messages/{pid}")
    ok = code == 200 and any("元神已为项目搭建好基础设施" in (m.get("text") or "") for m in (msgs or []))
    check("批次B：元神 bootstrap 开场消息落库", ok, f"HTTP {code} / 消息 {len(msgs) if isinstance(msgs, list) else 0} 条")

    # P1-1 任务级完成标准：话题提炼任务时写入 done_criteria
    code, d = call("GET", f"/api/projects/{pid}")
    tid = (d.get("topics") or [{}])[0].get("id") if isinstance(d, dict) else None
    code, b2 = call("POST", f"/api/topics/{tid}/tasks",
                    {"name": "写登录接口", "owner_role": "backend",
                     "done_criteria": "登录接口返回 200 且校验密码"})
    tk = b2.get("task_id") if isinstance(b2, dict) else None
    check("批次B：提炼任务带完成标准", code == 200 and bool(tk), str(b2)[:100])
    if tk:
        code, d2 = call("GET", f"/api/projects/{pid}")
        t = next((x for x in (d2.get("tasks") or []) if x["id"] == tk), None)
        check("批次B：done_criteria 已落库并透出",
              bool(t) and (t.get("done_criteria") or "").startswith("登录接口返回 200"),
              str(t.get("done_criteria"))[:50] if t else "task not found")

    # P2-1 工具分级：元神工具集无写文件、角色工具集含写文件（静态 import 验证）
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        from backend.main import META_TOOLS, ROLE_TOOLS
        meta_names = {t["function"]["name"] for t in META_TOOLS}
        role_names = {t["function"]["name"] for t in ROLE_TOOLS}
        check("批次B：元神工具集不含 write_file（只读+搭建）",
              "write_file" not in meta_names, "meta=" + ",".join(sorted(meta_names)))
        check("批次B：角色工具集含 write_file（可动手产出）",
              "write_file" in role_names and meta_names <= role_names,
              "role=" + ",".join(sorted(role_names)))
    except Exception as e:
        check("批次B：工具分级静态验证", False, str(e)[:80])

    # 清场
    call("DELETE", f"/api/projects/{pid}")
    check("批次B：清理测试数据", True)


def test_batch_c():
    """批次 C：预设技能活配件 / 角色动态加载 / 并行上限动态。"""
    print("\n── 7. 批次 C：技能活配件 / 角色动态加载 / 并行上限 ──")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

    # P3-2 预设技能：GET /api/skills 应有 12 条内置且全部启用
    code, sk = call("GET", "/api/skills")
    builtin = [s for s in sk if isinstance(s, dict) and s.get("category") == "builtin"] if isinstance(sk, list) else []
    check("批次C：13 条内置技能已 seed 且启用",
          len(builtin) == 13 and all(s.get("enabled") for s in builtin),
          f"内置 {len(builtin)} / 启用 {sum(1 for s in builtin if s.get('enabled'))}")

    # P3-2 触发注入：_match_skill_steps 命中 trigger → 返回步骤文本
    try:
        from backend.main import _match_skill_steps, BUILTIN_SKILLS, _roles_from_db, _role_id_by_name
        inj = _match_skill_steps("你是后端工程师", "请设计登录接口的 API 方案")
        check("批次C：技能 trigger 命中注入步骤", "【技能：API 设计】" in inj and "1." in inj, inj[:80].replace("\n", " "))
        inj_none = _match_skill_steps("你是客服", "今天天气不错，随便聊聊")
        check("批次C：未命中不注入", inj_none == "", inj_none[:40])
        check("批次C：BUILTIN_SKILLS 恰 13 种", len(BUILTIN_SKILLS) == 13, str(len(BUILTIN_SKILLS)))
    except Exception as e:
        check("批次C：技能注入静态验证", False, str(e)[:80])

    # P3-1 角色动态加载：新增自定义角色 → _roles_from_db 立即包含；_role_id_by_name 反查
    test_role = "ops"
    call("POST", "/api/roles", {"id": test_role, "name": "运维", "mandate": "负责部署上线与监控",
                                "skills": "deploy,ops", "gate": "线上可访问"})
    try:
        from backend.main import _roles_from_db, _role_id_by_name
        systems, names = _roles_from_db()
        check("批次C：角色表动态加载（含新增运维）",
              test_role in systems and names.get(test_role) == "运维",
              f"角色数 {len(systems)} · 运维mandate={'有' if test_role in systems else '无'}")
        check("批次C：中文名反查 id（消灭 ROLE_ID_MAP）",
              _role_id_by_name("运维") == "ops" and _role_id_by_name("后端") == "backend",
              f"运维→{_role_id_by_name('运维')} · 后端→{_role_id_by_name('后端')}")
    except Exception as e:
        check("批次C：角色动态加载验证", False, str(e)[:80])
    # 清场：直接删角色（roles 无 DELETE 接口，用 sqlite 直删）
    try:
        conn = __import__("sqlite3").connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "fenshen.db"))
        conn.execute("DELETE FROM roles WHERE id=?", (test_role,))
        conn.commit()
        conn.close()
        check("批次C：清理测试角色", True)
    except Exception as e:
        check("批次C：清理测试角色", False, str(e)[:60])


def test_batch_d():
    """批次 D（v4.2 自主闭环）：看板完成度 + 阶段自动流转。"""
    print("\n── 8. 批次 D：看板完成度 / 阶段自动流转 / autonomy ──")
    # 隔离：临时关闭自主推进循环（后台派单会并发改状态，干扰确定性断言），测完恢复
    _, s0 = call("GET", "/api/meta/settings", timeout=20)
    old_auto = s0.get("autonomy_enabled", "1") if isinstance(s0, dict) else "1"
    call("POST", "/api/meta/settings", {"autonomy_enabled": False}, timeout=20)
    # 建项目（带 standards 与 2 模块）
    code, b = call("POST", "/api/projects",
                   {"name": "批次D测试", "goal": "闭环验证", "standards": "全部可用",
                    "roles": ["backend", "frontend"],
                    "modules": [{"name": "模块A", "owner_role": "后端"},
                                {"name": "模块B", "owner_role": "前端"}]})
    pid = b.get("id") if isinstance(b, dict) else None
    check("批次D：创建项目", code == 200 and bool(pid), str(b)[:80])
    if not pid:
        call("POST", "/api/meta/settings", {"autonomy_enabled": old_auto == "1"}, timeout=20)
        return

    # 每模块建 2 张任务卡
    code, d = call("GET", f"/api/projects/{pid}")
    tids = []
    if isinstance(d, dict):
        for t in (d.get("topics") or []):
            for i in range(2):
                _, r = call("POST", f"/api/topics/{t['id']}/tasks",
                            {"name": f"任务{i + 1}", "owner_role": "后端", "done_criteria": "可验证"})
                if isinstance(r, dict) and r.get("task_id"):
                    tids.append(r["task_id"])

    # 完成度结构
    code, d2 = call("GET", f"/api/projects/{pid}")
    c = d2.get("completion", {}) if isinstance(d2, dict) else {}
    check("批次D：聚合详情含 completion",
          isinstance(c, dict) and c.get("total") == len(tids) and c.get("percent") == 0,
          f"done={c.get('done')}/total={c.get('total')}/pct={c.get('percent')}")

    # 阶段：未满 100% 不推进
    call("POST", f"/api/tasks/{tids[0]}/move", {"to": "done"})
    code, d3 = call("GET", f"/api/projects/{pid}")
    check("批次D：未满 100% 阶段不推进", isinstance(d3, dict) and d3.get("phase") == "requirement",
          f"phase={d3.get('phase')}")

    # 全部 done → 自动进入下一阶段（requirement → ui）
    for t in tids[1:]:
        call("POST", f"/api/tasks/{t}/move", {"to": "done"})
    code, d4 = call("GET", f"/api/projects/{pid}")
    c4 = d4.get("completion", {}) if isinstance(d4, dict) else {}
    check("批次D：看板 100% 自动进入下一阶段",
          isinstance(d4, dict) and d4.get("phase") == "ui" and c4.get("percent") == 100,
          f"phase={d4.get('phase')} pct={c4.get('percent')}")
    _, msgs = call("GET", f"/api/messages/{pid}")
    ok_msg = isinstance(msgs, list) and any("自动进入下一阶段" in (m.get("text") or "") for m in msgs)
    check("批次D：阶段流转群聊留痕", ok_msg)

    # v4.2 项目级自主推进暂停开关
    _, ab = call("POST", f"/api/projects/{pid}/autonomy", {"paused": True})
    check("批次D：项目级暂停自主推进", isinstance(ab, dict) and ab.get("paused") is True, str(ab)[:60])
    _, d5 = call("GET", f"/api/projects/{pid}")
    check("批次D：聚合详情透出 autonomy_paused",
          isinstance(d5, dict) and d5.get("autonomy_paused") == 1,
          f"paused={d5.get('autonomy_paused') if isinstance(d5, dict) else '?'}")
    _, ab2 = call("POST", f"/api/projects/{pid}/autonomy", {"paused": False})
    check("批次D：恢复自主推进", isinstance(ab2, dict) and ab2.get("paused") is False)

    # v5.1 应用市场：未完成项目上架被拒 + 市场列表
    _, pub = call("POST", f"/api/projects/{pid}/publish", {"target": "market"})
    check("批次D：未完成项目上架被拒",
          isinstance(pub, dict) and not pub.get("ok") and "仅已完成" in (pub.get("error") or ""),
          str(pub.get("error"))[:40])
    _, ml = call("GET", "/api/market")
    check("批次D：市场列表接口", isinstance(ml, list), f"{len(ml)} 项")

    # 清场
    call("DELETE", f"/api/projects/{pid}")
    call("POST", "/api/meta/settings", {"autonomy_enabled": old_auto == "1"}, timeout=20)
    check("批次D：清理测试数据", True)


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--skip-exec", action="store_true", help="跳过需人工确认弹窗的 exec 测试")
    a = ap.parse_args()
    BASE = a.base
    print(f"分身 v4.1 回归冒烟 → {BASE}  (token={'有' if TOK else '无'})")
    test_gets()
    test_security()
    if not a.skip_exec:
        test_exec()
    test_task_flow()
    test_standards_board()
    test_batch_b()
    test_batch_c()
    test_batch_d()
    print(f"\n{'=' * 52}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
