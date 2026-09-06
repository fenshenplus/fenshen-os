"""分身 Goal-Mode（Ralph loop）引擎。

单张看板卡开启 Goal-Mode 后，在该卡自己的会话上下文里自主迭代：
  worker = 元神调度 owner_role 执行一步（复用 _run_dispatch_job 派单链路）
  judge  = 验收（native gate 优先 → 轻量 LLM 兜底）

边界铁律（与分身架构咬合）：
- Goal-Mode 永不自动建卡、永不扇出群聊、worker ≠ judge；
- 多任务排程仍归看板 / 元神调度；
- 超 max_turns 预算一律标记 failed 并通知，绝不静默自标 done（元神宪法护栏）。

延迟导入 backend.main，避免循环依赖（与 meta_distill.py 同构）。
"""
import json
import asyncio
import uuid
import re
import time
from datetime import datetime

# 命令类质量门的工作目录白名单根（与 native/verify._ALLOWED_CWD_PREFIXES 对齐）
import os as _os
_REPO_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), ".."))


def _parse_json(s, default):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else default
    except Exception:
        return default


def build_contract(task: dict) -> dict:
    """把一张卡的完成契约聚合成统一结构（复用已有 done_criteria）。"""
    return {
        "done_criteria": (task.get("done_criteria") or "").strip(),
        "subgoals": _parse_json(task.get("goal_subgoals"), []),
        "gates": _parse_json(task.get("goal_gates"), []),
    }


# ── 状态读写（纯增量，不碰既有 status/stage/track）──
def _set_goal_status(task_id: str, status: str):
    from backend.main import get_db
    conn = get_db()
    conn.execute("UPDATE tasks SET goal_status=? WHERE id=?", (status, task_id))
    conn.commit()
    conn.close()


def _set_task_done(task_id: str):
    from backend.main import get_db
    conn = get_db()
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


def _is_active(task_id: str) -> bool:
    from backend.main import get_db
    conn = get_db()
    row = conn.execute("SELECT goal_mode, goal_status FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return False
    return row["goal_mode"] == 1 and row["goal_status"] in ("running", "")


def _log_run(task_id: str, turn: int, worker_out: str, verdict: str, reason: str):
    from backend.main import get_db
    conn = get_db()
    conn.execute(
        "INSERT INTO goal_runs (id,task_id,turn,worker_out,verdict,reason,created_at) VALUES (?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, task_id, turn, (worker_out or "")[:4000], verdict, reason, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


# ── worker：派发一步到该卡 owner_role，返回产物摘要 ──
async def run_worker_step(task: dict, gap: str) -> str:
    from backend.main import get_db, _run_dispatch_job
    pid = task.get("project_id")
    owner = task.get("owner_role") or "后端"
    name = task.get("name") or "任务"
    dc = task.get("done_criteria") or ""
    prompt = f"【Goal-Mode 自主推进】任务：{name}\n完成标准：{dc}\n"
    if gap:
        prompt += f"\n上一轮验收未通过，请重点修复以下 gap：\n{gap}\n"
    prompt += "\n请作为负责角色只执行一步推进，产出可见成果，不要等待用户确认。"
    job_id = f"goal{time.time_ns()}"
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO dispatch_jobs (id,project_id,status,progress,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (job_id, pid, "queued", "Goal-Mode 派单中…", now, now),
    )
    conn.commit()
    conn.close()
    try:
        await _run_dispatch_job(job_id, pid, prompt, target=owner)
    except Exception as e:
        return f"[worker 异常] {e}"
    conn = get_db()
    row = conn.execute("SELECT result FROM dispatch_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    result = row["result"] if row else ""
    try:
        rj = json.loads(result)
        return json.dumps(rj, ensure_ascii=False)[:1500]
    except Exception:
        return (result or "")[:1500]


# ── 原生校验调度（spec 形如 `tool:arg`）──
def _run_native_check(spec: str, task: dict):
    from backend.main import get_db
    from backend.native import verify
    if ":" not in spec:
        return {"ok": False, "msg": f"无法解析原生校验: {spec}"}
    tool, arg = spec.split(":", 1)
    tool, arg = tool.strip(), arg.strip()
    if tool == "file_exists":
        return verify.file_exists(None, arg)
    if tool == "page_reachable":
        return verify.page_reachable(None, arg)
    if tool == "build_ok":
        return verify.build_ok(None, arg)
    if tool == "db_row_count":
        parts = arg.rsplit(":", 1)
        table = parts[0]
        expect = int(parts[1]) if len(parts) > 1 else 1
        conn = get_db()
        try:
            return verify.db_row_count(conn, table, expect)
        finally:
            conn.close()
    return {"ok": False, "msg": f"未知原生校验: {tool}"}


# ── 轻量 LLM 裁判（主观 / 自由验收项）──
async def _llm_judge(criterion: str, worker_out: str, task: dict) -> bool:
    from backend.main import call_llm
    system = (
        "你是分身的独立验收裁判。你只根据「验收标准 + 当前产物」严格客观地判断该标准是否达成；"
        "你不与执行者同一身份，禁止自我满意。只输出 JSON：{\"pass\": true|false, \"reason\": \"...\"}"
    )
    user = f"验收标准：{criterion}\n\n当前产物摘要：\n{worker_out[:3000]}\n\n该标准是否达成？"
    try:
        text = call_llm("__meta__", [{"role": "user", "content": user}], system_prompt=system)
    except Exception:
        return False
    m = re.search(r'\{\s*"pass"\s*:\s*(true|false)', text, re.I)
    if not m:
        return False  # 解析失败保守判不过（不误杀由人工复核）
    return m.group(1).lower() == "true"


# ── judge：native gate 优先 → 轻量 LLM 兜底 ──
async def judge(task: dict, worker_out: str, contract: dict):
    from backend.native import verify
    # 1) 质量门（硬性）：native gate 优先，不烧 LLM
    for g in contract["gates"]:
        kind = (g.get("kind") or "").lower()
        spec = g.get("spec", "")
        if kind == "cmd":
            r = verify.test_passed(None, spec, cwd=_REPO_ROOT)
            if not r["ok"]:
                return "fail", f"质量门未过: {spec} -> {r['msg']}"
        elif kind == "native":
            r = _run_native_check(spec, task)
            if not r["ok"]:
                return "fail", f"原生校验未过: {r['msg']}"
        elif kind == "page":
            r = verify.page_reachable(None, spec)
            if not r["ok"]:
                return "fail", f"页面可达性未过: {spec} -> {r['msg']}"
    # 2) 完成契约 subgoals
    for s in contract["subgoals"]:
        check = (s.get("check") or "").lower()
        text = s.get("text", "")
        if check.startswith("native:"):
            r = _run_native_check(check[7:], task)
            if not r["ok"]:
                return "fail", f"验收项未过: {text} ({r['msg']})"
        elif check.startswith("cmd:"):
            r = verify.test_passed(None, check[4:], cwd=_REPO_ROOT)
            if not r["ok"]:
                return "fail", f"验收项未过: {text} ({r['msg']})"
        else:
            # 主观项 / 自由验收（含空 check）：轻量 LLM 裁判
            ok = await _llm_judge(text or contract["done_criteria"], worker_out, task)
            if not ok:
                return "fail", f"验收项未过(裁判): {text or contract['done_criteria']}"
    # 3) 无 subgoals/gates 时的兜底：用 done_criteria 做 LLM 判定
    if not contract["subgoals"] and not contract["gates"] and contract["done_criteria"]:
        ok = await _llm_judge(contract["done_criteria"], worker_out, task)
        if not ok:
            return "fail", f"完成标准未达成: {contract['done_criteria']}"
    return "pass", "全部验收通过"


# ── 元神宪法·完成前独立裁判三问（与执行者不同身份，禁止自我满意）──
async def _independent_referee(task: dict, worker_out: str, contract: dict):
    """在标记任务「完成」之前，强制跑独立裁判三问（证据 / 同事满意 / 漏需求）。
    任一不过即判不通过，转人工复核。返回 (pass: bool, reason: str)。"""
    from backend.main import call_llm
    dc = contract.get("done_criteria") or ""
    subs = contract.get("subgoals") or []
    sub_text = "\n".join(f"- {s.get('text', '')}" for s in subs) or "（无子目标）"
    system = (
        "你是分身宪法的独立验收裁判，与执行者不是同一身份。在元神要把任务标记为「完成」之前，"
        "你必须严格拷问三问，任一不过即判不通过：\n"
        "1) 有可验证的客观证据吗（文件 / 测试结果 / 产出物）？\n"
        "2) 若同事交付此产物，你会满意接收吗？\n"
        "3) 是否遗漏了用户原始需求或完成标准中的任何一条？\n"
        "只输出 JSON：{\"pass\": true|false, \"reason\": \"一句话说明\"}"
    )
    user = (
        f"用户原始完成标准：\n{dc}\n\n子目标清单：\n{sub_text}\n\n"
        f"当前产物摘要：\n{worker_out[:3000]}\n\n请按三问严格判定。"
    )
    try:
        text = call_llm("__meta__", [{"role": "user", "content": user}], system_prompt=system)
    except Exception as e:
        return False, f"裁判调用失败（保守判不过，转人工复核）：{e}"
    m = re.search(r'\{\s*"pass"\s*:\s*(true|false)', text, re.I)
    if not m:
        return False, "裁判输出无法解析（保守判不过，转人工复核）"
    if m.group(1).lower() == "true":
        return True, "三问全过"
    rm = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
    return False, rm.group(1) if rm else "裁判判不过"


# ── 主循环 ──
async def run_goal_loop(task_id: str):
    from backend.main import get_db
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return
    task = dict(row)
    if not task.get("goal_mode"):
        return
    _set_goal_status(task_id, "running")
    contract = build_contract(task)
    max_turns = int(task.get("goal_max_turns") or 20)
    gap = ""
    turn = 0
    fail_total = 0          # 失败预算：累计失败上限
    last_gap = ""
    same_gap_streak = 0     # 失败预算：同一问题反复上限
    deadline = time.time() + 600  # 失败预算：单任务卡死 10 分钟上限
    while turn < max_turns:
        if not _is_active(task_id):
            return  # 被 pause / clear 中断
        if time.time() > deadline:
            _set_goal_status(task_id, "failed")
            _log_run(task_id, turn, "", "fail", "失败预算耗尽：单任务卡死超过 10 分钟仍未达标（元神宪法护栏）")
            return
        turn += 1
        worker_out = await run_worker_step(task, gap)
        verdict, reason = await judge(task, worker_out, contract)
        _log_run(task_id, turn, worker_out, verdict, reason)
        if verdict == "pass":
            # 元神宪法：标记完成前强制跑「独立裁判三问」质量门
            ok, r2 = await _independent_referee(task, worker_out, contract)
            if ok:
                _set_goal_status(task_id, "done")
                _set_task_done(task_id)
                return
            gap = "独立裁判未通过：" + r2
            fail_total += 1
        else:
            gap = reason
            fail_total += 1
        # 失败预算：单步同一问题反复 3 次 / 总失败 5 次 → 判失败，绝不静默自标 done
        if gap == last_gap:
            same_gap_streak += 1
        else:
            same_gap_streak = 1
            last_gap = gap
        if same_gap_streak >= 3:
            _set_goal_status(task_id, "failed")
            _log_run(task_id, turn, "", "fail", f"失败预算耗尽：同一问题反复 {same_gap_streak} 次未解（{gap}）")
            return
        if fail_total >= 5:
            _set_goal_status(task_id, "failed")
            _log_run(task_id, turn, "", "fail", f"失败预算耗尽：累计 {fail_total} 次未达标（元神宪法护栏）")
            return
    # 超 max_turns：按元神宪法，失败并通知，绝不静默自标 done
    _set_goal_status(task_id, "failed")


def start_goal(task_id: str):
    """开启 Goal-Mode 并拉起循环（后台任务，不阻塞调用方）。"""
    from backend.main import get_db, asyncio as _a
    conn = get_db()
    conn.execute("UPDATE tasks SET goal_mode=1, goal_status='running' WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    _a.create_task(run_goal_loop(task_id))


__all__ = [
    "build_contract", "run_worker_step", "judge", "run_goal_loop",
    "start_goal", "_set_goal_status",
]
