"""
分身 v1 阶段2 — 后端 API 全链路回归测试
- 覆盖所有接口：health / projects / roles / resources / messages / meta
- 校验 HTTP 状态 + 数据真实落库（读 SQLite 复核）
- 元神 LLM 降级路径验证（402 / 无 key 时优雅降级）
- 测试数据自清理（用完即删，不污染演示库）
运行：python tests/phase2_api_test.py
"""
import os
import sqlite3

import requests

BASE = "http://127.0.0.1:8002"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fenshen.db")

results = []
T = []  # 测试产生的需清理主键


def check(name, cond, detail=""):
    results.append((name, "PASS" if cond else "FAIL", detail))


def db_val(sql, args=()):
    conn = sqlite3.connect(DB)
    row = conn.execute(sql, args).fetchone()
    conn.close()
    return row


print("== 分身 v1 阶段2 API 回归测试 ==")
print(f"BASE={BASE}\n")

# 1. health
r = requests.get(f"{BASE}/api/health", timeout=5)
h = r.json()
check("GET /api/health", r.status_code == 200 and h.get("status") == "ok", f"version={h.get('version')} llm={h.get('llm')}")

# 2. 项目列表（种子4条）
r = requests.get(f"{BASE}/api/projects", timeout=5)
projs = r.json()
check("GET /api/projects", r.status_code == 200 and len(projs) >= 4, f"count={len(projs)}")

# 3. 创建项目（落库校验）
PID = "phase2test_proj"
r = requests.post(f"{BASE}/api/projects", json={"id": PID, "name": "阶段2测试项目", "goal": "验证用"}, timeout=5)
check("POST /api/projects", r.status_code == 200 and r.json().get("ok"), f"pid={PID}")
row = db_val("SELECT name,status FROM projects WHERE id=?", (PID,))
check("   └ 落库校验", row is not None and row[0] == "阶段2测试项目", f"db={row}")
T.append(("projects", PID))

# 4. 更新项目状态（中止→暂停 联动）
r = requests.patch(f"{BASE}/api/projects/{PID}", json={"status": "paused"}, timeout=5)
row = db_val("SELECT status FROM projects WHERE id=?", (PID,))
check("PATCH /api/projects/{id} 状态联动", r.status_code == 200 and row[0] == "paused", f"status={row[0] if row else None}")

# 5. 角色库列表 + 创建
r = requests.get(f"{BASE}/api/roles", timeout=5)
roles = r.json()
check("GET /api/roles", r.status_code == 200 and len(roles) >= 4, f"count={len(roles)}")
RID = "phase2test_role"
r = requests.post(f"{BASE}/api/roles", json={"id": RID, "name": "测试角色", "mandate": "m", "skills": "s", "gate": "g"}, timeout=5)
check("POST /api/roles", r.status_code == 200 and r.json().get("ok"), f"rid={RID}")
row = db_val("SELECT name FROM roles WHERE id=?", (RID,))
check("   └ 落库校验", row is not None and row[0] == "测试角色", f"db={row}")
T.append(("roles", RID))

# 6. 资源库 + 授权切换
r = requests.get(f"{BASE}/api/resources", timeout=5)
res = r.json()
check("GET /api/resources", r.status_code == 200 and len(res) >= 3, f"count={len(res)}")
RID_RES = "deepseek"
before = db_val("SELECT auth FROM resources WHERE id=?", (RID_RES,))
r = requests.post(f"{BASE}/api/resources/{RID_RES}/auth", timeout=5)
after = db_val("SELECT auth FROM resources WHERE id=?", (RID_RES,))
check("POST /api/resources/{id}/auth 切换", r.status_code == 200 and before and after and before[0] != after[0], f"{before[0]}->{after[0]}")
# 还原
requests.post(f"{BASE}/api/resources/{RID_RES}/auth", timeout=5)

# 7. 消息列表（p1 种子）
r = requests.get(f"{BASE}/api/messages/p1", timeout=5)
msgs = r.json()
check("GET /api/messages/{pid}", r.status_code == 200 and len(msgs) >= 6, f"p1 msgs={len(msgs)}")

# 8. 发消息落库
r = requests.post(f"{BASE}/api/messages", json={"project_id": PID, "sender": "你", "kind": "self", "text": "阶段2测试消息"}, timeout=5)
row = db_val("SELECT text FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 1", (PID,))
check("POST /api/messages 落库", r.status_code == 200 and row and "阶段2测试消息" in row[0], f"db={row}")

# 9. 元神资料库
r = requests.get(f"{BASE}/api/meta/files", timeout=5)
files = r.json()
check("GET /api/meta/files", r.status_code == 200 and len(files) >= 2, f"count={len(files)}")
r = requests.post(f"{BASE}/api/meta/files", json={"name": "phase2测试资料.txt"}, timeout=5)
row = db_val("SELECT name FROM meta_files WHERE name=? ORDER BY id DESC LIMIT 1", ("phase2测试资料.txt",))
check("POST /api/meta/files 落库", r.status_code == 200 and row is not None, f"db={row}")
T.append(("meta_files_del", "phase2测试资料.txt"))

# 10. 元神对话（降级路径验证）
r = requests.post(f"{BASE}/api/meta/chat", json={"text": "阶段2回归测试：请确认你的角色边界"}, timeout=35)
j = r.json()
reply = j.get("reply", "")
degraded = ("降级" in reply) or ("离线" in reply) or ("调用" in reply and "失败" in reply)
check("POST /api/meta/chat 接口可用", r.status_code == 200 and j.get("ok"), f"reply_len={len(reply)}")
check("   └ LLM 降级/联网二态正常", degraded or len(reply) > 0, f"reply_head={reply[:30]!r}")
row = db_val("SELECT COUNT(*) FROM messages WHERE project_id='__meta__' AND text=?", ("阶段2回归测试：请确认你的角色边界",))
check("   └ 用户消息落库", row is not None and row[0] >= 1, f"count={row[0] if row else 0}")

# ---- 清理测试数据 ----
for tbl, key in T:
    if tbl == "meta_files_del":
        conn = sqlite3.connect(DB); conn.execute("DELETE FROM meta_files WHERE name=?", (key,)); conn.commit(); conn.close()
    else:
        conn = sqlite3.connect(DB); conn.execute(f"DELETE FROM {tbl} WHERE id=?", (key,)); conn.commit(); conn.close()
# 清理元神测试消息（幂等）
conn = sqlite3.connect(DB)
conn.execute("DELETE FROM messages WHERE project_id='__meta__' AND text=?", ("阶段2回归测试：请确认你的角色边界",))
conn.commit(); conn.close()
print("（测试数据已自清理）\n")

# ---- 汇总 ----
passed = sum(1 for _, s, _ in results if s == "PASS")
failed = sum(1 for _, s, _ in results if s == "FAIL")
print(f"{'结果':<6}{'用例':<42}{'详情'}")
print("-" * 80)
for name, status, detail in results:
    print(f"{status:<6}{name:<42}{detail}")
print("-" * 80)
print(f"总计 {len(results)} 项 | PASS {passed} | FAIL {failed}")
print("RESULT=" + ("ALL_PASS" if failed == 0 else "HAS_FAIL"))
