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
    # 弹窗默认等 90 秒真人点击，会拖垮回归。这里临时把确认超时压到 5 秒，跑完恢复原值。
    _, cur = call("GET", "/api/meta/settings", timeout=20)
    old_to = cur.get("approval_timeout", 90) if isinstance(cur, dict) else 90
    call("POST", "/api/meta/settings", {"approval_timeout": 5}, timeout=20)
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


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--skip-exec", action="store_true", help="跳过需人工确认弹窗的 exec 测试")
    a = ap.parse_args()
    BASE = a.base
    print(f"分身 v4.0 回归冒烟 → {BASE}  (token={'有' if TOK else '无'})")
    test_gets()
    test_security()
    if not a.skip_exec:
        test_exec()
    test_task_flow()
    test_standards_board()
    print(f"\n{'=' * 52}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
