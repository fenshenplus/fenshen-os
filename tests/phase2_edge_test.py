"""
分身 v1 阶段2 — 边界 / 错误态测试
验证：非法输入、空体、重复、超长文本等异常下系统不崩溃、数据完整。
"""
import os
import sqlite3

import requests

BASE = "http://127.0.0.1:8002"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fenshen.db")
results = []


def check(n, c, d=""):
    results.append((n, "PASS" if c else "FAIL", d))


def db_val(sql, args=()):
    conn = sqlite3.connect(DB)
    row = conn.execute(sql, args).fetchone()
    conn.close()
    return row


def db_exec(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


print("== 分身 v1 阶段2 边界/错误态测试 ==\n")

# E1 非法项目ID查消息 → 应返回空列表，不崩溃
r = requests.get(f"{BASE}/api/messages/nope_xyz_999", timeout=5)
ok1 = r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) == 0
check("E1 非法pid消息查询→空列表", ok1, f"code={r.status_code} len={len(r.json()) if r.status_code==200 else '?'}")

# E2 空文本元神chat → 不崩溃，降级/处理正常
r = requests.post(f"{BASE}/api/meta/chat", json={"text": ""}, timeout=35)
j = r.json()
check("E2 空文本元神chat→200", r.status_code == 200 and j.get("ok"), f"reply_len={len(j.get('reply',''))}")
db_exec("DELETE FROM messages WHERE project_id='__meta__' AND text=''")

# E3 空体创建项目 → 允许（空名），不崩溃
r = requests.post(f"{BASE}/api/projects", json={}, timeout=5)
pid = r.json().get("id")
check("E3 空体创建项目→200", r.status_code == 200 and pid, f"pid={pid}")
db_exec("DELETE FROM projects WHERE id=?", (pid,))

# E4 重复ID项目 → INSERT OR REPLACE 更新，无冲突
requests.post(f"{BASE}/api/projects", json={"id": "dup_test", "name": "A"}, timeout=5)
requests.post(f"{BASE}/api/projects", json={"id": "dup_test", "name": "B"}, timeout=5)
row = db_val("SELECT name FROM projects WHERE id='dup_test'")
check("E4 重复ID更新无冲突", row is not None and row[0] == "B", f"name={row[0] if row else None}")
db_exec("DELETE FROM projects WHERE id='dup_test'")

# E5 资源授权切换双击还原
before = db_val("SELECT auth FROM resources WHERE id='deepseek'")[0]
requests.post(f"{BASE}/api/resources/deepseek/auth", timeout=5)
mid = db_val("SELECT auth FROM resources WHERE id='deepseek'")[0]
requests.post(f"{BASE}/api/resources/deepseek/auth", timeout=5)
after = db_val("SELECT auth FROM resources WHERE id='deepseek'")[0]
check("E5 资源授权双击还原", before == after and before != mid, f"{before}->{mid}->{after}")

# E6 超长文本元神chat → 不崩溃
long_text = "压力测试" * 300
r = requests.post(f"{BASE}/api/meta/chat", json={"text": long_text}, timeout=35)
j = r.json()
check("E6 超长文本元神chat→200", r.status_code == 200 and j.get("ok"), f"reply_len={len(j.get('reply',''))}")
db_exec("DELETE FROM messages WHERE project_id='__meta__' AND text=?", (long_text,))

# ---- 汇总 ----
passed = sum(1 for _, s, _ in results if s == "PASS")
failed = sum(1 for _, s, _ in results if s == "FAIL")
print(f"{'结果':<6}{'用例':<34}{'详情'}")
print("-" * 70)
for n, s, d in results:
    print(f"{s:<6}{n:<34}{d}")
print("-" * 70)
print(f"边界测试 总计 {len(results)} | PASS {passed} | FAIL {failed}")
print("EDGE=" + ("ALL_PASS" if failed == 0 else "HAS_FAIL"))
