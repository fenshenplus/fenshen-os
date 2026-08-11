"""
元神人格蒸馏引擎（v3.9 新增）
- 持续访谈：问题库覆盖 8 大维度、24+ 问题，冷启动 + 自适应追问 + 完成反思
- 真摄取：上传聊天记录/工作笔记 → LLM 抽取结构化事实 → 写入 user_model
- 动态 grounding：compile_meta_system() 把画像编译进元神 system prompt
- 被动蒸馏：元神每次闲聊，后台自动抽取人格/偏好事实
- 画像仪表盘：维度置信度 + 人格镜像，让用户直观看到"它像不像我"
"""
import asyncio
import json
import re
from datetime import datetime
from fastapi import Request

try:
    from backend.main import app, get_db, META_PID, META_SYSTEM, call_llm
except Exception:
    from main import app, get_db, META_PID, META_SYSTEM, call_llm

# ── 维度定义 ─────────────────────────────────────────────────────
META_DIMS = ["fact", "personality", "preference", "knowledge",
             "workflow", "relationship", "secret", "expectation"]
DIM_LABEL = {
    "fact": "【身份与事实】", "personality": "【人格与腔调】", "preference": "【偏好】",
    "knowledge": "【知识与擅长】", "workflow": "【工作流】", "relationship": "【关系定位】",
    "secret": "【私密（仅你与元神·本地加密）】", "expectation": "【对分身的期望】",
}

# ── 访谈问题库（持续行为：冷启动核心 + 自适应追问 + 完成反思）─────
# 注意：问题文本内的强调引号一律用中文全角引号「" "」，不能用直引号，否则破坏 Python 字符串。
QUESTION_BANK = [
    # fact 维度
    {"id": "fact_who", "dim": "fact", "essential": True,
     "q": "先用一句话介绍你自己：你是谁、在做什么事业？",
     "probes": ["你最被人记住的身份标签是什么？"]},
    {"id": "fact_role", "dim": "fact", "essential": True,
     "q": "你日常扮演哪些角色？（创业者 / 管理者 / 内容创作者 / 父亲…）各自大概占你多少精力？",
     "probes": ["哪个角色最耗你心力？哪个最让你有成就感？"]},
    {"id": "fact_team", "dim": "fact",
     "q": "你团队大概什么规模？你最依赖哪一类人？",
     "probes": ["招人时你最看重什么？"]},

    # personality 维度
    {"id": "per_tone", "dim": "personality", "essential": True,
     "q": "你平时说话什么腔调？直接干脆、还是喜欢铺垫？举一个你最近怼人或拒绝别人的例子。",
     "probes": ["别人常说你“太直”还是“太绕”？"]},
    {"id": "per_decision", "dim": "personality", "essential": True,
     "q": "做决定时你更靠直觉还是数据？重大决定一般会犹豫多久？",
     "probes": ["有没有“反直觉但后来证明你对了”的决定？"]},
    {"id": "per_risk", "dim": "personality",
     "q": "你对风险的容忍度？愿意为“可能的大收益”冒多大失败概率？",
     "probes": ["你亏过的最贵的一笔“学费”是什么？"]},
    {"id": "per_conflict", "dim": "personality",
     "q": "遇到分歧，你通常正面刚、迂回、还是冷处理？",
     "probes": ["什么情况下你会选择沉默？"]},

    # preference 维度
    {"id": "pref_work", "dim": "preference", "essential": True,
     "q": "你最讨厌的工作方式 / 会议 / 工具是什么？最享受的又是什么？",
     "probes": ["有没有一个工具你恨不得全公司都用？"]},
    {"id": "pref_comm", "dim": "preference", "essential": True,
     "q": "你希望别人（包括分身）用多简练的方式跟你沟通？长文还是要点？",
     "probes": ["一条消息多少字你会开始划走？"]},
    {"id": "pref_tool", "dim": "preference",
     "q": "你日常离不开哪几个软件 / 工具？",
     "probes": ["如果有天只能留 3 个 App，你留哪 3 个？"]},
    {"id": "pref_learn", "dim": "preference",
     "q": "你学新东西靠看文档、抄别人的、还是直接上手试错？",
     "probes": ["最近一次“现学现卖”是什么？"]},

    # knowledge 维度
    {"id": "know_domain", "dim": "knowledge", "essential": True,
     "q": "你最被认可的专业能力是什么？别人常找你请教哪类问题？",
     "probes": ["如果开一门课，你讲什么？"]},
    {"id": "know_weak", "dim": "knowledge",
     "q": "哪些领域你明确不擅长、需要分身补位？",
     "probes": ["哪类活你宁愿花钱也不自己干？"]},

    # workflow 维度
    {"id": "flow_plan", "dim": "workflow", "essential": True,
     "q": "你一般怎么启动一个大任务？先列计划、还是先干起来再说？",
     "probes": ["你列的“计划”通常活过几天？"]},
    {"id": "flow_collab", "dim": "workflow",
     "q": "你理想中的协作是什么样？你定方向别人执行，还是一起脑暴？",
     "probes": ["你最受不了哪种协作方式？"]},
    {"id": "flow_delegate", "dim": "workflow",
     "q": "你交给别人做的事，最在意交付的哪一点（速度 / 质量 / 省心）？",
     "probes": ["“省心”对你具体意味着什么？"]},

    # relationship 维度
    {"id": "rel_team", "dim": "relationship",
     "q": "你希望分身怎么对待你的团队成员 / 客户？什么语气、什么边界？",
     "probes": ["当着外人的面，分身该替你挡事还是交给你？"]},
    {"id": "rel_family", "dim": "relationship",
     "q": "工作之外，你和家人 / 朋友的关系里，有什么是分身不该越界的？",
     "probes": ["有没有“别让我老婆知道”的事？"]},

    # secret 维度（敏感，可选，本地加密）
    {"id": "secret_core", "dim": "secret", "essential": True,
     "q": "有没有一些事——连最亲密的人都不一定知道、但会影响你判断的？比如某个执念、恐惧、或长期目标。愿意说的话，元神会严格保密（本地加密，不上云）。",
     "probes": ["说一个你从不对任何人承认的“野心”？"]},
    {"id": "secret_goal", "dim": "secret",
     "q": "你心里有没有一个“绝不对外说、但驱动你所有行动”的长期目标？",
     "probes": ["如果十年后回头，你希望自己做到了什么？"]},

    # expectation 维度
    {"id": "exp_must", "dim": "expectation", "essential": True,
     "q": "你最希望分身帮你搞定的是什么事？列 3 件你一想就烦、但不得不做的。",
     "probes": ["哪件“不得不做”最占你心力？"]},
    {"id": "exp_bound", "dim": "expectation", "essential": True,
     "q": "分身绝对不能做的红线是什么？（比如不能替你答应别人、不能乱花钱、不能替你做最终决策）",
     "probes": ["如果分身越界了一次，你的容忍度是多少？"]},
    {"id": "exp_tone", "dim": "expectation",
     "q": "你希望分身在对外（对客户 / 合作方）时，是更像你本人、还是更克制专业？",
     "probes": ["什么场合你必须亲自出马、不能让分身代劳？"]},
    {"id": "exp_wow", "dim": "expectation",
     "q": "如果分身能做到一件让你“卧槽这也行”的事，你希望那是什么？",
     "probes": ["有没有一个“别人做不到但你能定义清楚”的期待？"]},
]

ESSENTIAL_IDS = [q["id"] for q in QUESTION_BANK if q.get("essential")]
QB_BY_ID = {q["id"]: q for q in QUESTION_BANK}

# ── LLM 抽取系统提示 ─────────────────────────────────────────────
EXTRACT_SYSTEM = """你是元神的"人格蒸馏器"。从用户提供的资料/回答中，抽取关于用户本人的结构化事实。
只输出 JSON 数组，每个元素: {"dim":<维度>,"field":<字段名 snake_case>,"value":<具体值，简短一句话>,"confidence":<0.4-0.95 浮点>}
维度只能是: fact(事实身份), personality(人格腔调), preference(偏好), knowledge(知识擅长), workflow(工作流), relationship(关系), secret(秘密), expectation(对分身的期望)。
field 用简短英文或拼音，如 communication_tone / risk_tolerance / red_line。
不要编造。没有可抽取内容时输出 []。"""


# ── 基础工具 ─────────────────────────────────────────────────────
def ask_llm_json(system: str, user: str):
    """调用 LLM 并尽量解析出 JSON（容错剥离 ```json 围栏）。

    注意：call_llm 在 deepseek/openai 分支会忽略 system_prompt 参数，
    只读取 history 中的 system 消息。因此这里必须把 system 作为 history 首条传入。
    """
    hist = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    raw = call_llm(META_PID, hist, system_prompt=system)
    if raw.startswith("[元神·"):
        return None  # 离线/降级
    txt = raw.strip()
    if "```" in txt:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", txt)
        if m:
            txt = m.group(1)
    try:
        return json.loads(txt)
    except Exception:
        s = txt.find("{") if "{" in txt else txt.find("[")
        e = txt.rfind("}") if "}" in txt else txt.rfind("]")
        if s >= 0 and e >= 0:
            try:
                return json.loads(txt[s:e + 1])
            except Exception:
                return None
    return None


def _decode(v):
    try:
        return json.loads(v)
    except Exception:
        return v


def _store_facts(facts, source: str, qid=None):
    """把抽取到的结构化事实 upsert 进 user_model（同 dim+field 取更高置信）。"""
    if not facts:
        return 0
    conn = get_db()
    n = 0
    now = datetime.now().isoformat()
    for f in facts:
        dim = f.get("dim")
        field = f.get("field")
        value = f.get("value")
        if dim not in META_DIMS or not field or value in (None, ""):
            continue
        conf = max(0.3, min(0.97, float(f.get("confidence", 0.5))))
        val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        ex = conn.execute("SELECT id,confidence FROM user_model WHERE dim=? AND field=?",
                          (dim, field)).fetchone()
        if ex:
            if conf > ex["confidence"]:
                conn.execute(
                    "UPDATE user_model SET value=?,confidence=?,source=?,qid=?,updated_at=? WHERE id=?",
                    (val_str, conf, source, qid, now, ex["id"]))
        else:
            conn.execute(
                "INSERT INTO user_model (dim,field,value,confidence,source,qid,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (dim, field, val_str, conf, source, qid, now, now))
        n += 1
    conn.commit()
    conn.close()
    return n


def _dim_confidence(conn):
    rows = conn.execute("SELECT dim, AVG(confidence) a FROM user_model GROUP BY dim").fetchall()
    return {r["dim"]: round(r["a"], 2) for r in rows}


def compile_meta_system() -> str:
    """把已蒸馏的用户画像动态编译进元神 system prompt。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT dim,field,value,confidence FROM user_model ORDER BY dim,confidence DESC").fetchall()
    conn.close()
    if not rows:
        return META_SYSTEM
    by_dim = {}
    for r in rows:
        by_dim.setdefault(r["dim"], []).append(r)
    blocks = []
    for dim, items in by_dim.items():
        lines = [DIM_LABEL.get(dim, f"【{dim}】")]
        for it in items[:6]:
            lines.append(f"- {it['field']}：{str(_decode(it['value']))}")
        blocks.append("\n".join(lines))
    persona = "\n\n".join(blocks)
    return (META_SYSTEM +
            "\n\n【以下是从与岳衡的互动/资料中蒸馏出的真实人格画像，对话时务必贴合，让元神“像他本人”】\n"
            + persona)


# ── 访谈引擎 ─────────────────────────────────────────────────────
def _build_reflection(conn):
    rows = conn.execute(
        "SELECT dim,field,value,confidence FROM user_model ORDER BY dim,confidence DESC").fetchall()
    if not rows:
        return None
    by_dim = {}
    for r in rows:
        by_dim.setdefault(r["dim"], []).append(r)
    label = {"fact": "你是", "personality": "你的行事风格", "preference": "你偏好",
             "knowledge": "你擅长", "workflow": "你工作的方式", "relationship": "在关系里你",
             "secret": "有些事只对元神说", "expectation": "你希望分身"}
    parts = []
    for dim, items in by_dim.items():
        vs = "；".join(str(_decode(it["value"])) for it in items[:4])
        parts.append(f"{label.get(dim, dim)}：{vs}")
    return ("根据目前和你的互动，元神的理解是——\n" + "\n".join(parts) +
            "\n\n如果哪里理解得不对，直接告诉元神“其实我是…”，它会立刻修正。"
            "你也可以在“画像”里看到每个维度的置信度，点一下就能让元神来补齐不了解你的地方。")


def _next_question():
    """返回下一个访谈问题（冷启动顺序 → 自适应补最弱维度 → 完成反思）。"""
    conn = get_db()
    st = conn.execute("SELECT * FROM meta_interview ORDER BY id DESC LIMIT 1").fetchone()
    asked = set(json.loads(st["asked"]) if st and st["asked"] else [])
    reflection = None
    phase = "coldstart"

    # 1) 冷启动：按必需问题顺序
    nxt = None
    for qid in ESSENTIAL_IDS:
        if qid not in asked:
            nxt = QB_BY_ID[qid]
            break

    # 2) 自适应：补最弱维度里未问过的问题
    if nxt is None:
        phase = "adaptive"
        dim_conf = _dim_confidence(conn)
        cands = [q for q in QUESTION_BANK if q["id"] not in asked]
        if cands:
            cands.sort(key=lambda q: dim_conf.get(q["dim"], 0.3))
            nxt = cands[0]
        else:
            # 3) 全部问完 → 反思镜像（超预期）
            reflection = _build_reflection(conn)
            phase = "complete"

    conn.close()
    progress = len(asked)
    if nxt:
        return {"question": nxt["q"], "qid": nxt["id"], "dim": nxt["dim"],
                "probes": nxt.get("probes", []), "progress": progress,
                "total": len(QUESTION_BANK), "phase": phase, "reflection": reflection}
    return {"question": None, "qid": None, "dim": None, "probes": [],
            "progress": progress, "total": len(QUESTION_BANK),
            "phase": "complete", "reflection": reflection}


async def _extract_async(system: str, user: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: ask_llm_json(system, user))


# ── API：访谈 ────────────────────────────────────────────────────
@app.get("/api/meta/interview/next")
def interview_next():
    return _next_question()


@app.post("/api/meta/interview/answer")
async def interview_answer(req: Request):
    data = await req.json()
    qid = data.get("qid")
    answer = (data.get("answer") or "").strip()
    if not qid or not answer:
        return {"ok": False, "error": "missing qid/answer"}
    conn = get_db()
    st = conn.execute("SELECT * FROM meta_interview ORDER BY id DESC LIMIT 1").fetchone()
    asked = json.loads(st["asked"]) if st and st["asked"] else []
    answers = json.loads(st["answers"]) if st and st["answers"] else {}
    if qid not in asked:
        asked.append(qid)
    answers[qid] = answer
    askedj = json.dumps(asked, ensure_ascii=False)
    ansj = json.dumps(answers, ensure_ascii=False)
    now = datetime.now().isoformat()
    if st:
        conn.execute(
            "UPDATE meta_interview SET asked=?,answers=?,last_ask_at=?,updated_at=? WHERE id=?",
            (askedj, ansj, now, now, st["id"]))
    else:
        conn.execute(
            "INSERT INTO meta_interview (asked,answers,last_ask_at,updated_at) VALUES (?,?,?,?)",
            (askedj, ansj, now, now))
    conn.commit()
    conn.close()
    # LLM 抽取人格事实（维度提示来自该题）
    q = QB_BY_ID.get(qid)
    dim_hint = q["dim"] if q else "personality"
    facts = await _extract_async(EXTRACT_SYSTEM + f"\n本次问题维度提示：{dim_hint}。",
                                 f"用户回答：{answer}")
    n = _store_facts(facts, "interview", qid) if facts else 0
    return {"ok": True, "stored": n, "next": _next_question()}


# ── API：上传资料真摄取 ──────────────────────────────────────────
@app.post("/api/meta/ingest")
async def meta_ingest(req: Request):
    data = await req.json()
    text = (data.get("text") or "").strip()
    filename = data.get("filename", "")
    source = data.get("source", "upload")
    if not text:
        return {"ok": False, "error": "empty text"}
    facts = await _extract_async(
        EXTRACT_SYSTEM + "\n这是用户上传的资料（可能是聊天记录/笔记/工作记录），尽量多抽取真实事实，不确定不要编。",
        f"文件名：{filename}\n内容：\n{text[:8000]}")
    n = _store_facts(facts, "upload") if facts else 0
    if n:
        conn = get_db()
        conn.execute(
            "INSERT INTO long_term_memory (category,content,source,ts) VALUES (?,?,?,?)",
            ("distilled", f"从资料《{filename}》蒸馏出 {n} 条人格事实", source, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    return {"ok": True, "extracted": n,
            "summary": (facts[:5] if facts else [])}


# ── API：画像仪表盘 ──────────────────────────────────────────────
@app.get("/api/meta/profile")
def meta_profile():
    conn = get_db()
    rows = conn.execute(
        "SELECT dim,field,value,confidence,source FROM user_model ORDER BY dim,confidence DESC").fetchall()
    dim_conf = _dim_confidence(conn)
    reflection = _build_reflection(conn)
    # v4.2 蒸馏二期：充足度仪表盘——每维度条数 + 平均置信度 + 充足判定（≥3 条且 avg≥0.6）
    dim_suff = {}
    for dim in META_DIMS:
        dim_rows = [r for r in rows if r["dim"] == dim]
        if not dim_rows:
            dim_suff[dim] = {"count": 0, "avg": 0.0, "sufficient": False}
            continue
        avg = round(sum(r["confidence"] for r in dim_rows) / len(dim_rows), 2)
        dim_suff[dim] = {"count": len(dim_rows), "avg": avg,
                         "sufficient": len(dim_rows) >= 3 and avg >= 0.6}
    conn.close()
    facts = [{"dim": r["dim"], "field": r["field"], "value": str(_decode(r["value"])),
              "confidence": r["confidence"], "source": r["source"]} for r in rows]
    suff_dims = [d for d, s in dim_suff.items() if s["sufficient"]]
    return {"narrative": reflection or "元神还在了解你，去“了解我”里回答几个问题，或上传你的聊天记录/工作笔记吧。",
            "facts": facts, "dim_confidence": dim_conf,
            "total": len(facts), "completeness": min(1.0, len(facts) / 20.0),
            "dim_sufficiency": dim_suff,
            "sufficient_dims": len(suff_dims), "total_dims": len(META_DIMS)}


# ── 蒸馏二期：镜像校验（mirror_verify）────────────────────────────
# 元神用画像"预测你在具体情境下的行为"，你反馈同意/纠正——同意强化置信度，
# 纠正写入画像（source='mirror'，高置信）。让画像"越用越像"（产品定义 v2 卖点一）。
MIRROR_SYSTEM = (
    "你是「镜像校验」引擎：基于用户的真实人格画像，预测他在具体情境下的行为/选择/风格。\n"
    "用户画像（按置信度排序）：\n{p}\n\n"
    "输出严格 JSON：{{\"items\": [{{\"scenario\": \"具体情境描述（含细节、逼真，第二人称\"你\"）\", "
    "\"prediction\": \"基于画像的预测：你会怎么做/怎么选/怎么说（第一人称\"我\"，具体到行动或原话风格）\"}}]}}\n"
    "要求：生成 {n} 个情境，优先覆盖画像中最有把握的维度（personality/preference/fact）；"
    "预测必须具体、可验证、不空泛。不要输出 JSON 以外的内容。"
)


@app.post("/api/meta/mirror/generate")
async def mirror_generate(req: Request):
    data = await req.json()
    n = int(data.get("count") or 3)
    conn = get_db()
    rows = conn.execute(
        "SELECT dim,field,value,confidence FROM user_model ORDER BY confidence DESC LIMIT 12").fetchall()
    conn.close()
    if not rows:
        return {"ok": False, "error": "画像还太空，先去「了解我」回答几个问题，再来镜像校验"}
    p = "\n".join(f"- [{r['dim']}] {r['field']}：{str(_decode(r['value']))}" for r in rows)
    system = MIRROR_SYSTEM.format(p=p, n=n)
    try:
        reply = await _extract_async(system, f"请生成 {n} 个镜像校验情境")
        j = json.loads(reply) if isinstance(reply, str) else reply
        items = j.get("items") or []
    except Exception as e:
        return {"ok": False, "error": f"镜像生成失败：{e}", "raw": str(reply)[:200] if 'reply' in dir() else ""}
    for i, it in enumerate(items):
        it["id"] = f"mv{datetime.now().timestamp() * 1000:.0f}{i}"
    return {"ok": True, "items": items}


@app.post("/api/meta/mirror/judge")
async def mirror_judge(req: Request):
    data = await req.json()
    agree = bool(data.get("agree"))
    prediction = (data.get("prediction") or "").strip()[:500]
    correction = (data.get("correction") or "").strip()[:300]
    if not prediction:
        return {"ok": False, "error": "缺少预测内容"}
    if agree:
        # 同意 → 强化画像：给置信度最高的一条 personality 条目 +0.1（封顶 0.95）
        conn = get_db()
        row = conn.execute(
            "SELECT id,confidence FROM user_model WHERE dim='personality' ORDER BY confidence DESC LIMIT 1"
        ).fetchone()
        boosted = False
        if row:
            newc = min(0.95, row["confidence"] + 0.1)
            conn.execute("UPDATE user_model SET confidence=? WHERE id=?", (newc, row["id"]))
            conn.commit()
            boosted = True
        conn.close()
        return {"ok": True, "result": "已强化画像置信度（预测正确，画像是懂你的）", "boosted": boosted}
    # 不同意 → 写入纠正事实（高置信，source='mirror'）
    if not correction:
        return {"ok": False, "error": "不同意时需要给出纠正（你实际会怎么做）"}
    n = _store_facts([{"dim": "preference", "field": "镜像校验纠正",
                       "value": correction, "confidence": 0.9}], "mirror")
    return {"ok": True, "result": "已记录纠正并更新画像", "stored": n}


# ── 被动蒸馏：元神每次闲聊自动抽取人格事实 ───────────────────────
async def _auto_distill_user(user_text: str):
    if not user_text or len(user_text) < 12 or "?" in user_text:
        return
    try:
        facts = await _extract_async(
            EXTRACT_SYSTEM + "\n这是用户和元神闲聊时说的话，只抽取明确的人格/偏好事实，不确定不要抽。",
            f"用户说：{user_text}")
        if facts:
            _store_facts(facts, "chat")
    except Exception:
        pass
