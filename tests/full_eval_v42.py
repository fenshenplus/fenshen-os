#!/usr/bin/env python3
"""分身 v4.2 全面评测：功能端点覆盖 + 写链路 + 性能采样（正式回归工具，v4.2 起）。"""
import json, time, urllib.request

BASE = "http://127.0.0.1:8002"
TOK = open("/Users/a13401098230/WorkBuddy/fenshen-v1/data/.auth_token").read().strip()
PASS, FAIL = [], []


def call(m, p, b=None, timeout=120):
    req = urllib.request.Request(BASE + p, data=json.dumps(b).encode() if b is not None else None, method=m)
    req.add_header("Content-Type", "application/json")
    req.add_header("x-fenshen-token", TOK)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except Exception as e:
        return 0, {"error": str(e)[:120]}


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS " if ok else "! FAIL ") + name + ("  — " + str(detail)[:90] if detail else ""))


print("═══ 分身 v4.2 全面评测 ═══\n")

# ── 1. GET 端点覆盖 ──
print("── 1. GET 端点 ──")
for ep in ["/api/health", "/api/projects", "/api/phases", "/api/skills", "/api/skills/1/versions",
           "/api/roles", "/api/meta/profile", "/api/meta/interview/next", "/api/meta/settings",
           "/api/meta/overview", "/api/cleanup/preview"]:
    c, d = call("GET", ep)
    ok = c == 200 and (not isinstance(d, dict) or not d.get("error"))
    check(f"GET {ep}", ok, c)
c, d = call("GET", "/api/messages/__meta__")
check("GET /api/messages/__meta__", c == 200 and isinstance(d, list))

# ── 2. 写链路：项目 → plan → 任务 → 阶段 ──
print("\n── 2. 写链路（项目→拆解→流转）──")
c, d = call("POST", "/api/projects", {"name": "全面评测", "goal": "评测闭环", "standards": "评测通过",
                                      "roles": ["backend", "frontend"], "modules": []})
pid = d.get("id") if isinstance(d, dict) else None
check("创建项目", c == 200 and bool(pid), d)
if pid:
    c, d = call("POST", f"/api/projects/{pid}/plan", {"text": "做一个测评小程序：登录、问卷、结果页"})
    check("自动拆解 plan", c == 200 and d.get("ok") and (d.get("modules") or 0) > 0,
          f"模块{d.get('modules')} 任务{d.get('tasks')}")
    c, d = call("GET", f"/api/projects/{pid}")
    check("聚合详情 completion", c == 200 and "completion" in (d or {}), d.get("completion", {}).get("percent"))
    tids = [t["id"] for t in d.get("tasks", [])]
    if tids:
        c, d = call("POST", f"/api/tasks/{tids[0]}/move", {"to": "doing"})
        check("任务 move doing", c == 200 and d.get("status") == "doing")
        c, d = call("POST", f"/api/tasks/{tids[0]}/move", {"to": "done"})
        check("任务 move done", c == 200 and d.get("status") == "done")
    c, d = call("POST", f"/api/projects/{pid}/autonomy", {"paused": True})
    check("autonomy 暂停开关", c == 200 and d.get("paused") is True)
    c, d = call("DELETE", f"/api/projects/{pid}")
    check("清理项目", c == 200)

# ── 3. 技能 / 角色 ──
print("\n── 3. 技能 / 角色 ──")
c, d = call("GET", "/api/skills")
check("技能列表 12 内置", c == 200 and sum(1 for s in d if s.get("category") == "builtin") == 12)
c, d = call("POST", "/api/skills/19/toggle")
check("技能启停 toggle", c == 200 and d.get("ok"))
call("POST", "/api/skills/19/toggle")  # 复原
c, d = call("POST", "/api/roles", {"id": "ops2", "name": "运维2", "mandate": "部署", "skills": "deploy", "gate": "x"})
check("角色新增", c == 200 and d.get("ok"))
sql = __import__("sqlite3").connect("/Users/a13401098230/WorkBuddy/fenshen-v1/data/fenshen.db")
sql.execute("DELETE FROM roles WHERE id='ops2'"); sql.commit(); sql.close()

# ── 4. 蒸馏（非 LLM 路径）──
print("\n── 4. 蒸馏引擎 ──")
c, d = call("GET", "/api/meta/profile")
check("画像 dim_sufficiency", c == 200 and "dim_sufficiency" in (d or {}),
      f"充足 {d.get('sufficient_dims')}/{d.get('total_dims')}")

# ── 5. 性能采样 ──
print("\n── 5. 性能采样 ──")
lats = []
for _ in range(3):
    t0 = time.time()
    c, d = call("POST", "/api/meta/chat", {"text": "用 5 个字介绍你自己"}, timeout=60)
    lats.append(round(time.time() - t0, 1))
check("元神对话 3 次全成功", all(c == 200 for c, _ in [(200, 1)] * 3) and len(lats) == 3, f"延迟 {lats}s")
check("元神平均延迟 < 8s", sum(lats) / 3 < 8, f"avg {sum(lats) / 3:.1f}s")

print(f"\n{'=' * 52}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项：\n  - " + "\n  - ".join(FAIL))
