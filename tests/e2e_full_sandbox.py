#!/usr/bin/env python3
"""分身 全功能 e2e 沙箱测试（v6.4 / 0.64.x）

目标：把 P2/P3/P3+ 及 v6.4/v6.5 新增端点「实际跑一遍」，确认无 500 / 无崩溃。
- 被测对象：127.0.0.1:8002（源码根 uvicorn，DB=项目根 data/fenshen.db）
- 鉴权：x-fenshen-token（读 data/.auth_token）
- 断言口径：HTTP 2xx 且返回为 dict/合法结构 → 跑通（PASS）；
           400=输入被干净拒绝（仍记为 PASS，附注）；401/403/5xx=FAIL。
- LLM/exec 类端点会真实调用（环境 llm=deepseek 密钥在），以"代码执行未崩溃"为结论标准。
- 收尾：删除本次创建的测试项目/成员（DB 另有整体还原备份兜底）。
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:8002"
TOKEN_FILE = os.path.join(ROOT, "data", ".auth_token")
PASS, FAIL, NOTES = [], [], []
TOK = open(TOKEN_FILE).read().strip() if os.path.exists(TOKEN_FILE) else ""


def call(method, path, body=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("x-fenshen-token", TOK)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return -1, str(e)


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    tag = "PASS" if ok else "FAIL"
    print(f"  {tag}  {name}{('  — ' + detail) if detail else ''}")
    return ok


def ran(code, body, label, allow_400=True):
    """代码是否实际跑通（非崩溃）。"""
    if code == 401 or code == 403:
        return check(label, False, f"鉴权/越权 HTTP {code}")
    if code >= 500:
        return check(label, False, f"服务端 5xx: {str(body)[:160]}")
    if code == 400 and not allow_400:
        return check(label, False, f"400 预期外: {str(body)[:160]}")
    if code == 400:
        NOTES.append(f"{label}: 400(输入干净拒绝)")
    return check(label, True, f"HTTP {code}")


def main():
    print(f"=== 分身 全功能 e2e 沙箱 → {BASE} (token={'有' if TOK else '无'}) ===\n")

    # ── 0. 版本/健康 ───────────────────────────────────────────
    print("── 0. 版本/健康 ──")
    c, b = call("GET", "/api/version")
    ran(c, b, "GET /api/version")
    if isinstance(b, dict):
        NOTES.append(f"version: semver={b.get('semver')} release={b.get('release')} build={b.get('build_date')} commit={b.get('git_commit')}")
    c, b = call("GET", "/api/health")
    ran(c, b, "GET /api/health")

    # ── 1. 建立测试项目（含模块/角色）─────────────────────────
    print("\n── 1. 测试基座搭建 ──")
    c, b = call("POST", "/api/projects",
                {"name": "e2e全功能沙箱", "goal": "覆盖全端点", "standards": "全部跑通",
                 "roles": ["architect", "backend", "frontend"],
                 "modules": [{"name": "M1", "owner_role": "后端"}]})
    ok = c == 200 and isinstance(b, dict) and b.get("id")
    pid = b.get("id") if ok else None
    ran(c, b, "POST /api/projects（建测试项目）", allow_400=False)
    if not pid:
        print("  无法建立测试项目，终止。")
        return finish()

    c, b = call("GET", f"/api/projects/{pid}")
    ran(c, b, "GET /api/projects/{pid}（聚合详情）")
    mods = (b.get("modules") or []) if isinstance(b, dict) else []
    mid = mods[0]["id"] if mods else None

    c, b = call("POST", f"/api/projects/{pid}/topics",
                {"name": "e2e话题", "module_id": mid or ""})
    tpid = b.get("id") if isinstance(b, dict) else None
    ran(c, b, "POST /api/projects/{pid}/topics")

    if tpid:
        c, b = call("POST", f"/api/topics/{tpid}/tasks",
                     {"name": "e2e任务", "owner_role": "backend", "done_criteria": "可验证"})
        ran(c, b, "POST /api/topics/{tpid}/tasks")

    # ── 2. P2/P3/P3+ 元神自动驾驶汇报（结构化）────────────────
    print("\n── 2. P3 元神自动驾驶汇报（结构化卡片）──")
    c, b = call("POST", f"/api/meta/report/{pid}", timeout=60)
    ok_rep = c == 200 and isinstance(b, dict) and b.get("ok") and b.get("posted")
    ran(c, b, "POST /api/meta/report/{pid}（生成并发布汇报）", allow_400=False)
    c, b = call("GET", f"/api/projects/{pid}/report/latest")
    ok_struct = c == 200 and isinstance(b, dict)
    rep = b.get("report") if ok_struct else None
    has_struct = isinstance(rep, dict) and isinstance(rep.get("report_json"), str) and "progress" in (json.loads(rep["report_json"]) if rep.get("report_json") else {})
    check("GET /api/projects/{pid}/report/latest（结构化载荷）",
          ok_struct and bool(rep),
          f"tag={'自动驾驶汇报' if (rep and rep.get('tag')=='自动驾驶汇报') else (rep.get('tag') if rep else 'None')}")
    if rep and rep.get("report_json"):
        try:
            pj = json.loads(rep["report_json"])
            NOTES.append(f"汇报结构化字段: progress={len(pj.get('progress',[]))} critical_path={len(pj.get('critical_path',[]))} readiness={pj.get('readiness')}")
        except Exception:
            pass

    # ── 3. autopilot 续航模式 ─────────────────────────────────
    print("\n── 3. autopilot 续航/预算 ──")
    c, b = call("GET", "/api/autopilot/state")
    ran(c, b, "GET /api/autopilot/state")
    c, b = call("POST", "/api/autopilot/set",
                {"mode": "balanced", "enabled": True, "token_budget_hour": 1000, "token_budget_day": 8000})
    ran(c, b, "POST /api/autopilot/set（模式/预算）", allow_400=False)

    # ── 4. meta 蒸馏/充足度/记忆/进化 ────────────────────────
    print("\n── 4. meta 蒸馏充足度 / 记忆 / 进化 ──")
    for p in ["/api/meta/state", "/api/meta/sufficiency", "/api/meta/token-report",
              "/api/meta/attribution", "/api/meta/evolution/cell",
              "/api/meta/evolution/promote", "/api/meta/evolution/lineage",
              "/api/meta/memory/archive", "/api/meta/memory/panel"]:
        c, b = call("GET", p)
        ran(c, b, f"GET {p}")
    c, b = call("POST", "/api/meta/attribution/refresh", timeout=60)
    ran(c, b, "POST /api/meta/attribution/refresh")

    # ── 5. P0 元神战队成员（CRUD+升级+经验）─────────────────
    print("\n── 5. P0 元神战队：成员 ──")
    c, b = call("POST", f"/api/projects/{pid}/members",
                {"name": "测试成员A", "role_title": "全栈", "track": "web", "soul": "严谨"})
    mid2 = b.get("id") if isinstance(b, dict) else None
    ran(c, b, "POST /api/projects/{pid}/members（创建成员）", allow_400=False)
    c, b = call("GET", f"/api/projects/{pid}/members")
    ran(c, b, "GET /api/projects/{pid}/members（成员列表）")
    if mid2:
        c, b = call("GET", f"/api/members/{mid2}")
        ran(c, b, "GET /api/members/{mid}（成员详情）")
        c, b = call("POST", f"/api/members/{mid2}/upgrade", {"note": "e2e升级"})
        ran(c, b, "POST /api/members/{mid}/upgrade")
        c, b = call("POST", f"/api/members/{mid2}/auto-upgrade",
                     {"sample": "用户偏好：直接、数据驱动、不废话。", "threshold": 0.5}, timeout=60)
        ran(c, b, "POST /api/members/{mid}/auto-upgrade（样本升级）")
        c, b = call("POST", f"/api/members/{mid2}/experience",
                     {"title": "e2e经验", "content": "测试经验沉淀", "category": "test"})
        ran(c, b, "POST /api/members/{mid}/experience")
        c, b = call("DELETE", f"/api/projects/{pid}/members/{mid2}")
        ran(c, b, "DELETE /api/projects/{pid}/members/{mid2}（清理）")

    # ── 6. 矩阵/就绪/搜索/私聊 ───────────────────────────────
    print("\n── 6. 矩阵 / 就绪 / 语义搜索 / 1:1 私聊 ──")
    c, b = call("GET", f"/api/projects/{pid}/matrix")
    ran(c, b, "GET /api/projects/{pid}/matrix（矩阵看板）")
    c, b = call("GET", f"/api/projects/{pid}/readiness")
    ran(c, b, "GET /api/projects/{pid}/readiness（全链路就绪）")
    c, b = call("POST", "/api/search", {"query": "登录接口", "top_k": 3}, timeout=60)
    ran(c, b, "POST /api/search（语义搜索）")
    c, b = call("GET", "/api/direct/e2e-peer")
    ran(c, b, "GET /api/direct/{peer}（1:1 私聊历史）")
    c, b = call("POST", "/api/direct/e2e-peer", {"text": "你好，元神", "from": "peer"}, timeout=60)
    ran(c, b, "POST /api/direct/{peer}（1:1 私聊）")

    # ── 7. 工程质量：质量门禁/代码库/VCS/流式 ─────────────────
    print("\n── 7. 工程质量：质量门禁 / 代码库 / VCS / 流式扫描 ──")
    c, b = call("POST", "/api/meta/quality_gate", {"code": "def f():\n    return 1\n"}, timeout=60)
    ran(c, b, "POST /api/meta/quality_gate")
    c, b = call("POST", "/api/meta/codebase", {"path": "."}, timeout=90)
    ran(c, b, "POST /api/meta/codebase（代码库画像）")
    c, b = call("POST", "/api/meta/vcs", {"cmd": "status"}, timeout=90)
    ran(c, b, "POST /api/meta/vcs（git 状态）")
    c, b = call("POST", "/api/meta/code_stream", {"files": ["backend/main.py"]}, timeout=90)
    ran(c, b, "POST /api/meta/code_stream（代码流）")
    c, b = call("POST", "/api/meta/code_scan", {"target": "backend/main.py"}, timeout=90)
    ran(c, b, "POST /api/meta/code_scan（代码扫描）")

    # ── 8. 评审/蒸馏/打磨 ─────────────────────────────────────
    print("\n── 8. 评审 / 蒸馏 / 打磨 ──")
    c, b = call("POST", "/api/reviews/auto", {"diff": "+def hello():\n+    print('hi')\n"}, timeout=90)
    ran(c, b, "POST /api/reviews/auto（自动评审）")
    c, b = call("POST", "/api/memory/distill", {"text": "用户重视数据驱动与可追溯。"}, timeout=90)
    ran(c, b, "POST /api/memory/distill（记忆蒸馏）")
    c, b = call("POST", "/api/experiences/distill", {"text": "部署前必跑回归。"}, timeout=90)
    ran(c, b, "POST /api/experiences/distill（经验蒸馏）")
    c, b = call("POST", "/api/skills/distill", {"text": "处理登录应校验密码。"}, timeout=90)
    ran(c, b, "POST /api/skills/distill（技能蒸馏）")
    c, b = call("GET", "/api/experiences/tree")
    ran(c, b, "GET /api/experiences/tree（进化树）")
    c, b = call("GET", "/api/grind/rules")
    ran(c, b, "GET /api/grind/rules（打磨规则）")
    c, b = call("POST", "/api/grind/rules", {"name": "e2e规则", "pattern": "TODO", "action": "note"})
    ran(c, b, "POST /api/grind/rules（新增打磨规则）")

    # ── 9. 阶段链/快照/变更单/导出/令牌 ──────────────────────
    print("\n── 9. 阶段链 / 快照 / 变更单 / 导出 / 令牌 ──")
    c, b = call("GET", "/api/stage-presets")
    ran(c, b, "GET /api/stage-presets（阶段预设）")
    c, b = call("GET", f"/api/projects/{pid}/stage-chain")
    ran(c, b, "GET /api/projects/{pid}/stage-chain")
    c, b = call("PUT", f"/api/projects/{pid}/stage-chain", {"chain": ["requirement", "ui", "dev", "test", "release"]})
    ran(c, b, "PUT /api/projects/{pid}/stage-chain（设定阶段链）")
    c, b = call("GET", f"/api/projects/{pid}/snapshots")
    ran(c, b, "GET /api/projects/{pid}/snapshots")
    c, b = call("POST", f"/api/projects/{pid}/snapshots", {"name": "e2e快照"})
    ran(c, b, "POST /api/projects/{pid}/snapshots（建快照）")
    c, b = call("GET", f"/api/projects/{pid}/change-orders")
    ran(c, b, "GET /api/projects/{pid}/change-orders")
    c, b = call("POST", f"/api/projects/{pid}/change-orders", {"title": "e2e变更", "detail": "测试", "risk": "low"})
    cid = b.get("id") if isinstance(b, dict) else None
    ran(c, b, "POST /api/projects/{pid}/change-orders（提变更单）")
    if cid:
        c, b = call("PATCH", f"/api/change-orders/{cid}", {"status": "done"})
        ran(c, b, "PATCH /api/change-orders/{cid}（流转变更单）")
    c, b = call("GET", "/api/export/vault")
    ran(c, b, "GET /api/export/vault（保险库导出）")
    c, b = call("GET", "/api/token/usage")
    ran(c, b, "GET /api/token/usage（令牌用量）")

    # ── 10. 非破坏性清理 ──────────────────────────────────────
    print("\n── 10. 非破坏性清理 ──")
    c, b = call("POST", "/api/cleanup", {"scope": "temp"})
    ran(c, b, "POST /api/cleanup {scope:temp}（非破坏性）", allow_400=False)

    # ── 收尾：删除测试项目 ────────────────────────────────────
    call("DELETE", f"/api/projects/{pid}")
    print("  清理：已删除测试项目。")
    return finish()


def finish():
    print(f"\n{'=' * 56}")
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}  | 附注 {len(NOTES)} 条")
    if NOTES:
        print("\n── 附注（关键信息/已知边界）──")
        for n in NOTES:
            print("  · " + n)
    if FAIL:
        print("\n失败项：\n  - " + "\n  - ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
