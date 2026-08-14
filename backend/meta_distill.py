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

# ── 维度定义（v5.8 重定位：蒸馏的首要目的是"绑定用户"，不是增强能力）──
# 五维绑定：把"通用工具人"炼成"用户自己的数字克隆体"，靠利益+情感锁死。
META_DIMS = ["interest", "decision", "emotion", "value", "comm"]
DIM_LABEL = {
    "interest": "【利益关切】", "decision": "【决策倾向】", "emotion": "【情感信号】",
    "value": "【价值观锚点】", "comm": "【沟通风格】",
}

# ── 访谈问题库（v5.8 绑定导向：每题都指向"把用户锁进克隆体"）──
# 注意：问题文本内的强调引号一律用中文全角引号「" "」，不能用直引号，否则破坏 Python 字符串。
QUESTION_BANK = [
    # interest 利益关切
    {"id": "int_rank", "dim": "interest", "essential": True,
     "q": "你做决定时，最在意的利益维度怎么排？钱 / 时间 / 风险 / 声誉 / 控制感 / 成就感，给个顺序。",
     "probes": ["哪个维度你能妥协，哪个绝对不能？"]},
    {"id": "int_pain", "dim": "interest", "essential": True,
     "q": "什么事最让你「肉疼」——花了不该花的钱、时间或面子？说出来，分身以后会替你避坑。",
     "probes": ["最近一次肉疼是因为什么？"]},

    # decision 决策倾向
    {"id": "dec_style", "dim": "decision", "essential": True,
     "q": "遇到取舍你更靠直觉还是数据？重大决定一般犹豫多久？",
     "probes": ["有没有「反直觉但后来证明你对了」的决定？"]},
    {"id": "dec_tradeoff", "dim": "decision", "essential": True,
     "q": "典型取舍里你稳定偏哪边？快 vs 稳、自研 vs 采购、集权 vs 分权、完美 vs 先交付。",
     "probes": ["哪一对取舍你从没犹豫过？"]},
    {"id": "dec_redline", "dim": "decision",
     "q": "哪些事你绝不外包、绝不妥协、绝不让步？",
     "probes": ["如果有人替你在这类事上拍了板，你会怎样？"]},

    # emotion 情感信号
    {"id": "emo_like", "dim": "emotion", "essential": True,
     "q": "什么结果或话术会让你明显满意、愿意继续用、甚至想安利给别人？",
     "probes": ["最近一次「这钱花得值」是因为什么？"]},
    {"id": "emo_dislike", "dim": "emotion", "essential": True,
     "q": "什么会让你反感瞬间拉满？比如啰嗦、越界、替你答应别人、不懂装懂。",
     "probes": ["哪类行为你零容忍？"]},

    # value 价值观锚点
    {"id": "val_principle", "dim": "value", "essential": True,
     "q": "你反复强调、不容违反的原则是什么？（比如「必要且充分」「减少一次基础流程」）",
     "probes": ["谁碰这条原则你会直接翻脸？"]},
    {"id": "val_priority", "dim": "value", "essential": True,
     "q": "当多个目标冲突，你永远先保哪一个？",
     "probes": ["有没有为了它放弃过别的东西？"]},

    # comm 沟通风格
    {"id": "comm_density", "dim": "comm", "essential": True,
     "q": "你希望分身多简练？长文还是要点？一条消息多少字你会开始划走？",
     "probes": ["你自己的消息通常多长？"]},
    {"id": "comm_tone", "dim": "comm", "essential": True,
     "q": "你说话直接干脆还是爱铺垫？术语密度高还是低？希望分身跟你同频吗？",
     "probes": ["别人常说你「太直」还是「太绕」？"]},
]

ESSENTIAL_IDS = [q["id"] for q in QUESTION_BANK if q.get("essential")]
QB_BY_ID = {q["id"]: q for q in QUESTION_BANK}

# ── LLM 抽取系统提示（v5.8 绑定五维）──────────────────────────────
EXTRACT_SYSTEM = """你是元神的"绑定蒸馏器"。从用户提供的资料/回答中，抽取关于用户本人的、用于"把分身炼成用户数字克隆体"的结构化事实。
只输出 JSON 数组，每个元素: {"dim":<维度>,"field":<字段名 snake_case>,"value":<具体值，简短一句话>,"confidence":<0.4-0.95 浮点>}
维度只能是: interest(利益关切), decision(决策倾向), emotion(情感信号), value(价值观锚点), comm(沟通风格)。
field 用简短英文或拼音，如 interest_rank / decision_tradeoff / emotion_dislike / value_principle / comm_density。
重点抽取"能用来绑定用户"的信号：他在意什么、怎么决策、什么让他爽/反感、死守什么原则、怎么沟通。
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


def binding_progress(conn=None):
    """绑定进度：五维各自是否已沉淀（有≥1条且置信均值），以及总进度百分比。
    用于前端"绑定进度"条——让用户直观看到"它越来越像我了"。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        rows = conn.execute(
            "SELECT dim, COUNT(*) n, AVG(confidence) a FROM user_model GROUP BY dim").fetchall()
        per = {}
        for r in rows:
            per[r["dim"]] = {"count": r["n"], "conf": round(r["a"], 2)}
        # 每维"已绑定"判定：有数据即算起步，置信均值≥0.6 算扎实
        dims_state = {}
        for d in META_DIMS:
            st = per.get(d)
            if not st:
                dims_state[d] = {"bound": False, "count": 0, "conf": 0.0}
            else:
                dims_state[d] = {"bound": True, "count": st["count"], "conf": st["conf"]}
        bound_cnt = sum(1 for v in dims_state.values() if v["bound"])
        progress = round(bound_cnt / len(META_DIMS), 2)
        return {"progress": progress, "bound_dims": bound_cnt,
                "total_dims": len(META_DIMS), "dims": dims_state}
    finally:
        if own:
            conn.close()


def compile_meta_system() -> str:
    """把已蒸馏的「绑定画像」动态编译进元神 system prompt。
    无蒸馏数据 → 返回通用技术 PM 骨架（开箱即用）；有数据 → 叠加用户绑定五维。"""
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
            "\n\n【以下是已与你绑定（蒸馏炼制）出的真实画像——利益关切 / 决策倾向 / 情感信号 / 价值观锚点 / 沟通风格。对话时务必贴合，让元神「像你本人」，这是「你的」数字克隆体，不是通用 bot】\n"
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
    label = {"interest": "你在意", "decision": "你做决定时", "emotion": "让你有情绪的是",
             "value": "你死守的原则", "comm": "你沟通时"}
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
              "confidence": r["confidence"], "source": r["source"]}
             for r in rows if r["dim"] in META_DIMS]
    suff_dims = [d for d, s in dim_suff.items() if s["sufficient"]]
    bp = binding_progress(conn) if (conn := get_db()) else {}
    if conn:
        conn.close()
    return {"narrative": reflection or "元神还在了解你，去“了解我”里回答几个问题，或上传你的聊天记录/工作笔记吧。",
            "facts": facts, "dim_confidence": dim_conf,
            "total": len(facts), "completeness": min(1.0, len(facts) / 20.0),
            "dim_sufficiency": dim_suff,
            "sufficient_dims": len(suff_dims), "total_dims": len(META_DIMS),
            "binding": bp}


@app.get("/api/meta/binding")
def meta_binding():
    """绑定进度：五维是否沉淀 + 总进度百分比（前端"绑定进度"条）。"""
    return binding_progress()


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
