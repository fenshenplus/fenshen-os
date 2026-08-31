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
import time
from datetime import datetime
from fastapi import Request

try:
    from backend.main import app, get_db, META_PID, META_SYSTEM, call_llm
except Exception:
    from main import app, get_db, META_PID, META_SYSTEM, call_llm

# ── 维度定义（v5.8 重定位：蒸馏的首要目的是"绑定用户"，不是增强能力）──
# 绑定维度（v0.64.36 扩至 9 维）：把"通用工具人"炼成"用户自己的数字克隆体"。
# 利益+情感锁死为核心，叠加知识/工作流/协作/风险，让蒸馏更全更深更准。
META_DIMS = ["interest", "decision", "emotion", "value", "comm",
             "knowledge", "workflow", "collab", "risk"]

# 闲聊自动抽取节流：避免用户简单对话频繁调用 LLM
_LAST_CHAT_EXTRACT_TS = 0.0
_CHAT_EXTRACT_COOLDOWN = 60  # 同一用户两次闲聊抽取至少间隔 60 秒
_CHAT_EXTRACT_MIN_LEN = 30  # 太短的句子不触发抽取
# v0.64.43：素材蒸馏最小长度，低于此值拒绝并给出明确提示
_INGEST_MIN_LEN = 80
DIM_LABEL = {
    "interest": "【利益关切】", "decision": "【决策倾向】", "emotion": "【情感信号】",
    "value": "【价值观锚点】", "comm": "【沟通风格】",
}
# 人格维度（蒸馏仅允许注入这些）。任何 tool/capability/permission 维度一律拒绝——
# 这是「蒸馏不增能力」契约的纵深防御：即使未来扩展 META_DIMS，能力维度也永不可经蒸馏注入。
CAPABILITY_DIMS = {
    "tool", "capability", "permission", "skill", "command", "api_key",
    "system_prompt", "prompt", "role", "function", "tool_def", "instruction",
}

# ── v6.4 蒸馏二期：素材类型 & 授权模型 ─────────────────────────────
# 图1 蒸馏素材类型：自我介绍/简历、个人经历、聊天记录、工作文档、创作内容、别人对你的评价
MATERIAL_TYPES = {
    "resume": "自我介绍 / 简历",
    "experience": "个人经历（求学 / 工作 / 转行的关键故事）",
    "chat": "聊天记录（微信 / 飞书 / 钉钉对话导出）",
    "document": "工作文档（周报 / 方案 / 代码评审意见）",
    "creation": "创作内容（博客 / 视频脚本 / 朋友圈 / 社交媒体）",
    "feedback": "别人对我的评价（同事反馈 / 朋友评价）",
    # v0.64.43 移动蒸馏（保守采集策略）：相册 / 账单 / 社媒 / 语音自述
    "album": "相册照片（拍摄时间地点，端侧抽取生活轨迹）",
    "transaction": "交易账单（支付平台导出的 CSV / 账单文本）",
    "social": "社交媒体（公开发帖 / 评论 / 签名）",
    "voice": "语音自述（端侧转写后的文本）",
}
# 授权状态：蒸馏他人时必须获得被蒸馏人明示授权
AUTH_STATUS = ["owner", "pending", "authorized", "revoked", "denied"]

# ── 数据库迁移（v6.4 蒸馏升级）──────────────────────────────────────
def _migrate_distill_v2():
    conn = get_db()
    try:
        # user_model 新增蒸馏对象与授权字段
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_model)").fetchall()}
        for col, sql in [
            ("target_type", "ALTER TABLE user_model ADD COLUMN target_type TEXT DEFAULT 'self'"),
            ("subject", "ALTER TABLE user_model ADD COLUMN subject TEXT DEFAULT ''"),
            ("authorization_status", "ALTER TABLE user_model ADD COLUMN authorization_status TEXT DEFAULT 'owner'"),
            ("material_type", "ALTER TABLE user_model ADD COLUMN material_type TEXT DEFAULT ''"),
            ("quality_score", "ALTER TABLE user_model ADD COLUMN quality_score REAL DEFAULT 0"),
            # v0.64.43：人格事实溯源到素材，支撑「删除素材即删除其产生的事实」（用户主权）
            ("material_id", "ALTER TABLE user_model ADD COLUMN material_id INTEGER DEFAULT 0"),
        ]:
            if col not in cols:
                conn.execute(sql)
        # 旧数据迁移：空 subject 视为 self
        conn.execute("UPDATE user_model SET subject='self' WHERE subject IS NULL OR subject=''")
        conn.execute("UPDATE user_model SET target_type='self' WHERE target_type IS NULL OR target_type=''")
        conn.execute("UPDATE user_model SET authorization_status='owner' WHERE authorization_status IS NULL OR authorization_status=''")
        # 被蒸馏人/数字克隆体主表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS distill_subjects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                target_type TEXT DEFAULT 'self',
                authorization_status TEXT DEFAULT 'owner',
                authorization_proof TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # 授权记录（可审计）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS distill_authorizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id TEXT,
                action TEXT,
                proof TEXT DEFAULT '',
                ts TEXT
            )
        """)
        # 素材元数据
        conn.execute("""
            CREATE TABLE IF NOT EXISTS distill_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id TEXT,
                material_type TEXT,
                filename TEXT,
                content_preview TEXT,
                extracted_facts INTEGER DEFAULT 0,
                quality_score REAL DEFAULT 0,
                ts TEXT
            )
        """)
        # meta_interview 增加 subject 字段以支持多对象访谈
        iv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(meta_interview)").fetchall()}
        if "subject" not in iv_cols:
            conn.execute("ALTER TABLE meta_interview ADD COLUMN subject TEXT DEFAULT 'self'")
        conn.commit()
    finally:
        conn.close()


try:
    _migrate_distill_v2()
except Exception:
    pass

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

    # ════════════════════════════════════════════════════════════════
    # v0.64.36 大幅扩库：数百题按维度纵深，更快更深更准完成蒸馏
    # ════════════════════════════════════════════════════════════════

    # ── interest 利益关切（纵深）──
    {"id": "int_03", "dim": "interest", "q": "如果有一笔意外之财，你第一反应会拿来干嘛？", "probes": ["是消费、投资、还是还债？"]},
    {"id": "int_04", "dim": "interest", "q": "你愿意为「省时间」花多少钱？举个例子。", "probes": ["有没有为省时间花过「不划算」的钱？"]},
    {"id": "int_05", "dim": "interest", "q": "哪种「花钱买罪受」的事你绝对不干？", "probes": ["别人觉得值、你却觉得亏的事？"]},
    {"id": "int_06", "dim": "interest", "q": "你更怕「少赚」还是「亏钱」？", "probes": ["同样是波动，你更在意下行还是错失上行？"]},
    {"id": "int_07", "dim": "interest", "q": "面子在你的重要度里排第几？什么场合面子比钱重要？", "probes": ["有没有为了面子吃暗亏？"]},
    {"id": "int_08", "dim": "interest", "q": "控制感对你多重要？把事交给别人你会焦虑吗？", "probes": ["交出控制后你通常会怎么做？"]},
    {"id": "int_09", "dim": "interest", "q": "成就感主要来自「做成事」还是「被认可」？", "probes": ["没人看见的成就你还爽吗？"]},
    {"id": "int_10", "dim": "interest", "q": "你愿意为「自由」放弃多少收入？", "probes": ["你定义的自由于在哪个层面？"]},
    {"id": "int_11", "dim": "interest", "q": "健康、钱、时间、关系，这四项你隐形的优先级是？", "probes": ["真到极限时你先牺牲哪个？"]},
    {"id": "int_12", "dim": "interest", "q": "哪种「占便宜」你会欣然接受，哪种你会警惕？", "probes": ["免费的东西你会怀疑什么？"]},
    {"id": "int_13", "dim": "interest", "q": "你对「长期复利」耐心如何？能扛多久没正反馈？", "probes": ["有没有坚持很久才见效的事？"]},
    {"id": "int_14", "dim": "interest", "q": "你更享受「稳定现金流」还是「一次大爆发」？", "probes": ["哪种让你睡得着觉？"]},
    {"id": "int_15", "dim": "interest", "q": "别人请你帮忙，你衡量的是情分、成本还是交换？", "probes": ["你什么时候会拒绝帮忙？"]},
    {"id": "int_16", "dim": "interest", "q": "你眼里的「好deal」长什么样？", "probes": ["最近一次你觉得捡漏是什么？"]},
    {"id": "int_17", "dim": "interest", "q": "你愿意为「正确性」付出多少额外的钱或时间？", "probes": ["差不多就行 vs 必须对的临界点？"]},
    {"id": "int_18", "dim": "interest", "q": "地位/头衔对你重要吗？还是只在乎实权？", "probes": ["虚名和实利冲突时你选哪个？"]},
    {"id": "int_19", "dim": "interest", "q": "你对「被需要」的渴求有多强？", "probes": ["没人找你你会失落吗？"]},
    {"id": "int_20", "dim": "interest", "q": "隐私在你利益排序里什么位置？", "probes": ["你愿意用多少隐私换便利？"]},
    {"id": "int_21", "dim": "interest", "q": "你更在意「拥有」还是「使用」？", "probes": ["买断 vs 订阅你倾向哪个？"]},
    {"id": "int_22", "dim": "interest", "q": "哪种「浪费」你最不能忍——时间、钱、还是才华？", "probes": ["看到别人浪费你会怎样？"]},
    {"id": "int_23", "dim": "interest", "q": "你做选择时，会为「未来的自己」留多少余地？", "probes": ["你后悔过眼光太短吗？"]},
    {"id": "int_24", "dim": "interest", "q": "你愿意为家人/伴侣牺牲到什么程度？", "probes": ["利益和家庭冲突怎么选？"]},
    {"id": "int_25", "dim": "interest", "q": "你对「公平」的阈值在哪？被占便宜几次会翻脸？", "probes": ["你忍过最大的亏是什么？"]},
    {"id": "int_26", "dim": "interest", "q": "你更享受过程还是结果？", "probes": ["事情成了但过程痛苦你还愿意再来吗？"]},
    {"id": "int_27", "dim": "interest", "q": "你认为自己最「值钱」的能力是什么？", "probes": ["这项能力怎么变现过？"]},
    {"id": "int_28", "dim": "interest", "q": "你眼里的「浪费钱」和「投资自己」界限在哪？", "probes": ["最近一笔自我投资花在哪？"]},
    {"id": "int_29", "dim": "interest", "q": "你会在意「别人觉得我赚得多不多」吗？", "probes": ["和同龄人比你会焦虑吗？"]},
    {"id": "int_30", "dim": "interest", "q": "你更想「被需要」还是「被仰慕」？", "probes": ["两者冲突时你选哪个？"]},
    {"id": "int_31", "dim": "interest", "q": "如果你有无限钱，你还会工作吗？为什么？", "probes": ["你工作的内在驱动力是？"]},
    {"id": "int_32", "dim": "interest", "q": "你最不愿意「贱卖」的是什么——时间、专业、还是原则？", "probes": ["有人想白嫖你什么你会怒？"]},

    # ── decision 决策倾向（纵深）──
    {"id": "dec_06", "dim": "decision", "q": "面对不确定，你倾向先动起来试错还是想透再动？", "probes": ["有没有「想太久」错过的机会？"]},
    {"id": "dec_07", "dim": "decision", "q": "你做重大决策会请教谁？还是只信自己？", "probes": ["谁的的意见你能听进去？"]},
    {"id": "dec_08", "dim": "decision", "q": "你更看重「别出错」还是「别错过」？", "probes": ["错过和做错哪个更让你难受？"]},
    {"id": "dec_09", "dim": "decision", "q": "信息不全时你怎么拍板？", "probes": ["你会设 deadline 逼自己决断吗？"]},
    {"id": "dec_10", "dim": "decision", "q": "你容易被「沉没成本」绑架吗？", "probes": ["有没有该停没停、越陷越深的事？"]},
    {"id": "dec_11", "dim": "decision", "q": "群体压力和你的判断冲突时，你跟不跟？", "probes": ["你逆过主流判断吗？结果如何？"]},
    {"id": "dec_12", "dim": "decision", "q": "你偏好「小步快跑」还是「一次到位」？", "probes": ["你改方案的频率高吗？"]},
    {"id": "dec_13", "dim": "decision", "q": "你做决定前会列清单/打分，还是凭感觉？", "probes": ["结构化对你有用吗？"]},
    {"id": "dec_14", "dim": "decision", "q": "你更相信专家权威还是一线数据？", "probes": ["权威和你的数据打架你信谁？"]},
    {"id": "dec_15", "dim": "decision", "q": "你敢在反对声里坚持吗？坚持到什么程度？", "probes": ["你被证明对过最孤独的一次？"]},
    {"id": "dec_16", "dim": "decision", "q": "你做决定会留退路/B计划吗？", "probes": ["你常说「大不了怎样」吗？"]},
    {"id": "dec_17", "dim": "decision", "q": "你更怕「慢」还是「错」？", "probes": ["赶工期你会牺牲质量吗？"]},
    {"id": "dec_18", "dim": "decision", "q": "你相信「第一直觉」还是「再三权衡」？", "probes": ["直觉坑过你吗？"]},
    {"id": "dec_19", "dim": "decision", "q": "你把决策权下放给别人时，最担心失控什么？", "probes": ["你 micromanage 过吗？"]},
    {"id": "dec_20", "dim": "decision", "q": "你对「纠结」的容忍度？会为了快点定而随便选吗？", "probes": ["你有选择困难吗？"]},
    {"id": "dec_21", "dim": "decision", "q": "你更看重短期确定性还是长期可能性？", "probes": ["期权式选择你敢拿吗？"]},
    {"id": "dec_22", "dim": "decision", "q": "你做决定会受「锚定效应」影响吗（第一印象/第一个报价）？", "probes": ["你察觉过自己被锚定吗？"]},
    {"id": "dec_23", "dim": "decision", "q": "你会在意「别人会怎么看这个决定」吗？", "probes": ["面子影响决策的程度？"]},
    {"id": "dec_24", "dim": "decision", "q": "你敢为「可能更好」放弃「已经不错」吗？", "probes": ["你换过赛道吗？为什么？"]},
    {"id": "dec_25", "dim": "decision", "q": "你更倾向保守防守还是激进进攻？", "probes": ["什么情形下你会转守为攻？"]},
    {"id": "dec_26", "dim": "decision", "q": "你相信运气还是实力？", "probes": ["成功你归功什么？"]},
    {"id": "dec_27", "dim": "decision", "q": "你做决定会参考「最坏情况能接受吗」吗？", "probes": ["你的底线思维强吗？"]},
    {"id": "dec_28", "dim": "decision", "q": "你更愿为「确定性低但天花板高」赌一把，还是「稳但封顶」？", "probes": ["你人生最大的赌注是？"]},
    {"id": "dec_29", "dim": "decision", "q": "你做决定会受情绪影响大吗（高兴/生气时）？", "probes": ["你在什么状态下不拍板？"]},
    {"id": "dec_30", "dim": "decision", "q": "你更信「谋定后动」还是「边打边瞄」？", "probes": ["你计划和执行哪个强？"]},
    {"id": "dec_31", "dim": "decision", "q": "你愿意为「正确但 unpopular」付出社交代价吗？", "probes": ["你孤勇过吗？"]},
    {"id": "dec_32", "dim": "decision", "q": "你做决定时会算「机会成本」吗？", "probes": ["你后悔过「没选的另一条路』吗？"]},
    {"id": "dec_33", "dim": "decision", "q": "你更看重逻辑自洽还是结果有效？", "probes": ["管用但讲不通你接受吗？"]},
    {"id": "dec_34", "dim": "decision", "q": "你会在意「决策可解释性」吗（能跟人讲清楚why）？", "probes": ["你事后复盘习惯吗？"]},
    {"id": "dec_35", "dim": "decision", "q": "你更愿听多数派还是少数派？", "probes": ["反共识信息你会追吗？"]},
    {"id": "dec_36", "dim": "decision", "q": "你做决定前会设「什么情况下我改主意」的触发条件吗？", "probes": ["你善变吗？"]},
    {"id": "dec_37", "dim": "decision", "q": "你更信历史经验还是当下信号？", "probes": ["老办法失效过吗？"]},
    {"id": "dec_38", "dim": "decision", "q": "你敢把身家押在「一个判断」上吗？", "probes": ["你 all-in 过吗？"]},
    {"id": "dec_39", "dim": "decision", "q": "你做决定会受「损失厌恶」驱动吗？", "probes": ["你死扛过亏损吗？"]},
    {"id": "dec_40", "dim": "decision", "q": "你更愿「做难而正确的事」还是「容易但将就」？", "probes": ["你妥协过原则吗？代价？"]},

    # ── emotion 情感信号（纵深）──
    {"id": "emo_03", "dim": "emotion", "q": "什么话最能让你「破防」？", "probes": ["你最怕被说哪句话？"]},
    {"id": "emo_04", "dim": "emotion", "q": "什么场景你会「瞬间下头」？", "probes": ["最近一次下头是因为什么？"]},
    {"id": "emo_05", "dim": "emotion", "q": "你生气时是什么样子——爆发、冷处理、还是阴阳怪气？", "probes": ["你生气的触发点通常是什么？"]},
    {"id": "emo_06", "dim": "emotion", "q": "什么会让你产生「被理解」的感动？", "probes": ["谁最懂你？怎么做到的？"]},
    {"id": "emo_07", "dim": "emotion", "q": "你容易被「捧」还是「踩」影响情绪？", "probes": ["夸你和骂你哪个更让你动摇？"]},
    {"id": "emo_08", "dim": "emotion", "q": "你藏着不让别人看到的脆弱是什么？", "probes": ["你只敢跟谁暴露脆弱？"]},
    {"id": "emo_09", "dim": "emotion", "q": "哪种真诚你买账，哪种你觉得假？", "probes": ["你一眼假的信号是？"]},
    {"id": "emo_10", "dim": "emotion", "q": "你开心时会想分享还是自己偷着乐？", "probes": ["你第一分享欲给谁？"]},
    {"id": "emo_11", "dim": "emotion", "q": "你反感「被说教」吗？哪种说教最让你烦？", "probes": ["你听不进谁的话？"]},
    {"id": "emo_12", "dim": "emotion", "q": "你会对「努力却没结果」感到委屈吗？", "probes": ["你委屈的点通常是？"]},
    {"id": "emo_13", "dim": "emotion", "q": "什么让你「心累」？", "probes": ["你最近一次心累是因为？"]},
    {"id": "emo_14", "dim": "emotion", "q": "你更享受「被需要」还是「被崇拜」带来的满足？", "probes": ["两者你更缺哪个？"]},
    {"id": "emo_15", "dim": "emotion", "q": "你会对「不被尊重」零容忍吗？", "probes": ["你眼里的尊重具体指什么？"]},
    {"id": "emo_16", "dim": "emotion", "q": "你容易被「共情」打动而破例吗？", "probes": ["你心软过吃亏吗？"]},
    {"id": "emo_17", "dim": "emotion", "q": "你羡慕别人什么，又绝不愿意交换？", "probes": ["你表面想要其实不想要的？"]},
    {"id": "emo_18", "dim": "emotion", "q": "你会在意「被比较」吗？和谁比最敏感？", "probes": ["你暗暗较劲的人？"]},
    {"id": "emo_19", "dim": "emotion", "q": "什么会让你「无名火」？", "probes": ["你的情绪地雷是？"]},
    {"id": "emo_20", "dim": "emotion", "q": "你更需要「被肯定」还是「被理解」？", "probes": ["两者你更缺哪个？"]},
    {"id": "emo_21", "dim": "emotion", "q": "你对「不被看见的努力」什么态度？", "probes": ["你默默做过什么？"]},
    {"id": "emo_22", "dim": "emotion", "q": "你会对「画饼」上头吗？", "probes": ["你被画过最大的饼是？"]},
    {"id": "emo_23", "dim": "emotion", "q": "你更看重「过程舒服」还是「结果爽」？", "probes": ["苦尽甘来你吃这套吗？"]},
    {"id": "emo_24", "dim": "emotion", "q": "你会对「被依赖」感到压力还是满足？", "probes": ["你怕被人黏吗？"]},
    {"id": "emo_25", "dim": "emotion", "q": "你在什么情况下会「破防式坦诚」？", "probes": ["你酒后/深夜吐过真言吗？"]},
    {"id": "emo_26", "dim": "emotion", "q": "你更厌「虚伪」还是「粗鲁」？", "probes": ["哪类人你敬而远之？"]},
    {"id": "emo_27", "dim": "emotion", "q": "你会对「被低估」愤怒还是窃喜？", "probes": ["你藏过实力吗？"]},
    {"id": "emo_28", "dim": "emotion", "q": "你更想被当作「强者」还是「好人」？", "probes": ["你人设优先级？"]},
    {"id": "emo_29", "dim": "emotion", "q": "你对「背叛」的容忍度？", "probes": ["你被背过刺吗？怎么处理的？"]},
    {"id": "emo_30", "dim": "emotion", "q": "你会对「被敷衍」格外敏感吗？", "probes": ["你敷衍过别人吗？什么心态？"]},
    {"id": "emo_31", "dim": "emotion", "q": "什么让你「眼眶发热」？", "probes": ["你感性的一面在什么场景露出？"]},
    {"id": "emo_32", "dim": "emotion", "q": "你更享受「独处充电」还是「人群续航」？", "probes": ["你社交耗电吗？"]},

    # ── value 价值观锚点（纵深）──
    {"id": "val_03", "dim": "value", "q": "你绝对不能接受「为了结果不择手段」吗？边界在哪？", "probes": ["你踩过自己的底线吗？"]},
    {"id": "val_04", "dim": "value", "q": "你更认同「公平优先」还是「效率优先」？", "probes": ["两者冲突你怎么选？"]},
    {"id": "val_05", "dim": "value", "q": "诚实对你多重要？善意的谎言能接受吗？", "probes": ["你撒过善意的谎吗？"]},
    {"id": "val_06", "dim": "value", "q": "你更在意「对得起自己」还是「对得起别人」？", "probes": ["自我和他人冲突时？"]},
    {"id": "val_07", "dim": "value", "q": "你相信「因果/善恶有报」吗？", "probes": ["你的行事因此改变过吗？"]},
    {"id": "val_08", "dim": "value", "q": "你更看重「自由」还是「安稳」？", "probes": ["你为自由付出过什么？"]},
    {"id": "val_09", "dim": "value", "q": "你对「规则」的态度——守序、利用还是打破？", "probes": ["你钻过规则漏洞吗？"]},
    {"id": "val_10", "dim": "value", "q": "你更追求「卓越」还是「平衡」？", "probes": ["你卷过吗？为什么停/不停？"]},
    {"id": "val_11", "dim": "value", "q": "你眼里的「成功」是什么定义？", "probes": ["你自己的成功标准？"]},
    {"id": "val_12", "dim": "value", "q": "你更愿「利他」还是「利己」？什么情况下反转？", "probes": ["你无私过最狠的一次？"]},
    {"id": "val_13", "dim": "value", "q": "你对「权威」天然顺从还是质疑？", "probes": ["你挑战过权威吗？"]},
    {"id": "val_14", "dim": "value", "q": "你更在意「过程正义」还是「结果正义」？", "probes": ["程序不公但结果好你接受吗？"]},
    {"id": "val_15", "dim": "value", "q": "你相信「努力必有回报」吗？", "probes": ["你努力没回报时怎么想？"]},
    {"id": "val_16", "dim": "value", "q": "你更看重「家庭」还是「自我实现」？", "probes": ["你为家庭牺牲过梦想吗？"]},
    {"id": "val_17", "dim": "value", "q": "你对「承诺」的认真程度？", "probes": ["你爽约过吗？什么心态？"]},
    {"id": "val_18", "dim": "value", "q": "你更认同「个人英雄」还是「团队协作」？", "probes": ["你当过头还是手？"]},
    {"id": "val_19", "dim": "value", "q": "你眼里的「尊严」具体指什么？", "probes": ["什么让你觉得没尊严？"]},
    {"id": "val_20", "dim": "value", "q": "你更愿「求真」还是「求和」？", "probes": ["冲突时你选真相还是和谐？"]},
    {"id": "val_21", "dim": "value", "q": "你对「延迟满足」的耐力？", "probes": ["你最能等的一件事？"]},
    {"id": "val_22", "dim": "value", "q": "你更在意「被信任」还是「被喜欢」？", "probes": ["两者你更怕失去哪个？"]},
    {"id": "val_23", "dim": "value", "q": "你相信「人应该对自己负责」还是「环境决定论」？", "probes": ["你归因风格是？"]},
    {"id": "val_24", "dim": "value", "q": "你对「浪费生命」的零容忍标准？", "probes": ["什么算虚度？"]},
    {"id": "val_25", "dim": "value", "q": "你更追求「被记住」还是「舒服过」？", "probes": ["你留下的东西是？"]},
    {"id": "val_26", "dim": "value", "q": "你认同「弱肉强食」还是「互助共生」？", "probes": ["你现实里的做法是？"]},
    {"id": "val_27", "dim": "value", "q": "你更看重「专业主义」还是「实用主义」？", "probes": ["你鄙视过不专业吗？"]},
    {"id": "val_28", "dim": "value", "q": "你对「捷径」的态度？", "probes": ["你走过快车道吗？心安吗？"]},
    {"id": "val_29", "dim": "value", "q": "你更愿「守拙」还是「取巧」？", "probes": ["你笨办法做过什么？"]},
    {"id": "val_30", "dim": "value", "q": "你眼里的「体面」是什么？", "probes": ["什么让你觉得掉价？"]},
    {"id": "val_31", "dim": "value", "q": "你更认同「活在当下」还是「为将来囤」？", "probes": ["你焦虑未来吗？"]},
    {"id": "val_32", "dim": "value", "q": "你相信「人该念旧」还是「该断舍离」？", "probes": ["你留过不该留的人/物吗？"]},

    # ── comm 沟通风格（纵深）──
    {"id": "com_03", "dim": "comm", "q": "你偏好文字还是语音/当面沟通？", "probes": ["复杂的事你用什么讲？"]},
    {"id": "com_04", "dim": "comm", "q": "你发消息爱用标点/表情吗？", "probes": ["你最烦哪种消息格式？"]},
    {"id": "com_05", "dim": "comm", "q": "你希望对方先给结论还是先讲背景？", "probes": ["你自己的汇报顺序是？"]},
    {"id": "com_06", "dim": "comm", "q": "你忍受得了「废话铺垫」吗？到第几句会走神？", "probes": ["你最烦的开口方式是？"]},
    {"id": "com_07", "dim": "comm", "q": "你更爱「直接给方案」还是「先一起想」？", "probes": ["你被问意见时的反应？"]},
    {"id": "com_08", "dim": "comm", "q": "你会对「听不懂装懂」的人零容忍吗？", "probes": ["你当场拆穿过吗？"]},
    {"id": "com_09", "dim": "comm", "q": "你更想被「夸到点」还是「指出错」？", "probes": ["你最想听哪种反馈？"]},
    {"id": "com_10", "dim": "comm", "q": "你会在意「回复速度」吗？", "probes": ["你不回消息时通常在干嘛？"]},
    {"id": "com_11", "dim": "comm", "q": "你更爱「短句」还是「长段落」？", "probes": ["你读长文会跳吗？"]},
    {"id": "com_12", "dim": "comm", "q": "你希望对方「多问确认」还是「少打扰自己悟」？", "probes": ["你被问烦过吗？"]},
    {"id": "com_13", "dim": "comm", "q": "你更接受「委婉拒绝」还是「干脆说不」？", "probes": ["你拒绝人通常怎么措辞？"]},
    {"id": "com_14", "dim": "comm", "q": "你会对「过度客套」不耐烦吗？", "probes": ["你最烦的寒暄是？"]},
    {"id": "com_15", "dim": "comm", "q": "你更愿「把话说满」还是「留余地」？", "probes": ["你承诺风格是？"]},
    {"id": "com_16", "dim": "comm", "q": "你希望分身汇报时带「情绪」还是「纯事实」？", "probes": ["你要的是信息还是共情？"]},
    {"id": "com_17", "dim": "comm", "q": "你更爱「例子」还是「抽象原则」？", "probes": ["你怎么给人讲清楚一件事？"]},
    {"id": "com_18", "dim": "comm", "q": "你会对「答非所问」瞬间下头吗？", "probes": ["你最烦的答法是？"]},
    {"id": "com_19", "dim": "comm", "q": "你更想被「当专家」还是「当外行」沟通？", "probes": ["你伪装过不懂吗？"]},
    {"id": "com_20", "dim": "comm", "q": "你发长语音吗？为什么？", "probes": ["你听长语音吗？"]},
    {"id": "com_21", "dim": "comm", "q": "你更接受「先坏消息后好消息」还是反过来？", "probes": ["你怎么报忧？"]},
    {"id": "com_22", "dim": "comm", "q": "你会对「没结构的长篇大论」划走吗？", "probes": ["你最想看到的排版是？"]},
    {"id": "com_23", "dim": "comm", "q": "你更愿「被问开放式问题」还是「被给选择项」？", "probes": ["你答问卷喜欢哪种？"]},
    {"id": "com_24", "dim": "comm", "q": "你会对「过度使用术语」反感吗？", "probes": ["你装过术语吗？"]},
    {"id": "com_25", "dim": "comm", "q": "你希望分身「主动追问」还是「答完就停」？", "probes": ["你被冷场过吗？"]},
    {"id": "com_26", "dim": "comm", "q": "你更爱「幽默」还是「严肃」的沟通氛围？", "probes": ["你开玩笑分场合吗？"]},
    {"id": "com_27", "dim": "comm", "q": "你会对「重复啰嗦」零容忍吗？", "probes": ["你最烦被说几遍？"]},
    {"id": "com_28", "dim": "comm", "q": "你更想被「挑战观点」还是「被附和」？", "probes": ["你享受辩论吗？"]},
    {"id": "com_29", "dim": "comm", "q": "你希望对方「先共情再给方案」还是「直接解决」？", "probes": ["你低谷时要的是？"]},
    {"id": "com_30", "dim": "comm", "q": "你更接受「书面留痕」还是「口头说清」？", "probes": ["你吃过没留痕的亏吗？"]},
    {"id": "com_31", "dim": "comm", "q": "你会对「答得太快不思考」不放心吗？", "probes": ["你信慢思考还是快反应？"]},
    {"id": "com_32", "dim": "comm", "q": "你更愿「被平等对话」还是「被服务」？", "probes": ["你要的是伙伴还是工具？"]},

    # ── knowledge 知识领域（新增维度）──
    {"id": "kno_01", "dim": "knowledge", "q": "你最拿手、别人常来请教你的领域是哪几个？", "probes": ["你被贴过什么专家标签？"]},
    {"id": "kno_02", "dim": "knowledge", "q": "你系统学过、有方法论的知识体系是什么？", "probes": ["你能教别人的硬技能？"]},
    {"id": "kno_03", "dim": "knowledge", "q": "你现在主业/吃饭的手艺是什么？", "probes": ["你靠什么变现？"]},
    {"id": "kno_04", "dim": "knowledge", "q": "你业余钻研、比身边人懂的「冷门」领域？", "probes": ["你小众的爱好专长？"]},
    {"id": "kno_05", "dim": "knowledge", "q": "你读得最多的书/内容类型？", "probes": ["最近一本影响你的书？"]},
    {"id": "kno_06", "dim": "knowledge", "q": "你更擅长「与人打交道」还是「与系统/数据打交道」？", "probes": ["你天赋值在左脑还是右脑？"]},
    {"id": "kno_07", "dim": "knowledge", "q": "你懂技术到什么程度（能写代码/搭系统吗）？", "probes": ["你技术栈是？"]},
    {"id": "kno_08", "dim": "knowledge", "q": "你对「商业/赚钱」的理解来自实战还是书本？", "probes": ["你赚过最聪明的一笔？"]},
    {"id": "kno_09", "dim": "knowledge", "q": "你最想被分身补上的知识短板是什么？", "probes": ["你哪块总要现查？"]},
    {"id": "kno_10", "dim": "knowledge", "q": "你更信「通才广博」还是「专才精深」？", "probes": ["你自己是哪种？"]},
    {"id": "kno_11", "dim": "knowledge", "q": "你脑子里「随时能调出」的框架/模型有哪些？", "probes": ["你常用的思维模型？"]},
    {"id": "kno_12", "dim": "knowledge", "q": "你对哪类新知吸收特别快？", "probes": ["你一学就会的东西？"]},
    {"id": "kno_13", "dim": "knowledge", "q": "你踩过最贵「认知盲区」的坑？", "probes": ["你后来补了哪块认知？"]},
    {"id": "kno_14", "dim": "knowledge", "q": "你更依赖「经验直觉」还是「查资料」？", "probes": ["你搜索能力自评分？"]},
    {"id": "kno_15", "dim": "knowledge", "q": "你眼里的「真懂」和「装懂」界限？", "probes": ["你最鄙视哪种半吊子？"]},
    {"id": "kno_16", "dim": "knowledge", "q": "你愿意分身替你「深度学习」哪块领域？", "probes": ["你希望它比你还懂的？"]},
    {"id": "kno_17", "dim": "knowledge", "q": "你对「行业黑话/行话」的掌握度？", "probes": ["你混过哪几个圈子？"]},
    {"id": "kno_18", "dim": "knowledge", "q": "你更擅长「抽象建模」还是「落地执行」？", "probes": ["你出方案还是扛落地？"]},
    {"id": "kno_19", "dim": "knowledge", "q": "你懂点什么「别人觉得难、你觉得简单」的？", "probes": ["你的降维打击领域？"]},
    {"id": "kno_20", "dim": "knowledge", "q": "你更愿被当作「通才PM」还是「某领域专家」？", "probes": ["你自我定位是？"]},
    {"id": "kno_21", "dim": "knowledge", "q": "你对「跨界迁移」能力强吗？", "probes": ["你迁移过什么能力？"]},
    {"id": "kno_22", "dim": "knowledge", "q": "你更信「第一性原理」还是「行业惯例」？", "probes": ["你拆解过什么复杂问题？"]},
    {"id": "kno_23", "dim": "knowledge", "q": "你脑子里的「知识地图」长啥样？", "probes": ["你知识结构是树还是网？"]},
    {"id": "kno_24", "dim": "knowledge", "q": "你对「AI/工具」的熟练度？", "probes": ["你天天用哪些工具？"]},
    {"id": "kno_25", "dim": "knowledge", "q": "你更愿意分身「替代你干活」还是「补你短板」？", "probes": ["你最想甩给它的活？"]},
    {"id": "kno_26", "dim": "knowledge", "q": "你更信「实践出真知」还是「理论指导实践」？", "probes": ["你学习风格是？"]},
    {"id": "kno_27", "dim": "knowledge", "q": "你懂点「人情世故/职场政治」吗？", "probes": ["你吃过人际的亏吗？"]},
    {"id": "kno_28", "dim": "knowledge", "q": "你更擅长「发现问题」还是「解决问题」？", "probes": ["你价值在哪儿？"]},
    {"id": "kno_29", "dim": "knowledge", "q": "你对「趋势/未来」有判断框架吗？", "probes": ["你押对过趋势吗？"]},
    {"id": "kno_30", "dim": "knowledge", "q": "你更愿意分身「懂技术」还是「懂人心」？", "probes": ["你最缺哪种理解？"]},
    {"id": "kno_31", "dim": "knowledge", "q": "你脑子里有哪些「别人不知道的秘诀/偏方」？", "probes": ["你的独门经验？"]},
    {"id": "kno_32", "dim": "knowledge", "q": "你更愿「深度学习一个领域」还是「广度扫盲」？", "probes": ["你知识焦虑在深还是广？"]},
    {"id": "kno_33", "dim": "knowledge", "q": "你对「数据/数字」敏感吗？", "probes": ["你看报表会抓重点吗？"]},
    {"id": "kno_34", "dim": "knowledge", "q": "你更信「专家意见」还是「用户反馈」？", "probes": ["你做产品听谁？"]},
    {"id": "kno_35", "dim": "knowledge", "q": "你希望分身继承你哪套「思考方式」而非知识？", "probes": ["你最独特的思维习惯？"]},

    # ── workflow 工作流习惯（新增维度）──
    {"id": "wrk_01", "dim": "workflow", "q": "你一天精力最好的是哪段？怎么安排重要事？", "probes": ["你几点效率最高？"]},
    {"id": "wrk_02", "dim": "workflow", "q": "你更爱「列清单按计划」还是「凭状态随缘」？", "probes": ["你的待办系统长啥样？"]},
    {"id": "wrk_03", "dim": "workflow", "q": "你 multitask 还是单线程？", "probes": ["你切任务的损耗大吗？"]},
    {"id": "wrk_04", "dim": "workflow", "q": "你更愿「早起干活」还是「夜猫子」？", "probes": ["你生物钟是？"]},
    {"id": "wrk_05", "dim": "workflow", "q": "你多久清一次收件箱/待办？", "probes": ["你拖延吗？哪类事拖？"]},
    {"id": "wrk_06", "dim": "workflow", "q": "你更爱「深度专注块」还是「碎片化穿插」？", "probes": ["你保护专注吗？"]},
    {"id": "wrk_07", "dim": "workflow", "q": "你做一件事前会先「搭框架/写大纲」吗？", "probes": ["你结构化工作习惯？"]},
    {"id": "wrk_08", "dim": "workflow", "q": "你更愿「自动化重复活」还是「手动可控」？", "probes": ["你自动化过什么？"]},
    {"id": "wrk_09", "dim": "workflow", "q": "你开会多吗？更爱同步会还是异步文档？", "probes": ["你最烦的会是什么？"]},
    {"id": "wrk_10", "dim": "workflow", "q": "你更爱「小步提交」还是「攒一大波再交付」？", "probes": ["你版本节奏？"]},
    {"id": "wrk_11", "dim": "workflow", "q": "你更喜欢「被deadline逼」还是「自律推进」？", "probes": ["你没deadline会怎样？"]},
    {"id": "wrk_12", "dim": "workflow", "q": "你更愿「先做难的」还是「先做易的」？", "probes": ["你吃青蛙吗？"]},
    {"id": "wrk_13", "dim": "workflow", "q": "你工作时会「听音乐/背景音」吗？", "probes": ["你的环境偏好？"]},
    {"id": "wrk_14", "dim": "workflow", "q": "你更爱「纸质笔记」还是「数字工具」？", "probes": ["你的记录系统是？"]},
    {"id": "wrk_15", "dim": "workflow", "q": "你多久复盘一次？怎么复盘？", "probes": ["你复盘产出过什么？"]},
    {"id": "wrk_16", "dim": "workflow", "q": "你更愿「并行多项目」还是「串行聚焦」？", "probes": ["你同时扛几个项目？"]},
    {"id": "wrk_17", "dim": "workflow", "q": "你对「打断」的容忍度？", "probes": ["你被打断后回得来吗？"]},
    {"id": "wrk_18", "dim": "workflow", "q": "你更爱「定好流程照做」还是「每次即兴」？", "probes": ["你 SOP 化过工作吗？"]},
    {"id": "wrk_19", "dim": "workflow", "q": "你更愿「亲力亲为」还是「流程化外包」？", "probes": ["你什么活愿意交出去？"]},
    {"id": "wrk_20", "dim": "workflow", "q": "你工作间歇怎么休息？", "probes": ["你的恢复方式是？"]},
    {"id": "wrk_21", "dim": "workflow", "q": "你更爱「目标驱动」还是「任务驱动」？", "probes": ["你被目标还是待办推着走？"]},
    {"id": "wrk_22", "dim": "workflow", "q": "你对「工具链」有洁癖吗？", "probes": ["你折腾过工具吗？"]},
    {"id": "wrk_23", "dim": "workflow", "q": "你更愿「一次做对」还是「快速出糙版再改」？", "probes": ["你完美主义在哪儿？"]},
    {"id": "wrk_24", "dim": "workflow", "q": "你一天大概几个「深度工作小时」？", "probes": ["你保护 deep work 吗？"]},
    {"id": "wrk_25", "dim": "workflow", "q": "你更爱「晨间仪式」还是「说干就干」？", "probes": ["你的启动仪式是？"]},
    {"id": "wrk_26", "dim": "workflow", "q": "你对「邮件/消息」的处理节奏？", "probes": ["你批量处理还是即时回？"]},
    {"id": "wrk_27", "dim": "workflow", "q": "你更愿「文档先行」还是「先聊清楚」？", "probes": ["你异步协作习惯？"]},
    {"id": "wrk_28", "dim": "workflow", "q": "你工作时会「开很多标签页/工具」吗？", "probes": ["你的桌面环境？"]},
    {"id": "wrk_29", "dim": "workflow", "q": "你更爱「固定工位」还是「哪都行」？", "probes": ["你远程适应度？"]},
    {"id": "wrk_30", "dim": "workflow", "q": "你多久做一次「大扫除/整理」？", "probes": ["你有收纳癖吗？"]},
    {"id": "wrk_31", "dim": "workflow", "q": "你更愿「被提醒」还是「自己记」？", "probes": ["你的提醒系统？"]},
    {"id": "wrk_32", "dim": "workflow", "q": "你对「周计划/月目标」有用吗？", "probes": ["你做长远规划吗？"]},
    {"id": "wrk_33", "dim": "workflow", "q": "你更爱「按块时间」还是「按能量」安排？", "probes": ["你调度自己的方式？"]},
    {"id": "wrk_34", "dim": "workflow", "q": "你工作会「进入心流」吗？什么触发？", "probes": ["你心流状态长啥样？"]},
    {"id": "wrk_35", "dim": "workflow", "q": "你更愿分身「接管日常流程」还是「只做关键决策」？", "probes": ["你想甩给它的流程活？"]},

    # ── collab 协作委托（新增维度）──
    {"id": "col_01", "dim": "collab", "q": "你更愿「单干」还是「组队」？", "probes": ["你 solo 过最牛的事？"]},
    {"id": "col_02", "dim": "collab", "q": "你把任务交给别人时，给多少自由度？", "probes": ["你 micromanage 吗？"]},
    {"id": "col_03", "dim": "collab", "q": "你更爱「明确分工」还是「灵活补位」？", "probes": ["你团队角色通常是？"]},
    {"id": "col_04", "dim": "collab", "q": "你委托时最担心对方「哪点做不到」？", "probes": ["你被坑过 delegation 吗？"]},
    {"id": "col_05", "dim": "collab", "q": "你更愿「手把手带」还是「给目标自驱」？", "probes": ["你带人风格？"]},
    {"id": "col_06", "dim": "collab", "q": "你对「甩锅/抢功」零容忍吗？", "probes": ["你遇过最恶心的一次？"]},
    {"id": "col_07", "dim": "collab", "q": "你更爱「书面对齐」还是「口头共识」？", "probes": ["你协作留痕习惯？"]},
    {"id": "col_08", "dim": "collab", "q": "你更愿「和能力强但难处的人」还是「好相处但平庸的人」合作？", "probes": ["你合作底线是？"]},
    {"id": "col_09", "dim": "collab", "q": "你委托分身时，希望它「先问」还是「先干再报」？", "probes": ["你授权风格是？"]},
    {"id": "col_10", "dim": "collab", "q": "你更爱「扁平沟通」还是「层级汇报」？", "probes": ["你组织偏好？"]},
    {"id": "col_11", "dim": "collab", "q": "你对「冲突」的处理——回避、正面刚、还是调和？", "probes": ["你最近一次冲突怎么收？"]},
    {"id": "col_12", "dim": "collab", "q": "你更愿「被人需要」还是「被信任能搞定」？", "probes": ["你协作里的核心需求？"]},
    {"id": "col_13", "dim": "collab", "q": "你 delegation 时会给「完整上下文」还是「只给任务」？", "probes": ["你布置活的方式？"]},
    {"id": "col_14", "dim": "collab", "q": "你更爱「小团队精干」还是「大团队分工」？", "probes": ["你带过几人？"]},
    {"id": "col_15", "dim": "collab", "q": "你对「进度同步」的频率要求？", "probes": ["你多久要一次汇报？"]},
    {"id": "col_16", "dim": "collab", "q": "你更愿「自己扛雷」还是「团队共担」？", "probes": ["你责任感边界？"]},
    {"id": "col_17", "dim": "collab", "q": "你对「拍马屁/职场政治」的态度？", "probes": ["你玩过政治吗？"]},
    {"id": "col_18", "dim": "collab", "q": "你更爱「结果导向的伙伴」还是「过程同频的伙伴」？", "probes": ["你要的是哪种搭档？"]},
    {"id": "col_19", "dim": "collab", "q": "你委托后「忍不住插手」吗？", "probes": ["你信任交付吗？"]},
    {"id": "col_20", "dim": "collab", "q": "你更愿「分身当执行者」还是「分身当军师」？", "probes": ["你要它什么定位？"]},
    {"id": "col_21", "dim": "collab", "q": "你对「跨文化/跨背景协作」适应度？", "probes": ["你协调过差异吗？"]},
    {"id": "col_22", "dim": "collab", "q": "你更爱「公开透明」还是「小范围知会」？", "probes": ["你信息共享风格？"]},
    {"id": "col_23", "dim": "collab", "q": "你 delegation 失败通常因为什么？", "probes": ["你复盘过甩锅吗？"]},
    {"id": "col_24", "dim": "collab", "q": "你更愿「培养人」还是「用成熟人」？", "probes": ["你带徒弟吗？"]},
    {"id": "col_25", "dim": "collab", "q": "你对「甩给AI的活」和「甩给人的活」区分标准？", "probes": ["什么你绝不交给AI？"]},
    {"id": "col_26", "dim": "collab", "q": "你更爱「共识驱动」还是「负责人拍板」？", "probes": ["你决策权分配？"]},
    {"id": "col_27", "dim": "collab", "q": "你 delegation 时会「给资源」还是「只给目标」？", "probes": ["你支持下属的方式？"]},
    {"id": "col_28", "dim": "collab", "q": "你更愿「和分身吵架式对齐」还是「它听话执行」？", "probes": ["你要的是应声虫还是对手？"]},
    {"id": "col_29", "dim": "collab", "q": "你对「协作中的情绪劳动」在意吗？", "probes": ["你消耗在人际上的精力？"]},
    {"id": "col_30", "dim": "collab", "q": "你更愿「长期固定搭档」还是「按需组队」？", "probes": ["你关系策略是？"]},

    # ── risk 风险偏好（新增维度）──
    {"id": "rsk_01", "dim": "risk", "q": "你给自己的「风险承受力」打几分（1-10）？", "probes": ["你定义的高风险是？"]},
    {"id": "rsk_02", "dim": "risk", "q": "你更爱「稳赚小钱」还是「博一把大的」？", "probes": ["你赌性在哪方面最强？"]},
    {"id": "rsk_03", "dim": "risk", "q": "你做冒险决定前会做「最坏打算」吗？", "probes": ["你的风控习惯？"]},
    {"id": "rsk_04", "dim": "risk", "q": "你对「未知/模糊」的耐受度？", "probes": ["你迷路会慌吗？"]},
    {"id": "rsk_05", "dim": "risk", "q": "你更怕「确定的小损失」还是「可能的大损失」？", "probes": ["你保险意识？"]},
    {"id": "rsk_06", "dim": "risk", "q": "你愿意为「高回报」承受多大回撤？", "probes": ["你亏多少会止损？"]},
    {"id": "rsk_07", "dim": "risk", "q": "你对「公开表达/曝光」的风险态度？", "probes": ["你敢公开站台吗？"]},
    {"id": "rsk_08", "dim": "risk", "q": "你更愿「试错迭代」还是「论证万全再动」？", "probes": ["你容错预算？"]},
    {"id": "rsk_09", "dim": "risk", "q": "你对「法律/合规」风险的敏感度？", "probes": ["你踩过合规红线吗？"]},
    {"id": "rsk_10", "dim": "risk", "q": "你更敢「借钱/杠杆」还是「只用闲钱」？", "probes": ["你杠杆经历？"]},
    {"id": "rsk_11", "dim": "risk", "q": "你对「健康风险」的容忍度？", "probes": ["你透支过身体吗？"]},
    {"id": "rsk_12", "dim": "risk", "q": "你更愿「承担已知风险」还是「避开未知风险」？", "probes": ["你风险认知风格？"]},
    {"id": "rsk_13", "dim": "risk", "q": "你对「声誉风险」的零容忍点？", "probes": ["你最怕被传什么？"]},
    {"id": "rsk_14", "dim": "risk", "q": "你更爱「分散下注」还是「集中押注」？", "probes": ["你组合思维？"]},
    {"id": "rsk_15", "dim": "risk", "q": "你对「技术债/捷径」的风险态度？", "probes": ["你还技术债吗？"]},
    {"id": "rsk_16", "dim": "risk", "q": "你更愿「先发制人」还是「等等看」？", "probes": ["你先动优势信仰？"]},
    {"id": "rsk_17", "dim": "risk", "q": "你对「被封号/被限流」这类平台风险在意吗？", "probes": ["你鸡蛋放几个篮？"]},
    {"id": "rsk_18", "dim": "risk", "q": "你更敢「公开反对主流」还是「随大流避险」？", "probes": ["你 controversial 过吗？"]},
    {"id": "rsk_19", "dim": "risk", "q": "你对「关系破裂」的风险态度？", "probes": ["你敢翻脸吗？"]},
    {"id": "rsk_20", "dim": "risk", "q": "你更愿「把鸡蛋放一个篮并看紧」还是「分散」？", "probes": ["你集中还是分散？"]},
    {"id": "rsk_21", "dim": "risk", "q": "你对「时间风险」（错过窗口）敏感吗？", "probes": ["你抢过时间点吗？"]},
    {"id": "rsk_22", "dim": "risk", "q": "你更爱「保守承诺」还是「激进承诺」？", "probes": ["你承诺超额还是保守？"]},
    {"id": "rsk_23", "dim": "risk", "q": "你对「黑天鹅」有预案吗？", "probes": ["你备过灾吗？"]},
    {"id": "rsk_24", "dim": "risk", "q": "你更愿「承受波动换收益」还是「要稳」？", "probes": ["你投资性格？"]},
    {"id": "rsk_25", "dim": "risk", "q": "你让分身「自主冒险决策」的授权边界在哪？", "probes": ["你给它多大试错权？"]},
]

ESSENTIAL_IDS = [q["id"] for q in QUESTION_BANK if q.get("essential")]
QB_BY_ID = {q["id"]: q for q in QUESTION_BANK}

# ── LLM 抽取系统提示（v5.8 绑定五维）──────────────────────────────
EXTRACT_SYSTEM = """你是元神的"绑定蒸馏器"。从用户提供的资料/回答中，抽取关于用户本人的、用于"把分身炼成用户数字克隆体"的结构化事实。
只输出 JSON 数组，每个元素: {"dim":<维度>,"field":<字段名 snake_case>,"value":<具体值，简短一句话>,"confidence":<0.4-0.95 浮点>}
维度只能是: interest(利益关切), decision(决策倾向), emotion(情感信号), value(价值观锚点), comm(沟通风格), knowledge(知识领域), workflow(工作流习惯), collab(协作委托偏好), risk(风险偏好)。
field 用简短英文或拼音，如 interest_rank / decision_tradeoff / emotion_dislike / value_principle / comm_density / knowledge_domains / workflow_rhythm / collab_delegate / risk_appetite。
重点抽取"能用来绑定用户"的信号：他在意什么、怎么决策、什么让他爽/反感、死守什么原则、怎么沟通、擅长什么领域、工作节奏、怎么授权、对风险的态度。
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


def _store_facts(facts, source: str, qid=None, subject_id: str = "self",
                 target_type: str = "self", authorization_status: str = "owner",
                 material_type: str = "", quality_score: float = 0,
                 material_id: int = 0):
    """把抽取到的结构化事实 upsert 进 user_model（同 dim+field 取更高置信）。
    v6.4 支持蒸馏他人：必须传入 subject_id / target_type / authorization_status。
    v0.64.43：material_id 记录事实来源素材，支撑按素材精确删除（用户主权/合规删除权）。
    """
    if not facts:
        return 0
    conn = get_db()
    n = 0
    now = datetime.now().isoformat()
    for f in facts:
        dim = f.get("dim")
        field = f.get("field")
        value = f.get("value")
        if dim in CAPABILITY_DIMS:
            # 宪法层 persona-only 契约：拒绝任何能力/工具维度注入，防止通过蒸馏偷加能力
            print(f"[distill] 拒绝非人格维度注入（疑似能力注入）: {dim}/{field}")
            continue
        if dim not in META_DIMS or not field or value in (None, ""):
            continue
        conf = max(0.3, min(0.97, float(f.get("confidence", 0.5))))
        val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        # 同一 subject + dim + field 去重升级
        ex = conn.execute(
            "SELECT id,confidence FROM user_model WHERE subject=? AND dim=? AND field=?",
            (subject_id, dim, field)).fetchone()
        if ex:
            if conf > ex["confidence"]:
                conn.execute(
                    "UPDATE user_model SET value=?,confidence=?,source=?,qid=?,updated_at=?,"
                    "target_type=?,authorization_status=?,material_type=?,quality_score=? WHERE id=?",
                    (val_str, conf, source, qid, now, target_type, authorization_status,
                     material_type, quality_score, ex["id"]))
        else:
            conn.execute(
                "INSERT INTO user_model (dim,field,value,confidence,source,qid,created_at,updated_at,"
                "subject,target_type,authorization_status,material_type,quality_score,material_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dim, field, val_str, conf, source, qid, now, now,
                 subject_id, target_type, authorization_status, material_type, quality_score,
                 material_id))
        n += 1
    conn.commit()
    conn.close()
    return n


def _dim_confidence(conn):
    rows = conn.execute("SELECT dim, AVG(confidence) a FROM user_model GROUP BY dim").fetchall()
    return {r["dim"]: round(r["a"], 2) for r in rows}


def _dim_confidence_for_subject(conn, subject_id: str = "self"):
    rows = conn.execute(
        "SELECT dim, AVG(confidence) a FROM user_model WHERE subject=? GROUP BY dim",
        (subject_id,)).fetchall()
    return {r["dim"]: round(r["a"], 2) for r in rows}


def _quality_score(text_len: int, fact_count: int, dim_coverage: set, confidence_avg: float) -> float:
    """素材质量分：0–1。综合长度、事实数、维度覆盖、平均置信度。
    短文本只要信息密度高（事实多、维度覆盖广、置信高）也能拿高分。"""
    if text_len < 10:
        return 0.0
    # 长度采用对数评分，避免长文灌水；1000 字基本满分
    len_score = min(1.0, max(0.0, (text_len - 10) / 1000))
    fact_score = min(1.0, fact_count / 6)
    dim_score = len(dim_coverage) / len(META_DIMS)
    conf_score = max(0, min(1, confidence_avg))
    return round(0.20 * len_score + 0.35 * fact_score + 0.25 * dim_score + 0.20 * conf_score, 2)


def _ensure_subject(subject_id: str, name: str, target_type: str = "self",
                    authorization_status: str = "owner", authorization_proof: str = ""):
    """确保 distill_subjects 中存在该 subject；不存在则插入。"""
    conn = get_db()
    now = datetime.now().isoformat()
    try:
        row = conn.execute("SELECT 1 FROM distill_subjects WHERE id=?", (subject_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO distill_subjects (id,name,target_type,authorization_status,"
                "authorization_proof,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (subject_id, name, target_type, authorization_status, authorization_proof, now, now))
            conn.execute(
                "INSERT INTO distill_authorizations (subject_id,action,proof,ts) VALUES (?,?,?,?)",
                (subject_id, "create", authorization_proof, now))
            conn.commit()
    finally:
        conn.close()


def _log_auth(subject_id: str, action: str, proof: str = ""):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO distill_authorizations (subject_id,action,proof,ts) VALUES (?,?,?,?)",
            (subject_id, action, proof, datetime.now().isoformat()))
        conn.execute(
            "UPDATE distill_subjects SET authorization_status=?, authorization_proof=?, updated_at=? WHERE id=?",
            (action, proof, datetime.now().isoformat(), subject_id))
        conn.commit()
    finally:
        conn.close()


def binding_progress(conn=None, subject_id: str = "self"):
    """绑定进度：五维各自是否已沉淀（有≥1条且置信均值），以及总进度百分比。
    用于前端"绑定进度"条——让用户直观看到"它越来越像我了"。
    v6.4 支持按 subject 查询（默认 self）。"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        rows = conn.execute(
            "SELECT dim, COUNT(*) n, AVG(confidence) a FROM user_model WHERE subject=? GROUP BY dim",
            (subject_id,)).fetchall()
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


def compile_meta_system(subject_id: str = "self") -> str:
    """把已蒸馏的「绑定画像」动态编译进元神 system prompt。
    无蒸馏数据 → 返回通用技术 PM 骨架（开箱即用）；有数据 → 叠加用户绑定五维。
    v6.4 支持选择 subject：self=用户本人，other=被蒸馏的他人（赛博人类）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT dim,field,value,confidence FROM user_model WHERE subject=? ORDER BY dim,confidence DESC",
        (subject_id,)).fetchall()
    sub = conn.execute(
        "SELECT name,target_type,authorization_status FROM distill_subjects WHERE id=?",
        (subject_id,)).fetchone()
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
    subject_name = sub["name"] if sub else ("你" if subject_id == "self" else subject_id)
    subject_pronoun = "你" if subject_id == "self" else subject_name
    auth_note = ""
    if sub and sub["target_type"] == "other":
        auth_note = f"\n注意：该克隆体基于被蒸馏人「{subject_name}」的授权素材炼制，使用时应尊重其授权范围与隐私。"
    return (META_SYSTEM +
            f"\n\n【以下是已绑定（蒸馏炼制）出的真实画像——利益关切 / 决策倾向 / 情感信号 / 价值观锚点 / 沟通风格。"
            f"对话时务必贴合，让元神「像{subject_pronoun}本人」，这是「{subject_pronoun}」的数字克隆体，不是通用 bot】\n"
            + persona + auth_note)


# ── 访谈引擎 ─────────────────────────────────────────────────────
def _build_reflection(conn, subject_id: str = "self"):
    rows = conn.execute(
        "SELECT dim,field,value,confidence FROM user_model WHERE subject=? ORDER BY dim,confidence DESC",
        (subject_id,)).fetchall()
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
    pronoun = "你" if subject_id == "self" else "TA"
    return (f"根据目前对{pronoun}的了解，元神的理解是——\n" + "\n".join(parts) +
            f"\n\n如果哪里理解得不对，直接告诉元神“其实{pronoun}是…”，它会立刻修正。"
            "你也可以在“画像”里看到每个维度的置信度，点一下就能让元神来补齐不了解的地方。")


def _next_question(subject_id: str = "self"):
    """返回下一个访谈问题（冷启动顺序 → 自适应补最弱维度 → 完成反思）。
    v6.4 支持按 subject 访谈。"""
    conn = get_db()
    st = conn.execute(
        "SELECT * FROM meta_interview WHERE subject=? ORDER BY id DESC LIMIT 1",
        (subject_id,)).fetchone()
    asked = set(json.loads(st["asked"]) if st and st["asked"] else [])
    reflection = _build_reflection(conn, subject_id) if (st and st["asked"]) else None
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
        dim_conf = _dim_confidence_for_subject(conn, subject_id)
        cands = [q for q in QUESTION_BANK if q["id"] not in asked]
        if cands:
            cands.sort(key=lambda q: dim_conf.get(q["dim"], 0.3))
            nxt = cands[0]
        else:
            # 3) 全部问完 → 反思镜像（超预期）
            reflection = _build_reflection(conn, subject_id)
            phase = "complete"

    conn.close()
    progress = len(asked)
    if nxt:
        return {"question": nxt["q"], "qid": nxt["id"], "dim": nxt["dim"],
                "probes": nxt.get("probes", []), "progress": progress,
                "total": len(QUESTION_BANK), "phase": phase, "reflection": reflection,
                "subject": subject_id}
    return {"question": None, "qid": None, "dim": None, "probes": [],
            "progress": progress, "total": len(QUESTION_BANK),
            "phase": "complete", "reflection": reflection, "subject": subject_id}


async def _extract_async(system: str, user: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: ask_llm_json(system, user))


# ── API：访谈 ────────────────────────────────────────────────────
@app.get("/api/meta/interview/next")
def interview_next(request: Request):
    subject_id = request.query_params.get("subject") or "self"
    return _next_question(subject_id)


@app.post("/api/meta/interview/answer")
async def interview_answer(req: Request):
    data = await req.json()
    qid = data.get("qid")
    answer = (data.get("answer") or "").strip()
    subject_id = (data.get("subject") or "self").strip() or "self"
    target_type = (data.get("target_type") or "self").strip() or "self"
    authorization_status = (data.get("authorization_status") or "owner").strip() or "owner"
    if not qid or not answer:
        return {"ok": False, "error": "missing qid/answer"}
    # 蒸馏他人时必须有授权
    if target_type == "other" and authorization_status not in ("authorized", "owner"):
        return {"ok": False, "error": "蒸馏他人需先获得被蒸馏人明示授权（authorized）"}
    conn = get_db()
    try:
        conn.execute("ALTER TABLE meta_interview ADD COLUMN audio_clips JSON DEFAULT '{}'")
        conn.commit()
    except Exception:
        pass
    st = conn.execute(
        "SELECT * FROM meta_interview WHERE subject=? ORDER BY id DESC LIMIT 1",
        (subject_id,)).fetchone()
    asked = json.loads(st["asked"]) if st and st["asked"] else []
    answers = json.loads(st["answers"]) if st and st["answers"] else {}
    audio_clips = {}
    if st and st.get("audio_clips"):
        try:
            audio_clips = json.loads(st["audio_clips"])
        except Exception:
            audio_clips = {}
    if qid not in asked:
        asked.append(qid)
    answers[qid] = answer
    ac = (data.get("audio_clip_id") or "").strip()
    if ac:
        audio_clips[qid] = ac
    askedj = json.dumps(asked, ensure_ascii=False)
    ansj = json.dumps(answers, ensure_ascii=False)
    acj = json.dumps(audio_clips, ensure_ascii=False)
    now = datetime.now().isoformat()
    if st:
        try:
            conn.execute(
                "UPDATE meta_interview SET asked=?,answers=?,audio_clips=?,last_ask_at=?,updated_at=? WHERE id=?",
                (askedj, ansj, acj, now, now, st["id"]))
        except Exception:
            conn.execute(
                "UPDATE meta_interview SET asked=?,answers=?,last_ask_at=?,updated_at=? WHERE id=?",
                (askedj, ansj, now, now, st["id"]))
    else:
        try:
            conn.execute(
                "INSERT INTO meta_interview (subject,asked,answers,audio_clips,last_ask_at,updated_at) VALUES (?,?,?,?,?,?)",
                (subject_id, askedj, ansj, acj, now, now))
        except Exception:
            conn.execute(
                "INSERT INTO meta_interview (subject,asked,answers,last_ask_at,updated_at) VALUES (?,?,?,?,?)",
                (subject_id, askedj, ansj, now, now))
    conn.commit()
    conn.close()
    # 确保 subject 记录存在
    _ensure_subject(subject_id, subject_id, target_type, authorization_status)
    # LLM 抽取人格事实（维度提示来自该题）
    q = QB_BY_ID.get(qid)
    dim_hint = q["dim"] if q else "personality"
    facts = await _extract_async(EXTRACT_SYSTEM + f"\n本次问题维度提示：{dim_hint}。",
                                 f"用户回答：{answer}")
    quality = _quality_score(len(answer), len(facts) if facts else 0,
                            set(f["dim"] for f in facts if f.get("dim") in META_DIMS) if facts else set(),
                            sum(float(f.get("confidence", 0.5)) for f in facts) / len(facts) if facts else 0)
    n = _store_facts(facts, "interview", qid, subject_id, target_type, authorization_status,
                     material_type="interview", quality_score=quality) if facts else 0
    return {"ok": True, "stored": n, "quality_score": quality,
            "next": _next_question(subject_id)}


# ── API：上传资料真摄取 ──────────────────────────────────────────
@app.post("/api/meta/ingest")
async def meta_ingest(req: Request):
    data = await req.json()
    text = (data.get("text") or "").strip()
    filename = data.get("filename", "")
    source = data.get("source", "upload")
    subject_id = (data.get("subject") or "self").strip() or "self"
    subject_name = (data.get("subject_name") or subject_id).strip() or subject_id
    target_type = (data.get("target_type") or "self").strip() or "self"
    authorization_status = (data.get("authorization_status") or "owner").strip() or "owner"
    authorization_proof = (data.get("authorization_proof") or "").strip()
    material_type = (data.get("material_type") or "document").strip() or "document"
    if material_type not in MATERIAL_TYPES:
        material_type = "document"
    if not text:
        return {"ok": False, "error": "empty text"}
    # v0.64.43：素材过短时抽取无意义，直接拒绝并说明，避免"上传成功但 0 条事实"的困惑
    if len(text) < _INGEST_MIN_LEN:
        return {"ok": False, "error": f"too short",
                "hint": f"素材内容不足 {_INGEST_MIN_LEN} 字，无法蒸馏出有效人格特征。请提供更完整的文本。",
                "min_len": _INGEST_MIN_LEN, "actual_len": len(text)}
    # 伦理护栏：蒸馏他人必须已授权
    if target_type == "other" and authorization_status not in ("authorized", "owner"):
        return {"ok": False, "error": "蒸馏他人需先获得被蒸馏人明示授权（authorized），未经授权禁止克隆"}
    _ensure_subject(subject_id, subject_name, target_type, authorization_status, authorization_proof)
    # 根据素材类型调整抽取提示
    type_hint = MATERIAL_TYPES.get(material_type, "工作文档")
    facts = await _extract_async(
        EXTRACT_SYSTEM + f"\n这是{type_hint}类型的素材，尽量多抽取真实事实，不确定不要编。",
        f"文件名：{filename}\n内容：\n{text[:8000]}")
    # v0.64.43：离线/未配置模型时 facts=None，必须明确告知，不能伪装成"成功但 0 条"
    if facts is None:
        return {"ok": False, "error": "llm_offline",
                "hint": "元神模型未配置或不可用，无法从素材中蒸馏人格特征。请先为元神配置大模型 API Key。",
                "material_type": material_type, "subject": subject_id}
    dims = set(f["dim"] for f in facts if f.get("dim") in META_DIMS) if facts else set()
    conf_avg = sum(float(f.get("confidence", 0.5)) for f in facts) / len(facts) if facts else 0
    quality = _quality_score(len(text), len(facts) if facts else 0, dims, conf_avg)
    now = datetime.now().isoformat()
    # 先落素材记录拿到 material_id，再让事实溯源到它（支撑按素材精确删除）。
    # 注意：必须先 commit 再调用 _store_facts——后者会另开连接写入，
    # 若此处持有未提交写事务，会因 SQLite 写锁导致 "database is locked"。
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO distill_materials (subject_id,material_type,filename,content_preview,"
        "extracted_facts,quality_score,ts) VALUES (?,?,?,?,?,?,?)",
        (subject_id, material_type, filename, text[:200], 0, quality, now))
    material_id = cur.lastrowid or 0
    conn.commit()
    conn.close()

    n = _store_facts(facts, "upload", subject_id=subject_id, target_type=target_type,
                     authorization_status=authorization_status, material_type=material_type,
                     quality_score=quality, material_id=material_id)

    conn = get_db()
    conn.execute("UPDATE distill_materials SET extracted_facts=? WHERE id=?", (n, material_id))
    if n:
        conn.execute(
            "INSERT INTO long_term_memory (category,content,source,ts) VALUES (?,?,?,?)",
            ("distilled", f"从资料《{filename}》蒸馏出 {n} 条人格事实", source, now))
    conn.commit()
    conn.close()
    return {"ok": True, "extracted": n, "quality_score": quality,
            "material_type": material_type, "subject": subject_id,
            "material_id": material_id,
            "summary": (facts[:5] if facts else [])}


# ── API：画像仪表盘 ──────────────────────────────────────────────
@app.get("/api/meta/profile")
def meta_profile(request: Request):
    subject_id = request.query_params.get("subject") or "self"
    conn = get_db()
    rows = conn.execute(
        "SELECT dim,field,value,confidence,source,material_type,quality_score FROM user_model "
        "WHERE subject=? ORDER BY dim,confidence DESC", (subject_id,)).fetchall()
    dim_conf = _dim_confidence_for_subject(conn, subject_id)
    reflection = _build_reflection(conn, subject_id)
    sub = conn.execute(
        "SELECT name,target_type,authorization_status FROM distill_subjects WHERE id=?",
        (subject_id,)).fetchone()
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
              "confidence": r["confidence"], "source": r["source"],
              "material_type": r["material_type"], "quality_score": r["quality_score"]}
             for r in rows if r["dim"] in META_DIMS]
    suff_dims = [d for d, s in dim_suff.items() if s["sufficient"]]
    bp = binding_progress(subject_id=subject_id)
    subject_name = sub["name"] if sub else ("你" if subject_id == "self" else subject_id)
    return {"narrative": reflection or f"元神还在了解{subject_name}，去“了解我”里回答几个问题，或上传聊天记录/工作笔记吧。",
            "facts": facts, "dim_confidence": dim_conf,
            "total": len(facts), "completeness": min(1.0, len(facts) / 20.0),
            "dim_sufficiency": dim_suff,
            "sufficient_dims": len(suff_dims), "total_dims": len(META_DIMS),
            "binding": bp, "subject": subject_id, "subject_name": subject_name,
            "target_type": sub["target_type"] if sub else "self",
            "authorization_status": sub["authorization_status"] if sub else "owner"}


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


# ── 被动蒸馏：元神闲聊时自动抽取人格事实（已节流，降低 LLM 调用）
async def _auto_distill_user(user_text: str):
    if not user_text:
        return
    if "?" in user_text or "？" in user_text:
        return
    if len(user_text) < _CHAT_EXTRACT_MIN_LEN:
        return
    global _LAST_CHAT_EXTRACT_TS
    now = time.time()
    if now - _LAST_CHAT_EXTRACT_TS < _CHAT_EXTRACT_COOLDOWN:
        return
    try:
        facts = await _extract_async(
            EXTRACT_SYSTEM + "\n这是用户和元神闲聊时说的话，只抽取明确的人格/偏好事实，不确定不要抽。",
            f"用户说：{user_text}")
        if facts:
            _store_facts(facts, "chat")
            _LAST_CHAT_EXTRACT_TS = now
    except Exception:
        pass


# ── v6.4 蒸馏二期：数字克隆体（subjects）管理 ──────────────────────
@app.get("/api/meta/distill/subjects")
def list_subjects():
    """列出所有数字克隆体：自己 + 已授权蒸馏的他人（赛博人类）。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id,name,target_type,authorization_status,created_at,updated_at FROM distill_subjects "
            "ORDER BY target_type, created_at DESC").fetchall()
        out = []
        for r in rows:
            bp = binding_progress(subject_id=r["id"])
            out.append({
                "id": r["id"], "name": r["name"], "target_type": r["target_type"],
                "authorization_status": r["authorization_status"],
                "binding_progress": bp["progress"], "bound_dims": bp["bound_dims"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            })
        return {"ok": True, "subjects": out}
    finally:
        conn.close()


@app.post("/api/meta/distill/subjects")
async def create_subject(req: Request):
    """创建新的蒸馏对象（他人）。自己 self 由系统默认创建，无需调用。"""
    data = await req.json()
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "缺少被蒸馏人名称"}
    sid = "sub_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + str(hash(name) % 10000)
    _ensure_subject(sid, name, target_type="other", authorization_status="pending")
    return {"ok": True, "subject": {"id": sid, "name": name, "authorization_status": "pending"}}


@app.post("/api/meta/distill/authorize")
async def authorize_subject(req: Request):
    """更新被蒸馏人授权状态：authorized / revoked / denied / pending。"""
    data = await req.json()
    sid = (data.get("subject") or "").strip()
    status = (data.get("status") or "").strip()
    proof = (data.get("proof") or "").strip()
    if not sid or status not in AUTH_STATUS:
        return {"ok": False, "error": "参数错误"}
    conn = get_db()
    try:
        row = conn.execute("SELECT 1 FROM distill_subjects WHERE id=?", (sid,)).fetchone()
        if not row:
            return {"ok": False, "error": "未找到该蒸馏对象"}
    finally:
        conn.close()
    _log_auth(sid, status, proof)
    # 如果撤销/拒绝，则把所有该 subject 的事实标记为失效（不删除，保留审计）
    if status in ("revoked", "denied"):
        conn = get_db()
        try:
            conn.execute(
                "UPDATE user_model SET authorization_status=? WHERE subject=?",
                (status, sid))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "subject": sid, "status": status}


@app.get("/api/meta/distill/materials")
def list_materials(request: Request):
    subject_id = request.query_params.get("subject") or "self"
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id,material_type,filename,extracted_facts,quality_score,ts FROM distill_materials "
            "WHERE subject_id=? ORDER BY ts DESC", (subject_id,)).fetchall()
        return {"ok": True, "subject": subject_id,
                "materials": [{"id": r["id"], "type": r["material_type"],
                               "type_label": MATERIAL_TYPES.get(r["material_type"], r["material_type"]),
                               "filename": r["filename"], "extracted_facts": r["extracted_facts"],
                               "quality_score": r["quality_score"], "ts": r["ts"]}
                              for r in rows]}
    finally:
        conn.close()


# ── API：删除单条素材（v0.64.43 用户主权）——连带删除它由蒸馏产生的人格事实 ──
@app.delete("/api/meta/distill/materials/{mid}")
def delete_material(mid: int, request: Request):
    subject_id = request.query_params.get("subject") or "self"
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id,filename FROM distill_materials WHERE id=? AND subject_id=?",
            (mid, subject_id)).fetchone()
        if not row:
            return {"ok": False, "error": "素材不存在或不属于该对象"}
        # 只删除"由该素材首次产生"的事实；被后续素材再次强化的事实会保留（避免误删）
        cur = conn.execute("DELETE FROM user_model WHERE material_id=? AND subject=?",
                           (mid, subject_id))
        conn.execute("DELETE FROM distill_materials WHERE id=?", (mid,))
        conn.commit()
        return {"ok": True, "deleted_material": mid, "deleted_facts": cur.rowcount or 0,
                "filename": row["filename"]}
    finally:
        conn.close()


# ── API：清空某对象的全部素材与人格事实（v0.64.43 一键删除权）──
@app.delete("/api/meta/distill/materials")
def clear_materials(request: Request):
    subject_id = request.query_params.get("subject") or "self"
    confirm = request.query_params.get("confirm")
    if confirm != "yes":
        return {"ok": False, "error": "清空需显式确认", "hint": "请带 confirm=yes 调用"}
    conn = get_db()
    try:
        m = conn.execute("DELETE FROM distill_materials WHERE subject_id=?", (subject_id,))
        f = conn.execute("DELETE FROM user_model WHERE subject=?", (subject_id,))
        conn.commit()
        return {"ok": True, "deleted_materials": m.rowcount or 0,
                "deleted_facts": f.rowcount or 0, "subject": subject_id}
    finally:
        conn.close()
