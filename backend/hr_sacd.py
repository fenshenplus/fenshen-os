"""HR 分级 intake + SACD 串行编排运行时（P2 整体工作完成能力）。

借鉴 ~/Desktop/元神/WORKFLOW.md 的 SACD 协议与 HR 分级 intake：
- HR：把用户的原始请求分级（P0 你亲自盯 / P1 PM 日报 / P2 PM 全权）+ 拆解 +
      技能匹配 + 生成 Agent Profile；
- PM / SACD：串行队列，逐步骤配精确技能 → 派单执行（复用 _run_dispatch_job）→
      归档验证 → 进入下一步，绝不并行扇出。

设计约束（与元神架构咬合）：
- 纯编排层，不新增人格；所有执行仍走既有派单链路，保证审计/权限一致。
- LLM 不可用时有规则兜底，绝不静默丢信号。
"""
import json
import uuid
import re
import asyncio
from datetime import datetime

# ── 内存态：SACD 运行记录（演示级；生产可落 meta_home/data/sacd_runs.json）──
SACD_RUNS = {}

PRIORITY_LABEL = {
    "P0": "你要亲自盯（最高优先级，元神只调度不代决）",
    "P1": "PM 日报跟进（元神推进，关键节点向你汇报）",
    "P2": "PM 全权处理（元神自主推进到验收）",
}


def _rule_priority(text: str) -> str:
    """无 LLM 时的规则分级兜底。"""
    t = text or ""
    if re.search(r"紧急|立刻|马上|线上|故障|宕机|事故|封禁|资损|立刻|立即", t):
        return "P0"
    if re.search(r"计划|下周|有空|不急|之后|排期|规划", t):
        return "P2"
    return "P1"


def _llm_classify(text: str) -> dict:
    """调用 LLM 做 HR 分级 + 拆解；失败返回规则兜底。"""
    from backend.main import call_llm
    system = (
        "你是分身产品的 HR 调度官，负责把用户的原始请求变成可执行的编排计划。请严格输出 JSON：\n"
        "{\"priority\":\"P0|P1|P2\","
        "\"summary\":\"一句话概括\","
        "\"steps\":[{\"title\":\"步骤名\",\"role\":\"后端|前端|测试|设计|产品|运维\",\"skill\":\"建议技能名(无则空)\",\"done_criteria\":\"完成标准\"}],"
        "\"agent_profile\":\"一句话描述执行该任务所需的角色/能力配置\"}"
    )
    try:
        out = call_llm("__meta__", [{"role": "user", "content": text}], system_prompt=system)
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    # 规则兜底：单步，默认后端
    return {
        "priority": _rule_priority(text),
        "summary": (text or "")[:60],
        "steps": [{"title": (text or "")[:40], "role": "后端", "skill": "", "done_criteria": "完成"}],
        "agent_profile": "通用执行角色（无 LLM 时的规则兜底）",
    }


async def hr_grade(text: str) -> dict:
    """HR 分级 intake：返回结构化编排计划。"""
    data = _llm_classify(text)
    data.setdefault("priority", _rule_priority(text))
    data.setdefault("summary", (text or "")[:60])
    data.setdefault("steps", [{"title": (text or "")[:40], "role": "后端", "skill": "", "done_criteria": "完成"}])
    data.setdefault("agent_profile", "通用执行角色")
    data["priority_label"] = PRIORITY_LABEL.get(data["priority"], "")
    # 清洗 steps 字段
    clean = []
    for s in data.get("steps", []) or []:
        if not isinstance(s, dict):
            continue
        clean.append({
            "title": (s.get("title") or "").strip() or "未命名步骤",
            "role": (s.get("role") or "后端").strip(),
            "skill": (s.get("skill") or "").strip(),
            "done_criteria": (s.get("done_criteria") or "完成").strip(),
        })
    data["steps"] = clean
    return data


async def sacd_run(run_id: str, project_id: str, steps: list, owner_default: str = "后端"):
    """SACD 串行编排：逐步骤派单执行（复用 _run_dispatch_job），写回运行记录。

    串行、确定性、可审计；任一步异常不阻断后续（记录失败继续），符合「把活干完」语义。
    """
    from backend.main import _run_dispatch_job, get_db
    SACD_RUNS[run_id] = {"status": "running", "total": len(steps), "done": 0, "results": []}
    for i, s in enumerate(steps):
        role = s.get("role") or owner_default
        dc = s.get("done_criteria") or "完成"
        title = s.get("title") or f"步骤{i+1}"
        prompt = f"【SACD 编排·第{i+1}步/{len(steps)}】{title}\n完成标准：{dc}\n请作为{role}执行这一步，产出可见成果，不要等待用户确认。"
        job_id = f"sacd{uuid.uuid4().hex[:10]}"
        now = datetime.now().isoformat()
        conn = get_db()
        conn.execute(
            "INSERT INTO dispatch_jobs (id,project_id,status,progress,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (job_id, project_id, "queued", "SACD 派单中…", now, now),
        )
        conn.commit()
        conn.close()
        ok = False
        result = ""
        try:
            await _run_dispatch_job(job_id, project_id, prompt, target=role)
            ok = True
        except Exception as e:
            result = f"[SACD 派单异常] {e}"
        if ok:
            conn = get_db()
            row = conn.execute("SELECT result FROM dispatch_jobs WHERE id=?", (job_id,)).fetchone()
            conn.close()
            result = (row["result"] if row else "")[:800]
        SACD_RUNS[run_id]["results"].append({"step": i + 1, "title": title, "ok": ok, "result": result})
        SACD_RUNS[run_id]["done"] = i + 1
    SACD_RUNS[run_id]["status"] = "done"


def get_sacd_run(run_id: str):
    return SACD_RUNS.get(run_id)
