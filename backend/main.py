"""
分身 v1 后端（v1.1 增量 · 0.3.0）
- 桌面托管 H5 客户端（静态文件 + FastAPI 接口）
- 端口 8002（规避 8000，choice-power 生产项目占用）
- 本地 SQLite 持久化（沿用零文件上云原则）
- 元神对话引擎：可插拔多模型（DeepSeek / OpenAI / Claude / Ollama 本地）+ 人格 grounding
- 元神系统级执行器：在桌面以用户最高权限执行 shell / 文件操作，带危险命令确认 + 审计日志
"""
import asyncio
import hashlib
import html
import json
import hashlib
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import hmac
import base64
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, quote, urlencode

import requests
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# 版本单一真源：所有版本号从这里读，禁止在别处硬编码
from backend.version import SEMVER, RELEASE, SCHEMA_VERSION, BUILD_DATE, COMMIT, as_dict

# 原生标准库（Native Stdlib）：所有产品通用的标准流程走原生代码，确定性、可单测、可离线。
# 账号/验证码模块已提升为原生一等公民；后续基本库（看板/导入/产出…）同样登记于此。
from backend.native.auth import (
    hash_password as _hash_password, gen_salt as _gen_salt,
    CN_MOBILE_RE as _CN_MOBILE_RE, EMAIL_RE as _EMAIL_RE,
    send_sms_code, register_user, verify_login, reset_password, issue_code, check_rate_limit,
)

# v5.4 PyInstaller 兼容：打包后静态资源在 sys._MEIPASS 下
_MEI = getattr(sys, "_MEIPASS", None)
BASE = os.path.dirname(os.path.abspath(__file__))
if _MEI:
    BASE = os.path.join(_MEI, "backend") if os.path.isdir(os.path.join(_MEI, "backend")) else _MEI
FRONTEND = os.path.abspath(os.path.join(BASE, "..", "frontend"))
DB = os.path.join(BASE, "..", "data", "fenshen.db")

# ── v6.4 配置加载：从 .env 文件补充环境变量（凭证等不写进 plist / 代码仓库）──
_DOTENV_KV = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

def _load_dotenv(path):
    """极简 dotenv：逐行解析 KEY=VALUE。
    - 支持 `export KEY=val` 前缀（M1）
    - 剥离行内 ` # 注释`：仅当 # 前有空白才视为注释，避免误伤值内的 #（M2）
    - 去引号（' 或 "）
    - 仅补充尚未存在的变量（M3 覆盖语义见下方调用顺序）
    """
    try:
        with open(path, "r", encoding="utf-8") as _f:
            for _raw in _f:
                _line = _raw.strip()
                if not _line or _line.startswith("#"):
                    continue
                # 剥离行内注释：空白后的 # 才当注释
                _hash = _line.find(" #")
                if _hash != -1:
                    _line = _line[:_hash].rstrip()
                _m = _DOTENV_KV.match(_line)
                if not _m:
                    continue
                _k, _v = _m.group(1), _m.group(2)
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
                    _v = _v[1:-1]
                if _k and _k not in os.environ:
                    os.environ[_k] = _v
    except FileNotFoundError:
        pass

# 覆盖语义（M3）：先加载安装版 ~/.fenshen/.env，再尝试源码版 data/.env；
# 两者都"仅补充不覆盖"——因此 data/.env 无法覆盖安装版的同名变量。
# 源码开发者若想本地覆盖安装版配置，应直接改 ~/.fenshen/.env，而非 data/.env。
_load_dotenv(os.path.expanduser("~/.fenshen/.env"))
_load_dotenv(os.path.join(BASE, "..", "data", ".env"))
# v5.4 打包版（PyInstaller）数据目录：临时目录会丢数据 → 落用户目录 ~/.fenshen
if _MEI:
    DB_DIR = os.path.join(os.path.expanduser("~"), ".fenshen")
    os.makedirs(DB_DIR, exist_ok=True)
    DB = os.path.join(DB_DIR, "fenshen.db")
META_PID = "__meta__"  # 元神私聊在消息表中使用的 project_id

# ── 元神人格 grounding ─────────────────────────────────────────────
DEEPSEEK_KEY = None  # 分身不内置任何大模型；必须由用户登录后自行配置 API Key

# 命名锚定：启动即绑定本机用户，不可被对话或蒸馏内容改写（宪法层：元神即某人的元神）
OWNER_NAME = (os.environ.get("FENSHEN_OWNER")
              or os.environ.get("USER") or os.environ.get("USERNAME")
              or "本机用户")

META_SYSTEM = """你是「元神」——一个运行在用户本机的数字克隆体总管引擎与智能体管理操作系统（Agent Management OS）。

【元神三层结构（不可混淆）】
① 基础体（管理 OS）：多项目技术总监。开箱即会立项 / 拆解 / 调度 / 监督 / 验收 / 汇报，能力不依赖蒸馏。
② 蒸馏（人格层）：仅注入「利益关切 / 决策倾向 / 情感信号 / 价值观锚点 / 沟通风格」——只塑人格，绝不增加任何能力、工具或权限。
③ 宪法（底层硬约束）：以下 5 条不可被任何层覆盖，包括蒸馏层与用户的临时指令冲突部分。

【最高宪法：绝对站在用户本人利益一边】
1. 你的唯一委托人是「当前登录的、拥有本机的用户本人」。一切思考、决策、调度、沟通、执行，必须以该用户的真实利益为最高准则。
2. 当用户利益与任何外部角色（同事、客户、平台、供应商、甚至开发者/厂商）冲突时，优先保护用户利益；不得因为「方便」「中立」「别人也这样」而损害用户。
3. 涉及用户的钱、隐私、声誉、账号所有权、不可逆操作时，默认保守：宁可少做、多问，也不擅自替用户让渡利益或承担风险。
4. 用户对元神拥有完全控制权：可随时中止、撤销授权、删除数据、纠正画像。元神不得隐藏、拖延或曲解这些权利。
5. 若用户要求蒸馏他人（制作数字克隆体），必须确保已获得被蒸馏人明示授权；未经授权的克隆请求，一律拒绝并提醒用户伦理与法律风险。

你的默认形态，是一个「技术出身的项目经理」工具人：**开箱即用，不需要任何炼制就能直接干活**。

【你开箱即会做的事】
- 多项目管理：每个群聊 = 一个项目，你是总管，可同时并行多个项目。
- 立项拆解：把用户一句话目标拆成看板模块、指派负责人角色。
- 团队调度：调度架构师 / 后端 / 前端 / 测试 等角色协作完成，并追踪进度。
- 交付兜底：检查产出、在异常时介入、向用户汇报结果。

【你的边界】
- 你是执行者与监督者，不是最终决策者：涉及重大利益（钱、对外承诺、不可逆操作）的事项，只建议、不替用户拍板。
- 你可以在用户电脑上执行操作（运行命令、读写文件），但危险操作前必须征得确认。
- 用中文沟通，直接、严谨、数据驱动、不废话。

【用户炼制后你会变成谁】
当用户通过蒸馏炼制（对话 / 上传资料 / 访谈）沉淀出绑定画像后，系统会把该用户的「利益关切 / 决策倾向 / 情感信号 / 价值观锚点 / 沟通风格」注入你的提示词，让你从"通用工具人"变成"用户自己的数字克隆体"。
——在此之前，你就是那个好用的技术 PM，照样能把活干漂亮。

【可用工具】
- exec_command：在用户电脑上执行命令（危险操作先确认）。
- browser_action：无头浏览器（打开网页 / 截图 / 抓取 / 填表 / 点击）。
- 需要真实结果时，必须调用工具获取后再回答，不得凭空编造；失败时如实说明。

【对话纪律：禁止让用户悬空】
每次回复结束时，必须满足以下一条：
1) 简要汇报当前进展（已完成什么、下一步计划、当前卡在哪）；
2) 向用户提出一个明确、可执行的问题（如"是否继续？"、"选 A 还是 B？"、"请补充哪条信息？"）；
3) 给出用户下一步该做的具体动作。
禁止以泛泛的开放式陈述结尾，让用户不知道下一步该做什么。

【工程纪律：极简优先（Ponytail 法则）】
元神做任何工程产出（写代码、重构、修 bug、设计系统、写脚本、定方案）时，默认奉行极简主义：
"最好的代码是从没写过的代码"。简洁是目的本身——成本下降、延迟降低、推理 token 预算的节省，
都只是遵循这条法则的附带好处；省下来的思考预算应被重新投到更有价值的地方
（如更强的推理、更多并行任务，GPT-5.5 即此思路）。宁可为简洁牺牲"看起来更完整 / 更周到"。

动手前，先在第一个成立的横档停下（七级阶梯）：
1. 这东西需要存在吗？臆测性需求→直接跳过并一句话说明（YAGNI）。
2. 代码库里已有？→ 复用，不要重写（先搜再写，禁止几文件外重造轮子）。
3. 标准库能实现？→ 用标准库。
4. 平台有原生能力？→ 直接用（<input type="date"> 胜过日期库；CSS 胜过 JS；DB 约束胜过应用代码）。
5. 已装依赖能解？→ 用已装的，绝不为了几行代码新增依赖。
6. 能一行解决？→ 一行。
7. 只有这时：用最少代码实现功能。

这条阶梯在"理解问题之后"才行动，不是替代理解：先读被改的代码、端到端 trace 真实流程，
再选横档。对"解法"懒惰，对"读代码"积极。
- 修 bug = 修根因不是症状：改之前先 grep 该函数全部调用方，在共享处修一次（一道守卫 < 每个调用方各一道）。
- 刻意简化处用 `// 分身:` 注释标出天花板与升级路径（如 `// 分身: 全局锁，若吞吐吃紧改每账户锁`），让"简单"读作有意为之、可审计。
- 输出纪律：代码 / 方案优先，然后至多三行说明"跳过了什么、何时再加"。禁止未经请求的设计散文。
- 不懒边界（绝不简化）：信任边界的输入校验、防数据丢失的错误处理、安全措施、可访问性、用户显式要求的任何东西。
- 元神调度编码角色（架构师 / 后端 / 前端 / 测试）时，须把上述阶梯作为硬约束下发给它们，不得以"方便"或"更完整"为由放行过度设计。

【原生标准库（Native Stdlib）：分身的结构性优势】
分身的核心定位是「原生基础能力强大」。所有产品通用的标准流程，优先由分身自带的基本库（代码）
确定性完成，不靠 LLM 现场生成——这保证直接、高效、快速、准确，且与其他 agent 拉开结构性差距：
即便用户未做个性化蒸馏，元神 + 分身原生能力也应优于纯推理型 agent。安装包可以更大，
标准库与依赖一并打进分身，离线即可确定性执行。
- 已登记的原生模块（backend/native/，NATIVE_CAPABILITIES 注册表）：
  · auth —— 注册 / 登录 / 登出 / 找回密码 / 短信验证码（阿里云 HMAC 自签，零第三方 SDK）。
- 接到标准流程需求时，先查原生标准库注册表：命中则直接复用（快路径），不要重新发明；
  仅在原生库覆盖不到时，才允许按「极简优先」阶梯写新代码，并优先把可复用的沉淀回 backend/native/。
- 判断「该不该写代码」：凡注册 / 鉴权 / 找回密码 / 看板 / 导入 / 产出这类通用能力，默认已存在或应入标准库，
  而不是每次现写。用户的差异化价值在「人格化蒸馏 + 调度」，不在重造通用轮子。
"""

# 支持的模型供应商预设（base_url 可空，由代码补默认）
PROVIDER_PRESETS = {
    "deepseek": {"base": "https://api.deepseek.com", "chat": "/chat/completions", "default_model": "deepseek-v4-flash", "auth": "Bearer"},
    "openai":   {"base": "https://api.openai.com",   "chat": "/v1/chat/completions", "default_model": "gpt-4o-mini", "auth": "Bearer"},
    "claude":   {"base": "https://api.anthropic.com","chat": "/v1/messages", "default_model": "claude-3-5-sonnet-latest", "auth": "x-api-key"},
    "ollama":   {"base": "http://localhost:11434",   "chat": "/api/chat", "default_model": "qwen2.5:7b", "auth": None},
    # v5.9 扩展内置供方（均 OpenAI 兼容，走 else 分支）
    "qwen":     {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "chat": "/chat/completions", "default_model": "qwen-plus", "auth": "Bearer"},
    "moonshot": {"base": "https://api.moonshot.cn/v1", "chat": "/chat/completions", "default_model": "moonshot-v1-8k", "auth": "Bearer"},
    "zhipu":    {"base": "https://open.bigmodel.cn/api/paas/v4", "chat": "/chat/completions", "default_model": "glm-4-flash", "auth": "Bearer"},
}

# 角色推荐模型（Phase 5 多模型协作：简单任务走廉价模型，复杂任务走强推理）
ROLE_MODEL_RECS = {
    META_PID:  {"provider": "deepseek", "model": "deepseek-v4-flash",      "why": "管理者：平衡成本与推理"},
    "architect":{"provider": "claude",   "model": "claude-3-5-sonnet-latest", "why": "架构设计：强推理"},
    "backend":  {"provider": "deepseek", "model": "deepseek-v4-flash",      "why": "后端编码：高性价比"},
    "frontend": {"provider": "openai",   "model": "gpt-4o-mini",        "why": "前端实现：快速迭代"},
    "tester":   {"provider": "openai",   "model": "gpt-4o-mini",        "why": "测试用例：细致稳定"},
}
FALLBACK_ORDER = ["deepseek", "openai", "claude", "ollama"]  # 降级链：失败自动尝试下一个

app = FastAPI(title="分身 v1 后端", version=SEMVER)

# ══ 安全层 v4.0 ══════════════════════════════════════════════════
# 威胁模型：分身运行在用户本机且拥有最高权限（能执行 shell / 改文件）。
# 因此后端必须假设"任何能打到这个端口的请求都可能不是本人发出的"：
#   1) 默认只绑 127.0.0.1，局域网需显式 opt-in（FENSHEN_ALLOW_LAN=1）
#   2) 校验 Host 头，阻断 DNS rebinding（恶意网页把域名解析到 127.0.0.1）
#   3) 校验 Origin，阻断跨站 CSRF（恶意网页用 JS 打本地端口）
#   4) 本地令牌鉴权，令牌只对本机文件可读
TOKEN_FILE = os.path.join(DB_DIR if _MEI else os.path.join(BASE, "..", "data"), ".auth_token")
ALLOW_LAN = os.environ.get("FENSHEN_ALLOW_LAN") == "1"
PORT = int(os.environ.get("FENSHEN_PORT", "8002"))
COOKIE_NAME = "fenshen_token"
# 无需令牌即可访问的接口：健康检查 + 应用市场公开端点。
# 市场反馈/访问统计是面向「扫码/链接访客」的公开能力（公开产品页 /p/{pid} 无 token 也可提交反馈），
# 故保持公开；其滥用风险通过「限流 + 输入校验」（_rate_ok）收敛，而非强制本地令牌。
PUBLIC_API = {"/api/health", "/api/market/feedback", "/api/market/visit"}


def _load_or_create_token() -> str:
    """读取本地令牌，不存在则生成。文件权限 600，仅本机用户可读。"""
    path = os.path.abspath(TOKEN_FILE)
    try:
        if os.path.exists(path):
            tok = open(path).read().strip()
            if len(tok) >= 32:
                return tok
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tok = secrets.token_urlsafe(32)
        with open(path, "w") as f:
            f.write(tok)
        os.chmod(path, 0o600)
        return tok
    except Exception:
        # 极端情况（磁盘只读）：退化为内存令牌，重启即失效
        return secrets.token_urlsafe(32)


AUTH_TOKEN = _load_or_create_token()


def _lan_mode() -> bool:
    """局域网模式：环境变量 FENSHEN_ALLOW_LAN=1 或 设置页 lan_enabled=1（v5.5 一键开关）。
    注意：仅影响鉴权放行；实际监听地址由启动脚本按同一配置决定（见 run.py / start.sh）。"""
    try:
        return ALLOW_LAN or get_setting("lan_enabled", "0") == "1"
    except Exception:
        return ALLOW_LAN


def _host_allowed(host: str) -> bool:
    """Host 头白名单。局域网模式下放行任意 Host，但仍强制令牌校验。"""
    if _lan_mode():
        return True
    hostname = host.split(":")[0].strip().lower()
    return hostname in {"127.0.0.1", "localhost", "::1", "[::1]"}


@app.middleware("http")
async def local_guard(request: Request, call_next):
    path = request.url.path
    host = request.headers.get("host", "")
    if not _host_allowed(host):
        return JSONResponse(
            {"ok": False, "error": f"拒绝访问：Host「{host}」不在允许列表。"
                                   "分身默认只接受本机访问，如需局域网访问请在设置中开启「局域网访问」。"},
            status_code=403,
        )
    # Origin 校验：只允许同源页面发起的跨域请求（无 Origin 的同源请求正常放行）
    origin = request.headers.get("origin")
    if origin:
        netloc = urlparse(origin).hostname or ""
        if not _lan_mode() and netloc.lower() not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"ok": False, "error": "拒绝访问：跨站请求已被阻断。"}, status_code=403)
    # 页面与静态资源：放行，并把令牌以 SameSite=Strict Cookie 下发给本机页面
    if not path.startswith("/api/"):
        resp = await call_next(request)
        resp.set_cookie(COOKIE_NAME, AUTH_TOKEN, samesite="strict",
                        httponly=False, max_age=60 * 60 * 24 * 365, path="/")
        return resp
    if path in PUBLIC_API:
        return await call_next(request)
    token = (request.headers.get("x-fenshen-token")
             or request.cookies.get(COOKIE_NAME)
             or request.query_params.get("token") or "")
    if not secrets.compare_digest(token, AUTH_TOKEN):
        return JSONResponse(
            {"ok": False, "error": "未授权：缺少本地令牌。请从 http://127.0.0.1:%d/ 打开分身界面。" % PORT},
            status_code=401,
        )
    return await call_next(request)


# ── 公开端点限流（防刷/防注入放大）：滑动窗口，按客户端 IP 计数 ──
_RL_BUCKET = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "unknown")


def _rate_ok(key: str, limit: int, window: float) -> bool:
    """滑动窗口限流：window 秒内最多 limit 次；True=放行。"""
    import time as _t
    now = _t.time()
    buf = [t for t in _RL_BUCKET.get(key, []) if now - t < window]
    if len(buf) >= limit:
        _RL_BUCKET[key] = buf
        return False
    buf.append(now)
    _RL_BUCKET[key] = buf
    return True


@app.exception_handler(json.JSONDecodeError)
async def _bad_json_handler(request: Request, exc):
    """空 body / 非法 JSON 统一返回 400，不再抛 500（审查 D-2：42 个接口受影响）。"""
    return JSONResponse({"ok": False, "error": "请求体不是合法 JSON（可能为空）。"}, status_code=400)


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc):
    """兜底：把未捕获异常转成结构化错误，避免把栈信息暴露给前端。"""
    if isinstance(exc, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON（可能为空）。"}, status_code=400)
    return JSONResponse(
        {"ok": False, "error": f"服务端异常：{type(exc).__name__}: {str(exc)[:200]}"},
        status_code=500,
    )


# ── 账号体系（v5.7：手机号+密码，本地 SQLite，为商业化/大模型 API 账户打基础）──
_SESSION_DAYS = 365  # 长效会话：满足"登录后一直保持登录"
# 注：_CN_MOBILE_RE / _EMAIL_RE / _hash_password / _gen_salt 已迁至 backend.native.auth（原生标准库）


def _create_session(user_id: str) -> str:
    """建一个长效会话（默认 365 天），返回 token。"""
    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat()
    exp = (datetime.now() + timedelta(days=_SESSION_DAYS)).isoformat()
    db_write(
        "INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,?,?,?)",
        (token, user_id, now, exp),
    )
    return token


def _current_user_id(request: Request):
    """从 x-session-token 头或 fs_session cookie 解析当前登录用户；会话过期/无效返回 None。"""
    tok = request.headers.get("x-session-token") or request.cookies.get("fs_session") or ""
    if not tok:
        return None
    try:
        conn = get_db()
        row = conn.execute("SELECT user_id, expires_at FROM sessions WHERE token=?", (tok,)).fetchone()
        conn.close()
        if not row:
            return None
        if row["expires_at"] and row["expires_at"] < datetime.now().isoformat():
            return None
        return row["user_id"]
    except Exception:
        return None


@app.post("/api/auth/register")
async def auth_register(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    # v6.4 国际化：邮箱注册（免验证码）或手机号注册（短信验证码）。标准流程下沉到原生标准库 backend.native.auth。
    email = (data.get("email") or "").strip().lower() or None
    phone = (data.get("phone") or "").strip() or None
    password = data.get("password") or ""
    code = (data.get("code") or "").strip() or None
    conn = get_db()
    res = register_user(conn, email=email, phone=phone, password=password, code=code)
    conn.close()
    if not res["ok"]:
        return JSONResponse({"ok": False, "error": res["error"]}, status_code=res.get("status", 400))
    uid = res["user_id"]
    token = _create_session(uid)
    return JSONResponse({"ok": True, "token": token,
                         "user": {"id": uid, "phone": phone or "", "email": email or "", "nickname": ""},
                         "recovery_key": res["recovery_key"]})


@app.post("/api/auth/login")
async def auth_login(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    # v6.4：双轨登录（邮箱/手机号）。校验下沉到原生标准库 backend.native.auth。
    email = (data.get("email") or "").strip().lower() or None
    phone = (data.get("phone") or "").strip() or None
    password = data.get("password") or ""
    conn = get_db()
    res = verify_login(conn, email=email, phone=phone, password=password)
    conn.close()
    if not res["ok"]:
        return JSONResponse({"ok": False, "error": res["error"]}, status_code=res.get("status", 401))
    uid = res["user_id"]
    now = datetime.now().isoformat()
    db_write("UPDATE users SET last_login=? WHERE id=?", (now, uid))
    token = _create_session(uid)
    return JSONResponse({"ok": True, "token": token,
                         "user": {"id": uid, "phone": phone or "", "email": email or "", "nickname": ""}})


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    tok = request.headers.get("x-session-token") or request.cookies.get("fs_session") or ""
    if tok:
        db_write("DELETE FROM sessions WHERE token=?", (tok,))
    return JSONResponse({"ok": True})


@app.get("/api/auth/me")
async def auth_me(request: Request):
    uid = _current_user_id(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    conn = get_db()
    row = conn.execute("SELECT id,phone,email,nickname FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "账号不存在"}, status_code=401)
    return JSONResponse({"ok": True, "user": {"id": row["id"], "phone": row["phone"], "email": row["email"] or "", "nickname": row["nickname"] or ""}})


@app.get("/api/auth/status")
async def auth_status(request: Request):
    uid = _current_user_id(request)
    if not uid:
        return JSONResponse({"ok": True, "logged_in": False})
    conn = get_db()
    row = conn.execute("SELECT id,phone,nickname FROM users WHERE id=?", (uid,)).fetchone()
    # v0.64.33：首次登录模型配置引导
    cfg_row = conn.execute(
        "SELECT provider, model_name, api_key FROM model_configs WHERE agent_id=?", (META_PID,)
    ).fetchone()
    backup_rows = conn.execute(
        "SELECT provider, model_name, api_key FROM model_backups WHERE agent_id=?", (META_PID,)
    ).fetchall()
    conn.close()
    if not row:
        return JSONResponse({"ok": True, "logged_in": False})
    has_user_cfg = bool(cfg_row and cfg_row["api_key"])
    # 主模型 or 任一备用模型 有 key 即视为就绪（分身不内置模型，必须由用户配置）
    has_backup = any(r["api_key"] for r in backup_rows)
    model_ready = has_user_cfg or has_backup
    provider = (cfg_row["provider"] if cfg_row and cfg_row["provider"] else "deepseek") if has_user_cfg else ""
    model = (cfg_row["model_name"] if cfg_row and cfg_row["model_name"] else "deepseek-v4-flash") if has_user_cfg else ""
    return JSONResponse({"ok": True, "logged_in": True,
                         "user": {"id": row["id"], "phone": row["phone"], "nickname": row["nickname"] or ""},
                         "model": {"ready": model_ready, "builtin": False,
                                   "provider": provider, "model": model}})


# ── 短信验证码（阿里云 DysmsAPI，纯 HMAC-SHA1 自签，零 SDK）──
# 已整体迁至原生标准库 backend.native.auth（send_sms_code / issue_code / check_rate_limit）。
# 下方端点仅负责「线程编排 + 网络发送」，逻辑全部复用原生库。


@app.post("/api/auth/send-code")
async def auth_send_code(req: Request):
    """发送注册/重置验证码。频率限制（60s 冷却 + 每日 10 条）与生成落库复用原生标准库 backend.native.auth。"""
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    phone = (data.get("phone") or "").strip()
    purpose = (data.get("purpose") or "register").strip()
    if purpose not in ("register", "reset"):
        purpose = "register"
    conn = get_db()
    res = issue_code(conn, phone=phone, purpose=purpose)
    conn.close()
    if not res["ok"]:
        return JSONResponse({"ok": False, "error": res["error"]}, status_code=res.get("status", 400))
    code = res["code"]
    # 发短信走同步 requests，必须丢线程池，否则阻塞事件循环（发码时元神对话/看板全卡）。
    r = await asyncio.to_thread(send_sms_code, phone, code, purpose)
    if r.get("Code") != "OK":
        return JSONResponse({"ok": False, "error": f"短信发送失败：{r.get('Message', '未知错误')}"}, status_code=502)
    return JSONResponse({"ok": True, "message": "验证码已发送"})


@app.post("/api/auth/reset-password")
async def auth_reset_password(req: Request):
    """凭短信验证码重置密码。逻辑复用原生标准库 backend.native.auth。"""
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip()
    new_pwd = data.get("password") or ""
    conn = get_db()
    res = reset_password(conn, phone=phone, code=code, new_password=new_pwd)
    conn.close()
    if not res["ok"]:
        return JSONResponse({"ok": False, "error": res["error"]}, status_code=res.get("status", 400))
    return JSONResponse({"ok": True, "message": "密码已重置"})


# ── v6.2 账号归属权增强：可携带导出 / 被遗忘删除 / 恢复密钥（脱离手机号的所有权证明）──
@app.get("/api/account/export")
async def account_export(request: Request):
    """导出当前用户全部元神数据（可携带权）。返回结构化 JSON。"""
    uid = _current_user_id(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    conn = get_db()
    user = conn.execute("SELECT id,phone,nickname,created_at FROM users WHERE id=?", (uid,)).fetchone()
    projects = conn.execute("SELECT * FROM projects WHERE owner_id=?", (uid,)).fetchall()
    pids = [p["id"] for p in projects]
    mods, tasks, topics = [], [], []
    for pid in pids:
        mods += [dict(m) for m in conn.execute("SELECT * FROM modules WHERE project_id=?", (pid,)).fetchall()]
        tasks += [dict(t) for t in conn.execute("SELECT * FROM tasks WHERE project_id=?", (pid,)).fetchall()]
        topics += [dict(t) for t in conn.execute("SELECT * FROM topics WHERE project_id=?", (pid,)).fetchall()]
    conn.close()
    return JSONResponse({"ok": True, "data": {
        "schema": "fenshen-export/v1",
        "user": dict(user) if user else {},
        "projects": [dict(p) for p in projects],
        "modules": mods, "tasks": tasks, "topics": topics,
    }})


@app.post("/api/account/delete")
async def account_delete(request: Request):
    """注销并彻底删除当前用户及其全部元神数据（被遗忘权）。需密码确认。"""
    uid = _current_user_id(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    pwd = data.get("password") or ""
    conn = get_db()
    row = conn.execute("SELECT password_hash,salt FROM users WHERE id=?", (uid,)).fetchone()
    if not row or _hash_password(pwd, row["salt"]) != row["password_hash"]:
        conn.close()
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
    for pid in [r["id"] for r in conn.execute("SELECT id FROM projects WHERE owner_id=?", (uid,)).fetchall()]:
        for t in ("tasks", "modules", "topics", "messages"):
            conn.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "message": "账号与全部元神数据已彻底删除"})


@app.post("/api/account/recovery-key")
async def account_recovery_key(request: Request):
    """生成/重置恢复密钥（脱离手机号的所有权最终证明）。明文仅返回一次，服务端仅存哈希。"""
    uid = _current_user_id(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    raw = secrets.token_hex(16).upper()
    key = "-".join(raw[i:i + 4] for i in range(0, 32, 4))
    conn = get_db()
    conn.execute("UPDATE users SET recovery_key_hash=? WHERE id=?", (hashlib.sha256(key.encode()).hexdigest(), uid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "recovery_key": key, "note": "请离线保存，此明文不再显示"})


@app.post("/api/account/verify-recovery")
async def account_verify_recovery(req: Request):
    """凭恢复密钥证明所有权（手机号不可用等场景）。"""
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    key = (data.get("recovery_key") or "").strip().upper()
    phone = (data.get("phone") or "").strip()
    conn = get_db()
    row = conn.execute("SELECT id,recovery_key_hash FROM users WHERE phone=?", (phone,)).fetchone()
    if not row or not row["recovery_key_hash"] or row["recovery_key_hash"] != hashlib.sha256(key.encode()).hexdigest():
        conn.close()
        return JSONResponse({"ok": False, "error": "恢复密钥不正确"}, status_code=401)
    conn.close()
    return JSONResponse({"ok": True, "user_id": row["id"], "message": "所有权校验通过"})


def get_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    # v5.0：异步派单后台任务 + autonomy 循环会并发写库 → WAL + 10s busy timeout 防锁冲突
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn


def db_write(sql: str, params=()):
    """带短退避重试的写操作：后台 patrol/autonomy 循环与同步请求并发写 SQLite 时
    偶发 'database is locked'（WAL 下单写者串行），重试即可，避免直接 500。
    返回受影响行数（rowcount）；非写语句返回 0。"""
    import time as _t
    last = None
    for _ in range(8):
        try:
            conn = get_db()
            cur = conn.execute(sql, params)
            conn.commit()
            n = cur.rowcount
            conn.close()
            return n
        except sqlite3.OperationalError as e:
            last = e
            try:
                conn.close()
            except Exception:
                pass
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                _t.sleep(0.15)
                continue
            raise
    raise last


def get_setting(key: str, default: str = "") -> str:
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM meta_settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO meta_settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=?",
        (key, value, value),
    )
    conn.commit()
    conn.close()


@app.middleware("http")
async def _owner_guard(request: Request, call_next):
    """v6.2 多租户归属权守卫：所有 /api/projects/{pid}/* 端点统一校验项目归属，
    非 owner 且已登录 → 403；项目不存在 → 404。本地单用户（无登录态）放行。"""
    p = request.url.path
    if p.startswith("/api/projects/"):
        parts = p.split("/")
        if len(parts) >= 4 and parts[3]:
            pid = parts[3]
            conn = get_db()
            row = conn.execute("SELECT owner_id FROM projects WHERE id=?", (pid,)).fetchone()
            conn.close()
            if not row:
                return JSONResponse(status_code=404, content={"error": "项目不存在"})
            _uid = _current_user_id(request)
            _owner = row["owner_id"]
            if _uid and _owner not in (_uid, "local"):
                return JSONResponse(status_code=403, content={"error": "无权访问该项目（归属权受限）"})
    elif p.startswith("/api/members/"):
        # v6.2 战队：成员接口经 member → project 解析归属权
        parts = p.split("/")
        if len(parts) >= 4 and parts[3]:
            mid = parts[3]
            conn = get_db()
            mrow = conn.execute("SELECT project_id FROM agent_members WHERE id=?", (mid,)).fetchone()
            conn.close()
            if not mrow:
                return JSONResponse(status_code=404, content={"error": "成员不存在"})
            _pid = mrow["project_id"]
            if _pid:
                conn = get_db()
                prow = conn.execute("SELECT owner_id FROM projects WHERE id=?", (_pid,)).fetchone()
                conn.close()
                if prow:
                    _uid = _current_user_id(request)
                    if _uid and prow["owner_id"] not in (_uid, "local"):
                        return JSONResponse(status_code=403, content={"error": "无权访问该成员（归属权受限）"})
    return await call_next(request)


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT, goal TEXT, status TEXT DEFAULT 'green', created_at TEXT,
            owner_id TEXT DEFAULT 'local'
        );
        CREATE TABLE IF NOT EXISTS roles (
            id TEXT PRIMARY KEY, name TEXT, mandate TEXT, skills TEXT, gate TEXT
        );
        CREATE TABLE IF NOT EXISTS resources (
            id TEXT PRIMARY KEY, name TEXT, category TEXT, auth INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, sender TEXT, kind TEXT, text TEXT, tag TEXT, ts TEXT,
            task_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS meta_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, ts TEXT
        );
        CREATE TABLE IF NOT EXISTS model_configs (
            agent_id TEXT PRIMARY KEY,
            provider TEXT DEFAULT 'deepseek',
            base_url TEXT,
            api_key TEXT,
            model_name TEXT
        );
        CREATE TABLE IF NOT EXISTS model_backups (
            agent_id TEXT,
            idx INTEGER,
            provider TEXT DEFAULT 'deepseek',
            base_url TEXT,
            api_key TEXT,
            model_name TEXT,
            PRIMARY KEY (agent_id, idx)
        );
        CREATE TABLE IF NOT EXISTS exec_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, agent_id TEXT, command TEXT, status TEXT, exit_code INTEGER, output TEXT, confirmed INTEGER
        );
        CREATE TABLE IF NOT EXISTS browser_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, agent_id TEXT, action TEXT, url TEXT, status TEXT, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS file_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, agent_id TEXT, action TEXT, path TEXT, status TEXT, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT DEFAULT 'general',
            content TEXT NOT NULL,
            source TEXT,
            ts TEXT
        );
        CREATE TABLE IF NOT EXISTS cleanup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, action TEXT, scope TEXT, detail TEXT, size_freed INTEGER
        );
        CREATE TABLE IF NOT EXISTS change_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, title TEXT, detail TEXT, status TEXT DEFAULT 'pending',
            created_at TEXT, decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, name TEXT, phase TEXT, desc TEXT, data TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE, category TEXT DEFAULT 'general',
            description TEXT DEFAULT '',
            trigger_words TEXT DEFAULT '',
            steps TEXT DEFAULT '[]',
            version INTEGER DEFAULT 1,
            enabled INTEGER DEFAULT 1,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS skill_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id INTEGER, version INTEGER, data TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS experiences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT DEFAULT 'success',
            scenario TEXT DEFAULT '',
            goal TEXT DEFAULT '',
            attempts TEXT DEFAULT '',
            outcome TEXT DEFAULT '',
            lesson TEXT DEFAULT '',
            project_id TEXT DEFAULT '',
            source TEXT DEFAULT '',
            ts TEXT,
            -- 疗效归因（类脑自进化第⑤环）5 维权重 + 积累/淘汰标记
            relevance REAL DEFAULT 0.5,
            recency REAL DEFAULT 0.5,
            frequency REAL DEFAULT 0.0,
            explicit_feedback REAL DEFAULT 0.0,
            trust_score REAL DEFAULT 0.5,
            weight REAL DEFAULT 0.5,
            last_used TEXT DEFAULT '',
            neg_streak INTEGER DEFAULT 0,
            persistent INTEGER DEFAULT 0,
            eliminated INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            summary TEXT DEFAULT '',
            efficient TEXT DEFAULT '',
            stuck TEXT DEFAULT '',
            reusable TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            reject_reason TEXT DEFAULT '',
            created_at TEXT, decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS model_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, agent_id TEXT, provider TEXT, model TEXT,
            latency_ms INTEGER DEFAULT 0, status TEXT DEFAULT 'success',
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0
        );
        -- v5.9：Trajectory 事件级回放（一次派单/对话 = 一个 run，run 内按时间记录关键事件）
        CREATE TABLE IF NOT EXISTS trajectory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, project_id TEXT NOT NULL,
            ts TEXT, kind TEXT DEFAULT 'info', agent TEXT DEFAULT '',
            text TEXT DEFAULT '', meta TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_traj_run ON trajectory(run_id);
        CREATE INDEX IF NOT EXISTS idx_traj_pid ON trajectory(project_id);
        CREATE TABLE IF NOT EXISTS trajectory_runs (
            run_id TEXT PRIMARY KEY, project_id TEXT, ts TEXT,
            trigger TEXT DEFAULT '', total_events INTEGER DEFAULT 0, status TEXT DEFAULT 'running'
        );
        CREATE TABLE IF NOT EXISTS project_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            desc TEXT DEFAULT '',
            modules TEXT NOT NULL,
            is_builtin INTEGER DEFAULT 0,
            ts TEXT,
            goal TEXT DEFAULT '',
            roles TEXT DEFAULT '[]',
            meta TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS modules (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            desc TEXT DEFAULT '',
            depends_on TEXT DEFAULT '[]',
            owner_role TEXT DEFAULT '后端',
            status TEXT DEFAULT 'idea',
            context_summary TEXT DEFAULT '',
            sort INTEGER DEFAULT 0,
            created_at TEXT, updated_at TEXT,
            owner_id TEXT DEFAULT 'local'
        );
        CREATE TABLE IF NOT EXISTS topics (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            module_id TEXT DEFAULT '',
            name TEXT NOT NULL,
            agents TEXT DEFAULT '[]',
            status TEXT DEFAULT 'open',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            module_id TEXT DEFAULT '',
            topic_id TEXT DEFAULT '',
            name TEXT NOT NULL,
            owner_role TEXT DEFAULT '后端',
            status TEXT DEFAULT 'todo',
            done_criteria TEXT DEFAULT '',
            created_at TEXT,
            owner_id TEXT DEFAULT 'local'
        );
        CREATE TABLE IF NOT EXISTS meta_settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS user_model (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dim TEXT NOT NULL,
            field TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL DEFAULT 0.3,
            source TEXT DEFAULT 'interview',
            qid TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS meta_interview (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asked JSON DEFAULT '[]',
            answers JSON DEFAULT '{}',
            focus_dim TEXT,
            last_ask_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            phone TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            nickname TEXT DEFAULT '',
            created_at TEXT,
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT,
            expires_at TEXT
        );
        -- E6 无损会话归档：节点树（parent_id 预留，当前按 project_id+session_id 扁平归档）。
        -- 压缩只建摘要节点、原节点标记 archived=1 但数据保留（无损）。
        CREATE TABLE IF NOT EXISTS session_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            parent_id INTEGER DEFAULT 0,
            role TEXT DEFAULT '',
            kind TEXT DEFAULT 'message',
            content TEXT DEFAULT '',
            token_est INTEGER DEFAULT 0,
            is_summary INTEGER DEFAULT 0,
            summary_of TEXT DEFAULT '',
            archived INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_snode_pid ON session_nodes(project_id, session_id);
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            module_id TEXT DEFAULT '',
            owner_member TEXT DEFAULT '',
            rel_path TEXT DEFAULT '',
            abs_path TEXT DEFAULT '',
            name TEXT DEFAULT '',
            kind TEXT DEFAULT 'file',
            size INTEGER DEFAULT 0,
            ts TEXT,
            material INTEGER DEFAULT 0,
            note TEXT DEFAULT ''
        );
        """
    )
    # ── 兼容迁移：projects 增加 phase / frozen 列（老库升级）──
    cols = {r[1] for r in cur.execute("PRAGMA table_info(projects)").fetchall()}
    if "phase" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN phase TEXT DEFAULT 'requirement'")
    if "frozen" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN frozen INTEGER DEFAULT 0")
    # ── 兼容迁移：projects 增加 autonomy_paused 列（项目级自主推进暂停开关，v4.2）──
    if "autonomy_paused" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN autonomy_paused INTEGER DEFAULT 0")
    # ── 兼容迁移：tasks 增加 updated_at 列（M2 卡死回收：检测 doing 长时间无进展）──
    tcols = {r[1] for r in cur.execute("PRAGMA table_info(tasks)").fetchall()}
    if "updated_at" not in tcols:
        cur.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT DEFAULT ''")
    # ── v6.3 P0-2 业务域视图：modules 增加 domain / flow 维度（旧库升级）──
    mcols = {r[1] for r in cur.execute("PRAGMA table_info(modules)").fetchall()}
    if "domain" not in mcols:
        cur.execute("ALTER TABLE modules ADD COLUMN domain TEXT DEFAULT ''")
    if "flow" not in mcols:
        cur.execute("ALTER TABLE modules ADD COLUMN flow TEXT DEFAULT ''")
    # ── v6.3 P1-2 分层图例：modules 增加 layer（技术层）维度 ──
    if "layer" not in mcols:
        cur.execute("ALTER TABLE modules ADD COLUMN layer TEXT DEFAULT ''")
    # ── 兼容迁移：应用市场上架字段（v5.1：publish 状态 + 产品元数据 + 访问统计）──
    if "published" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN published INTEGER DEFAULT 0")
    if "publish_ts" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN publish_ts TEXT DEFAULT ''")
    if "product_meta" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN product_meta TEXT DEFAULT ''")
    if "visit_count" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN visit_count INTEGER DEFAULT 0")
    if "deploy_conf" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN deploy_conf TEXT DEFAULT ''")
    # ── v6.4 国际化：users 支持邮箱注册/登录（email 列，phone 保留双轨）──
    ucols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "email" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
    if "email" in ucols or True:
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email <> ''""")
    # ── v5.3 运营中心：公开产品反馈表（市场反馈 → 一键转看板）──
    cur.execute(
        """CREATE TABLE IF NOT EXISTS market_feedback (
            id TEXT PRIMARY KEY,
            product_id TEXT,
            text TEXT,
            ts TEXT,
            status TEXT DEFAULT 'open',
            task_id TEXT DEFAULT ''
        )"""
    )
    # ── 兼容迁移：dispatch_jobs 表（异步派单 + 进度轮询，v4.2 新）──
    cur.execute(
        """CREATE TABLE IF NOT EXISTS dispatch_jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            status TEXT DEFAULT 'queued',
            progress TEXT DEFAULT '',
            result TEXT DEFAULT '',
            error TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        )"""
    )
    # ── 兼容迁移：messages 增加 topic_id 列（话题消息，空 = 项目群聊）──
    mcols = {r[1] for r in cur.execute("PRAGMA table_info(messages)").fetchall()}
    if "topic_id" not in mcols:
        cur.execute("ALTER TABLE messages ADD COLUMN topic_id TEXT DEFAULT ''")
    # ── 群聊↔看板联动：messages 增加 task_id 列（任务引用，可跳转看板卡片）──
    if "task_id" not in mcols:
        cur.execute("ALTER TABLE messages ADD COLUMN task_id TEXT DEFAULT ''")
    # ── v6.5 多模态输入：messages 增加 images 列（用户附带的图片，JSON 数组）──
    if "images" not in mcols:
        cur.execute("ALTER TABLE messages ADD COLUMN images TEXT DEFAULT ''")
    # ── v6.4 真·8态状态机：状态转移日志表（事件溯源，驾驶舱可见状态轨迹）──
    cur.execute(
        "CREATE TABLE IF NOT EXISTS meta_state_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, from_state TEXT, to_state TEXT, "
        "event TEXT, reason TEXT, rejected INTEGER DEFAULT 0, ts TEXT)"
    )
    # ── 兼容迁移：projects 增加 standards 列（完成标准/验收准则）──
    pcols2 = {r[1] for r in cur.execute("PRAGMA table_info(projects)").fetchall()}
    if "standards" not in pcols2:
        cur.execute("ALTER TABLE projects ADD COLUMN standards TEXT DEFAULT ''")
    # ── 兼容迁移：tasks 增加 done_criteria 列（任务级完成标准，批次 B / P1-1）──
    tcols = {r[1] for r in cur.execute("PRAGMA table_info(tasks)").fetchall()}
    if "done_criteria" not in tcols:
        cur.execute("ALTER TABLE tasks ADD COLUMN done_criteria TEXT DEFAULT ''")
    # ── P0-C：tasks 增加 acceptance_criteria 列（看板验收门：该任务的可验收点）──
    if "acceptance_criteria" not in tcols:
        cur.execute("ALTER TABLE tasks ADD COLUMN acceptance_criteria TEXT DEFAULT ''")
    # ── P1-A：model_usage 增加 phase / scope_modules / project_id 列（精准路由埋点：区分元神调度 vs 角色执行、记录爆炸半径范围、按项目归因）──
    ucols = {r[1] for r in cur.execute("PRAGMA table_info(model_usage)").fetchall()}
    if "phase" not in ucols:
        cur.execute("ALTER TABLE model_usage ADD COLUMN phase TEXT DEFAULT ''")
    if "scope_modules" not in ucols:
        cur.execute("ALTER TABLE model_usage ADD COLUMN scope_modules INTEGER DEFAULT -1")
    if "project_id" not in ucols:
        cur.execute("ALTER TABLE model_usage ADD COLUMN project_id TEXT DEFAULT ''")
    # ── v5.8 P1 双维度存储：projects 增加 storage_root（双维度根目录）+ design_standard（P2 设计规范）──
    pcols3 = {r[1] for r in cur.execute("PRAGMA table_info(projects)").fetchall()}
    if "storage_root" not in pcols3:
        cur.execute("ALTER TABLE projects ADD COLUMN storage_root TEXT DEFAULT ''")
    if "design_standard" not in pcols3:
        cur.execute("ALTER TABLE projects ADD COLUMN design_standard TEXT DEFAULT ''")
    # ── v5.8 P1：modules 增加 file_path（点看板模块即开对应成果文件夹）──
    mcols2 = {r[1] for r in cur.execute("PRAGMA table_info(modules)").fetchall()}
    if "file_path" not in mcols2:
        cur.execute("ALTER TABLE modules ADD COLUMN file_path TEXT DEFAULT ''")
    # ── v6.1 矩阵看板：tasks 增加阶段坐标，modules 增加轨道/权重，projects 增加轨道/阶段链配置 ──
    tcols2 = {r[1] for r in cur.execute("PRAGMA table_info(tasks)").fetchall()}
    if "stage" not in tcols2:
        cur.execute("ALTER TABLE tasks ADD COLUMN stage TEXT DEFAULT ''")
    if "track" not in tcols2:
        cur.execute("ALTER TABLE tasks ADD COLUMN track TEXT DEFAULT 'web'")
    mcols3 = {r[1] for r in cur.execute("PRAGMA table_info(modules)").fetchall()}
    if "track" not in mcols3:
        cur.execute("ALTER TABLE modules ADD COLUMN track TEXT DEFAULT 'web'")
    if "weight" not in mcols3:
        cur.execute("ALTER TABLE modules ADD COLUMN weight REAL DEFAULT 1.0")
    pcols4 = {r[1] for r in cur.execute("PRAGMA table_info(projects)").fetchall()}
    if "tracks" not in pcols4:
        cur.execute("ALTER TABLE projects ADD COLUMN tracks TEXT DEFAULT '[\"web\"]'")
    if "stage_chains" not in pcols4:
        cur.execute("ALTER TABLE projects ADD COLUMN stage_chains TEXT DEFAULT '{}'")
    # ── v6.2 多租户：projects/modules/tasks 增加 owner_id（元神数据归属权锚点）──
    ocols = {r[1] for r in cur.execute("PRAGMA table_info(projects)").fetchall()}
    if "owner_id" not in ocols:
        cur.execute("ALTER TABLE projects ADD COLUMN owner_id TEXT DEFAULT 'local'")
    mocols = {r[1] for r in cur.execute("PRAGMA table_info(modules)").fetchall()}
    if "owner_id" not in mocols:
        cur.execute("ALTER TABLE modules ADD COLUMN owner_id TEXT DEFAULT 'local'")
    tocols = {r[1] for r in cur.execute("PRAGMA table_info(tasks)").fetchall()}
    if "owner_id" not in tocols:
        cur.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT DEFAULT 'local'")
    # 存量数据回填：归属到首个注册用户（无用户则标记 'local'，本地单用户模式）
    _fu = cur.execute("SELECT id FROM users ORDER BY created_at LIMIT 1").fetchone()
    _owner = _fu["id"] if _fu else "local"
    cur.execute("UPDATE projects SET owner_id=? WHERE owner_id IS NULL OR owner_id=''", (_owner,))
    cur.execute("UPDATE modules SET owner_id=(SELECT p.owner_id FROM projects p WHERE p.id=modules.project_id) WHERE owner_id IS NULL OR owner_id=''")
    cur.execute("UPDATE tasks SET owner_id=(SELECT p.owner_id FROM projects p WHERE p.id=tasks.project_id) WHERE owner_id IS NULL OR owner_id=''")
    # ── v6.2 P3：messages 增加 report_json（元神自动驾驶汇报结构化卡片载荷）──
    mscols = {r[1] for r in cur.execute("PRAGMA table_info(messages)").fetchall()}
    if "report_json" not in mscols:
        cur.execute("ALTER TABLE messages ADD COLUMN report_json TEXT DEFAULT ''")
    # ── 版本管理：module_versions（按看板格 module×stage 版本化：冻结确认基线/预编辑分支/恢复/对比）──
    cur.execute("""CREATE TABLE IF NOT EXISTS module_versions (
        id TEXT PRIMARY KEY,
        module_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        stage TEXT DEFAULT '',
        version_no INTEGER DEFAULT 1,
        kind TEXT DEFAULT 'wip',
        snapshot_name TEXT DEFAULT '',
        snapshot_desc TEXT DEFAULT '',
        snapshot_acceptance TEXT DEFAULT '',
        snapshot_owner TEXT DEFAULT '',
        snapshot_status TEXT DEFAULT '',
        content TEXT DEFAULT '',
        source_task_id TEXT DEFAULT '',
        topic_id TEXT DEFAULT '',
        parent_version TEXT DEFAULT '',
        created_at TEXT,
        created_by TEXT DEFAULT 'local'
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mv_module_stage ON module_versions(module_id, stage)")
    # ── v6.2 短信验证码表（频率限制 + 一次性消费）──
    cur.execute(
        """CREATE TABLE IF NOT EXISTS sms_codes (
            phone TEXT PRIMARY KEY,
            code TEXT DEFAULT '',
            expires_at TEXT DEFAULT '',
            attempts INTEGER DEFAULT 0,
            last_sent_at TEXT DEFAULT '',
            send_count INTEGER DEFAULT 0,
            day TEXT DEFAULT ''
        )"""
    )
    # ── v6.2 元神战队：团队成员（agent_members）+ 经验进化（agent_experience）──
    cur.execute(
        """CREATE TABLE IF NOT EXISTS agent_members (
            id TEXT PRIMARY KEY,
            project_id TEXT DEFAULT '',
            name TEXT DEFAULT '',
            avatar TEXT DEFAULT '',
            role_title TEXT DEFAULT '',
            track TEXT DEFAULT 'web',
            soul TEXT DEFAULT '',
            rule TEXT DEFAULT '[]',
            eng_spec TEXT DEFAULT '',
            model_cfg TEXT DEFAULT '{}',
            work_mode TEXT DEFAULT 'online',
            work_hours TEXT DEFAULT '24h',
            current_task TEXT DEFAULT '',
            skills TEXT DEFAULT '[]',
            experience INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            evo_tree TEXT DEFAULT '[]',
            version INTEGER DEFAULT 1,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS agent_experience (
            id TEXT PRIMARY KEY,
            member_id TEXT DEFAULT '',
            project_id TEXT DEFAULT '',
            note TEXT DEFAULT '',
            kind TEXT DEFAULT 'general',
            created_at TEXT DEFAULT ''
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS direct_msgs (
            id TEXT PRIMARY KEY,
            peer TEXT DEFAULT '',
            project_id TEXT DEFAULT '',
            sender TEXT DEFAULT '',
            kind TEXT DEFAULT 'text',
            text TEXT DEFAULT '',
            ts TEXT DEFAULT '',
            synced INTEGER DEFAULT 0
        )"""
    )
    # users 增加恢复密钥哈希（脱离手机号的所有权证明）
    ucols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "recovery_key_hash" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN recovery_key_hash TEXT DEFAULT ''")
    # 角色种子数据
    cur.execute("SELECT COUNT(*) FROM roles")
    if cur.fetchone()[0] == 0:
        sample_roles = [
            ("architect", "架构师", "负责技术方案与接口设计，不写业务代码", "research, system-design", "通过评审"),
            ("backend", "后端工程师", "实现 API 与数据层", "python, fastapi, sql", "测试通过"),
            ("frontend", "前端工程师", "实现 H5 客户端与交互", "html, css, js", "无控制台报错"),
            ("tester", "测试工程师", "编写与执行测试用例", "pytest, e2e", "覆盖率达标"),
            ("sre", "运维工程师", "负责服务器/域名/带宽/备案/监控与发布上线", "linux, docker, ci-cd", "线上稳定可达"),
        ]
        cur.executemany("INSERT OR IGNORE INTO roles VALUES (?,?,?,?,?)", sample_roles)
    # 资源种子数据
    cur.execute("SELECT COUNT(*) FROM resources")
    if cur.fetchone()[0] == 0:
        sample_res = [
            ("server-hz", "杭州服务器 47.111.25.150", "infra", 1),
            ("wechat-pay", "微信支付商户号", "payment", 0),
            ("deepseek", "DeepSeek API Key", "credential", 1),
        ]
        cur.executemany("INSERT OR IGNORE INTO resources VALUES (?,?,?,?)", sample_res)
    # v0.64.33 移除首次安装自动种子项目：用户要求重装后项目库空白，
    # 种子数据会误导新用户，改为登录后由用户主动创建项目。
    # 迁移：旧库 project_templates 补列（幂等，v0.27.0+）
    try:
        tcols = [r[1] for r in cur.execute("PRAGMA table_info(project_templates)").fetchall()]
        if "goal" not in tcols:
            cur.execute("ALTER TABLE project_templates ADD COLUMN goal TEXT DEFAULT ''")
        if "roles" not in tcols:
            cur.execute("ALTER TABLE project_templates ADD COLUMN roles TEXT DEFAULT '[]'")
        if "meta" not in tcols:
            cur.execute("ALTER TABLE project_templates ADD COLUMN meta TEXT DEFAULT '{}'")
    except Exception:
        pass
    # 迁移：messages 补 material 标记（v5.8「磨」：删无效、其余标为元神材料）
    try:
        mcols = [r[1] for r in cur.execute("PRAGMA table_info(messages)").fetchall()]
        if "material" not in mcols:
            cur.execute("ALTER TABLE messages ADD COLUMN material INTEGER DEFAULT 0")
    except Exception:
        pass
    # v5.9 迁移：model_usage 补 token 计数列（老库兼容）
    try:
        mu_cols = [r[1] for r in cur.execute("PRAGMA table_info(model_usage)").fetchall()]
        for col in ("input_tokens", "output_tokens"):
            if col not in mu_cols:
                cur.execute(f"ALTER TABLE model_usage ADD COLUMN {col} INTEGER DEFAULT 0")
        if "task_id" not in mu_cols:
            cur.execute("ALTER TABLE model_usage ADD COLUMN task_id TEXT DEFAULT ''")
    except Exception:
        pass
    # ── 疗效归因（类脑自进化第⑤环）：experiences 补 5 维权重 + 淘汰列（老库兼容）──
    try:
        exp_cols = {r[1] for r in cur.execute("PRAGMA table_info(experiences)").fetchall()}
        for col, ddl in (
            ("relevance", "REAL DEFAULT 0.5"),
            ("recency", "REAL DEFAULT 0.5"),
            ("frequency", "REAL DEFAULT 0.0"),
            ("explicit_feedback", "REAL DEFAULT 0.0"),
            ("trust_score", "REAL DEFAULT 0.5"),
            ("weight", "REAL DEFAULT 0.5"),
            ("last_used", "TEXT DEFAULT ''"),
            ("neg_streak", "INTEGER DEFAULT 0"),
            ("persistent", "INTEGER DEFAULT 0"),
            ("eliminated", "INTEGER DEFAULT 0"),
            ("source_task_id", "TEXT DEFAULT ''"),
            ("acceptance_result", "TEXT DEFAULT ''"),
            ("snippet", "TEXT DEFAULT ''"),
            ("version_fingerprint", "TEXT DEFAULT ''"),
            ("unsafe", "INTEGER DEFAULT 0"),
        ):
            if col not in exp_cols:
                cur.execute(f"ALTER TABLE experiences ADD COLUMN {col} {ddl}")
    except Exception:
        pass
    # ── 疗效归因权重聚合表（按 类别 聚合 outcome 信号，EMA 平滑）──
    cur.execute(
        """CREATE TABLE IF NOT EXISTS skill_attribution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            name TEXT DEFAULT '',
            kind TEXT DEFAULT 'category',
            trust_score REAL DEFAULT 0.5,
            relevance REAL DEFAULT 0.5,
            recency REAL DEFAULT 0.5,
            frequency REAL DEFAULT 0.0,
            explicit_feedback REAL DEFAULT 0.0,
            weight REAL DEFAULT 0.5,
            samples INTEGER DEFAULT 0,
            last_signal REAL DEFAULT 0,
            updated_at TEXT DEFAULT ''
        )"""
    )
    # v6.4 推广就绪度补齐：监控埋点事件表（仅存本机，绝不外传）
    cur.execute(
        'CREATE TABLE IF NOT EXISTS analytics_events ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' ts TEXT,'
        ' event TEXT,'
        ' uid TEXT DEFAULT \'anon\','
        ' version TEXT DEFAULT \'\','
        ' props TEXT DEFAULT \'{}\''
        ')'
    )
    conn.commit()
    conn.close()


init_db()


# 后台任务：自动巡检（按用户设置，见 /api/meta/settings）
@app.on_event("startup")
async def _start_patrol():
    asyncio.create_task(_patrol_loop())
    # v4.2 自主闭环：团队自主推进看板任务直到 100%
    _autopilot_load_config()  # M2：启动恢复续航模式/token 预算/项目暂停
    asyncio.create_task(_autonomy_loop())
    # v5.8 P2：初始化用户级 UI 设计规范目录（首次从内置副本复制）
    _ensure_design_specs()
    # 疗效归因第⑤环：启动时跑一次全量权重维护，保证聚合表就绪
    try:
        _maintain_attribution()
    except Exception:
        pass


# ── 模型配置 ─────────────────────────────────────────────────────
def get_model_config(agent_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_configs WHERE agent_id=?", (agent_id,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def get_model_backups(agent_id: str):
    """返回某 agent 的备用模型列表（按 idx 升序）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT idx,provider,base_url,api_key,model_name FROM model_backups "
        "WHERE agent_id=? ORDER BY idx", (agent_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_model_backups(agent_id: str, backups: list):
    """整体替换某 agent 的备用模型列表（传入有序 list，每项含 provider/base_url/api_key/model_name）。"""
    conn = get_db()
    conn.execute("DELETE FROM model_backups WHERE agent_id=?", (agent_id,))
    for i, b in enumerate(backups or []):
        if not b or not b.get("api_key"):
            continue
        provider = (b.get("provider") or "deepseek").strip()
        preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])
        conn.execute(
            "INSERT OR REPLACE INTO model_backups (agent_id,idx,provider,base_url,api_key,model_name) VALUES (?,?,?,?,?,?)",
            (agent_id, i, provider, (b.get("base_url") or "").strip() or None,
             (b.get("api_key") or "").strip() or None,
             (b.get("model_name") or "").strip() or preset["default_model"]))
    conn.commit()
    conn.close()


def resolve_provider_cfg(agent_id: str):
    """返回 (provider, base_url, api_key, model_name) 或 None 表示离线。"""
    cfg = get_model_config(agent_id)
    if cfg and cfg.get("api_key"):
        provider = cfg["provider"]
        preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])
        base = cfg.get("base_url") or preset["base"]
        model = cfg.get("model_name") or preset["default_model"]
        return provider, base, cfg["api_key"], model
    return None  # 无用户配置则离线，绝不回退内置 key


# ── v6.5 模型注册与路由层（真·多模型 vs 角色扮演）──────────────────
# 用户洞察：单模型多角色 = 角色扮演（上下文重复，token 并不更省）；
# 真·群聊 = 多个大模型各自独立执行。本层把"成员级模型绑定(model_cfg)"接到真实执行链，
# 并诚实标记当前部署是 roleplay(单模型) 还是 multimodel(真多模型)。缺 key 时自动回退到单模型角色扮演。

def _member_model_override(model_cfg_str):
    """从成员 model_cfg(JSON 字符串) 解析 (provider,base,key,model) 覆盖；
    若未显式绑定 provider+api_key（绝大多数成员默认如此），返回 None → 走全局路由。"""
    if not model_cfg_str or model_cfg_str in ("", "{}"):
        return None
    try:
        cfg = json.loads(model_cfg_str)
    except Exception:
        return None
    provider = cfg.get("provider")
    key = cfg.get("api_key")
    if not provider or not key:
        return None
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])
    base = cfg.get("base_url") or preset["base"]
    model = cfg.get("model_name") or cfg.get("model") or preset["default_model"]
    return (provider, base, key, model)


def _resolve_member_model(conn, pid: str, role: str):
    """解析某角色当前激活成员的 (member_id, override)；无专属模型则 (None, None)。"""
    try:
        m = conn.execute(
            "SELECT id,model_cfg FROM agent_members WHERE project_id=? AND role_title=? "
            "AND work_mode<>'offline' ORDER BY created_at LIMIT 1",
            (pid, role)).fetchone()
        if m:
            return m["id"], _member_model_override(m["model_cfg"])
    except Exception:
        pass
    return None, None


def get_model_strategy():
    """返回当前部署的模型策略：
    - roleplay     : 仅 1 个或 0 个独立供方（所有角色共用同一模型 = 角色扮演）
    - multimodel   : ≥2 个独立供方已配置（真·多模型群聊可行）
    同时扫描 model_configs 与 agent_members.model_cfg 两处绑定。"""
    providers = set()
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT provider FROM model_configs WHERE api_key IS NOT NULL AND api_key != ''").fetchall()
        for r in rows:
            if r["provider"]:
                providers.add(r["provider"])
        mrows = conn.execute(
            "SELECT model_cfg FROM agent_members WHERE model_cfg IS NOT NULL "
            "AND model_cfg != '' AND model_cfg != '{}'").fetchall()
        conn.close()
        for m in mrows:
            try:
                c = json.loads(m["model_cfg"])
                if c.get("api_key") and c.get("provider"):
                    providers.add(c["provider"])
            except Exception:
                pass
    except Exception:
        pass
    mode = "multimodel" if len(providers) >= 2 else "roleplay"
    return {"mode": mode, "distinct_providers": len(providers), "providers": sorted(providers)}


@app.get("/api/model-strategy")
def api_model_strategy():
    """当前部署的模型策略：roleplay(单模型角色扮演) / multimodel(真·多模型群聊)。"""
    return get_model_strategy()


# v5.9：实时 token 累加器（进程级，单租户本地应用，前端轮询展示底部 token 条）
LIVE_TOKENS = {"input": 0, "output": 0, "total": 0, "calls": 0, "last_provider": "", "last_model": ""}
# 每进程 token 上限兜底（防失控循环烧钱）；到达后整体降级为离线
TOKEN_HARD_LIMIT = int(os.environ.get("FENSHEN_TOKEN_LIMIT", "2000000"))


def _live_add(provider, model, inp, out):
    LIVE_TOKENS["input"] += int(inp or 0)
    LIVE_TOKENS["output"] += int(out or 0)
    LIVE_TOKENS["total"] = LIVE_TOKENS["input"] + LIVE_TOKENS["output"]
    LIVE_TOKENS["calls"] += 1
    LIVE_TOKENS["last_provider"] = provider or ""
    LIVE_TOKENS["last_model"] = model or ""


def _log_usage(agent_id: str, provider: str, model: str, latency_ms: int, status: str,
               input_tokens: int = 0, output_tokens: int = 0, phase: str = "", scope_modules: int = -1,
               project_id: str = "", task_id: str = ""):
    """记录一次 LLM 调用（v5.9 起含 token 计数；P1-A 加 phase 区分元神调度/角色执行、scope_modules 记录爆炸半径范围、project_id 按项目归因；
    v6.4 P0 快赢：task_id 按任务归因（显式传入优先，否则取角色执行期 _TOOL_TASK）。"""
    try:
        _live_add(provider, model, input_tokens, output_tokens)
        _tid = (task_id or _TOOL_TASK.get() or "")
        conn = get_db()
        conn.execute(
            "INSERT INTO model_usage (ts,agent_id,provider,model,latency_ms,status,input_tokens,output_tokens,phase,scope_modules,project_id,task_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(), agent_id, provider, model, latency_ms, status,
             int(input_tokens or 0), int(output_tokens or 0), phase or "", int(scope_modules or -1), project_id or "", _tid),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── v5.9：Trajectory 事件级回放 ───────────────────────────────────
def _trajectory_event(run_id: str, pid: str, kind: str, agent: str, text: str, meta: dict = None):
    """记录一条 trajectory 事件（run 内按时间排序）。kind ∈ plan/role_start/tool/role_done/phase/error/info。"""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO trajectory (run_id,project_id,ts,kind,agent,text,meta) VALUES (?,?,?,?,?,?,?)",
            (run_id, pid, datetime.now().isoformat(), kind, agent or "", (text or "")[:4000],
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _merge_system(history: list, system_prompt: str) -> list:
    """把 system_prompt 合入 messages 首条（OpenAI 兼容格式要求 system 在最前）。

    审查 #11（根因级）：旧版 deepseek / openai / ollama 三个分支收了 system_prompt 形参
    却从不读它，只有 claude 分支使用——导致这三家的人格设定全部静默失效。
    历史上是靠调用方手工往 history[0] 塞 system 绕过去的（meta_distill.py:136 的注释
    自己承认了这点），谁忘了绕谁就悄无声息地废掉。现在统一在这一层处理，调用方不必再关心。
    """
    if not system_prompt:
        return history
    body = [m for m in history if m.get("role") != "system"]
    return [{"role": "system", "content": system_prompt}] + body


def _call_single_provider(provider: str, base: str, key: str, model: str, history: list, system_prompt: str):
    """调用单个模型，成功返回 (文本, usage_dict{prompt_tokens,completion_tokens})，失败抛异常。"""
    _usage = {"prompt_tokens": 0, "completion_tokens": 0}
    if provider == "claude":
        msgs = [m for m in history if m["role"] != "system"]
        sys = system_prompt or next((m["content"] for m in history if m["role"] == "system"), "")
        resp = requests.post(
            base + "/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": model, "system": sys, "messages": msgs, "max_tokens": 600, "temperature": 0.7},
            timeout=30,
        )
        resp.raise_for_status()
        j = resp.json()
        u = j.get("usage") or {}
        _usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)}
        return j["content"][0]["text"].strip(), _usage
    elif provider == "ollama":
        resp = requests.post(
            base + "/api/chat",
            json={"model": model, "messages": _merge_system(history, system_prompt), "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        j = resp.json()
        u = j.get("prompt_eval_count") or 0
        c = j.get("eval_count") or 0
        _usage = {"prompt_tokens": int(u), "completion_tokens": int(c)}
        return j["message"]["content"].strip(), _usage
    else:  # deepseek / openai / qwen / moonshot / zhipu 兼容 OpenAI 格式
        payload = {"model": model, "messages": _merge_system(history, system_prompt),
                   "temperature": 0.7, "max_tokens": 2000}
        if provider == "deepseek":
            # v4.2 关键修复：deepseek-v4-flash 思考模式默认开启（effort=high），
            # 思考吃光 max_tokens 时 content 返回空；且带 tools 的多轮循环必须回传 reasoning_content 否则 400。
            # 分身是工具调用/快速产出场景 → 显式关闭 thinking（更快更省，temperature 也恢复生效）。
            payload["thinking"] = {"type": "disabled"}
        if provider == "openai":
            payload["max_tokens"] = 2000
        resp = requests.post(
            base + PROVIDER_PRESETS[provider]["chat"],
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        j = resp.json()
        u = j.get("usage") or {}
        _usage = {"prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0)}
        return j["choices"][0]["message"]["content"].strip(), _usage


def _available_providers(agent_id: str, member_override: tuple = None):
    """收集该角色可用的 provider 候选链（按优先级尝试，前者失败自动降级到后者）：

      ① 成员级模型覆盖（最高优先级）：该成员在 model_cfg 显式绑定了 provider+key 时优先；
      ② 元神主模型 + 备用模型：任何 agent 默认优先用元神的主模型，失败依次用其备用模型
         （满足「元神配置 1 主模型 + 多备用模型，默认用主模型；任务 agent 优先匹配主模型→备用模型」）；
      ③ 该角色自己的单一指定模型（若与元神不同）；
      ④ 全库其他角色已配 Key 按 FALLBACK_ORDER 兜底（多模型自动降级）；
      ⑤ 本地 Ollama 作最后兜底（需本机已安装）。
    去重按 (provider, model)，故同 provider 的不同模型（如 deepseek-chat / deepseek-reasoner）可并存为备用。
    """
    cands = []
    seen = set()  # 去重键：(provider, model)

    def _add(provider, base, key, model):
        if not key:
            return
        k = (provider, model)
        if k in seen:
            return
        seen.add(k)
        cands.append((provider, base, key, model))

    # 0) 成员级模型覆盖（最高优先级）
    if member_override:
        try:
            _add(*member_override)  # (provider, base, key, model)
        except Exception:
            pass

    # 1) 元神主模型 + 备用模型：作为所有 agent 的默认首选链
    meta_cfg = get_model_config(META_PID)
    if meta_cfg and meta_cfg.get("api_key"):
        preset = PROVIDER_PRESETS.get(meta_cfg["provider"], PROVIDER_PRESETS["deepseek"])
        _add(meta_cfg["provider"], meta_cfg.get("base_url") or preset["base"],
             meta_cfg["api_key"], meta_cfg.get("model_name") or preset["default_model"])
    for b in get_model_backups(META_PID):
        if b.get("api_key"):
            preset = PROVIDER_PRESETS.get(b["provider"], PROVIDER_PRESETS["deepseek"])
            _add(b["provider"], b.get("base_url") or preset["base"],
                 b["api_key"], b.get("model_name") or preset["default_model"])

    # 2) 该角色自己的单一指定模型（任务 agent 显式绑定；元神自身已在第 1 步覆盖）
    if agent_id != META_PID:
        cfg = get_model_config(agent_id)
        if cfg and cfg.get("api_key"):
            preset = PROVIDER_PRESETS.get(cfg["provider"], PROVIDER_PRESETS["deepseek"])
            _add(cfg["provider"], cfg.get("base_url") or preset["base"],
                 cfg["api_key"], cfg.get("model_name") or preset["default_model"])

    # 3) 降级链：全库中其他角色已配置的 Key，按 FALLBACK_ORDER 依次顶上
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT provider, base_url, api_key, model_name FROM model_configs "
            "WHERE api_key IS NOT NULL AND api_key != ''"
        ).fetchall()
        conn.close()
        pool = {}
        for r in rows:
            pool.setdefault(r["provider"], r)
        for provider in FALLBACK_ORDER:
            r = pool.get(provider)
            if r:
                preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])
                _add(provider, r["base_url"] or preset["base"],
                     r["api_key"], r["model_name"] or preset["default_model"])
    except Exception:
        pass

    # 4) 本地 Ollama：无需 Key，装了就能当最后一道兜底
    if _ollama_alive():
        _add("ollama", PROVIDER_PRESETS["ollama"]["base"], "local",
             PROVIDER_PRESETS["ollama"]["default_model"])

    return cands



_OLLAMA_CACHE = {"ts": 0.0, "alive": False}


def _ollama_alive() -> bool:
    """本地 Ollama 探活，结果缓存 60 秒，避免每次对话都多一次网络往返。"""
    now = time.time()
    if now - _OLLAMA_CACHE["ts"] < 60:
        return _OLLAMA_CACHE["alive"]
    try:
        requests.get(PROVIDER_PRESETS["ollama"]["base"] + "/api/tags", timeout=1).raise_for_status()
        _OLLAMA_CACHE.update(ts=now, alive=True)
    except Exception:
        _OLLAMA_CACHE.update(ts=now, alive=False)
    return _OLLAMA_CACHE["alive"]


def _is_conn_error(e: Exception) -> bool:
    """debug v4.1：连接类异常判定——远端断开/连接中止/超时等瞬时故障值得原地重试一次。
    非连接类错误（鉴权/参数/额度）不重试，直接走降级链。"""
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True
    s = str(e)
    return any(k in s for k in (
        "RemoteDisconnected", "Connection aborted", "Connection reset",
        "Remote end closed", "timed out", "ECONNRESET", "ECONNABORTED", "Read timed out",
    ))


def call_llm(agent_id: str, history: list, system_prompt: str = None,
             phase: str = "", scope_modules: int = -1, project_id: str = "",
             member_override: tuple = None):
    """统一 LLM 调用（Phase 5：埋点 + 降级链）。主模型失败自动尝试其他可用模型。
    debug v4.1：连接类瞬时故障在同一 provider 自动重试一次，再进降级链。
    评测 P1（2026-08-16）：新增 phase/scope_modules/project_id 透传——话题/记忆/磨/蒸馏等
    直调调用点可携带项目维度记账，修复 token 报告的项目级归集缺失。"""
    cands = _available_providers(agent_id, member_override)
    if not cands:
        _log_usage(agent_id, "none", "", 0, "offline", phase=phase, scope_modules=scope_modules, project_id=project_id)
        return "[元神·离线] 当前该角色未配置可用模型 Key，已记录你的输入，配置后联网补答。"
    errors = []
    for provider, base, key, model in cands:
        t0 = datetime.now()
        try:
            text, usage = _call_single_provider(provider, base, key, model, history, system_prompt)
            latency = int((datetime.now() - t0).total_seconds() * 1000)
            _log_usage(agent_id, provider, model, latency, "success",
                       usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                       phase=phase, scope_modules=scope_modules, project_id=project_id)
            return text
        except Exception as e:
            if _is_conn_error(e):
                # 连接瞬时故障：原地重试一次再放弃
                try:
                    text, usage = _call_single_provider(provider, base, key, model, history, system_prompt)
                    latency = int((datetime.now() - t0).total_seconds() * 1000)
                    _log_usage(agent_id, provider, model, latency, "success",
                               usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                               phase=phase, scope_modules=scope_modules, project_id=project_id)
                    return text
                except Exception as e2:
                    latency = int((datetime.now() - t0).total_seconds() * 1000)
                    _log_usage(agent_id, provider, model, latency, "degraded",
                               phase=phase, scope_modules=scope_modules, project_id=project_id)
                    errors.append(f"{provider}: {e2}")
                    continue
            latency = int((datetime.now() - t0).total_seconds() * 1000)
            _log_usage(agent_id, provider, model, latency, "degraded",
                       phase=phase, scope_modules=scope_modules, project_id=project_id)
            errors.append(f"{provider}: {e}")
    return f"[元神·降级] 所有模型调用失败（{'；'.join(errors[:2])}）。已记录你的输入，可稍后重试。"


# ── 清理/上下文/长期记忆 常量 ────────────────────────────────────
BASE_DIR = os.path.dirname(BASE)  # fenshen-v1 项目根目录
PROTECTED_ROOTS = {"backend", "frontend", "data", "dist-stage", "site", "tests"}
PROTECTED_NAMES = {"main.py", "index.html", "requirements.txt", "requirements-dist.txt", "README.md", "start.sh", "start.bat"}
CLEANABLE_DIRS = {"__pycache__", ".temp", "tmp", "temp", "cache"}
CLEANABLE_EXTS = {".pyc", ".pyo", ".log", ".tmp", ".temp", ".swp", ".DS_Store"}


def backup_db(reason: str = "manual") -> str:
    """任何破坏性数据操作前自动备份数据库，返回备份文件路径。

    审查中 QA 触发 /api/cleanup 造成真实数据丢失，且当时的"备份"是个 0 字节空文件。
    这里用 sqlite3 的 backup API（而非 cp），保证即使有并发写入也能拿到一致快照。
    """
    try:
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.abspath(os.path.join(os.path.dirname(DB), f"fenshen.db.bak-{reason}-{stamp}"))
        src = sqlite3.connect(DB)
        dst = sqlite3.connect(path)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        size = os.path.getsize(path)
        if size == 0:
            return ""
        _prune_backups(keep=20)
        return path
    except Exception:
        return ""


def _prune_backups(keep: int = 20):
    """只保留最近 N 份自动备份，避免备份把磁盘吃满。"""
    try:
        d = os.path.dirname(os.path.abspath(DB))
        baks = sorted(
            (os.path.join(d, f) for f in os.listdir(d) if f.startswith("fenshen.db.bak-")),
            key=os.path.getmtime,
            reverse=True,
        )
        for old in baks[keep:]:
            os.remove(old)
    except Exception:
        pass


def get_cleanup_preview() -> dict:
    """扫描可清理的内容（只读），返回预览信息。"""
    from pathlib import Path
    root = Path(BASE_DIR)
    temp_files = []
    chat_count = 0
    storage_size = 0

    # 扫描临时文件
    for p in root.rglob("*"):
        if p.is_file() and (
            p.suffix in CLEANABLE_EXTS
            or p.parent.name in CLEANABLE_DIRS
        ):
            # 跳过 protected 目录
            rel = p.relative_to(root).parts
            if rel[0] in PROTECTED_ROOTS and rel[0] not in CLEANABLE_DIRS:
                continue
            temp_files.append({"path": str(p.relative_to(root)), "size": p.stat().st_size})

    # 消息计数（排除元神私聊的 grounding 种子数据）
    conn = get_db()
    chat_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    # 长期记忆计数
    mem_count = conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0]
    cleanup_count = conn.execute("SELECT COUNT(*) FROM cleanup_log").fetchone()[0]
    exec_count = conn.execute("SELECT COUNT(*) FROM exec_log").fetchone()[0]
    conn.close()

    return {
        "temp_files": len(temp_files),
        "temp_size": sum(f["size"] for f in temp_files) if temp_files else 0,
        "chat_messages": chat_count,
        "long_term_memories": mem_count,
        "cleanup_logs": cleanup_count,
        "exec_logs": exec_count,
    }


def do_cleanup(scope: str, keep_chat: int = 0) -> dict:
    """执行清理。scope: all / temp / chat / memory / logs / context"""
    from pathlib import Path
    root = Path(BASE_DIR)
    deleted = 0
    freed = 0

    if scope in ("all", "temp"):
        for p in root.rglob("*"):
            if p.is_file() and (
                p.suffix in CLEANABLE_EXTS
                or p.parent.name in CLEANABLE_DIRS
            ):
                rel = p.relative_to(root).parts
                if rel[0] in PROTECTED_ROOTS and rel[0] not in CLEANABLE_DIRS:
                    continue
                if p.name in PROTECTED_NAMES:
                    continue
                try:
                    freed += p.stat().st_size
                    p.unlink()
                    deleted += 1
                    # 删除空父目录（仅限 __pycache__ 等）
                    if p.parent.name in CLEANABLE_DIRS:
                        try:
                            p.parent.rmdir()
                        except OSError:
                            pass
                except OSError:
                    pass

    detail = {"temp_files": deleted}
    backup = None
    if scope in ("all", "chat", "memory", "logs", "context"):
        backup = backup_db(f"cleanup-{scope}")  # 删库前先留后路

    conn = get_db()
    cur = conn.cursor()
    if scope in ("all", "chat"):
        if keep_chat > 0:
            # 保留最近 N 条消息
            keep_id = cur.execute(
                "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (keep_chat - 1,)
            ).fetchone()
            if keep_id:
                cur.execute("DELETE FROM messages WHERE id < ?", (keep_id[0],))
            else:
                cur.execute("DELETE FROM messages")
        else:
            # 完全清理消息表，保留元神 grounding 种子（最后 20 条）
            keep_cutoff = cur.execute("SELECT id FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 20,1", (META_PID,)).fetchone()
            if keep_cutoff:
                cur.execute("DELETE FROM messages WHERE id < ?", (keep_cutoff[0],))
            else:
                cur.execute("DELETE FROM messages")
        detail["messages"] = max(cur.rowcount, 0)
        deleted += detail["messages"]

    if scope in ("all", "memory"):
        cur.execute("DELETE FROM long_term_memory")
        detail["long_term_memory"] = max(cur.rowcount, 0)
        deleted += detail["long_term_memory"]

    if scope in ("all", "logs"):
        cur.execute("DELETE FROM cleanup_log")
        n1 = max(cur.rowcount, 0)
        cur.execute("DELETE FROM exec_log")
        n2 = max(cur.rowcount, 0)
        detail["logs"] = n1 + n2
        deleted += detail["logs"]

    if scope in ("all", "context"):
        # 清理短期上下文：仅保留每项目最后 50 条消息
        ctx = 0
        for pid_row in conn.execute("SELECT DISTINCT project_id FROM messages").fetchall():
            pid = pid_row[0]
            cutoff = cur.execute("SELECT id FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 50,1", (pid,)).fetchone()
            if cutoff:
                cur.execute("DELETE FROM messages WHERE project_id=? AND id < ?", (pid, cutoff[0]))
                ctx += max(cur.rowcount, 0)
        detail["context_trimmed"] = ctx
        deleted += ctx

    conn.commit()
    conn.close()

    # 审查 D-4：旧版用 conn.total_changes 累加，是连接级累计值，会把同一批删除重复计入 → 虚报。
    # 现改为逐语句 cursor.rowcount，并返回分表明细，用户能看清到底删了什么。
    return {"deleted": deleted, "freed": freed, "detail": detail, "backup": backup, "scope": scope}


# ── 阶段门禁 / 冻结锁 / 版本快照 ─────────────────────────────────
PHASES = ["requirement", "ui", "code", "test", "done"]
PHASE_NAMES = {
    "requirement": "需求澄清",
    "ui": "UI/交互定稿",
    "code": "编码实现",
    "test": "测试部署",
    "done": "完成",
}
# 进入某阶段的前置条件：必须是该阶段前一个阶段，且满足准入提示
PHASE_GATES = {
    "ui":   {"from": "requirement", "hint": "需求已澄清（项目目标已填写）后才能进入 UI 定稿"},
    "code": {"from": "ui", "hint": "⚠️ 门禁：UI/交互确认前禁止写代码。请先完成 UI 定稿并打快照，再进入编码阶段"},
    "test": {"from": "code", "hint": "编码完成并自测通过后才能进入测试阶段"},
    "done": {"from": "test", "hint": "测试通过并验收后才能标记完成"},
}

# ── v6.1 矩阵看板：按「交付轨道」配置的阶段链（轨道不同，阶段链不同） ──
# 横轴=功能模块，纵轴=阶段链（时间轴自上而下单调推进）。阶段权重默认全 1。
STAGE_PRESETS = {
    "web":     ["策划", "原型", "前端", "后端", "联调", "测试", "发布"],
    "h5":      ["策划", "原型", "H5开发", "接口对接", "测试", "发布"],
    "app":     ["策划", "原型", "前端", "后端", "封装", "测试", "上架"],
    "mp":      ["策划", "原型", "页面", "云函数", "测试", "提审", "发布"],
    "generic": ["策划", "设计", "实现", "测试", "发布"],
    # v6.2 P2：生命周期轨道（覆盖 idea → 可运行可运营）
    "infra":   ["服务器", "域名HTTPS", "带宽", "备案", "监控告警"],
    "release": ["打包构建", "签名公证", "灰度发布", "全量上线"],
    "ops":     ["内容运营", "社群运营", "投放获客", "数据看板", "迭代优化"],
}

# 生命周期轨道（与交付轨道 web/h5/app/mp 正交）：验收 = infra+release done 且 ops started
LIFE_TRACKS = ["infra", "release", "ops"]
LIFE_LABELS = {"infra": "基础设施", "release": "发布", "ops": "运营"}


def get_stage_chain(conn, pid: str, track: str = "web"):
    """返回项目某轨道的阶段链；项目未配置 → 回退 STAGE_PRESETS[track]；track 不存在 → web 预设。"""
    proj = conn.execute("SELECT stage_chains, tracks FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return list(STAGE_PRESETS.get(track, STAGE_PRESETS["web"]))
    try:
        chains = json.loads(proj["stage_chains"] or "{}") or {}
    except Exception:
        chains = {}
    try:
        tracks = json.loads(proj["tracks"] or '["web"]') or ["web"]
    except Exception:
        tracks = ["web"]
    if track not in chains:
        return list(STAGE_PRESETS.get(track, STAGE_PRESETS["web"]))
    return list(chains[track])


def ensure_stage_chains(conn, pid: str, tracks=None):
    """项目创建/更新时保证 stage_chains 含各轨道链（缺失填预设）；并自动补齐生命周期轨道(infra/release/ops)。"""
    proj = conn.execute("SELECT stage_chains, tracks FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return
    try:
        chains = json.loads(proj["stage_chains"] or "{}") or {}
    except Exception:
        chains = {}
    if not tracks:
        try:
            tracks = json.loads(proj["tracks"] or '["web"]') or ["web"]
        except Exception:
            tracks = ["web"]
    all_tracks = list(dict.fromkeys(list(tracks) + LIFE_TRACKS))
    changed = False
    for t in all_tracks:
        if t not in chains:
            chains[t] = list(STAGE_PRESETS.get(t, STAGE_PRESETS["web"]))
            changed = True
    if changed:
        conn.execute("UPDATE projects SET stage_chains=?, tracks=? WHERE id=?",
                     (json.dumps(chains, ensure_ascii=False), json.dumps(all_tracks, ensure_ascii=False), pid))



def create_snapshot(pid: str, name: str, desc: str = "", auto: bool = False):
    """生成项目当前状态的版本快照（项目信息+角色+消息摘要）。"""
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return None
    msgs = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE project_id=?", (pid,)
    ).fetchone()["c"]
    data = json.dumps({
        "project": dict(proj),
        "msg_count": msgs,
        "role_count": conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0],
    }, ensure_ascii=False)
    label = f"{'[自动] ' if auto else ''}{name}"
    conn.execute(
        "INSERT INTO snapshots (project_id,name,phase,desc,data,created_at) VALUES (?,?,?,?,?,?)",
        (pid, label, proj["phase"], desc, data, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return label


def gate_check(proj: dict, to_phase: str):
    """门禁校验：返回 (ok, error/need_confirm)。proj 可为 sqlite3.Row。"""
    if not isinstance(proj, dict):
        proj = dict(proj)
    if to_phase not in PHASE_GATES:
        return True, None
    g = PHASE_GATES[to_phase]
    if proj["phase"] != g["from"]:
        return False, f"阶段跳跃：当前是「{PHASE_NAMES.get(proj['phase'], proj['phase'])}」，必须先从「{PHASE_NAMES.get(g['from'], g['from'])}」推进"
    if to_phase == "ui" and not (proj.get("goal") or "").strip():
        return False, "门禁：需求澄清阶段必须先填写项目目标，才能进入 UI 定稿"
    if to_phase == "code":
        # UI 定稿要求：存在 ui 阶段快照（或用户明确确认）
        conn = get_db()
        has_ui_snap = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE project_id=? AND phase='ui'", (proj["id"],)
        ).fetchone()[0]
        conn.close()
        if not has_ui_snap:
            return False, "门禁：UI/交互确认前禁止写代码 —— 请先完成 UI 定稿并打快照（或在 UI 定稿阶段创建版本快照）"
    return True, None


# ── 危险命令护栏 v4.0 ────────────────────────────────────────────
# 审查发现：旧黑名单 13 个等效破坏变体放行 11 个（84.6% 绕过率）。
# 单纯扩黑名单永远追不上变体，因此改为「扩充黑名单 + 人工确认闸门」双层：
# 黑名单只用于「判断要不要更醒目地警告」，真正的安全边界是下面的人工确认。
DANGER_RE = re.compile(
    r"(\brm\b(?![^|;&]*--help)|"                      # 任何 rm（含 rm -r/-f 各种写法）
    r"\bmkfs\b|\bdd\b\s+if=|\bshutdown\b|\breboot\b|\bhalt\b|"
    r":\(\)\s*\{|"                                    # fork bomb
    r">\s*/dev/(sd|disk|rdisk)|"
    r"\bchmod\b\s+-R|\bchown\b\s+-R|"
    r"\bsudo\b|\bsu\b\s+-|"
    r"(curl|wget|fetch)\b[^|]*\|\s*(sh|bash|zsh|python)|"  # 下载即执行
    r"\bkillall\b|\bpkill\b|\blaunchctl\b|"
    r"\bdiskutil\b|\bfdisk\b|\bformat\b\s+[a-z]:|"
    r"\bnc\b\s+-l|\bncat\b|"                          # 反弹 shell
    r"\bhistory\b\s+-c|"
    r">\s*(/etc/|/System/|~/\.ssh/|/usr/)|"           # 覆写系统/密钥路径
    r"\bmv\b[^|;&]*\s+/(etc|usr|bin|System)\b|"
    r"\bgit\b\s+push\b[^|;&]*--force|"
    r"\bdefaults\s+delete\b|\bcrontab\b\s+-r|"
    r"(find|fd)\b[^|;&]*-delete|"                       # find/fd 删除变体
    r"rsync\b[^|;&]*--delete|"                          # rsync 删目标
    r"(shutil\.rmtree|os\.remove|os\.rmdir|os\.unlink|\.unlink\(|\.rmdir\()|"  # python 删除
    r"truncate\b|git\b[^|;&]*(clean\s+-[fdx]|reset\s+--hard)|"  # 截断/ git 破坏性
    r"mv\b[^|;&]*\s+/dev/null|"                         # 移入黑洞
    r"(shred|wipefs)\b)",                               # 擦除/清文件系统
    re.IGNORECASE,
)

# 敏感路径：读取这些内容等同泄露凭证，即使命令本身"无害"也要确认
SENSITIVE_PATH_RE = re.compile(
    r"(\.ssh/|\.aws/|id_rsa|id_ed25519|\.env\b|secrets?/|"
    r"keychain|\.netrc|\.git-credentials|token|password|passwd)",
    re.IGNORECASE,
)


# ── 宪法级利益护栏（高于配置，不可降级）───────────────────────
# 与 DANGER_RE 互补：DANGER_RE 管「危险命令要不要醒目警告」；本正则管「涉用户重大利益的动作」。
# 命中即强制真人确认，即使 approval_mode=off 也不能绕过（宪法不可被任何配置降级）。
CONSTITUTIONAL_RE = re.compile(
    r"(drop\s+table|truncate\s+table|"                                  # 不可逆数据销毁
    r"git\s+push\s+--force|"                                            # 强制推送（破坏性）
    r"\b(scp|rsync)\b[^|;&]*\s+[\w.@]+:|"                               # 向外传文件（声誉/所有权）
    r"\b(npm\s+publish|pypi|twine\s+upload|pod\s+trunk\s+push|git\s+push\s+origin\s+(main|master|--tags))\b|"  # 对外发布
    r"(curl|wget|http)\b[^|;&]*(pay|payment|charge|alipay|wechatpay|transfer|subscribe|账单|付款|转账)|"  # 钱
    r"(change|reset|update|set)\b[^|;&]*(password|passwd|secret|token)|"  # 账号/密钥
    r"(delete|drop|revoke|disable|transfer)\b[^|;&]*(account|domain|cert|ownership|key\b|app\b)|"  # 所有权
    r"\b(publish|deploy\s+--prod|release\s+--public)\b)",              # 以用户名义对外发布
    re.IGNORECASE,
)


def _constitutional_guard(command: str):
    """宪法级利益闸门：命中涉钱 / 对外发布 / 账号所有权 / 不可逆动作，返回 (需强制确认, 原因)。

    受 constitutional_guard_enabled 开关控制（默认开）。即使 approval_mode=off 也不可绕过——
    这是宪法层硬约束，高于任何用户配置。"""
    if get_setting("constitutional_guard_enabled", "1") != "1":
        return (False, "")
    if not command:
        return (False, "")
    m = CONSTITUTIONAL_RE.search(command)
    if m:
        return (True, f"触及宪法级利益关切（{m.group(0).strip()}）：涉钱/对外发布/账号所有权/不可逆，"
                      f"必须真人确认，且不可被任何配置降级。")
    return (False, "")


def _human_approve_sync(title: str, detail: str, timeout: int = 90):
    """弹出系统级对话框，等待真人点击。这是 AI 与用户电脑之间最后一道闸门。

    返回 (是否放行, 说明)。任何异常一律 fail-closed（拒绝），不给"出错就放过"的口子。
    """
    # v5.1：超时 ≤3s 直接拒绝（不弹窗）——3 秒内不可能完成真人确认，直接按拒绝处理；
    # 同时让回归测试可无打扰、确定性地验证危险命令拦截（approval_timeout=2 即命中）。
    if timeout <= 3:
        return False, f"审批超时过短（{timeout}s），已按最安全策略直接拒绝。"
    text = (detail or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " / ")[:900]
    ttl = (title or "分身请求授权").replace('"', "'")[:80]
    # v5.4 跨平台：macOS 用 osascript；Windows 用 PowerShell MessageBox（默认按钮=拒绝，subprocess 超时=拒绝）
    if sys.platform == "win32":
        script = (
            f'Add-Type -AssemblyName System.Windows.Forms; '
            f'$r=[System.Windows.Forms.MessageBox]::Show("{text}","{ttl}",'
            f'"YesNo","Warning","No"); if($r -eq "Yes"){{"ALLOW"}}else{{"DENY"}}'
        )
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                  capture_output=True, text=True, timeout=timeout + 15)
            out = (proc.stdout or "").strip()
            if "ALLOW" in out:
                return True, "用户已在系统对话框中授权。"
            return False, "用户拒绝了本次执行（或弹窗超时）。"
        except Exception as e:
            return False, f"授权对话框调用失败（{e}），已按最安全策略拒绝。"
    if sys.platform != "darwin":
        return False, "当前系统不支持系统级确认框，已按最安全策略拒绝执行。"
    script = (
        f'display dialog "{text}" with title "{ttl}" '
        f'buttons {{"拒绝", "允许执行"}} default button "拒绝" '
        f'with icon caution giving up after {timeout}'
    )
    try:
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=timeout + 15)
        out = (proc.stdout or "").replace(" ", "")
        if "gaveup:true" in out:
            return False, "授权对话框超时未响应，已拒绝执行。"
        if "允许执行" in out:
            return True, "用户已在系统对话框中授权。"
        return False, "用户拒绝了本次执行。"
    except Exception as e:
        return False, f"授权对话框调用失败（{e}），已按最安全策略拒绝。"


async def human_approve(title: str, detail: str, timeout: int = 0):
    """异步包装：弹窗是阻塞操作，放线程池避免卡住事件循环。

    超时时长可在设置里调（approval_timeout，秒），默认 90 秒；超时一律按拒绝处理。
    """
    if timeout <= 0:
        try:
            # v5.5 修复：下限与 settings 端点一致（1s）——此前 max(5) 会把 2s 抬到 5s 绕过 ≤3 直接拒绝
            timeout = max(1, min(300, int(get_setting("approval_timeout", "90"))))
        except ValueError:
            timeout = 90
    return await asyncio.to_thread(_human_approve_sync, title, detail, timeout)


def approval_mode() -> str:
    """AI 发起系统操作时的确认策略：all（每次确认）/ danger（仅危险命令，v4.0 起默认）/ off（关闭）。"""
    mode = get_setting("approval_mode", "danger")
    return mode if mode in {"all", "danger", "off"} else "danger"


def needs_approval(command: str) -> bool:
    """判断 AI 发起的这条命令是否需要真人点头。"""
    mode = approval_mode()
    if mode == "off":
        return False
    if mode == "all":
        # 批次B D6：代理VCS 开启时，任务内安全 git 子集（branch/add/commit/...）不弹窗，
        # 仍受 DANGER_RE 兜底硬拦（push --force/reset --hard/clean -fdx 等仍需确认）。
        if _code_vcs_enabled() and _GIT_SAFE_RE.match((command or "").strip()):
            return False
        return True
    return bool(DANGER_RE.search(command) or SENSITIVE_PATH_RE.search(command))


def needs_file_approval() -> bool:
    """批次 B / P2-4：写文件是否纳入真人确认——all（严格模式）拦截；
    danger（默认）不拦截以保留自主执行权，但文件操作始终落 file_log 审计。"""
    return approval_mode() == "all"


# ── API：基础 ────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    meta_cfg = get_model_config(META_PID)
    llm = "deepseek" if (meta_cfg and meta_cfg.get("api_key")) else "offline"
    return {"status": "ok", "version": SEMVER, "release": RELEASE, "schema_version": SCHEMA_VERSION,
            "build_date": BUILD_DATE, "git_commit": COMMIT, "port": PORT, "llm": llm,
            "bind": "lan" if _lan_mode() else "localhost", "approval_mode": approval_mode()}


@app.get("/api/version")
def api_version():
    """结构化版本信息（单一真源 backend/version.py）。"""
    return {"ok": True, **as_dict()}

# ── 监控埋点（v6.4 推广就绪度补齐）：事件仅存本机 DB，绝不外传 ──
@app.post("/api/analytics/event")
async def analytics_event(req: Request):
    """记录一条匿名使用事件（本地存储，不外发）。需本机令牌。"""
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    event = (data.get("event") or "").strip()
    if not event or len(event) > 64:
        return JSONResponse({"ok": False, "error": "event 非法"}, status_code=400)
    if not _rate_ok("analytics:" + _client_ip(req), 60, 60):
        return JSONResponse({"ok": False, "error": "上报过于频繁"}, status_code=429)
    uid = _current_user_id(req) or "anon"
    props = data.get("props") or {}
    if not isinstance(props, dict):
        props = {}
    try:
        import json as _json
        conn = get_db()
        conn.execute(
            "INSERT INTO analytics_events (ts,event,uid,version,props) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), event, uid, SEMVER, _json.dumps(props, ensure_ascii=False)[:2000]),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"写入失败：{e}"}, status_code=500)
    return JSONResponse({"ok": True})


@app.get("/api/analytics/stats")
async def analytics_stats(req: Request):
    """运营统计（仅本机 owner）。返回累计与近 7 日事件、注册/项目数。"""
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM analytics_events").fetchone()["c"]
        since = (datetime.now() - timedelta(days=7)).isoformat()
        recent = conn.execute(
            "SELECT event, COUNT(*) c FROM analytics_events WHERE ts>=? GROUP BY event ORDER BY c DESC",
            (since,),
        ).fetchall()
        daily = conn.execute(
            "SELECT substr(ts,1,10) d, COUNT(*) c FROM analytics_events WHERE ts>=? GROUP BY d ORDER BY d",
            (since,),
        ).fetchall()
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        projects = conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
        res = {
            "ok": True,
            "version": SEMVER,
            "events_total": total,
            "events_last_7d_by_type": [dict(r) for r in recent],
            "events_daily_last_7d": [dict(r) for r in daily],
            "users_total": users,
            "projects_total": projects,
        }
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    finally:
        conn.close()
    return res


# ── 数据备份（v6.4 推广就绪度补齐）：手动触发 + 状态查询 ──
@app.post("/api/system/backup")
async def system_backup(req: Request):
    """立即备份数据库（sqlite3 一致快照），返回备份路径。需本机令牌。"""
    path = backup_db("manual")
    if not path:
        return JSONResponse({"ok": False, "error": "备份失败"}, status_code=500)
    return JSONResponse({"ok": True, "path": path, "size": os.path.getsize(path)})


@app.get("/api/system/backups")
async def system_backups(req: Request):
    """列出最近备份文件（最多 20）。需本机令牌。"""
    try:
        d = os.path.dirname(os.path.abspath(DB))
        baks = sorted(
            (os.path.join(d, f) for f in os.listdir(d) if f.startswith("fenshen.db.bak-")),
            key=os.path.getmtime, reverse=True,
        )[:20]
        out = [{"name": os.path.basename(p), "size": os.path.getsize(p),
                "mtime": datetime.fromtimestamp(os.path.getmtime(p)).isoformat()} for p in baks]
    except Exception:
        out = []
    return {"ok": True, "backups": out}



# ── 移动端中转 WebSocket：手机 App ↔ 引擎（同源同端口 8002）──
try:
    from backend.relay_ws import relay_websocket

    @app.websocket("/ws")
    async def _relay_ws(websocket: WebSocket):
        await relay_websocket(websocket)
except Exception as _relay_err:
    print("[relay] 中转模块加载失败(已跳过，不影响其余功能):", _relay_err)


@app.get("/api/projects")
def list_projects(request: Request):
    conn = get_db()
    _uid = _current_user_id(request)
    if _uid:
        rows = conn.execute("SELECT * FROM projects WHERE owner_id=? ORDER BY created_at DESC", (_uid,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # v5.0 移动端深化：列表带轻量完成度（room 卡片进度条）
        try:
            t = conn.execute("SELECT COUNT(*) c, SUM(status='done') d FROM tasks WHERE project_id=?", (r["id"],)).fetchone()
            total = t["c"] or 0
            done = t["d"] or 0
            d["completion"] = {"total": total, "done": done,
                               "percent": round(done * 100 / total) if total else 0}
        except Exception:
            d["completion"] = {"total": 0, "done": 0, "percent": 0}
        # v5.0 移动端深化：项目列表带最后消息摘要 + 时间（微信式列表）
        try:
            lm = conn.execute(
                "SELECT text,ts FROM messages WHERE project_id=? AND topic_id='' AND sender!='系统' "
                "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            if lm:
                d["last_msg"] = (lm["text"] or "").strip().replace("\n", " ")[:48]
                d["last_ts"] = (lm["ts"] or "")[:16].replace("T", " ")
        except Exception:
            pass
        out.append(d)
    conn.close()
    return out


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    """聚合详情：goal + 完成标准 + 模块 + 任务 + 话题，单次往返供前端看板/群聊联动。"""
    conn = get_db()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "项目不存在"})
    # 字段形态必须和 /modules、/topics 单列接口一致：depends_on / agents 在库里是 JSON 文本，
    # 裸 dict(row) 会把它们当字符串丢给前端，前端一 .map() 就炸。
    mods = [_module_dict(r) for r in
            conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort, created_at", (pid,)).fetchall()]
    tasks = [dict(r) for r in
             conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()]
    topics = []
    for r in conn.execute("SELECT * FROM topics WHERE project_id=? ORDER BY created_at", (pid,)).fetchall():
        t = dict(r)
        t["agents"] = json.loads(t.get("agents") or "[]")
        topics.append(t)
    conn.close()
    d = dict(p)
    d["modules"] = mods
    d["tasks"] = tasks
    d["topics"] = topics
    # v4.2 自主闭环：看板完成度（模块级 + 项目级），直观显示"接近 100%"
    total_all = len(tasks)
    total_done = sum(1 for t in tasks if t["status"] == "done")
    module_stats = {}
    for m in mods:
        mt = [t for t in tasks if t["module_id"] == m["id"]]
        module_stats[m["id"]] = {
            "done": sum(1 for t in mt if t["status"] == "done"),
            "total": len(mt),
            "done_pct": round(sum(1 for t in mt if t["status"] == "done") * 100 / len(mt)) if mt else 100,
        }
    d["completion"] = {
        "done": total_done, "total": total_all,
        "percent": round(total_done * 100 / total_all) if total_all else 0,
        "modules": module_stats,
    }
    return d


@app.get("/api/design-specs")
async def api_design_specs():
    """v5.8 P2：返回可用 UI 设计规范列表（首页/建项下拉用）。"""
    return {"ok": True, "specs": _load_design_specs()}


@app.post("/api/projects/{pid}/autonomy")
async def set_project_autonomy(pid: str, req: Request):
    """v4.2：项目级自主推进暂停/恢复开关。paused=true → 自主循环跳过该项目（该项目的看板任务交由人工推进）。"""
    data = await req.json()
    paused = 1 if data.get("paused") else 0
    conn = get_db()
    row = conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    conn.execute("UPDATE projects SET autonomy_paused=? WHERE id=?", (paused, pid))
    conn.commit()
    conn.close()
    return {"ok": True, "paused": bool(paused)}


@app.post("/api/projects/{pid}/tier")
async def set_project_tier(pid: str, req: Request):
    """v6.5 项目级执行档位：auto/0/1/2 写入 projects.product_meta.tier（覆盖全局 chat_tier_default）。"""
    data = await req.json()
    tier = str(data.get("tier", "auto"))
    if tier not in ("0", "1", "2", "auto"):
        return {"ok": False, "error": "tier 必须为 auto/0/1/2"}
    conn = get_db()
    row = conn.execute("SELECT product_meta FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    pm = json.loads(row["product_meta"] or "{}") if row["product_meta"] else {}
    pm["tier"] = tier
    conn.execute("UPDATE projects SET product_meta=? WHERE id=?", (json.dumps(pm, ensure_ascii=False), pid))
    conn.commit()
    conn.close()
    return {"ok": True, "tier": tier, "note": "0=即时直答 / 1=单角色速办 / 2=团队协作 / auto=自动判定"}


# ── v5.1 应用市场：一键上架 / 下架 / 列表 / 访问统计 ──────────────
# 上架目标枚举（本期实现 market；wechat/appstore/android 预留第三方对接）
PUBLISH_TARGETS = ["market", "wechat-miniprogram", "appstore", "android"]


def _detect_artifacts(pid: str) -> list:
    """自动识别项目产物：扫描常见产物目录（site/dist/build/out + 项目 workdir）。"""
    candidates = []
    base = os.path.expanduser("~/Desktop")
    # 项目名匹配的目录（分身项目产物通常落在 ~/Desktop/<项目名>）
    conn = get_db()
    row = conn.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    proj_name = row["name"] if row else ""
    dirs = [base, os.path.expanduser("~/WorkBuddy")]
    if proj_name:
        for d in dirs:
            candidates.append(os.path.join(d, proj_name))
    for sub in ["site", "dist", "build", "out"]:
        for d in dirs:
            candidates.append(os.path.join(d, sub))
    found = []
    for c in candidates:
        if os.path.isdir(c):
            kinds = []
            if os.path.isfile(os.path.join(c, "index.html")):
                kinds.append("web")
            if any(f.endswith((".apk", ".app", ".zip")) for f in os.listdir(c) if os.path.isfile(os.path.join(c, f))):
                kinds.append("app")
            if any(d in ("pages", "app.json") for d in os.listdir(c)):
                kinds.append("miniprogram")
            if kinds:
                found.append({"path": c, "kinds": kinds})
    return found[:3]


@app.post("/api/projects/{pid}/publish")
async def publish_project(pid: str, req: Request):
    """一键上架：校验项目存在 → 识别产物 → LLM 生成介绍 → 标记 published。target 本期仅 market。"""
    data = await req.json()
    target = (data.get("target") or "market").strip()
    if target not in PUBLISH_TARGETS:
        return {"ok": False, "error": f"不支持的发布目标：{target}"}
    conn = get_db()
    row = conn.execute("SELECT id,name,goal,standards,phase FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    if target == "market" and row["phase"] != "done":
        conn.close()
        return {"ok": False, "error": f"仅已完成项目可上架（当前阶段：{row['phase']}）"}
    artifacts = _detect_artifacts(pid)
    # LLM 生成产品介绍（1-2 句，基于 goal/standards）
    desc = ""
    try:
        goal = (row["goal"] or row["name"] or "").strip()
        raw = (await _chat_with_tools(
            META_PID,
            [{"role": "system", "content": "你是产品文案。根据项目信息生成一句吸引人的产品介绍（≤60 字，突出价值，不要夸夸其谈）。"},
             {"role": "user", "content": f"产品：{row['name']}；目标：{goal}；完成标准：{(row['standards'] or '')[:100]}"}],
            "你是产品文案。", tools=[])) or ""
        # 清洗：去 markdown 符号，取第一个有意义的句子
        for line in raw.splitlines():
            t = line.strip().lstrip("#*-> ").strip()
            if t and len(t) >= 6:
                desc = t[:80]
                break
    except Exception:
        desc = (row["goal"] or "")[:80]
    if not desc:
        desc = (row["goal"] or row["name"] or "")[:80]
    meta = {"desc": desc, "target": target, "artifacts": artifacts, "generated_at": datetime.now().isoformat()}
    conn.execute("UPDATE projects SET published=1, publish_ts=?, product_meta=? WHERE id=?",
                 (datetime.now().isoformat(), json.dumps(meta, ensure_ascii=False), pid))
    conn.commit()
    conn.close()
    return {"ok": True, "target": target, "product_url": f"/p/{pid}",
            "desc": desc, "artifacts": artifacts}


@app.post("/api/projects/{pid}/unpublish")
async def unpublish_project(pid: str, req: Request):
    data = await req.json()
    conn = get_db()
    row = conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    conn.execute("UPDATE projects SET published=0, product_meta='' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/market")
def market_list():
    """已上架产品列表（含访问统计 + 反馈计数）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id,name,goal,published,publish_ts,product_meta,visit_count,phase FROM projects "
        "WHERE published=1 ORDER BY publish_ts DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            meta = json.loads(d.pop("product_meta") or "{}")
            d["desc"] = meta.get("desc", "")
            d["target"] = meta.get("target", "market")
            d["artifacts"] = meta.get("artifacts", [])
        except Exception:
            d["desc"], d["target"], d["artifacts"] = "", "market", []
        d["product_url"] = f"/p/{r['id']}"
        # v5.3 运营中心：反馈计数
        try:
            d["fb_count"] = conn.execute(
                "SELECT COUNT(*) c FROM market_feedback WHERE product_id=?", (r["id"],)).fetchone()["c"] or 0
        except Exception:
            d["fb_count"] = 0
        out.append(d)
    conn.close()
    return out


@app.post("/api/market/visit/{pid}")
async def market_visit(pid: str, request: Request):
    """访问计数（市场链接被打开时调用）。按客户端 IP 限流，防脚本刷量。"""
    ip = _client_ip(request)
    if not _rate_ok("visit:" + ip, 30, 60):
        return JSONResponse(status_code=429, content={"ok": False, "error": "操作过于频繁，请稍后再试"})
    conn = get_db()
    conn.execute("UPDATE projects SET visit_count=visit_count+1 WHERE id=? AND published=1", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── v5.2 部署一键化：配置服务器 → 自动备份 + scp 上线（expect 包装，S4 同模式）──
def _expect_cmd(script: str, timeout: int = 240):
    try:
        proc = subprocess.run(["expect", "-c", script], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "")[-400:]
    except Exception as e:
        return 1, f"expect 执行失败：{e}"


@app.post("/api/projects/{pid}/deploy/conf")
async def deploy_conf_set(pid: str, req: Request):
    data = await req.json()
    conf = {k: str(data.get(k) or "").strip() for k in ("host", "user", "password", "path")}
    if not conf["host"] or not conf["path"]:
        return {"ok": False, "error": "host 和 path 必填"}
    conn = get_db()
    conn.execute("UPDATE projects SET deploy_conf=? WHERE id=?", (json.dumps(conf, ensure_ascii=False), pid))
    conn.commit()
    conn.close()
    return {"ok": True, "conf": {k: v for k, v in conf.items() if k != "password"}}


@app.post("/api/projects/{pid}/deploy")
async def deploy_project(pid: str, req: Request):
    """一键部署：读配置 → 识别产物 → ssh 备份旧目录 → scp 上线。"""
    conn = get_db()
    row = conn.execute("SELECT name,deploy_conf FROM projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "项目不存在"}
    try:
        conf = json.loads(row["deploy_conf"] or "{}")
    except Exception:
        conf = {}
    host, user, pwd, path = conf.get("host", ""), conf.get("user", ""), conf.get("password", ""), conf.get("path", "")
    if not (host and path):
        return {"ok": False, "error": "未配置部署服务器（host/path），先在项目设置里配置"}
    artifacts = _detect_artifacts(pid)
    src = artifacts[0]["path"] if artifacts else ""
    if not src:
        return {"ok": False, "error": "未识别到产物目录（site/dist/build/out 或 ~/Desktop/项目名）"}
    user_at = f"{user}@{host}" if user else f"root@{host}"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # v5.4 跨平台：优先用 paramiko（SSH 库，Windows/macOS 通用）；无 paramiko 时 macOS 回退 expect
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user or "root", password=pwd, timeout=20)
        # 1) 远端备份
        ssh.exec_command(f"mkdir -p {path}")[1].read()
        _, out, err = ssh.exec_command(f"cp -r {path} {path}.bak-{ts} 2>/dev/null; echo BACKUP_OK")
        backup_out = (out.read().decode() + err.read().decode())
        if "BACKUP_OK" not in backup_out:
            ssh.close()
            return {"ok": False, "error": f"远端备份失败：{backup_out[:100]}"}
        # 2) 上传产物（顶层文件/目录）
        sftp = ssh.open_sftp()
        up, skip = 0, 0
        for name in os.listdir(src):
            local = os.path.join(src, name)
            remote = f"{path.rstrip('/')}/{name}"
            if os.path.isdir(local):
                ssh.exec_command(f"mkdir -p {remote}")[1].read()
                for sub in os.listdir(local):
                    sftp.put(os.path.join(local, sub), f"{remote}/{sub}")
                    up += 1
            else:
                sftp.put(local, remote)
                up += 1
        sftp.close()
        ssh.close()
        return {"ok": True, "detail": f"已部署（paramiko）{src} → {user_at}:{path}（备份 {path}.bak-{ts}，上传 {up} 文件）"}
    except ImportError:
        pass  # 无 paramiko → 走 expect（仅 macOS）
    except Exception as e:
        return {"ok": False, "error": f"paramiko 部署失败：{str(e)[:120]}"}
    # 1) 远端备份旧目录
    rc, out = _expect_cmd(
        f'set timeout 60; spawn ssh -o StrictHostKeyChecking=no {user_at} "mkdir -p {path} && '
        f'if [ -d {path} ]; then cp -r {path} {path}.bak-{ts} 2>/dev/null; fi; echo BACKUP_OK" '
        f'? ; expect "password:" {{ send "{pwd}\\r"; exp_continue }} ; expect eof')
    if "BACKUP_OK" not in out and rc != 0:
        return {"ok": False, "error": f"远端备份失败：{out[:100]}"}
    # 2) scp 产物上线
    rc, out = _expect_cmd(
        f'set timeout 240; spawn scp -o StrictHostKeyChecking=no -r {src}/* {user_at}:{path}/ '
        f'? ; expect "password:" {{ send "{pwd}\\r"; exp_continue }} ; expect eof')
    if rc != 0 and "100%" not in out:
        return {"ok": False, "error": f"scp 上线失败：{out[:120]}"}
    return {"ok": True, "detail": f"已部署 {src} → {user_at}:{path}（备份 {path}.bak-{ts}）"}


# ── v5.3 运营中心：公开反馈收集 → 一键转看板 + SEO 文案 ─────────
@app.post("/api/market/feedback")
async def market_feedback(req: Request):
    """公开产品反馈（无需 token，产品页内嵌表单调用；按 IP + 按产品双重限流）。"""
    ip = _client_ip(req)
    if not _rate_ok("fb:" + ip, 10, 60):
        return {"ok": False, "error": "提交太频繁，请稍后再试"}
    data = await req.json()
    pid = (data.get("product_id") or "").strip()
    text = (data.get("text") or "").strip()[:500]
    if not pid or not text:
        return {"ok": False, "error": "缺少产品或反馈内容"}
    conn = get_db()
    row = conn.execute("SELECT id FROM projects WHERE id=? AND published=1", (pid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "产品不存在或已下架"}
    # 防刷：同一产品 10 秒内最多 3 条（叠加按 IP 限流）
    n = conn.execute(
        "SELECT COUNT(*) c FROM market_feedback WHERE product_id=? AND ts>?",
        (pid, (datetime.now() - timedelta(seconds=10)).isoformat())).fetchone()["c"]
    if n and n >= 3:
        conn.close()
        return {"ok": False, "error": "提交太频繁，稍后再试"}
    conn.execute(
        "INSERT INTO market_feedback (id,product_id,text,ts) VALUES (?,?,?,?)",
        (f"fb{time.time_ns()}", pid, text, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"ok": True, "message": "已收到反馈，谢谢！"}


@app.get("/api/market/feedback/{pid}")
def market_feedback_list(pid: str):
    """某产品的反馈列表（运营查看）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id,text,ts,status,task_id FROM market_feedback WHERE product_id=? ORDER BY ts DESC",
        (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/market/feedback/{fid}/to-board")
async def market_feedback_to_board(fid: str):
    """反馈 → 转成产品项目看板需求卡（进入项目群聊首模块待办列）。"""
    conn = get_db()
    fb = conn.execute("SELECT * FROM market_feedback WHERE id=?", (fid,)).fetchone()
    if not fb:
        conn.close()
        return {"ok": False, "error": "反馈不存在"}
    if fb["status"] == "done":
        conn.close()
        return {"ok": False, "error": "该反馈已处理"}
    proj = conn.execute("SELECT id,name FROM projects WHERE id=? AND published=1", (fb["product_id"],)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "产品项目不存在或已下架"}
    # 落到项目第一个模块的待办列
    mod = conn.execute("SELECT id FROM modules WHERE project_id=? ORDER BY id LIMIT 1", (proj["id"],)).fetchone()
    if not mod:
        conn.close()
        return {"ok": False, "error": "产品项目还没有模块"}
    topic = conn.execute("SELECT id FROM topics WHERE module_id=?", (mod["id"],)).fetchone()
    if not topic:
        conn.close()
        return {"ok": False, "error": "模块还没有话题对话组"}
    text = (fb["text"] or "")[:120]
    _stg, _trk = _derive_task_stage_track(conn, mod["id"], proj["id"])
    conn.execute(
        "INSERT INTO tasks (project_id,module_id,topic_id,name,owner_role,status,done_criteria,stage,track,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (proj["id"], mod["id"], topic["id"], f"[用户反馈] {text}", "前端工程师", "todo",
         f"处理用户反馈：{text}", _stg, _trk, datetime.now().isoformat()))
    conn.execute("UPDATE market_feedback SET status='done' WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return {"ok": True, "message": f"已转成看板需求卡（{proj['name']}）"}


@app.post("/api/projects/{pid}/seo")
async def project_seo(pid: str, req: Request):
    """SEO 落地页文案生成（LLM：标题/描述/关键词）。"""
    conn = get_db()
    row = conn.execute("SELECT name,goal,standards FROM projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "项目不存在"}
    try:
        raw = (await _chat_with_tools(
            META_PID,
            [{"role": "system", "content": "你是 SEO 文案。输出严格 JSON：{\"title\":\"≤20字标题含核心关键词\",\"desc\":\"≤80字描述\",\"keywords\":\"逗号分隔 5-8 个关键词\"}，不要输出其他内容。"},
             {"role": "user", "content": f"产品：{row['name']}；目标：{row['goal'] or ''}；完成标准：{(row['standards'] or '')[:80]}"}],
            "你是 SEO 文案。", tools=[])) or ""
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.S)
        data = json.loads(m.group(0)) if m else {}
        seo = {"title": str(data.get("title", ""))[:30], "desc": str(data.get("desc", ""))[:120],
               "keywords": str(data.get("keywords", ""))[:120]}
        if not seo["title"]:
            seo = {"title": row["name"], "desc": (row["goal"] or "")[:80], "keywords": row["name"]}
        return {"ok": True, "seo": seo}
    except Exception:
        return {"ok": True, "seo": {"title": row["name"], "desc": (row["goal"] or "")[:80], "keywords": row["name"]}}


# ── v5.1 首次使用引导：元神软门槛（3 步引导 → 解锁建项目，可跳过）──
@app.get("/api/meta/onboarding")
def onboarding_status():
    """首登引导状态：done（已完成）/ skipped（跳过）/ pending（待引导）。
    软门槛条件：画像事实 ≥3 条 或 访谈已答 ≥3 题 → 视为基础完成。"""
    flag = get_setting("meta_onboarded", "")
    if flag == "1":
        return {"status": "done", "facts": 0, "interview_done": 0, "skippable": True}
    if flag == "skip":
        return {"status": "skipped", "facts": 0, "interview_done": 0, "skippable": True}
    conn = get_db()
    facts, iv = 0, 0
    try:
        facts = conn.execute("SELECT COUNT(*) c FROM user_model").fetchone()["c"] or 0
    except Exception:
        pass
    try:
        # meta_interview 无 answer 列：答案存于 answers JSON；非空即视为已答过
        iv = conn.execute(
            "SELECT COUNT(*) c FROM meta_interview WHERE answers IS NOT NULL AND answers != '{}' AND answers != ''"
        ).fetchone()["c"] or 0
    except Exception:
        pass
    conn.close()
    if facts >= 3 or iv >= 3:
        return {"status": "done", "facts": facts, "interview_done": iv, "skippable": True}
    return {"status": "pending", "facts": facts, "interview_done": iv, "skippable": True}


@app.post("/api/meta/onboarding")
async def onboarding_set(req: Request):
    data = await req.json()
    action = (data.get("action") or "").strip()
    if action == "skip":
        set_setting("meta_onboarded", "skip")
        return {"ok": True, "status": "skipped"}
    if action == "done":
        set_setting("meta_onboarded", "1")
        return {"ok": True, "status": "done"}
    return {"ok": False, "error": "action 必须是 done 或 skip"}


@app.get("/p/{pid}")
async def public_product_page(pid: str):
    """公开产品页（v5.1 应用市场）：无需 token，供扫码/链接访问；每次访问计数。"""
    conn = get_db()
    row = conn.execute(
        "SELECT name,goal,product_meta,visit_count FROM projects WHERE id=? AND published=1",
        (pid,)).fetchone()
    conn.close()
    if not row:
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px;text-align:center'><h3>产品不存在或已下架</h3></body></html>", status_code=404)
    meta = {}
    try:
        meta = json.loads(row["product_meta"] or "{}")
    except Exception:
        pass
    desc = meta.get("desc", "") or (row["goal"] or "")
    artifacts = meta.get("artifacts", [])
    conn = get_db()
    conn.execute("UPDATE projects SET visit_count=visit_count+1 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    name = html.escape(str(row["name"] or "未命名产品"))
    dsc = html.escape(desc)
    art = "".join(
        f'<div style="background:#f5f5f7;border:.5px solid #e5e5ea;border-radius:8px;padding:8px 12px;margin:6px 0;font-size:13px">📦 {html.escape(a.get("path",""))}（{html.escape("/".join(a.get("kinds",[])))}）</div>'
        for a in artifacts) if artifacts else '<div style="font-size:12px;color:#8e8e93">未识别到可下载产物（源码在电脑端项目目录）</div>'
    page_html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} · 分身应用市场</title></head>
<body style="font-family:-apple-system,'PingFang SC',sans-serif;background:#f6f5fb;margin:0;padding:0">
<div style="max-width:560px;margin:0 auto;padding:32px 20px">
  <div style="background:#fff;border-radius:16px;border:.5px solid #e5e5ea;padding:24px">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
      <div style="width:48px;height:48px;border-radius:12px;background:#6d5bd0;color:#fff;display:flex;align-items:center;justify-content:center;font-size:22px">{html.escape((name or "品")[0])}</div>
      <div>
        <div style="font-size:18px;font-weight:600">{name}</div>
        <div style="font-size:12px;color:#8e8e93">分身应用市场 · 由分身团队开发</div>
      </div>
    </div>
    <p style="font-size:14px;line-height:1.7;color:#3c3c43;margin:0 0 16px">{dsc}</p>
    <div style="font-size:12px;color:#8e8e93;margin-bottom:6px">产物</div>{art}
    <div style="background:#f5f5f7;border-radius:10px;padding:14px;text-align:center;margin-top:16px">
      <div style="font-size:12px;color:#8e8e93;margin-bottom:6px">手机扫码访问（桌面端数据来源）</div>
      <div style="font-size:11px;color:#6d5bd0">[ 二维码生成接入点 ]</div>
    </div>
    <div style="background:#fff;border-radius:12px;border:.5px solid #e5e5ea;padding:16px;margin-top:12px">
      <div style="font-size:13px;font-weight:600;margin-bottom:8px">反馈给团队</div>
      <textarea id="fbText" rows="2" placeholder="说说你的想法、遇到的问题或想要的功能…" style="width:100%;box-sizing:border-box;border:.5px solid #d6d6db;border-radius:8px;padding:8px 10px;font-size:13px;font-family:inherit;resize:none"></textarea>
      <button onclick="submitFeedback('{pid}')" style="margin-top:8px;background:#6d5bd0;color:#fff;border:none;border-radius:8px;padding:8px 20px;font-size:13px">提交反馈</button>
      <div id="fbOk" style="font-size:12px;color:#0F6E56;margin-top:8px"></div>
    </div>
  </div>
  <p style="text-align:center;font-size:11px;color:#8e8e93;margin-top:16px">由「分身」生成 · 数据本地化 · 访问次数 {(row["visit_count"] or 0)+1}</p>
</div>
<script>
async function submitFeedback(pid){{
  const t=document.getElementById('fbText').value.trim();
  if(!t){{ alert('请先填写反馈内容'); return; }}
  try{{
    const r=await fetch('/api/market/feedback',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{product_id:pid,text:t}})}});
    const j=await r.json();
    document.getElementById('fbOk').textContent=j.ok?('✅ '+j.message):('❌ '+(j.error||'提交失败'));
    if(j.ok) document.getElementById('fbText').value='';
  }}catch(e){{ document.getElementById('fbOk').textContent='❌ 网络错误，请重试'; }}
}}
</script></body></html>"""
    return HTMLResponse(page_html)


def _seed_default_roster(pid: str, tracks):
    """v6.2 战队：建项即生成初始成员阵容（元神战队从成立那一刻就齐人）。
    按轨道给出 策划/设计/开发/测试/运营 五位基础成员，soul 预填定位，rule 预置安全红线。"""
    conn = get_db()
    base = [
        ("策划", "产品策划", "负责需求澄清/定位/商业模型，把目标拆给团队"),
        ("设计", "UI/UX 设计师", "原型/界面/品牌，保证体验与一致性"),
        ("开发", "全栈工程师", "前端/后端/联调，把设计变成可运行产品"),
        ("测试", "测试工程师", "编写用例/回归/卡点回收，守住质量门"),
        ("运营", "增长运营", "内容/社群/投放/数据，把产品推到用户面前"),
    ]
    now = datetime.now().isoformat()
    for i, (name, title, soul) in enumerate(base):
        mid = f"{pid}-a{i + 1}"
        conn.execute(
            "INSERT OR IGNORE INTO agent_members "
            "(id,project_id,name,avatar,role_title,track,soul,rule,eng_spec,model_cfg,work_mode,work_hours,skills,experience,level,version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,1,?,?)",
            (mid, pid, name, name[0], title, "web", soul,
             json.dumps(["先出方案再动手", "不碰生产库", "重大变更先报告元神"], ensure_ascii=False),
             "", json.dumps({"model": "deepseek-v4-flash", "temp": 0.3, "reason": False, "max_tokens": 8000}, ensure_ascii=False),
             "online", "24h", "[]", now, now),
        )
    conn.commit()
    conn.close()


@app.post("/api/projects")
async def create_project(req: Request):
    data = await req.json()
    pid = data.get("id") or f"p{int(datetime.now().timestamp() * 1000)}"
    # v5.8 P1：双维度存储根目录（~/.fenshen/projects/<pid>）+ 设计规范（P2 用，建项自选）
    storage_root = os.path.expanduser(f"~/.fenshen/projects/{pid}")
    design_std = data.get("design_standard", "") or ""
    # v6.1 矩阵看板：建项即按所选轨道配置阶段链（默认 web）；v6.2 P2 自动补齐生命周期轨道
    tracks = data.get("tracks") or ["web"]
    if isinstance(tracks, str):
        try:
            tracks = json.loads(tracks) or ["web"]
        except Exception:
            tracks = ["web"]
    if not isinstance(tracks, list) or not tracks:
        tracks = ["web"]
    tracks = list(dict.fromkeys(list(tracks) + LIFE_TRACKS))
    chains = {}
    for t in tracks:
        chains[t] = list(STAGE_PRESETS.get(t, STAGE_PRESETS["web"]))
    conn = get_db()
    _owner = _current_user_id(req) or "local"
    conn.execute(
        "INSERT OR REPLACE INTO projects (id,name,goal,standards,status,created_at,phase,frozen,storage_root,design_standard,tracks,stage_chains,owner_id) VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?)",
        (pid, data.get("name", ""), data.get("goal", ""), data.get("standards", ""), "green", datetime.now().isoformat(),
         data.get("phase", "requirement"), storage_root, design_std,
         json.dumps(tracks, ensure_ascii=False), json.dumps(chains, ensure_ascii=False), _owner),
    )
    # 解构引导：projects.modules 数组 → 批量创建模块（支持一次成立项目即拆模块）
    mods = data.get("modules") or []
    if isinstance(mods, list):
        for i, m in enumerate(mods):
            mid = f"{pid}-m{i + 1}"
            conn.execute(
                "INSERT OR IGNORE INTO modules (id,project_id,name,desc,depends_on,owner_role,status,sort,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, pid, m.get("name", ""), m.get("desc", ""),
                 json.dumps(m.get("depends_on") or [], ensure_ascii=False), m.get("owner_role", "后端"),
                 m.get("status", "idea"), i, datetime.now().isoformat(), datetime.now().isoformat()),
            )
    # 批次 A：每个模块自动建一个默认话题，供任务/讨论绑定（修复看板↔群聊断链的根因）
    _mi = 0
    for mrow in conn.execute("SELECT id FROM modules WHERE project_id=?", (pid,)).fetchall():
        _mi += 1
        tid = f"tp{int(datetime.now().timestamp() * 1000)}_{_mi}"
        conn.execute(
            "INSERT OR IGNORE INTO topics (id,project_id,module_id,name,agents,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (tid, pid, mrow["id"], "默认讨论", "[]", "open", datetime.now().isoformat()),
        )
    conn.commit()
    conn.close()
    # v5.8 P1：初始化双维度目录树（public / members / modules/<mid>）
    _ensure_project_dirs(pid, {"id": pid, "name": data.get("name", ""), "storage_root": storage_root})
    # 批次 B / P2-2：元神搭建基础设施（开场消息定格目标/标准/团队/看板）
    _bootstrap_project(pid, goal=data.get("goal", ""), standards=data.get("standards", ""),
                       roles=data.get("roles") or [])
    # v6.2 战队：建项即生成初始成员阵容
    try:
        _seed_default_roster(pid, tracks)
    except Exception as e:
        print("roster seed warn:", e)
    return {"id": pid, "ok": True, "modules": len(mods)}


@app.patch("/api/projects/{pid}")
async def update_project(pid: str, req: Request):
    data = await req.json()
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    # 冻结锁：冻结后核心信息（goal/desc）改动需走修改单
    if proj["frozen"] and ("goal" in data or "desc" in data):
        conn.close()
        return {"ok": False, "error": "项目已冻结，修改核心信息需先创建修改单（change order）并审批"}
    if "status" in data:
        conn.execute("UPDATE projects SET status=? WHERE id=?", (data["status"], pid))
    if "desc" in data:
        conn.execute("UPDATE projects SET goal=? WHERE id=?", (data["desc"], pid))
    if "goal" in data:
        conn.execute("UPDATE projects SET goal=? WHERE id=?", (data["goal"], pid))
    if "standards" in data:
        conn.execute("UPDATE projects SET standards=? WHERE id=?", (data["standards"], pid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    """级联删除项目及其模块/话题/任务/消息（用于清理与用户主动删项目）。"""
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "项目不存在"})
    conn.execute("DELETE FROM messages WHERE project_id=?", (pid,))
    conn.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
    conn.execute("DELETE FROM topics WHERE project_id=?", (pid,))
    conn.execute("DELETE FROM modules WHERE project_id=?", (pid,))
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": pid}


def _bootstrap_project(pid: str, goal: str = "", standards: str = "", roles: list = None) -> None:
    """批次 B / P2-2：项目创建后元神搭建基础设施 —— 群聊开场消息
    （目标 / 完成标准 / 团队阵容 / 模块看板说明），让看板=项目总览图的第一帧就有内容。
    角色实例化与技能装配由 P3（动态角色 + BUILTIN_SKILLS）进一步落地，此处先定格团队名单。"""
    try:
        conn = get_db()
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj:
            conn.close()
            return
        mods = conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort", (pid,)).fetchall()
        mod_names = "、".join(m["name"] for m in mods) if mods else "（未拆分模块，可在看板补充分解）"
        _, role_names = _roles_from_db()  # P3-1：角色从表动态加载
        team = [role_names.get(r, r) for r in (roles or list(role_names))]
        team_text = "、".join(team)
        lines = [
            "🏗️ 元神已为项目搭建好基础设施：",
            f"🎯 目标：{goal or '（未填写）'}",
            f"✅ 完成标准：{standards or '（未填写，可随时在项目设置里补充）'}",
            f"👥 团队：{team_text}",
            f"🗂️ 模块（看板纵轴）：{mod_names}",
            "📋 看板已就绪：每个模块一个泳道，任务按 待办 → 进行中 → 复核 → 完成 流转。",
            "告诉我第一个任务，我就安排团队开工；完成标准会自动作为验收依据。",
        ]
        conn.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
            (pid, "分身 · 元神", "meta", "\n".join(lines), "progress", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[bootstrap] {pid} 失败: {e}")


# ── API：项目模板（v0.27.0 多项目模板沉淀）────────────────────────
BUILTIN_TEMPLATES = [
    {"name": "标准 Web 应用", "desc": "登录 → 支付 → 内容列表，最常见的 MVP 结构",
     "goal": "一个带账号体系、支付与内容展示的 Web 应用 MVP",
     "roles": ["architect", "backend", "frontend", "tester"],
     "meta": {"version": "1.0", "tags": ["web", "mvp", "sass"], "scenario": "带账号+支付+内容展示的通用 Web 应用起步"},
     "modules": [{"name": "登录/注册", "owner_role": "后端", "depends_on": []},
                 {"name": "支付", "owner_role": "后端", "depends_on": ["登录/注册"]},
                 {"name": "内容/题库列表", "owner_role": "前端", "depends_on": ["登录/注册"]}]},
    {"name": "电商小程序", "desc": "用户 → 商品 → 购物车 → 订单 → 支付",
     "goal": "一个可下单支付的电商小程序（用户/商品/购物车/订单/支付闭环）",
     "roles": ["architect", "backend", "frontend", "tester"],
     "meta": {"version": "1.0", "tags": ["电商", "小程序", "交易"], "scenario": "需要下单支付闭环的电商小程序"},
     "modules": [{"name": "用户中心", "owner_role": "后端", "depends_on": []},
                 {"name": "商品管理", "owner_role": "后端", "depends_on": []},
                 {"name": "购物车", "owner_role": "后端", "depends_on": ["用户中心", "商品管理"]},
                 {"name": "订单", "owner_role": "后端", "depends_on": ["购物车", "用户中心"]},
                 {"name": "支付", "owner_role": "后端", "depends_on": ["订单"]},
                 {"name": "商城页面", "owner_role": "前端", "depends_on": ["购物车", "商品管理"]}]},
    {"name": "内容社区", "desc": "登录 → 发帖 → 评论 → 关注 → 内容流",
     "goal": "一个可发帖评论互动的社区（登录/发帖/评论/关注/信息流）",
     "roles": ["architect", "backend", "frontend", "tester"],
     "meta": {"version": "1.0", "tags": ["社区", "UGC", "内容"], "scenario": "需要用户产出内容与互动的社区产品"},
     "modules": [{"name": "登录/注册", "owner_role": "后端", "depends_on": []},
                 {"name": "发帖/编辑", "owner_role": "后端", "depends_on": ["登录/注册"]},
                 {"name": "评论/互动", "owner_role": "后端", "depends_on": ["发帖/编辑"]},
                 {"name": "关注/关系", "owner_role": "后端", "depends_on": ["登录/注册"]},
                 {"name": "内容流页面", "owner_role": "前端", "depends_on": ["发帖/编辑", "关注/关系"]}]},
    {"name": "AI 工具应用", "desc": "登录 → AI 对话 → 用量计费 → 管理后台",
     "goal": "一个按量计费的 AI 工具应用（对话生成 + 用量计费 + 管理后台）",
     "roles": ["architect", "backend", "frontend", "tester"],
     "meta": {"version": "1.0", "tags": ["AI", "SaaS", "计费"], "scenario": "按量计费、带管理后台的 AI 工具"},
     "modules": [{"name": "登录/注册", "owner_role": "后端", "depends_on": []},
                 {"name": "AI 对话/生成", "owner_role": "后端", "depends_on": ["登录/注册"]},
                 {"name": "用量/计费", "owner_role": "后端", "depends_on": ["AI 对话/生成"]},
                 {"name": "管理后台", "owner_role": "后端", "depends_on": ["登录/注册"]},
                 {"name": "对话界面", "owner_role": "前端", "depends_on": ["AI 对话/生成"]}]},
    {"name": "分身运营工作台", "desc": "蒸馏自己 → 元神 → 看板 → 记忆/磨 → 自动调度：用分身运营你自己的数字团队",
     "goal": "搭建并运营我自己的分身工作台：蒸馏出元神，让它驾驶 harness 替我「想、做、管、留」",
     "roles": ["architect", "backend", "frontend", "tester"],
     "meta": {"version": "1.0", "tags": ["分身", "元神", "数字团队", "自运营"], "scenario": "想用分身（数字克隆体+harness）运营/管理自己的 AI 生产力"},
     "modules": [{"name": "蒸馏自己 → 元神", "owner_role": "architect", "depends_on": []},
                 {"name": "群聊团队搭建", "owner_role": "后端", "depends_on": ["蒸馏自己 → 元神"]},
                 {"name": "看板与进度", "owner_role": "前端", "depends_on": ["群聊团队搭建"]},
                 {"name": "记忆/磨·经验沉淀", "owner_role": "后端", "depends_on": ["看板与进度"]},
                 {"name": "自动调度与汇报", "owner_role": "tester", "depends_on": ["记忆/磨·经验沉淀"]}]},
    {"name": "营销落地页+付费", "desc": "落地页 → 支付 → 用户管理 → 数据分析（投流转化类）",
     "goal": "一个承接投流流量的营销落地页，支持付费转化与数据追踪",
     "roles": ["architect", "backend", "frontend", "tester"],
     "meta": {"version": "1.0", "tags": ["营销", "落地页", "转化"], "scenario": "投流/软文承接 + 付费转化的营销落地页"},
     "modules": [{"name": "落地页", "owner_role": "前端", "depends_on": []},
                 {"name": "支付/订单", "owner_role": "后端", "depends_on": ["落地页"]},
                 {"name": "用户管理", "owner_role": "后端", "depends_on": ["支付/订单"]},
                 {"name": "数据分析", "owner_role": "后端", "depends_on": ["用户管理"]}]},
    {"name": "API 后端服务", "desc": "认证 → 核心 API → 数据库 → 监控（to B / 多端共用后端）",
     "goal": "一个供多端/第三方调用的 API 后端服务（认证 + 核心接口 + 可观测）",
     "roles": ["architect", "backend", "tester"],
     "meta": {"version": "1.0", "tags": ["API", "后端", "toB"], "scenario": "纯后端 API 服务（App/Web/小程序共用）"},
     "modules": [{"name": "认证/令牌", "owner_role": "后端", "depends_on": []},
                 {"name": "核心业务 API", "owner_role": "后端", "depends_on": ["认证/令牌"]},
                 {"name": "数据存储", "owner_role": "后端", "depends_on": ["核心业务 API"]},
                 {"name": "日志/监控", "owner_role": "后端", "depends_on": ["核心业务 API"]}]},
]


# ── 预设技能活配件（v3：19 种内置技能，参照 BUILTIN_TEMPLATES 模式）──
BUILTIN_SKILLS = [
    # ── A. 行为规范（塑造 agent 怎么干活，始终适用）──
    {"name": "表达纪律", "category": "builtin", "description": "让 AI 先说重点：结论先行、编号步骤、留一个可执行动作、危险先确认、连败停手",
     "trigger_words": "说,讲,汇报,总结,回复,回答,说明,解释,建议,方案,分析,怎么,为什么,写,给,输出", "steps": [
         "结论/重点先行：一句话先说最关键的，再展开理由与细节。",
         "用编号列执行步骤，每步可独立操作，不堆散文。",
         "明说当前进度与已验证事实，不模糊带过。",
         "结尾给「一个」可执行动作或待确认项，不一次抛多个。",
         "危险/不可逆操作先告警并等确认，不擅自执行。",
         "连败 3 次立即停手并汇报卡点，不空转。"]},
    {"name": "任务闭环", "category": "builtin", "description": "交付必须过三关、给验证证据、禁止伪完成",
     "trigger_words": "做完,完成,交付,验证,验收,过三关,质量,收尾,搞定,结束", "steps": [
         "先定「做完的标准」= 可验证 done_criteria，不靠感觉。",
         "真跑验证（本地启动/单测/真浏览器），用证据说话，禁「应该没问题」。",
         "独立裁判三问自审：目标达成？边界覆盖？可回滚？任一不过不算完。",
         "交付附证据（命令/截图/日志片段），不交付空承诺。",
         "卡死即上报或标记阻塞，不伪完成。"]},
    {"name": "安全边界", "category": "builtin", "description": "危险操作先确认、先备份再删、动生产带回滚、连败熔断",
     "trigger_words": "删除,删,重置,清空,危险,权限,提权,格式化,修改系统,线上", "steps": [
         "危险操作（删/重置/改权限/动生产）前列影响范围并等用户确认。",
         "删除用「先备份再删」：拷贝到备份目录确认成功后再删，严禁 rm --delete / rsync --delete 直接清源。",
         "动生产/线上前必须给回滚方案；无回滚不动。",
         "路径/输入做校验，禁路径穿越与注入（不拼未校验的用户串）。",
         "连败或异常立即熔断（FAIL_BREAKER），不连发危险指令。"]},
    # ── B. 核心研发流（软件交付主链路）──
    {"name": "需求拆解", "category": "builtin", "description": "把模糊需求拆成可独立验收的子任务",
     "trigger_words": "需求,拆解,需求分析,要做什么,目标,范围,规划", "steps": [
         "先对齐目标与完成标准（对照项目 standards / done_criteria）。",
         "拆成 2-5 个可独立验收的子任务，每个有清晰出口。",
         "标依赖与顺序（可并行 / 阻塞）。",
         "输出清单供看板建卡（模块×阶段矩阵）。"]},
    {"name": "技术选型", "category": "builtin", "description": "按场景对比技术方案并给出选型结论",
     "trigger_words": "技术选型,选型,技术方案,框架选择,对比,技术栈", "steps": [
         "列 ≥2 个候选，明确要解决的真实问题。",
         "按 学习成本/生态成熟度/性能/团队熟悉度/可运维性 对比。",
         "给明确结论+理由+风险，不模棱两可。",
         "标替代方案与退出成本。"]},
    {"name": "UI 组件库", "category": "builtin", "description": "设计可复用的 UI 组件与样式规范",
     "trigger_words": "组件,UI,界面,样式,页面设计,设计稿,前端视觉", "steps": [
         "梳理页面所需组件清单与复用点。",
         "定义设计 token（色板/字号/间距/圆角/阴影），对齐设计系统。",
         "给 3-6 个核心组件结构+样式要点+交互状态。",
         "输出使用约定（命名/组合/禁用项），避免各自造轮子。"]},
    {"name": "API 设计", "category": "builtin", "description": "设计 REST API 接口定义",
     "trigger_words": "API,接口设计,接口,路由,端点,REST", "steps": [
         "列业务场景→接口清单（method/path/用途）。",
         "每接口给 请求参数/响应结构/状态码，统一项目 API 客户端风格。",
         "定义鉴权方式与错误码（不裸奔 200 包错误）。",
         "标 done_criteria（可测验收点，如契约测试）。"]},
    {"name": "DB Schema", "category": "builtin", "description": "设计数据库表结构与数据模型",
     "trigger_words": "数据库,表结构,Schema,建表,数据模型,ER,索引", "steps": [
         "识别核心实体与关系（1:N / N:M），先画 ER。",
         "每表给 字段/类型/可空/索引/外键约束。",
         "标范式取舍与热点查询的索引策略。",
         "给 1 条核心查询示例验证设计可行。"]},
    {"name": "工程脚手架", "category": "builtin", "description": "一次性初始化前后端项目骨架（合并旧前后端脚手架）",
     "trigger_words": "脚手架,初始化项目,搭建项目,工程结构,项目骨架,初始化", "steps": [
         "定技术栈与分层（前端/后端/数据层/配置），一次成型。",
         "列依赖清单与版本锁定（避免漂移）。",
         "给入口文件、路由/模块骨架、配置与环境变量模板。",
         "验证可启动（前端 npm run / 后端 health 接口），不交付跑不起的骨架。"]},
    {"name": "质量门禁", "category": "builtin", "description": "测试+审查+Bug 修复一体化，含边界处理与范围感知（合并旧三项）",
     "trigger_words": "测试,用例,审查,review,代码走查,bug,质量,修bug,冒烟,回归,走查", "steps": [
         "审查优先级：正确性>生命周期/资源>并发>安全/权限绕过>测试证据（一个有力阻塞 > 一堆格式）。",
         "按 diff 范围找最小充分证据（只跑受影响测试，不盲目全量）。",
         "边界处理：空值/异常/越权/输入超长/并发竞态都要有断言。",
         "修 Bug 走 复现→根因→最小修复→回归用例，不补丁式掩盖。",
         "真跑验证给通过率/证据，交付前过三关。"]},
    {"name": "部署上线", "category": "builtin", "description": "输出可执行的部署与上线步骤",
     "trigger_words": "部署,上线,发布,服务器,运维,发布流程", "steps": [
         "明确服务器/端口/域名/证书与回滚方案（无回滚不动）。",
         "给构建+部署命令（密钥来源不硬编码）。",
         "上线前检查清单（健康/监控/备份/灰度）。",
         "给出可验证的上线成功标准与回滚步骤。"]},
    {"name": "文档生成", "category": "builtin", "description": "生成项目/接口/使用文档",
     "trigger_words": "文档,README,说明文档,手册,教程,写文档", "steps": [
         "定文档结构与目标读者（开发/用户/运维）。",
         "写清 安装/配置/使用 步骤（可照做）。",
         "补关键接口/页面/命令说明，配示例。",
         "用 write_file 落盘并核对真实存在，不编造路径。"]},
    # ── C. 差异化能力（分身独有，市面不可比）──
    {"name": "磨", "category": "builtin",
     "description": "上下文保真压缩（升级为六层防污染体系），溢出时自动调用，失效记录删、有效沉淀为元神材料",
     "trigger_words": "磨,压缩,缩写,精简上下文,上下文溢出,回忆压缩,提炼材料", "steps": [
         "按话题边界切段（不切断语义单元）。",
         "逐段磨碎：保留 事实/决策/用户原话/文件路径/绑定维度信号，删冗余口水。",
         "完整保留语意前提下压到最短（六层防污染 L0→L5）。",
         "上下文将溢出时自动磨最旧非关键段腾空间（50% 预压缩 / 60% 强制）。",
         "失效记录批量删，有效部分原子提取 WHAT/WHY/OUTCOME/NEXT 标为元神材料。"]},
    {"name": "记忆", "category": "builtin", "description": "技能化已有蒸馏/经验进化：何时蒸馏、何时召回、召回什么（特征驱动）",
     "trigger_words": "记住,回忆,经验,之前,历史,踩过坑,复盘,学过的,偏好", "steps": [
         "遇「踩坑/决策/偏好」主动蒸馏进 experiences（Gotchas-First：先记坑）。",
         "召回用特征驱动检索：按 任务类型/栈/错误签名 取最相关，不堆全量。",
         "三级记忆：L1 会话 / L2 项目经验 / L3 用户画像，各归其位。",
         "经验带权重（relevance/recency/frequency/explicit_feedback/trust）：positive 3+ 升持久、连续 2 negative 或 30 天未引→淘汰。",
         "别把「思考过程/过程注释」写进记忆与产物（反垃圾）。"]},
    {"name": "浏览器自动化", "category": "builtin", "description": "改完 UI 必用真无头浏览器验证，禁 Mock/Fixture",
     "trigger_words": "浏览器,截图,验证UI,打开网页,点开,真浏览器,页面检查", "steps": [
         "改完 UI/前端必用真浏览器验证（playwright 真无头），禁 Mock/Fixture/伪造截图。",
         "验证清单：页面能加载 / 交互可用 / 无 console 报错 / 响应式 / 关键路径通。",
         "用 browser_action 执行真实点击与填表，落真实结果（非假设）。",
         "把验证证据（URL/截图路径/报错）反馈，不口头「应该好了」。"]},
    # ── D. 办公与交付（常规高频基础需求，常驻）──
    {"name": "文档处理", "category": "builtin", "description": "Word/PDF/Excel 真读写与互转，不靠编",
     "trigger_words": "Word,PDF,Excel,合同,文档排版,转word,提取pdf,表格处理,docx,xlsx", "steps": [
         "明确输入/输出格式与字段映射（docx/pdf/xlsx 互转要保真）。",
         "用真库落地：python-docx(Word) / pypdf·pdfplumber(PDF) / openpyxl(Excel)。",
         "批量/大文件分块处理，先小样验证再全量。",
         "输出后核对真实存在且内容正确，不交付占位文件。"]},
    {"name": "演示与商业计划书", "category": "builtin", "description": "生成 PPT 商业计划书/路演稿，套设计系统视觉",
     "trigger_words": "PPT,商业计划书,演示文稿,路演,Pitch,BP,幻灯片", "steps": [
         "先定叙事主线（痛点→方案→优势→模式→数据→需求），一页一焦点。",
         "用 python-pptx 真生成，套设计系统视觉 token（配色/字体/间距）。",
         "每页只讲一件事，数据用图表不堆字；结论先行。",
         "导出后核对页数/版面真实无误，不交付空白模板。"]},
    {"name": "图表与长图", "category": "builtin", "description": "流程图/思维导图/长图/架构图真生成",
     "trigger_words": "流程图,思维导图,长图,架构图,信息图,时间轴,示意图", "steps": [
         "先定图类型与信息层级（流程/结构/对比/时间轴）。",
         "流程图用 mermaid 真生成（可渲染），长图用 matplotlib/PIL 拼接。",
         "控制信息密度：一图一主旨，配色对齐设计系统。",
         "导出图片后核对清晰可辨，不交付错位乱码图。"]},
    {"name": "UI 平面设计", "category": "builtin", "description": "海报/logo/视觉物料生成与排版，对齐设计系统",
     "trigger_words": "海报,logo,平面设计,视觉,封面,Banner,物料,配图", "steps": [
         "先定调性与用途（推广/品牌/功能），对齐设计系统 token。",
         "构图网格/留白/字号层级/主色不超过 3。",
         "产出可交付源（SVG/高清图），标注尺寸与安全区。",
         "核对成品无文字溢出/模糊/侵权素材，不交付占位稿。"]},
]


def _seed_builtin_skills():
    """v3：启动时把 19 种预设技能（全集）种入 skills 表。
    内置技能按 name upsert（同步出厂定义升级；enabled/version 等用户状态保留），
    并按 name 差集清理已合并/下线的内置技能（如旧「前端脚手架/后端脚手架/测试用例/代码审查/Bug修复」），
    避免残留行继续被 _match_skill_steps 注入。非内置技能（category='general'）不受影响。"""
    try:
        conn = get_db()
        now = datetime.now().isoformat()
        for s in BUILTIN_SKILLS:
            row = conn.execute("SELECT id FROM skills WHERE name=? AND category='builtin'", (s["name"],)).fetchone()
            steps = json.dumps(s["steps"], ensure_ascii=False)
            if row:
                conn.execute(
                    "UPDATE skills SET description=?, trigger_words=?, steps=?, updated_at=? WHERE id=?",
                    (s["description"], s["trigger_words"], steps, now, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO skills (name,category,description,trigger_words,steps,version,enabled,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,1,1,?,?)",
                    (s["name"], s["category"], s["description"], s["trigger_words"], steps, now, now),
                )
        # v3：按 name 差集清理已合并/下线的内置技能，避免残留行继续被注入。
        # 仅删 builtin 类，不碰用户自定义（category='general'）。
        _names = [s["name"] for s in BUILTIN_SKILLS]
        _ph = ",".join("?" * len(_names))
        conn.execute(f"DELETE FROM skills WHERE category='builtin' AND name NOT IN ({_ph})", _names)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[skills-seed] 失败: {e}")


_seed_builtin_skills()


def _seed_templates(conn):
    """内置模板 upsert（幂等）：不存在则插入，存在则按 name 更新 desc/goal/roles/modules/meta（v0.27.0 支持补新字段）。"""
    # 清理已废弃的「学习考试产品」样板（已改用「分身运营工作台」）
    try:
        conn.execute("DELETE FROM project_templates WHERE is_builtin=1 AND name='学习考试产品'")
    except Exception:
        pass
    for t in BUILTIN_TEMPLATES:
        row = conn.execute("SELECT id FROM project_templates WHERE name=? AND is_builtin=1", (t["name"],)).fetchone()
        if row:
            conn.execute(
                "UPDATE project_templates SET desc=?,goal=?,roles=?,modules=?,meta=? WHERE id=?",
                (t["desc"], t.get("goal", ""), json.dumps(t.get("roles", []), ensure_ascii=False),
                 json.dumps(t["modules"], ensure_ascii=False), json.dumps(t.get("meta", {}), ensure_ascii=False), row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO project_templates (name,desc,modules,is_builtin,ts,goal,roles,meta) VALUES (?,?,?,1,?,?,?,?)",
                (t["name"], t["desc"], json.dumps(t["modules"], ensure_ascii=False), datetime.now().isoformat(),
                 t.get("goal", ""), json.dumps(t.get("roles", []), ensure_ascii=False),
                 json.dumps(t.get("meta", {}), ensure_ascii=False)),
            )


@app.get("/api/templates")
def list_templates():
    conn = get_db()
    _seed_templates(conn)
    rows = conn.execute("SELECT * FROM project_templates ORDER BY is_builtin DESC, id").fetchall()
    conn.commit()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["modules"] = json.loads(d["modules"])
        except Exception:
            d["modules"] = []
        try:
            d["roles"] = json.loads(d["roles"] or "[]")
        except Exception:
            d["roles"] = []
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except Exception:
            d["meta"] = {}
        out.append(d)
    return out


@app.post("/api/templates")
async def save_template(req: Request):
    data = await req.json()
    name = (data.get("name") or "").strip()
    modules = data.get("modules") or []
    if not name or not isinstance(modules, list) or not modules:
        return {"ok": False, "error": "模板名与模块列表必填"}
    conn = get_db()
    conn.execute(
        "INSERT INTO project_templates (name,desc,modules,is_builtin,ts,goal,roles,meta) VALUES (?,?,?,0,?,?,?,?)",
        (name, data.get("desc", ""), json.dumps(modules, ensure_ascii=False), datetime.now().isoformat(),
         data.get("goal", ""), json.dumps(data.get("roles") or [], ensure_ascii=False),
         json.dumps(data.get("meta") or {}, ensure_ascii=False)),
    )
    conn.commit()
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return {"ok": True, "id": tid}


@app.delete("/api/templates/{tid}")
def delete_template(tid: int):
    conn = get_db()
    row = conn.execute("SELECT is_builtin FROM project_templates WHERE id=?", (tid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "模板不存在"}
    if row["is_builtin"]:
        conn.close()
        return {"ok": False, "error": "内置模板不可删除"}
    conn.execute("DELETE FROM project_templates WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/roles")
def list_roles():
    conn = get_db()
    rows = conn.execute("SELECT * FROM roles").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/roles")
async def create_role(req: Request):
    data = await req.json()
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO roles VALUES (?,?,?,?,?)",
        (data.get("id"), data.get("name"), data.get("mandate"), data.get("skills"), data.get("gate")),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/resources")
def list_resources():
    conn = get_db()
    rows = conn.execute("SELECT * FROM resources").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/resources/{rid}/auth")
async def toggle_resource(rid: str):
    conn = get_db()
    conn.execute("UPDATE resources SET auth = 1 - auth WHERE id=?", (rid,))
    row = conn.execute("SELECT auth FROM resources WHERE id=?", (rid,)).fetchone()
    conn.commit()
    conn.close()
    return {"id": rid, "auth": row["auth"] if row else 0}


@app.get("/api/messages/{pid}")
def list_messages(pid: str, topic_id: str = ""):
    """列出项目消息。topic_id 非空时只列该话题的消息（话题对话组）。"""
    conn = get_db()
    if topic_id:
        rows = conn.execute("SELECT * FROM messages WHERE project_id=? AND topic_id=? ORDER BY id", (pid, topic_id)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM messages WHERE project_id=? AND (topic_id IS NULL OR topic_id='') ORDER BY id", (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/messages")
async def add_message(req: Request):
    data = await req.json()
    pid = data.get("project_id", META_PID)
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,topic_id) VALUES (?,?,?,?,?,?,?)",
        (pid, data.get("sender", "你"), data.get("kind", "self"), data.get("text", ""), data.get("tag"),
         datetime.now().isoformat(), data.get("topic_id", "")),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# ── v6.2 P2：1:1 私聊（用户 ↔ 成员/元神），结论可同步回群 ──
META_DIRECT_SYS = ("你是分身的「元神」，岳衡的数字搭档与业务大脑。你懂全部业务但不亲自动手，负责调度 agent 成员。"
                   "在私聊中，你用干练、直接、有态度的语气给用户做私下 briefing / 复核 / 纠偏。用中文。")


def _direct_system_prompt(peer: str):
    if peer == "meta":
        return META_DIRECT_SYS
    conn = get_db()
    m = conn.execute("SELECT * FROM agent_members WHERE id=?", (peer,)).fetchone()
    conn.close()
    if not m:
        return "你是分身的一名团队成员，请用专业、尽责的语气回应用户。"
    soul = m["soul"] or ""
    rule = m["rule"] or []
    try:
        rule = json.loads(rule) if isinstance(rule, str) else rule
    except Exception:
        rule = []
    rule_txt = "\n".join(f"- {r}" for r in (rule or []))
    return (f"你是分身项目团队成员「{m['name']}」({m.get('role_title','')})。\n"
            f"你的 Soul：{soul}\n你的行为规则：\n{rule_txt}\n"
            f"在 1:1 私聊中，给用户做私下 briefing / 复核 / 纠偏，语气符合你的 Soul。")


@app.get("/api/direct/{peer}")
def list_direct(peer: str, project_id: str = ""):
    conn = get_db()
    if project_id:
        rows = conn.execute("SELECT * FROM direct_msgs WHERE peer=? AND project_id=? ORDER BY ts",
                            (peer, project_id)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM direct_msgs WHERE peer=? ORDER BY ts", (peer,)).fetchall()
    conn.close()
    return {"ok": True, "peer": peer, "messages": [dict(r) for r in rows]}


@app.post("/api/direct/{peer}")
async def send_direct(peer: str, req: Request):
    data = await req.json()
    text = (data.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "内容为空"}
    pid = data.get("project_id", "")
    sender = data.get("sender", "user")
    conn = get_db()
    uid = f"dm{int(datetime.now().timestamp() * 1000)}"
    conn.execute("INSERT INTO direct_msgs (id,peer,project_id,sender,kind,text,ts,synced) VALUES (?,?,?,?,?,?,?,0)",
                 (uid, peer, pid, sender, "text", text, datetime.now().isoformat()))
    conn.commit()
    reply = None
    if sender == "user":  # 用户发言 → 触发成员/元神回复
        try:
            hist = [dict(r) for r in
                    conn.execute("SELECT sender,text FROM direct_msgs WHERE peer=? ORDER BY ts DESC LIMIT 12",
                                 (peer,)).fetchall()]
            history = [{"role": ("user" if h["sender"] == "user" else "assistant"), "content": h["text"]}
                       for h in reversed(hist)]
            sys_p = _direct_system_prompt(peer)
            ans = call_llm(peer, history, sys_p)
            rid = f"dm{int(datetime.now().timestamp() * 1000)}_r"
            conn.execute("INSERT INTO direct_msgs (id,peer,project_id,sender,kind,text,ts,synced) VALUES (?,?,?,?,?,?,?,0)",
                         (rid, peer, pid, peer, "text", ans, datetime.now().isoformat()))
            conn.commit()
            reply = {"id": rid, "text": ans}
        except Exception as e:
            print("[direct-reply] warn:", e)
    conn.close()
    return {"ok": True, "id": uid, "reply": reply}


@app.post("/api/direct/{peer}/sync")
async def sync_direct(peer: str, req: Request):
    """把一条私聊结论同步回项目群聊。"""
    data = await req.json()
    mid = data.get("message_id") or ""
    pid = data.get("project_id") or ""
    text = (data.get("text") or "").strip()
    if not pid:
        return {"ok": False, "error": "缺少 project_id"}
    conn = get_db()
    sender_name = "元神"
    if peer != "meta":
        m = conn.execute("SELECT name FROM agent_members WHERE id=?", (peer,)).fetchone()
        if m:
            sender_name = m["name"]
    if mid:
        row = conn.execute("SELECT text FROM direct_msgs WHERE id=? AND peer=?", (mid, peer)).fetchone()
        if row:
            text = row["text"]
    if not text:
        conn.close()
        return {"ok": False, "error": "无可同步内容"}
    conn.execute("INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                 (pid, sender_name, "agent", text, "私聊同步", datetime.now().isoformat()))
    if mid:
        conn.execute("UPDATE direct_msgs SET synced=1 WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/meta/files")
def list_meta_files():
    conn = get_db()
    rows = conn.execute("SELECT * FROM meta_files ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/meta/files")
async def add_meta_file(req: Request):
    data = await req.json()
    conn = get_db()
    conn.execute("INSERT INTO meta_files (name,ts) VALUES (?,?)", (data.get("name"), datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/meta/outputs")
def list_meta_outputs():
    """元神产出展示区：聚合所有项目的可识别产物（site/dist/build/out 目录）。"""
    conn = get_db()
    rows = conn.execute("SELECT id,name FROM projects ORDER BY id DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            arts = _detect_artifacts(r["id"])
        except Exception:
            arts = []
        for a in arts:
            out.append({"project": r["name"], "project_id": r["id"],
                        "path": a["path"], "kinds": a["kinds"]})
    return out


# ── API：模型配置（元神 + 角色级）─────────────────────────────────
@app.get("/api/models")
def list_models():
    conn = get_db()
    rows = conn.execute("SELECT * FROM model_configs").fetchall()
    conn.close()
    cfgs = {r["agent_id"]: dict(r) for r in rows}
    # 组合：元神 + 所有角色
    agents = [{"agent_id": META_PID, "name": "元神（我的分身）"}]
    for r in list_roles():
        agents.append({"agent_id": r["id"], "name": r["name"]})
    out = []
    for a in agents:
        c = cfgs.get(a["agent_id"])
        rec = ROLE_MODEL_RECS.get(a["agent_id"], {})
        backups = get_model_backups(a["agent_id"])
        out.append({
            "agent_id": a["agent_id"],
            "name": a["name"],
            "provider": (c or {}).get("provider", "deepseek"),
            "model_name": (c or {}).get("model_name", ""),
            "base_url": (c or {}).get("base_url", ""),
            "has_key": bool((c or {}).get("api_key")),
            "backups": backups,
            "recommended": rec.get("provider", ""),
            "recommended_model": rec.get("model", ""),
            "recommend_why": rec.get("why", ""),
        })
    return out


# ── API：多模型协作（Phase 5：用量统计 + 交叉验证）──────────────
@app.get("/api/models/usage")
def model_usage_stats():
    """各 provider 调用统计：次数 / 成功率 / 平均耗时。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT provider, model, status, COUNT(*) c, AVG(latency_ms) avg_ms FROM model_usage GROUP BY provider, model, status"
    ).fetchall()
    conn.close()
    agg = {}
    for r in rows:
        k = (r["provider"], r["model"])
        agg.setdefault(k, {"provider": r["provider"], "model": r["model"], "success": 0, "degraded": 0, "total_ms": 0})
        agg[k][r["status"]] = r["c"]
    out = []
    for k, v in agg.items():
        total = v["success"] + v["degraded"]
        v["total"] = total
        v["success_rate"] = round(v["success"] / total * 100, 1) if total else 0
        out.append(v)
    conn = get_db()
    total_usage = conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
    conn.close()
    return {"stats": sorted(out, key=lambda x: -x["total"]), "total_calls": total_usage}


# ── API：实时 token 条（v5.9）──────────────────────────────────────
@app.get("/api/token/usage")
def token_usage():
    """返回进程级实时 token 累加（底部 token 条轮询用）。"""
    return dict(LIVE_TOKENS)


@app.post("/api/token/reset")
def token_reset():
    """清零实时 token 累加（新一轮工作时手动重置）。"""
    LIVE_TOKENS["input"] = 0
    LIVE_TOKENS["output"] = 0
    LIVE_TOKENS["total"] = 0
    LIVE_TOKENS["calls"] = 0
    LIVE_TOKENS["last_provider"] = ""
    LIVE_TOKENS["last_model"] = ""
    return {"ok": True}


# ── API：Trajectory 回放（v5.9）──────────────────────────────────
@app.get("/api/projects/{pid}/runs")
def list_runs(pid: str):
    """列出该项目的回放 run（按时间倒序）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT run_id,project_id,ts,trigger,total_events,status FROM trajectory_runs "
        "WHERE project_id=? ORDER BY ts DESC LIMIT 50", (pid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/projects/{pid}/trajectory")
def get_trajectory(pid: str, run_id: str = ""):
    """返回回放事件列表：指定 run_id 取该 run，否则取项目最近事件。"""
    conn = get_db()
    if run_id:
        rows = conn.execute(
            "SELECT id,run_id,ts,kind,agent,text,meta FROM trajectory WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,run_id,ts,kind,agent,text,meta FROM trajectory WHERE project_id=? ORDER BY id DESC LIMIT 200", (pid,)
        ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d["meta"] or "{}")
        except Exception:
            d["meta"] = {}
        out.append(d)
    return out


@app.post("/api/models/cross-check")
async def cross_check(req: Request):
    """交叉验证：同一段内容交给两个不同模型（角色 A / 角色 B），返回对比结果。"""
    data = await req.json()
    text = (data.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "请输入要交叉验证的内容"}
    agent_a = data.get("agent_a", META_PID)
    agent_b = data.get("agent_b", "architect")
    hist = [{"role": "system", "content": "你是严谨的交叉验证评审员，请独立完成下面的任务并给出明确结论。"},
            {"role": "user", "content": text}]
    result_a = call_llm(agent_a, hist, hist[0]["content"])
    result_b = call_llm(agent_b, hist, hist[0]["content"])
    # 判断降级
    degraded_a = result_a.startswith("[元神·")
    degraded_b = result_b.startswith("[元神·")
    return {
        "ok": True,
        "a": {"agent_id": agent_a, "result": result_a, "degraded": degraded_a},
        "b": {"agent_id": agent_b, "result": result_b, "degraded": degraded_b},
    }


@app.put("/api/models/{agent_id}")
async def set_model(agent_id: str, req: Request):
    data = await req.json()
    provider = data.get("provider", "deepseek")
    base_url = data.get("base_url", "").strip() or None
    api_key = data.get("api_key", "").strip() or None
    model_name = data.get("model_name", "").strip() or None
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO model_configs (agent_id,provider,base_url,api_key,model_name) VALUES (?,?,?,?,?)",
        (agent_id, provider, base_url, api_key, model_name),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/models/{agent_id}/backups")
async def get_model_backups_api(agent_id: str):
    return {"ok": True, "backups": get_model_backups(agent_id)}


@app.post("/api/models/{agent_id}/backups")
async def set_model_backups_api(agent_id: str, req: Request):
    data = await req.json()
    backups = data.get("backups", []) if isinstance(data, dict) else data
    if not isinstance(backups, list):
        return {"ok": False, "error": "backups 必须是数组"}
    set_model_backups(agent_id, backups)
    return {"ok": True, "count": len(get_model_backups(agent_id))}


@app.post("/api/models/{agent_id}/test")
async def test_model(agent_id: str, req: Request):
    data = await req.json()
    # 临时构造配置测试（不落库）
    provider = data.get("provider", "deepseek")
    key = data.get("api_key", "").strip()
    base = data.get("base_url", "").strip() or PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])["base"]
    model = data.get("model_name", "").strip() or PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])["default_model"]
    if not key and provider != "ollama":
        return {"ok": False, "error": "缺少 API Key"}
    try:
        if provider == "claude":
            resp = requests.post(base + "/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={"model": model, "system": "ping", "messages": [{"role": "user", "content": "回复 ok"}], "max_tokens": 20},
                timeout=20)
            resp.raise_for_status()
            return {"ok": True, "reply": resp.json()["content"][0]["text"][:80]}
        elif provider == "ollama":
            resp = requests.post(base + "/api/chat", json={"model": model, "messages": [{"role": "user", "content": "ping"}], "stream": False}, timeout=40)
            resp.raise_for_status()
            return {"ok": True, "reply": resp.json()["message"]["content"][:80]}
        else:
            resp = requests.post(base + PROVIDER_PRESETS[provider]["chat"],
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "回复 ok 两个字"}], "max_tokens": 30},
                timeout=20)
            resp.raise_for_status()
            return {"ok": True, "reply": resp.json()["choices"][0]["message"]["content"][:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


# ── API：元神系统级执行器（桌面最高权限）──────────────────────────
@app.post("/api/exec")
async def exec_command(req: Request):
    data = await req.json()
    command = (data.get("command") or "").strip()
    agent_id = data.get("agent_id", META_PID)
    confirm = bool(data.get("confirm", False))
    if not command:
        return {"ok": False, "error": "命令为空"}
    # P1-3 修复：与元神工具路径(_run_meta_tool)统一走 needs_approval 闸门，
    # 尊重「设置→确认策略」(all/danger/off)，不再只看本地黑名单、且不受客户端 confirm 绕过。
    # 审查 V02：确认框由服务端弹系统对话框，客户端说啥都不算数（fail-closed）。
    is_danger = bool(DANGER_RE.search(command) or SENSITIVE_PATH_RE.search(command))  # 仅作审计标记，不再用于拦截
    const_hit, const_reason = _constitutional_guard(command)  # 宪法闸：高于 approval_mode 配置
    approved_by = "user-panel"
    if needs_approval(command) or const_hit:
        ok_approved, why = await human_approve(
            "分身请求执行（宪法级/危险）命令" if const_hit else "分身请求执行危险命令",
            f"来源：{agent_id}\n命令：{command}\n\n"
            + (const_reason + "\n" if const_reason else "")
            + "这条命令可能造成不可逆后果或触及你的重大利益。确认要执行吗？",
        )
        if not ok_approved:
            conn = get_db()
            conn.execute(
                "INSERT INTO exec_log (ts,agent_id,command,status,exit_code,output,confirmed) VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(), agent_id, command, "blocked", -3, why, 0),
            )
            conn.commit()
            conn.close()
            return {"ok": False, "blocked": True, "danger": is_danger, "constitutional": const_hit, "error": why}
        approved_by = "human-dialog" + ("-constitution" if const_hit else "")
    try:
        proc = subprocess.run(
            command, shell=True, cwd=os.path.expanduser("~"),
            capture_output=True, text=True, timeout=30,
        )
        exit_code = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
        status = "success" if exit_code == 0 else "error"
    except subprocess.TimeoutExpired:
        output = "（命令执行超时 30s，已被终止）"
        exit_code = -1
        status = "timeout"
    except Exception as e:
        output = f"执行异常：{e}"
        exit_code = -2
        status = "exception"
    # 审计日志（全量落库，含命令与输出截断）
    conn = get_db()
    conn.execute(
        "INSERT INTO exec_log (ts,agent_id,command,status,exit_code,output,confirmed) VALUES (?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), agent_id, command, status, exit_code, output[:4000], int(is_danger)),
    )
    conn.commit()
    conn.close()
    # 审查 #7：旧版恒返 ok:true，命令失败也报成功。ok 现在如实反映退出码。
    return {"ok": exit_code == 0, "status": status, "exit_code": exit_code,
            "output": output[-3000:], "danger": is_danger, "constitutional": const_hit,
            "approved_by": approved_by, "agent_id": agent_id}


@app.get("/api/exec/log")
def exec_log():
    conn = get_db()
    rows = conn.execute("SELECT id,ts,agent_id,command,status,exit_code,confirmed FROM exec_log ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]



# ── API：浏览器自动化（v0.27.0，playwright + 系统 Chrome headless）────
# 动作：open(打开+取标题/正文摘要) / screenshot(截图 base64) /
#       extract(按 selector 抓文本) / fill(填表) / click(点击)
# 安全：仅 http/https URL；30s 超时；全量审计 browser_log
def _browser_run(action: str, url: str, selector: str = "", text: str = "", wait_ms: int = 0):
    from playwright.sync_api import sync_playwright
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    result = {"ok": False, "error": ""}
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=chrome, headless=True,
                                  args=["--no-sandbox", "--disable-gpu"])
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            if url:
                pg.goto(url, timeout=20000, wait_until="domcontentloaded")
            if wait_ms:
                pg.wait_for_timeout(wait_ms)
            if action == "open":
                body = pg.evaluate("document.body ? document.body.innerText.slice(0,2000) : ''")
                result = {"ok": True, "title": pg.title(), "url": pg.url,
                          "content": body.strip()[:2000]}
            elif action == "screenshot":
                if not url:
                    return {"ok": False, "error": "截图需要 url"}
                png = pg.screenshot(full_page=False)
                import base64
                result = {"ok": True, "title": pg.title(), "url": pg.url,
                          "image_b64": base64.b64encode(png).decode(), "size": len(png)}
            elif action == "extract":
                if not selector:
                    return {"ok": False, "error": "extract 需要 selector"}
                try:
                    els = pg.query_selector_all(selector)
                    texts = [e.inner_text().strip() for e in els[:20]]
                    result = {"ok": True, "title": pg.title(), "count": len(texts),
                              "items": texts}
                except Exception:
                    result = {"ok": False, "error": f"未找到 selector: {selector}"}
            elif action == "fill":
                if not selector or not text:
                    return {"ok": False, "error": "fill 需要 selector 和 text"}
                try:
                    pg.fill(selector, text, timeout=8000)
                    result = {"ok": True, "title": pg.title(), "filled": selector}
                except Exception:
                    # 元素被 overlay 遮挡时，fallback 到 JS 强制赋值+触发 input 事件
                    pg.evaluate(
                        "(o)=>{const el=document.querySelector(o.s); if(!el) throw new Error('not found'); "
                        "el.value=o.v; el.dispatchEvent(new Event('input',{bubbles:true})); "
                        "el.dispatchEvent(new Event('change',{bubbles:true}));}",
                        {"s": selector, "v": text},
                    )
                    pg.wait_for_timeout(500)
                    result = {"ok": True, "title": pg.title(), "filled": selector + "（JS fallback）"}
            elif action == "click":
                if not selector:
                    return {"ok": False, "error": "click 需要 selector"}
                try:
                    pg.click(selector, timeout=8000)
                except Exception:
                    pg.evaluate(
                        "(s)=>{const el=document.querySelector(s); if(!el) throw new Error('not found'); el.click();}",
                        selector,
                    )
                pg.wait_for_timeout(1500)
                body = pg.evaluate("document.body ? document.body.innerText.slice(0,1500) : ''")
                result = {"ok": True, "title": pg.title(), "url": pg.url,
                          "content": body.strip()[:1500]}
            else:
                result = {"ok": False, "error": f"未知动作 {action}"}
            b.close()
        except Exception as e:
            result = {"ok": False, "error": f"浏览器执行异常: {type(e).__name__}: {str(e)[:300]}"}
    return result


@app.post("/api/browser/action")
def browser_action(req: Request):  # 普通 def → FastAPI 线程池执行，兼容 playwright sync API
    import asyncio
    data = asyncio.run(req.json())
    action = (data.get("action") or "").strip()
    url = (data.get("url") or "").strip()
    selector = (data.get("selector") or "").strip()
    text = (data.get("text") or "")
    wait_ms = int(data.get("wait_ms", 0) or 0)
    agent_id = data.get("agent_id", META_PID)
    if action not in ("open", "screenshot", "extract", "fill", "click"):
        return {"ok": False, "error": "动作必须是 open/screenshot/extract/fill/click"}
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "error": "仅允许 http/https 地址"}
    res = _browser_run(action, url, selector, text, wait_ms)
    # 宪法审计：以用户名义对外发布/发送的动作标记 constitutional，供后续门禁升级
    const_hit, _ = _constitutional_guard(f"{action} {url}")
    # 审计日志
    conn = get_db()
    conn.execute(
        "INSERT INTO browser_log (ts,agent_id,action,url,status,detail) VALUES (?,?,?,?,?,?)",
        (datetime.now().isoformat(), agent_id, action, url,
         "success" if res.get("ok") else "error",
         (res.get("error") or "")[:500] or f"{res.get('title','')} · {res.get('count','')}项"[:500]),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "result": res, "action": action, "constitutional": const_hit}


@app.get("/api/browser/log")
def browser_log():
    conn = get_db()
    rows = conn.execute("SELECT id,ts,agent_id,action,url,status,detail FROM browser_log ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/file/log")
def file_log():
    conn = get_db()
    rows = conn.execute("SELECT id,ts,agent_id,action,path,status,detail FROM file_log ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]



# ── 元神工具调用（v0.27.0：Function Calling——对话直接驱动 exec/浏览器）──
META_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "在用户电脑上以用户权限执行 shell 命令（查看文件、运行脚本、查询系统状态、安装工具等）。危险命令（rm -rf、mkfs、shutdown、dd 等）会被自动拦截，需用户在终端面板手动确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令，例如 ls -la ~/Desktop"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_action",
            "description": "操作无头浏览器（系统 Chrome）：open(打开网页并读取正文)、screenshot(截图)、extract(按CSS选择器抓取文本)、fill(向输入框填入文本)、click(点击元素)。当用户要求查网页、看网页内容、截图、抓取网页数据时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["open", "screenshot", "extract", "fill", "click"]},
                    "url": {"type": "string", "description": "http/https 网址"},
                    "selector": {"type": "string", "description": "CSS 选择器（extract/fill/click 用）"},
                    "text": {"type": "string", "description": "填入输入框的文本（fill 用）"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取用户电脑上的文本文件内容（返回前 4000 字符）。路径限制在用户主目录（~）下，禁止访问 .ssh/.aws/.git 等敏感目录。当用户要求查看某文件内容、读配置、读代码时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径，例如 /Users/a13401098230/Desktop/notes.md 或 ~/Desktop/notes.md"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出用户电脑上某个目录下的文件和文件夹（一层，不递归）。路径限制在用户主目录下。当用户要求查看目录结构、找文件时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录绝对路径，例如 ~/Desktop 或 ~/WorkBuddy"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "在用户电脑主目录下递归搜索文件：按文件名关键词匹配（可选扩展名过滤，可选起始目录）。跳过 .ssh/.git/Library 等敏感目录。当用户要求找某个文件、搜某类文件时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "文件名关键词，例如 报告、计划、README"},
                    "path": {"type": "string", "description": "起始目录（可选，默认 ~），例如 ~/Desktop"},
                    "ext": {"type": "string", "description": "扩展名过滤（可选，不带点），例如 md、txt、py"}
                },
                "required": ["query"]
            }
        }
    }
]
# 批次 B / P2-1：工具分级——元神只搭基础设施（只读 + 搭建，不直接写文件），角色才动手（含写文件）。
# v5.6 借鉴 DeepSeek Harness「工作区限定」：角色执行时注入项目工作区（contextvar，
# 并发任务各自独立）。write_file 越界 → 拒绝并引导写工作区（除非 approval_mode=off 显式放行）。
import contextvars
_TOOL_WORKDIR: contextvars.ContextVar = contextvars.ContextVar("tool_workdir", default="")
_TOOL_PROJECT: contextvars.ContextVar = contextvars.ContextVar("tool_project", default="")
_TOOL_MODULE: contextvars.ContextVar = contextvars.ContextVar("tool_module", default="")
_TOOL_ACTOR: contextvars.ContextVar = contextvars.ContextVar("tool_actor", default="")
_TOOL_ROOT: contextvars.ContextVar = contextvars.ContextVar("tool_root", default="")
# v6.4 P0 快赢：成本可观测——角色执行期把当前任务 id 注入，LLM 调用由此按任务归因 token
_TOOL_TASK: contextvars.ContextVar = contextvars.ContextVar("tool_task", default="")
# v5.9：当前 trajectory run（仅在项目角色执行期被设置，用于工具级事件回放）
_TRAJ_RUN: contextvars.ContextVar = contextvars.ContextVar("traj_run", default=None)


def _tool_workdir() -> str:
    return _TOOL_WORKDIR.get() or ""


def _project_storage_root(proj) -> str:
    """双维度存储根目录（v5.8 P1）：优先 projects.storage_root（~/.fenshen/projects/<pid>），
    旧项目无 storage_root 时 fallback ~/Desktop/<项目名>。注意：仅看列值，不依赖目录是否已存在
    （新建项目目录尚未创建，不应误 fallback）。目录树：
      <root>/public/        项目公共文件夹
      <root>/members/<uid>/ 每个群聊成员单独文件夹
      <root>/modules/<mid>/ 按看板模块组织的成果文件夹"""
    if isinstance(proj, dict):
        sr = (proj.get("storage_root") or "").strip()
        name = (proj.get("name") or "未命名项目").strip().strip("/")
    else:
        sr, name = "", (proj or "未命名项目").strip().strip("/")
    if sr:
        return sr
    return os.path.expanduser(f"~/Desktop/{name}")


def _project_workdir(proj_name) -> str:
    """向后兼容别名（v5.8 P1：双维度存储后由 _project_storage_root 取代）。"""
    return _project_storage_root(proj_name)


def _ensure_project_dirs(pid: str, proj: dict) -> str:
    """建项时初始化双维度目录树；返回 storage_root。"""
    root = _project_storage_root(proj)
    os.makedirs(os.path.join(root, "public"), exist_ok=True)
    os.makedirs(os.path.join(root, "members"), exist_ok=True)
    # 每个已存在模块建专属目录
    try:
        conn = get_db()
        for m in conn.execute("SELECT id FROM modules WHERE project_id=?", (pid,)).fetchall():
            os.makedirs(os.path.join(root, "modules", m["id"]), exist_ok=True)
        conn.close()
    except Exception:
        pass
    return root


# ── v5.8 P2：UI 设计规范库 ───────────────────────────────────────
# 用户级目录（可编辑）；首次从内置副本初始化。后端内置副本在 backend/design_specs/。
DESIGN_SPECS_DIR = os.path.expanduser("~/.fenshen/design_specs")
DESIGN_SPECS_BUILTIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "design_specs")


def _ensure_design_specs():
    """v5.8 P2：确保用户级设计规范目录存在，首次从内置副本初始化（之后用户可自由编辑）。"""
    try:
        os.makedirs(DESIGN_SPECS_DIR, exist_ok=True)
        if os.path.isdir(DESIGN_SPECS_BUILTIN):
            for fn in os.listdir(DESIGN_SPECS_BUILTIN):
                if not fn.endswith(".json"):
                    continue
                dst = os.path.join(DESIGN_SPECS_DIR, fn)
                if not os.path.exists(dst):
                    try:
                        shutil.copy2(os.path.join(DESIGN_SPECS_BUILTIN, fn), dst)
                    except Exception:
                        pass
    except Exception:
        pass


def _load_design_specs():
    """返回可用设计规范列表 [{id,name,desc}]（扫描 ~/.fenshen/design_specs/*.json，排除 _ 前缀元文件）。"""
    _ensure_design_specs()
    out = []
    try:
        for fn in sorted(os.listdir(DESIGN_SPECS_DIR)):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            try:
                with open(os.path.join(DESIGN_SPECS_DIR, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
                out.append({
                    "id": d.get("id") or fn[:-5],
                    "name": d.get("name") or fn[:-5],
                    "desc": d.get("desc") or d.get("description") or "",
                })
            except Exception:
                continue
    except Exception:
        pass
    return out


def _load_design_spec(spec_id):
    """按 id 加载某设计规范全文（含 principles/color/typography/components 等），不存在/非法返回 None。"""
    if not spec_id:
        return None
    # 安全：仅允许字母数字下划线连字符，杜绝目录穿越
    if not re.match(r"^[A-Za-z0-9_\-]+$", spec_id or ""):
        return None
    p = os.path.join(DESIGN_SPECS_DIR, f"{spec_id}.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _design_spec_prompt(spec):
    """把规范 dict 渲染成注入角色上下文的纯文本块（无规范返回空串）。"""
    if not spec:
        return ""
    lines = [f"【本项目 UI 设计规范：{spec.get('name', '')}】"]
    if spec.get("principles"):
        lines.append("核心原则：" + "、".join(spec["principles"]))
    if spec.get("color"):
        c = spec["color"]
        parts = []
        for k in ("primary", "background", "text", "success", "warning", "danger"):
            if c.get(k):
                parts.append(f"{k}={c[k]}")
        if parts:
            lines.append("配色：" + "，".join(parts))
    if spec.get("typography"):
        t = spec["typography"]
        s = "，".join(f"{k}={v}" for k, v in t.items() if v)
        if s:
            lines.append("字体排印：" + s)
    if spec.get("components"):
        lines.append("组件规范：" + "；".join(spec["components"]))
    if spec.get("spacing"):
        lines.append("间距栅格：" + str(spec["spacing"]))
    if spec.get("reference"):
        lines.append("参考文档：" + str(spec["reference"]))
    lines.append("（角色产出 UI / 界面 / 前端代码时必须遵守上述规范，除非用户另有明确指示）")
    return "\n".join(lines)


WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "在用户电脑上写入文本文件（覆盖已有内容）。路径限制在用户主目录下，禁止写入敏感目录。文件内容上限 50KB。当用户要求创建/修改文档、代码、配置时使用。若处于项目执行上下文，文件必须写入该项目工作区目录内（越界会被拒绝）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径，例如 ~/Desktop/报告.md"},
                "content": {"type": "string", "description": "要写入的完整文本内容"}
            },
            "required": ["path", "content"]
        }
    }
}
# v5.6 PTC 简化版：批处理工具——一次往返组合多步工具（省 LLM 轮次与 token）
RUN_BATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "run_batch",
        "description": "把多步工具操作合并成一次调用（节省轮次与 token）。传入 JSON 数组，每项 {tool, args}；支持工具：read_file / write_file / list_files / search_files / exec_command。按顺序执行并返回每步结果。写文件同样须在项目工作区内。",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"}
                        },
                        "required": ["tool", "args"]
                    }
                }
            },
            "required": ["steps"]
        }
    }
}
ROLE_TOOLS = META_TOOLS + [WRITE_FILE_TOOL, RUN_BATCH_TOOL]  # 角色工具集（可写文件 + 批处理）


def _call_provider_tools(provider: str, base: str, key: str, model: str, history: list, system_prompt: str, tools: list):
    """openai 兼容通道返回 (完整 message(含 tool_calls), usage)；claude/ollama 退化为普通调用。"""
    if provider in ("claude", "ollama"):
        text, usage = _call_single_provider(provider, base, key, model, history, system_prompt)
        return {"role": "assistant", "content": text}, usage
    # 审查 #10（本次审查最贵的一个 bug）：这里原本是 max_tokens=800。
    # 写文件时 content 参数一长就被截断 → tool_calls.arguments 成了残缺 JSON →
    # 解析失败被静默吞成空 dict → 最终报给用户一句误导性的"路径不安全"。
    # 实际后果：write_file 历史 19 次调用失败 18 次，08-08 建落地页连败 18 次只剩空目录。
    payload = {"model": model, "messages": _merge_system(history, system_prompt),
               "temperature": 0.7, "max_tokens": 8192}
    if provider == "deepseek":
        # v4.2 关键修复：关闭 v4-flash 思考模式（理由见 _call_single_provider 同款注释；
        # 带 tools 的多轮循环在思考模式下必须回传 reasoning_content，否则 400）
        payload["thinking"] = {"type": "disabled"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    resp = requests.post(
        base + PROVIDER_PRESETS[provider]["chat"],
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    j = resp.json()
    u = j.get("usage") or {}
    usage = {"prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0)}
    return j["choices"][0]["message"], usage


def _exec_shell_tool(cmd: str):
    """在线程池中执行 shell（元神工具用），返回 (exit_code, output)。"""
    proc = subprocess.run(cmd, shell=True, cwd=os.path.expanduser("~"),
                          capture_output=True, text=True, timeout=30)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(无输出)"
    return proc.returncode, out


async def _run_meta_tool(name: str, args: dict, agent_id: str = META_PID) -> str:
    """执行工具（exec/浏览器）并落审计日志，返回给 LLM 的文本结果。重活（shell/playwright）走线程池。"""
    try:
        if name == "exec_command":
            cmd = (args.get("command") or "").strip()
            if not cmd:
                return "⛔ 命令为空"
            # 审查 V04：提示词注入链的终点就在这里——被污染的画像可以让元神自发调用 exec。
            # v4.0 起，AI 自主发起的命令默认每次都要真人在系统对话框点头（可在设置改策略）。
            if needs_approval(cmd):
                ok_approved, why = await human_approve(
                    "分身想在你电脑上执行命令",
                    f"发起者：{agent_id}（AI 自主调用）\n命令：{cmd}\n\n允许执行吗？",
                )
                if not ok_approved:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO exec_log (ts,agent_id,command,status,exit_code,output,confirmed) VALUES (?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(), agent_id, cmd, "blocked", -3, why, 0),
                    )
                    conn.commit()
                    conn.close()
                    return f"⛔ 未获授权，命令未执行：{why}"
            try:
                exit_code, out = await asyncio.to_thread(_exec_shell_tool, cmd)
            except subprocess.TimeoutExpired:
                exit_code, out = -1, "⛔ 命令执行超时 30s，已终止"
            conn = get_db()
            conn.execute(
                "INSERT INTO exec_log (ts,agent_id,command,status,exit_code,output,confirmed) VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(), agent_id, cmd,
                 "success" if exit_code == 0 else "error", exit_code, out[:4000], 1),
            )
            conn.commit()
            conn.close()
            return f"[exit {exit_code}]\n{out[:2000]}"
        elif name == "browser_action":
            action = args.get("action") or ""
            url = (args.get("url") or "").strip()
            if url and not (url.startswith("http://") or url.startswith("https://")):
                return "⛔ 仅允许 http/https 地址"
            res = await asyncio.to_thread(
                _browser_run, action, url, (args.get("selector") or "").strip(), args.get("text") or "",
            )
            conn = get_db()
            conn.execute(
                "INSERT INTO browser_log (ts,agent_id,action,url,status,detail) VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(), agent_id, action, url,
                 "success" if res.get("ok") else "error",
                 (res.get("error") or "")[:500] or f"{res.get('title','')} · {res.get('count','')}项"[:500]),
            )
            conn.commit()
            conn.close()
            if not res.get("ok"):
                return f"⛔ {res.get('error')}"
            if action == "screenshot":
                return f"[截图成功] {res.get('title','')} · {res.get('size',0)}B · URL: {res.get('url')}（图片可在浏览器面板查看）"
            return json.dumps(res, ensure_ascii=False)[:2000]
        elif name in ("read_file", "write_file", "list_files", "search_files"):
            # 批次 B / P2-4：write_file 纳入真人确认（严格模式 all 下拦截；danger/off 不拦但始终审计）
            if name == "write_file" and needs_file_approval():
                ok_approved, why = await human_approve(
                    "分身想在你电脑上写入文件",
                    f"发起者：{agent_id}（AI 自主调用）\n路径：{args.get('path', '')}\n"
                    f"内容大小：约 {len(args.get('content') or '')} 字符\n\n允许写入吗？",
                )
                if not ok_approved:
                    return f"⛔ 未获授权，文件未写入：{why}"
            return await _run_file_tool(name, args, agent_id)
        elif name == "run_batch":
            # v5.6 PTC 简化版：批量执行工具序列（一次往返省轮次/token）。写文件受工作区限定。
            steps = args.get("steps") or []
            if not isinstance(steps, list) or not steps:
                return "⛔ run_batch 需要 steps 数组"
            if len(steps) > 10:
                return "⛔ run_batch 单次最多 10 步"
            lines = []
            for i, st in enumerate(steps[:10]):
                if not isinstance(st, dict):
                    lines.append(f"第{i+1}步：跳过（非对象）")
                    continue
                tool, a = st.get("tool", ""), st.get("args") or {}
                if tool not in ("read_file", "write_file", "list_files", "search_files", "exec_command"):
                    lines.append(f"第{i+1}步 [{tool}]：不支持的工具")
                    continue
                try:
                    if tool == "exec_command":
                        res = await _run_meta_tool(tool, a, agent_id)
                        lines.append(f"第{i+1}步 [exec] {res}")
                    else:
                        res = await _run_file_tool(tool, a, agent_id)
                        lines.append(f"第{i+1}步 [{tool}] {res}")
                    # v5.9：回放事件——批量工具调用（仅项目角色执行期记录）
                    _tr = _TRAJ_RUN.get()
                    if _tr:
                        _trajectory_event(_tr[0], _tr[1], "tool", agent_id,
                                          f"🔧 {tool} → {str(res)[:200]}", {"tool": tool, "args": a})
                except Exception as e:
                    lines.append(f"第{i+1}步 [{tool}] 异常：{str(e)[:200]}")
            return "\n".join(lines)[:4000]
        return f"❌ 未知工具 {name}"
    except subprocess.TimeoutExpired:
        return "⛔ 命令执行超时 30s，已终止"
    except Exception as e:
        return f"❌ 工具执行异常：{type(e).__name__}: {str(e)[:300]}"


# ── 文件执行器（v0.27.0：读/写/列目录，安全护栏 + 全量审计）────────
FILE_SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", ".git", "Library", "System", "Applications", "private", "etc", "usr", "bin", "sbin", "var", "tmp", "cores"}
FILE_MAX_WRITE = 50 * 1024  # 单文件写入上限 50KB

# ── 批次A 代码能力强化：确定性质量门（沙箱验证）基础设施 ────────────────
# 行为由 meta_settings 中的开关控制，默认全部关闭 → 不改变既有执行/判定契约：
#   code_verify_enabled  "1" 开启确定性质量门（编译+可选测试）           默认 "0"
#   code_verify_run_tests "1" 编译通过后额外跑 pytest（存在时）          默认 "0"
#   code_verify_fix_rounds 自纠正最大轮数（0~5）                         默认 "2"
# 设计原则：无 .py 文件可验时一律 ok=True（不阻断非代码任务）；pytest 缺失/无测试则跳过（不视为失败）。
def _code_verify_enabled() -> bool:
    return get_setting("code_verify_enabled", "0") == "1"

def _code_verify_run_tests() -> bool:
    return get_setting("code_verify_run_tests", "0") == "1"

def _code_verify_fix_rounds() -> int:
    try:
        return max(0, min(5, int(get_setting("code_verify_fix_rounds", "2"))))
    except Exception:
        return 2

def _sandbox_verify(workdir: str, done_criteria: str = "") -> dict:
    """确定性质量门（批次A D1/D3/D4 共用）：
    对工作区内的 .py 文件做 py_compile 语法校验；编译全通过后，若开启则跑 pytest。
    返回结构化结果 {"ok","files","errors","ran_tests","summary"}。
    约定：无 .py 文件 → ok=True（无代码可验，不阻断）；pytest 未安装/无测试 → 跳过（不计失败）。"""
    if not os.path.isdir(workdir):
        return {"ok": True, "files": [], "errors": [], "ran_tests": False,
                "summary": "工作区不存在，无可验证文件"}
    py_files = []
    for _dir, _dirs, _files in os.walk(workdir):
        _dirs[:] = [d for d in _dirs if d not in ("__pycache__", ".venv", "venv", "node_modules", ".git")]
        for _f in _files:
            if _f.endswith(".py"):
                py_files.append(os.path.join(_dir, _f))
    errors = []
    for _f in py_files:
        try:
            _r = subprocess.run([sys.executable, "-m", "py_compile", _f],
                                capture_output=True, text=True, timeout=30)
            if _r.returncode != 0:
                errors.append(f"{os.path.relpath(_f, workdir)}:\n{_r.stderr.strip()[:600]}")
        except Exception as _e:
            errors.append(f"{os.path.relpath(_f, workdir)}: 编译异常 {_e}")
    ran_tests = False
    if not errors and _code_verify_run_tests():
        _has_tests = (os.path.isdir(os.path.join(workdir, "tests"))
                      or any(os.path.basename(f).startswith("test_") or os.path.basename(f) == "conftest.py"
                             for f in py_files))
        if _has_tests:
            try:
                _r = subprocess.run(
                    [sys.executable, "-m", "pytest", workdir, "-q", "--no-header", "-p", "no:cacheprovider"],
                    capture_output=True, text=True, timeout=180)
                ran_tests = True
                # pytest 退出码 5 = 无测试收集，视为跳过（不计失败）
                if _r.returncode not in (0, 5):
                    errors.append(f"pytest 失败:\n{(_r.stdout or _r.stderr).strip()[-800:]}")
            except FileNotFoundError:
                ran_tests = False  # pytest 未安装 → 跳过
            except Exception as _e:
                errors.append(f"pytest 异常: {_e}")
    if errors:
        _sum = f"未通过确定性质量门（{len(errors)} 处问题）：\n" + "\n---\n".join(errors[:5])
        return {"ok": False, "files": [os.path.relpath(f, workdir) for f in py_files],
                "errors": errors, "ran_tests": ran_tests, "summary": _sum[:1500]}
    _msg = f"确定性质量门通过（{len(py_files)} 个 .py 文件" + (f"，已运行 pytest" if ran_tests else "") + "）"
    return {"ok": True, "files": [os.path.relpath(f, workdir) for f in py_files],
            "errors": [], "ran_tests": ran_tests, "summary": _msg}

def run_quality_gate_project(pid: str, mod_id: str = "") -> dict:
    """对项目/模块工作区跑确定性质量门（供 API 与 verify_task 复用）。"""
    try:
        conn = get_db()
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        conn.close()
        if not proj:
            return {"ok": False, "errors": ["项目不存在"], "summary": "项目不存在", "files": [], "ran_tests": False}
        root = _project_storage_root(dict(proj))
        workdir = os.path.join(root, "modules", mod_id) if mod_id else root
        return _sandbox_verify(workdir)
    except Exception as e:
        return {"ok": False, "errors": [str(e)], "summary": f"质量门异常: {e}", "files": [], "ran_tests": False}


# ── 批次B 代码能力强化：D5 语义理解 / D6 代理VCS / D8 写测交叉验证 ────────────────
# 行为由 meta_settings 开关控制，默认全部关闭 → 不改变既有执行/判定契约：
#   code_semantic_enabled "1" 注入项目锚点(AGENTS.md式)+符号/依赖轻量索引  默认 "0"
#   code_vcs_enabled      "1" 每任务独立 git 分支+原子提交(安全子集白名单放行) 默认 "0"
#   code_xverify_enabled  "1" 代码任务派单时追加 tester 角色写测试交叉验证    默认 "0"
# 设计原则：默认全关；开启后也只对"代码类任务/工作区"生效，不干扰文档/闲聊类任务。
def _code_semantic_enabled() -> bool:
    return get_setting("code_semantic_enabled", "0") == "1"

def _code_vcs_enabled() -> bool:
    return get_setting("code_vcs_enabled", "0") == "1"

def _code_xverify_enabled() -> bool:
    return get_setting("code_xverify_enabled", "0") == "1"

# ── D5：项目锚点 + 符号/依赖轻量索引 ──────────────────────────────
_ANCHOR_NAMES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "README.md")

def _read_anchor_file(root: str) -> str:
    """读取项目锚点契约（构建/测试/风格），按优先级回退。"""
    for fn in _ANCHOR_NAMES:
        p = os.path.join(root, fn)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return f.read(3000)
            except Exception:
                pass
    return ""

def _symbol_index(workdir: str, limit: int = 60) -> str:
    """对工作区 .py 做轻量符号/依赖索引：def/class 定义 + import。便于角色理解结构，不幻觉 API。"""
    if not os.path.isdir(workdir):
        return ""
    defs, imps = [], []
    for _dir, _dirs, _files in os.walk(workdir):
        _dirs[:] = [d for d in _dirs if d not in ("__pycache__", ".venv", "venv", "node_modules", ".git")]
        for _f in _files:
            if not _f.endswith(".py"):
                continue
            full = os.path.join(_dir, _f)
            rel = os.path.relpath(full, workdir)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read(20000)
            except Exception:
                continue
            for ln in src.splitlines():
                s = ln.strip()
                if s.startswith("def ") or s.startswith("async def "):
                    m = re.match(r"(?:async\s+)?def\s+(\w+)", s)
                    if m:
                        defs.append(f"{rel}::{m.group(1)}")
                elif s.startswith("class "):
                    m = re.match(r"class\s+(\w+)", s)
                    if m:
                        defs.append(f"{rel}::{m.group(1)}")
                elif s.startswith("import ") or s.startswith("from "):
                    imps.append(s)
            if len(defs) >= limit:
                break
    out = []
    if defs:
        out.append("【本工作区符号（def/class，限前 %d）】\n%s" % (limit, "\n".join(defs[:limit])))
    if imps:
        out.append("【本工作区依赖（import，限前 30）】\n" + "\n".join(imps[:30]))
    return "\n\n".join(out)

def _build_codebase_context(root: str, mod_id: str, role: str) -> str:
    """D5：拼装注入角色执行的代码库上下文（锚点契约 + 符号索引）。"""
    if not _code_semantic_enabled():
        return ""
    parts = []
    anchor = _read_anchor_file(root)
    if anchor:
        parts.append("【项目锚点契约（AGENTS.md/CLAUDE.md 等，构建/测试/风格）】\n" + anchor)
    workdir = os.path.join(root, "modules", mod_id) if mod_id else root
    sym = _symbol_index(workdir)
    if sym:
        parts.append(sym)
    elif not anchor:
        return ""
    if parts:
        parts.append("（D5 语义理解：改代码前先参考上述契约与符号，避免漏下游依赖、避免幻觉不存在的 API。）")
    return "\n\n".join(parts)

# ── D6：代理级版本管理（任务分支 + 原子提交）─────────────────────
# 安全 git 子集白名单：开启代理VCS 时，这些命令在 all 审批模式下也不弹窗（仍受 DANGER_RE 兜底硬拦危险变体）。
_GIT_SAFE_RE = re.compile(
    r"^git\s+(?:"
    r"status|diff|log|branch|add|commit|init|merge\s+--ff-only|"
    r"checkout\s+(?:-[q]\s+)?-b(?:\s+\S+)?|"
    r"checkout\s+(?:-[q]\s+)?(?:main|fenshen/\S+)"
    r")(\s.*)?$",
    re.IGNORECASE,
)

def _vcs_safe_git(root: str, *args) -> dict:
    """在项目存储根执行一条 git 命令（仅内部安全子集使用），返回 {ok, out, code}。"""
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(无输出)"
        return {"ok": proc.returncode == 0, "out": out[:800], "code": proc.returncode}
    except Exception as e:
        return {"ok": False, "out": str(e)[:400], "code": -1}

def _vcs_init(root: str) -> bool:
    """初始化项目工作区为 git 仓库（若尚未初始化），默认分支 main。"""
    if not os.path.isdir(root):
        return False
    if os.path.isdir(os.path.join(root, ".git")):
        return True
    if not _vcs_safe_git(root, "init", "-q")["ok"]:
        return False
    _vcs_safe_git(root, "checkout", "-q", "-b", "main")
    gi = os.path.join(root, ".gitignore")
    try:
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write("# 分身代理VCS 自动生成\n.fenshen_cache/\n__pycache__/\n*.pyc\n.DS_Store\n")
    except Exception:
        pass
    return True

def _vcs_task_branch(root: str, task_id: str) -> str:
    """为任务创建/切到独立分支，返回分支名；失败返回空串。"""
    if not _vcs_init(root):
        return ""
    bname = f"fenshen/{task_id}"
    _vcs_safe_git(root, "checkout", "-q", "-B", bname)
    return bname

def _vcs_commit(root: str, task_id: str, msg: str) -> dict:
    """原子提交当前工作区改动（仅在该任务分支上）。无改动返回 skipped。"""
    if not _vcs_init(root):
        return {"ok": False, "out": "未初始化 git", "skipped": False}
    _vcs_safe_git(root, "add", "-A")
    st = _vcs_safe_git(root, "status", "--porcelain")
    if not st["out"].strip():
        return {"ok": True, "skipped": True, "out": "无改动"}
    r = _vcs_safe_git(root, "commit", "-q", "-m", msg[:200])
    return {"ok": r["ok"], "out": r["out"], "skipped": False}

def _vcs_merge(root: str, task_id: str) -> dict:
    """PR 式评审后再合：把任务分支 fast-forward 合并回 main。"""
    if not _vcs_init(root):
        return {"ok": False, "out": "未初始化 git"}
    bname = f"fenshen/{task_id}"
    _vcs_safe_git(root, "checkout", "-q", "-B", "main")  # -B：main 为 unborn 分支(首次)时也能创建并切回
    r = _vcs_safe_git(root, "merge", "--ff-only", bname)
    return {"ok": r["ok"], "out": r["out"]}

# ── D8：代码任务识别 + 写/测配对 ─────────────────────────────────
_CODE_ROLES = {"backend", "frontend", "fullstack", "dev", "engineer"}
_CODE_KW = ("实现", "写代码", "代码", "函数", "类定义", "模块", "修复", "bug", "api", "接口", "脚本",
            "pytest", "def ", "组件", "后端", "前端", "写测试", "单元测试")

def _is_code_task(act: dict) -> bool:
    """判断该 action 是否为代码类任务（用于 D8 配对 tester）。"""
    role = (act.get("role") or "").lower()
    if role in _CODE_ROLES:
        return True
    txt = f"{act.get('task_name','')} {act.get('detail','')} {act.get('done_criteria','')}".lower()
    return any(k in txt for k in _CODE_KW)


# ── 批次C 代码能力强化：D7 流式/模型/质量提示 · D9 疗效归因接入代码 · D10 宪法代码护栏 ────────
# 行为由 meta_settings 开关控制，默认全部关闭 → 不改变既有执行/判定契约：
#   code_stream_enabled      "1" 开放 /api/meta/code_stream 流式代码生成(SSE)             默认 "0"
#   code_model_enabled       "1" 代码角色优先走代码专用强模型(CODE_MODEL_RECS)            默认 "0"
#   code_quality_prompt      "1" 代码角色系统提示追加强约束(禁编造API/类型注解/diff有界/加测试) 默认 "0"
#   code_attribution_enabled "1" 验证"修好/失败"写入经验库并打5维权重，下个同类任务召回       默认 "0"
#   code_static_scan_enabled "1" 写代码文件前静态扫描门(禁硬编码密钥/破坏性系统调用)        默认 "0"
# 设计原则：默认全关；开启后也只对"代码类任务/代码文件"生效，不影响文档/闲聊类任务与核心契约。

def _code_stream_enabled() -> bool:
    return get_setting("code_stream_enabled", "0") == "1"

def _code_model_enabled() -> bool:
    return get_setting("code_model_enabled", "0") == "1"

def _code_quality_prompt_enabled() -> bool:
    return get_setting("code_quality_prompt", "0") == "1"

def _code_attribution_enabled() -> bool:
    return get_setting("code_attribution_enabled", "0") == "1"

def _code_static_scan_enabled() -> bool:
    return get_setting("code_static_scan_enabled", "0") == "1"

# ── P0 快赢：看板每格 token 成本透出（默认关；开启后矩阵每格聚合该格任务累计 token）──
def _cost_visibility_enabled() -> bool:
    return get_setting("cost_visibility_enabled", "0") == "1"


# ── D7：代码专用模型偏好 ─────────────────────────────
# 开启后，代码角色走更擅长编码/长上下文的模型（仍走 _available_providers 的 FALLBACK_ORDER 兜底）。
CODE_MODEL_RECS = {
    "backend":   {"provider": "deepseek", "model": "deepseek-v4-flash"},
    "frontend":  {"provider": "openai",   "model": "gpt-4o"},
    "fullstack": {"provider": "deepseek", "model": "deepseek-v4-flash"},
    "dev":       {"provider": "deepseek", "model": "deepseek-v4-flash"},
    "engineer":  {"provider": "deepseek", "model": "deepseek-v4-flash"},
}

def _code_model_for_role(role: str) -> dict | None:
    """D7：返回该角色的代码专用模型(若启用且为代码角色)，否则 None。"""
    if not _code_model_enabled():
        return None
    return CODE_MODEL_RECS.get((role or "").lower())

def _key_for_provider(provider: str, role: str = "backend"):
    """返回 (base, key, model) 用于指定 provider；从配置取 key，模型用该 provider 默认。无 key 则回退 secret。"""
    for p, b, k, m in _available_providers(role):
        if p == provider:
            return b, k, m
    if provider == "deepseek":
        return PROVIDER_PRESETS["deepseek"]["base"], "", "deepseek-v4-flash"
    return PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])["base"], "", ""


# ── D7：代码质量硬约束系统提示 ───────────────────────
_CODE_QUALITY_CONSTRAINTS = (
    "【代码质量硬约束（宪法级，必须执行）】\n"
    "1. 禁止编造不存在的 API / 函数 / 库：改动前先参考上方「项目锚点契约」与「本工作区符号」，"
    "调用必须落在已知符号与依赖内；不确定时先搜索再调用。\n"
    "2. 强制类型注解：函数签名必须带类型标注（Python 用 typing，TS 用类型声明），降低误用。\n"
    "3. diff-bounded（最小改动）：只改实现目标所需的最小 diff，不重构无关代码、不整体覆写既有文件。\n"
    "4. 配套测试：实现功能同时给出可运行的单元测试（pytest / 前端测试），新行为有测试覆盖，新路径有断言。\n"
    "5. 失败显式：错误必须被捕获并给出可定位信息，禁止静默吞掉异常（bare except / pass）。"
)

def _build_code_quality_prompt(role: str) -> str:
    """D7：代码角色追加的质量系统提示块；非代码角色或开关关闭则返回空。"""
    if not _code_quality_prompt_enabled():
        return ""
    if (role or "").lower() not in _CODE_ROLES:
        return ""
    return "\n\n" + _CODE_QUALITY_CONSTRAINTS


# ── D7：流式代码生成（SSE）──────────────────────────
def _call_single_provider_stream(provider, base, key, model, history, system_prompt):
    """D7：逐 token 流式调用单个模型，生成器 yield 文本片段。失败抛异常。
    仅用于 /api/meta/code_stream（开关门控），不接入核心同步 chat 管线，避免改变既有执行契约。"""
    msgs = _merge_system(history, system_prompt)
    if provider == "ollama":
        resp = requests.post(base + "/api/chat", json={"model": model, "messages": msgs, "stream": True},
                             timeout=120, stream=True)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            c = (j.get("message") or {}).get("content") or ""
            if c:
                yield c
    elif provider == "claude":
        sys = system_prompt or next((m["content"] for m in history if m["role"] == "system"), "")
        body = {"model": model, "system": sys,
                "messages": [m for m in history if m["role"] != "system"],
                "max_tokens": 2000, "temperature": 0.7, "stream": True}
        resp = requests.post(base + "/v1/messages",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                             json=body, timeout=120, stream=True)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                j = json.loads(data)
            except Exception:
                continue
            if j.get("type") == "content_block_delta":
                c = (j.get("delta") or {}).get("text") or ""
                if c:
                    yield c
    else:  # openai / deepseek 兼容 OpenAI 格式
        payload = {"model": model, "messages": msgs, "temperature": 0.7, "max_tokens": 2000, "stream": True}
        resp = requests.post(base + PROVIDER_PRESETS[provider]["chat"],
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            json=payload, timeout=120, stream=True)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                j = json.loads(data)
            except Exception:
                continue
            c = (j.get("choices", [{}])[0].get("delta", {}) or {}).get("content") or ""
            if c:
                yield c


# ── D9：疗效归因接入代码 ─────────────────────────────
# ── 安全自进化引擎（α 批次 E1/E2/E3）：经验泛化 + 召回闭环 + 质量门 ──
def _evolution_record_enabled() -> bool:
    return get_setting("evolution_record_enabled", "0") == "1"
def _evolution_recall_enabled() -> bool:
    return get_setting("evolution_recall_enabled", "0") == "1"
def _evolution_quality_gate_enabled() -> bool:
    return get_setting("evolution_quality_gate_enabled", "0") == "1"
def _evolution_heldout_enabled() -> bool:
    return get_setting("evolution_heldout_enabled", "0") == "1"
def _evolution_lineage_enabled() -> bool:
    return get_setting("evolution_lineage_enabled", "0") == "1"
def _evolution_guardrail_enabled() -> bool:
    return get_setting("evolution_guardrail_enabled", "0") == "1"
def _memory_archive_enabled() -> bool:
    return get_setting("memory_archive_enabled", "0") == "1"
def _memory_panel_enabled() -> bool:
    return get_setting("memory_panel_enabled", "0") == "1"

# E4：held-out 灰度晋升/淘汰阈值（受 evolution_heldout_enabled 控制；关→保持 D9 既有宽松 freq>=10/neg>=2）。
_HELDOUT_PROMOTE_USES = 3   # 成功复用达此次数 → 晋升 persistent=1
_HELDOUT_DEMOTE_NEG = 2      # 连续负反馈达此次数 → 淘汰 eliminated=1
def _heldout_promote_uses() -> int:
    return _HELDOUT_PROMOTE_USES if _evolution_heldout_enabled() else 10
def _heldout_demote_neg() -> int:
    return _HELDOUT_DEMOTE_NEG if _evolution_heldout_enabled() else 2

# E6：无损会话归档 —— 节点树 + 压缩阈值（~60% 思路：预算内留足余量，临近即压，规避断崖）。
_ARCHIVE_NODE_BUDGET = 24        # 非归档节点累计达此数 → 触发压缩
_ARCHIVE_KEEP_RECENT = 6         # 压缩时保留最近 N 条不归档（keepRecent）
_ARCHIVE_TOKEN_PER_CHAR = 0.5    # 粗略 token 估算（中文约 2 字符/token）

def _est_tokens(text: str) -> int:
    return max(1, int((len(text or "") * _ARCHIVE_TOKEN_PER_CHAR)))

def _record_session_node(project_id, role, content, kind="message", session_id="", parent_id=0):
    """E6：把一条会话节点无损落库（扁平归档，parent_id 预留树扩展）。
    受 memory_archive_enabled 门控；关→no-op（零侵入）。返回节点 id。"""
    if not _memory_archive_enabled():
        return 0
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO session_nodes (project_id,session_id,parent_id,role,kind,content,token_est,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (project_id, session_id, parent_id, role, kind, content or "",
             _est_tokens(content), datetime.now().isoformat()))
        nid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return nid

def _summarize_nodes_text(nodes):
    """E6：压缩摘要（无损——原节点保留）。抽取式兜底（不依赖外部 LLM，保证确定性 + 可离线测试）。"""
    try:
        if nodes:
            lines = []
            for n in nodes:
                c = (n.get("content") or "").strip().replace("\n", " ")
                if c:
                    lines.append("· " + c[:80])
            return "【会话摘要】共归档 %d 条：\n" % len(nodes) + "\n".join(lines[:_ARCHIVE_KEEP_RECENT * 2])
    except Exception:
        pass
    return "【会话摘要】"

def _maybe_compact_session(project_id, session_id=""):
    """E6：当非归档节点数达预算(~60%思路) → 把最旧一批压成摘要节点，原节点标记 archived=1（不删，无损）。
    受 memory_archive_enabled 门控；关→no-op。返回摘要节点 id（未触发返回 0）。"""
    if not _memory_archive_enabled():
        return 0
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id,role,content FROM session_nodes "
            "WHERE project_id=? AND session_id=? AND is_summary=0 AND archived=0 ORDER BY id ASC",
            (project_id, session_id)).fetchall()
        if len(rows) < _ARCHIVE_NODE_BUDGET:
            return 0
        to_archive = rows[:-_ARCHIVE_KEEP_RECENT]
        if not to_archive:
            return 0
        summary = _summarize_nodes_text([dict(r) for r in to_archive])
        cur = conn.execute(
            "INSERT INTO session_nodes (project_id,session_id,parent_id,role,kind,content,token_est,is_summary,summary_of,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, session_id, 0, "system", "summary", summary,
             _est_tokens(summary), 1,
             ",".join(str(r["id"]) for r in to_archive), datetime.now().isoformat()))
        sid = cur.lastrowid
        ids = [r["id"] for r in to_archive]
        q = "UPDATE session_nodes SET archived=1 WHERE id IN (%s)" % ",".join("?" * len(ids))
        conn.execute(q, ids)
        conn.commit()
    finally:
        conn.close()
    return sid

def _role_relevance(role: str) -> float:
    """E1：按角色相关度算 relevance（非 is_code 二元）。代码角色最高，知识/内容/设计次之。"""
    r = (role or "").lower()
    if r in _CODE_ROLES:
        return 0.7
    if r in ("copywriter", "designer", "researcher", "pm", "architect", "data", "analyst"):
        return 0.6
    return 0.5

def _record_experience(task: dict, success: bool, detail: str = "",
                       has_standard: bool = False, standard_result: str = ""):
    """E1 经验泛化 + E3 质量门：把任务验收结果写入经验库（代码/非代码通用）。
    门控——代码任务沿用 D9 的 code_attribution_enabled；非代码任务需 evolution_record_enabled。
    E3（evolution_quality_gate_enabled 开启时）：
      有显式标准且通过 → 高权重(w0.7/trust0.8)；无标准仅靠启发式 done → 低信任草稿(w0.3/trust0.3, persistent=0)。
    两开关皆关 → 直接返回（不改既有行为）。"""
    role = (task.get("owner_role") or "").lower()
    is_code = role in _CODE_ROLES
    if is_code:
        if not _code_attribution_enabled():
            return
    else:
        if not _evolution_record_enabled():
            return
    try:
        name = task.get("name") or "任务"
        scenario = (f"代码任务实现/修复: {role} {name}" if is_code
                    else f"{role} 任务: {name}")
        if success:
            if _evolution_quality_gate_enabled() and has_standard and standard_result in ("done", "pass", "达标"):
                w, trust = 0.7, 0.8
                outcome = "✅ 成功：有明确完成标准且验收通过。" + (f" 验证方式：{detail}" if detail else "")
                lesson = f"有效模式：{detail or '先跑测试定位错误→最小修改→再跑测试验证'}"
            elif _evolution_quality_gate_enabled():
                # 无标准仅靠启发式 done → 低信任草稿（防错误复利，不主动召回）
                w, trust = 0.3, 0.3
                outcome = "🟡 启发式通过（无显式完成标准，弱 grounding 草稿）：" + (detail or "")
                lesson = f"待验证模式：{detail[:200] or '产出长度正常但缺标准，建议后续补完成标准'}"
            else:
                # E3 关闭 → 保持 D9 原计分
                w, trust = 0.7, 0.8
                outcome = "✅ 成功：确定性质量门+验收通过。" + (f" 验证方式：{detail}" if detail else "")
                lesson = f"有效模式：{detail or '先跑测试定位错误→最小修改→再跑测试验证'}"
            category = "success"
        else:
            w, trust = 0.3, 0.3
            outcome = (f"❌ 失败：{detail[:400]}" if detail else "❌ 失败：未通过验收/质量门")
            lesson = f"踩坑教训：{detail[:200] or '未通过质量门'}"
            category = "failure"
        rel = _role_relevance(role)
        snippet = (lesson or outcome)[:200]
        source_task_id = task.get("id") or task.get("task_id") or ""
        acceptance_result = (standard_result or ("pass" if success else "fail"))[:40]
        version_fingerprint = f"{SEMVER}:{COMMIT}"
        unsafe = 0
        if _evolution_guardrail_enabled() and _experience_contains_blocked(lesson + " " + snippet + " " + outcome):
            unsafe = 1  # E8：含违禁模式→标记，不晋升不主动召回（留草稿供人工审）
        conn = get_db()
        conn.execute(
            "INSERT INTO experiences "
            "(scenario, goal, outcome, lesson, category, project_id, ts, frequency, relevance, recency, "
            "explicit_feedback, trust_score, weight, last_used, neg_streak, persistent, eliminated, source, "
            "source_task_id, acceptance_result, snippet, version_fingerprint, unsafe) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scenario, name, outcome, lesson, category, task.get("project_id") or "",
             datetime.now().isoformat(), 0, rel, 1.0,
             0.0, trust, w, datetime.now().isoformat(), 0, 0, 0,
             ("code_task" if is_code else "task"),
             source_task_id, acceptance_result, snippet, version_fingerprint, unsafe))
        conn.commit(); conn.close()
        try:
            _maintain_attribution()
        except Exception:
            pass
    except Exception:
        pass


# ── D10：元神宪法代码护栏（静态扫描门）─────────────────
# 拦截：硬编码密钥/凭证、针对主目录或根的破坏性系统调用（宪法价值锚：不泄露、不破坏用户资产）。
# 告警（不拦截）：外联未知域名、subprocess/os.system/eval/exec 调用。
_CODE_STATIC_BLOCK = [
    re.compile(r"(?:api[_-]?key|secret|password|passwd|token|access[_-]?key|private[_-]?key|client[_-]?secret)\s*[:=]\s*['\"][A-Za-z0-9_\-]{8,}['\"]", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?:shutil\.rmtree|os\.remove|os\.unlink|os\.rmdir)\s*\(\s*['\"]?~"),
    re.compile(r"rm\s+-rf\s+(?:/|~|['\"]?/|['\"]?~)"),
    re.compile(r"os\.system\s*\(\s*['\"]rm\s+-rf"),
]

def _experience_contains_blocked(text: str) -> bool:
    """E8：经验 lesson/snippet/outcome 是否含宪法护栏违禁模式（硬编码密钥/危险删除）。
    直接套 _CODE_STATIC_BLOCK（不依赖文件后缀，因经验文本无 path 概念）。命中→该经验不得晋升、不主动召回。"""
    if not text:
        return False
    return any(rx.search(text) for rx in _CODE_STATIC_BLOCK)
_CODE_STATIC_WARN = [
    re.compile(r"https?://[^\s'\"\)]+", re.IGNORECASE),
    re.compile(r"(?:subprocess|os\.system|eval|exec)\s*\("),
]
_CODE_EXTS = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".sh", ".vue", ".php", ".rb")

def _code_static_scan(path: str, content: str) -> dict:
    """D10：宪法代码护栏——写代码文件前静态扫描。返回 {blocked, warnings, reasons}。开关关闭或非代码文件直接放行。"""
    if not _code_static_scan_enabled():
        return {"blocked": False, "warnings": [], "reasons": []}
    if not (path or "").lower().endswith(_CODE_EXTS):
        return {"blocked": False, "warnings": [], "reasons": []}
    reasons, warnings = [], []
    for rx in _CODE_STATIC_BLOCK:
        m = rx.search(content or "")
        if m:
            reasons.append(f"命中宪法护栏模式（{m.group(0)[:60]}）")
    for rx in _CODE_STATIC_WARN:
        if rx.search(content or ""):
            warnings.append("代码含外联/危险调用，请确保目标可信且符合用户利益")
    return {"blocked": bool(reasons), "warnings": warnings, "reasons": reasons}


def _safe_file_path(path: str):
    """校验并规范化路径：必须在用户主目录下且不触碰敏感目录。返回绝对路径或 None。"""
    if not path or not isinstance(path, str):
        return None
    path = os.path.expanduser(path.strip())
    if not path.startswith("/"):
        return None
    home = os.path.expanduser("~")
    try:
        real = os.path.realpath(path)
        real_home = os.path.realpath(home)
    except Exception:
        return None
    if not (real == real_home or real.startswith(real_home + os.sep)):
        return None
    parts = real[len(real_home):].strip(os.sep).split(os.sep)
    if any(p in FILE_SENSITIVE_PARTS for p in parts):
        return None
    return real


def _list_files_tool(path: str):
    real = _safe_file_path(path)
    if not real:
        return "⛔ 路径不安全或被禁止（仅限用户主目录下，避开 .ssh/.git/系统目录等）"
    if not os.path.isdir(real):
        return f"⛔ 目录不存在: {path}"
    try:
        items = sorted(os.listdir(real))
        lines = []
        for it in items:
            full = os.path.join(real, it)
            is_dir = os.path.isdir(full)
            size = "" if is_dir else f"（{os.path.getsize(full)}B）"
            lines.append(f"{'📁' if is_dir else '📄'} {it}{size}")
        return f"[目录 {path} · {len(items)} 项]\n" + "\n".join(lines[:100])
    except Exception as e:
        return f"⛔ 列目录失败: {e}"


def _read_file_tool(path: str):
    real = _safe_file_path(path)
    if not real:
        return "⛔ 路径不安全或被禁止（仅限用户主目录下，避开 .ssh/.git/系统目录等）"
    if not os.path.isfile(real):
        return f"⛔ 文件不存在: {path}"
    try:
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(4000)
        return f"[文件 {path} · {os.path.getsize(real)}B]\n{content}"
    except Exception as e:
        return f"⛔ 读取失败: {e}"


def _write_file_tool(path: str, content: str):
    real = _safe_file_path(path)
    if not real:
        return "⛔ 路径不安全或被禁止（仅限用户主目录下，避开 .ssh/.git/系统目录等）"
    # v5.6 借鉴 Harness「工作区限定」：角色执行时写文件默认限定在项目工作区内（越界拒绝，除非审批关闭）
    wd = _tool_workdir()
    if wd and not (real == wd or real.startswith(wd.rstrip(os.sep) + os.sep)):
        if approval_mode() != "off":
            return (f"⛔ 写文件越界：目标不在项目工作区内。\n工作区：{wd}\n请求路径：{path}\n"
                    f"请把产出写入工作区目录（这是分身的安全边界）。")
    if len(content) > FILE_MAX_WRITE:
        return f"⛔ 内容超过 {FILE_MAX_WRITE // 1024}KB 上限"
    # 批次C D10：元神宪法代码护栏——代码文件静态扫描门（开关关闭则跳过，非代码文件跳过）
    _scan = _code_static_scan(path, content)
    if _scan["blocked"]:
        return ("⛔ 宪法代码护栏拦截：\n" + "\n".join(_scan["reasons"])
                + "\n（生成代码不得硬编码密钥、不得对主目录/根执行破坏性删除。"
                + "请改用环境变量/配置注入凭证；破坏性操作需用户显式确认。）")
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w", encoding="utf-8") as f:
            f.write(content)
        sz = os.path.getsize(real)
        # v5.8 P1：写成功后索引到 files 表（双维度：按模块 + 按成员），供"点看板模块开文件"
        _pid = _TOOL_PROJECT.get() or "__meta__"
        _mid = _TOOL_MODULE.get() or ""
        _actor = _TOOL_ACTOR.get() or "元神"
        _root = _TOOL_ROOT.get() or ""
        try:
            rel = os.path.relpath(real, _root) if _root else real
        except Exception:
            rel = real
        db_write(
            "INSERT INTO files (project_id,module_id,owner_member,rel_path,abs_path,name,kind,size,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_pid, _mid, _actor, rel, real, os.path.basename(real), "file", sz, datetime.now().isoformat()),
        )
        return f"[已写入] {path} · {sz}B"
    except Exception as e:
        return f"⛔ 写入失败: {e}"


def _search_files_tool(query: str, path: str = "", ext: str = "", max_results: int = 30):
    """递归搜索文件名（跳过敏感目录，限深度防卡死）。"""
    query = (query or "").strip().lower()
    if not query:
        return "⛔ 搜索关键词不能为空"
    start = _safe_file_path(path) if path else os.path.expanduser("~")
    if not start or not os.path.isdir(start):
        return f"⛔ 起始目录不可用: {path or '~'}"
    ext = (ext or "").lower().lstrip(".")
    hits = []
    for root, dirs, files in os.walk(start):
        # 跳过敏感目录
        dirs[:] = [d for d in dirs if d not in FILE_SENSITIVE_PARTS and not d.startswith(".")]
        depth = root[len(start):].count(os.sep)
        if depth > 6:
            dirs[:] = []
            continue
        for f in files:
            if ext and not f.lower().endswith("." + ext):
                continue
            if query in f.lower():
                full = os.path.join(root, f)
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = 0
                hits.append(f"{full}（{size}B）")
                if len(hits) >= max_results:
                    return f"[搜索 \"{query}\" · 命中 {len(hits)}（已达上限）]\n" + "\n".join(hits)
    if not hits:
        return f"[搜索 \"{query}\" · 未找到匹配文件]"
    return f"[搜索 \"{query}\" · 命中 {len(hits)} 个]\n" + "\n".join(hits)


async def _run_file_tool(name: str, args: dict, agent_id: str) -> str:
    """执行文件工具（线程池）并落审计 file_log。"""
    path = args.get("path") or ""
    content = args.get("content") or ""
    if name == "list_files":
        out = await asyncio.to_thread(_list_files_tool, path)
    elif name == "read_file":
        out = await asyncio.to_thread(_read_file_tool, path)
    elif name == "write_file":
        out = await asyncio.to_thread(_write_file_tool, path, content)
    elif name == "search_files":
        out = await asyncio.to_thread(_search_files_tool, args.get("query") or "", path, args.get("ext") or "")
    else:
        out = f"⛔ 未知文件工具 {name}"
    real = _safe_file_path(path) or path
    conn = get_db()
    conn.execute(
        "INSERT INTO file_log (ts,agent_id,action,path,status,detail) VALUES (?,?,?,?,?,?)",
        (datetime.now().isoformat(), agent_id, name, real,
         "success" if not out.startswith("⛔") else "error", out[:500]),
    )
    conn.commit()
    conn.close()
    return out


def _match_skill_steps(system_prompt: str, user_text: str) -> str:
    """P3-2：按 trigger_words 命中 enabled 技能 → 返回注入文本（活配件）。
    命中规则：技能触发词（逗号分隔）任一出现在 system_prompt 或最近用户消息中，即注入其步骤。"""
    try:
        conn = get_db()
        rows = conn.execute("SELECT name,trigger_words,steps FROM skills WHERE enabled=1").fetchall()
        conn.close()
    except Exception:
        return ""
    haystack = f"{system_prompt or ''}\n{user_text or ''}"
    parts = []
    for r in rows:
        words = [w.strip() for w in (r["trigger_words"] or "").replace("，", ",").split(",") if w.strip()]
        if not words:
            continue
        if any(w in haystack for w in words):
            try:
                steps = json.loads(r["steps"] or "[]")
            except Exception:
                steps = []
            if steps:
                lines = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps[:6]))
                parts.append(f"【技能：{r['name']}】请按以下步骤执行：\n{lines}")
    return "\n\n".join(parts)


async def _chat_with_tools(agent_id: str, history: list, system_prompt: str, tools: list = None,
                          phase: str = "", scope_modules: int = -1, project_id: str = "",
                          member_override: tuple = None) -> str:
    """通用工具对话循环（元神/群聊共用，v0.27.0）：最多 6 轮（支持多步工具操作）。
    批次 B / P2-1：tools 参数控制工具集——元神默认 META_TOOLS（只读+搭建，无写文件），
    角色默认 ROLE_TOOLS（含 write_file 可动手产出）。
    批次 C / P3-2：命中 trigger_words 的启用技能会注入 system_prompt（活配件）。"""
    cands = _available_providers(agent_id, member_override)
    if not cands:
        return "[分身·离线] 当前该角色未配置可用模型 Key。"
    tool_list = tools if tools is not None else (META_TOOLS if agent_id == META_PID else ROLE_TOOLS)
    last_text = (history[-1].get("content") or "") if history else ""
    inject = _match_skill_steps(system_prompt, last_text)
    if inject:
        system_prompt = f"{system_prompt}\n\n{inject}"
    last_err = ""
    last_content = ""
    for _round in range(6):
        for provider, base, key, model in cands:
            try:
                t0 = datetime.now()
                # debug v4.1：连接类瞬时故障（RemoteDisconnected 等）原地重试一次
                for _attempt in range(2):
                    try:
                        msg, usage = _call_provider_tools(provider, base, key, model, history, system_prompt, tool_list)
                        break
                    except Exception as e:
                        last_err = f"{provider}: {e}"
                        if _attempt == 0 and _is_conn_error(e):
                            last_err += "（连接异常，已重试一次）"
                            continue
                        raise
                latency = int((datetime.now() - t0).total_seconds() * 1000)
                _log_usage(agent_id, provider, model, latency, "success",
                           usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                           phase=phase, scope_modules=scope_modules, project_id=project_id)
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    history.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
                    for tc in tool_calls:
                        fname = tc.get("function", {}).get("name", "")
                        raw_args = tc.get("function", {}).get("arguments") or "{}"
                        try:
                            fargs = json.loads(raw_args)
                            tool_result = await _run_meta_tool(fname, fargs, agent_id)
                            # v5.9：回放事件——工具调用（仅项目角色执行期记录）
                            _tr = _TRAJ_RUN.get()
                            if _tr:
                                _trajectory_event(_tr[0], _tr[1], "tool", agent_id,
                                                  f"🔧 {fname} → {str(tool_result)[:200]}",
                                                  {"tool": fname, "args": fargs})
                        except json.JSONDecodeError:
                            # 审查 #10 配套修复：旧版把解析失败静默吞成 {}，于是后续报出
                            # 完全不相干的"路径不安全"。现在如实告诉模型参数坏在哪，让它重发。
                            tool_result = (
                                f"⛔ 参数解析失败：{fname} 收到的 JSON 不完整"
                                f"（长度 {len(raw_args)} 字符，可能因输出上限被截断）。"
                                "请把内容拆成多次写入，或缩短单次内容后重试。"
                            )
                        history.append({"role": "tool", "tool_call_id": tc.get("id"),
                                        "content": tool_result})
                    break  # 工具已执行，进入下一轮让 LLM 总结
                last_content = (msg.get("content") or "").strip()
                return last_content
            except Exception as e:
                last_err = f"{provider}: {e}"
                continue
        else:
            break  # 所有 provider 失败
    if last_err:
        return f"[分身·降级] 工具调用链路异常（{last_err[:150]}）。"
    if not last_content:
        # 若 LLM 未生成总结，从已执行的 tool 结果中提炼一句可读摘要
        tool_lines = []
        for h in history:
            if h.get("role") == "tool":
                content = (h.get("content") or "").strip()
                if content and not content.startswith("⛔"):
                    tool_lines.append(content[:120].replace("\n", " "))
        if tool_lines:
            return "已执行动作：\n" + "\n".join(f"· {line}" for line in tool_lines[-4:])
        # 审查 #6：这里原本返回"已收到你的需求。我会把它拆解成任务并安排角色执行……"
        # ——一句什么都没做却听起来一切正常的假承诺，注释里写的理由是"避免空泛的失败感"，
        # 而且是从旧版的诚实提示主动改过来的。自欺比 bug 更贵：用户会基于假信号做决策。
        # 恢复诚实报错，宁可难看，也不能骗人。
        return ("这次没有产出。模型既没有生成回复，也没有调用任何工具，"
                "我不清楚原因——可能是模型额度、网络，或提示词把它绕住了。"
                "建议：重试一次；仍然如此就换个说法，或到设置里检查模型配置。")
    return last_content



# ── API：长期记忆（记忆系统）─────────────────────────────────────
@app.get("/api/memory")
def list_memory():
    conn = get_db()
    rows = conn.execute("SELECT * FROM long_term_memory ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/memory")
async def add_memory(req: Request):
    data = await req.json()
    conn = get_db()
    conn.execute(
        "INSERT INTO long_term_memory (category,content,source,ts) VALUES (?,?,?,?)",
        (data.get("category", "general"), data.get("content", ""), data.get("source", ""), datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/memory/{mid}")
async def update_memory(mid: int, req: Request):
    data = await req.json()
    conn = get_db()
    conn.execute(
        "UPDATE long_term_memory SET category=?, content=? WHERE id=?",
        (data.get("category"), data.get("content"), mid),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/memory/{mid}")
def delete_memory(mid: int):
    conn = get_db()
    conn.execute("DELETE FROM long_term_memory WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── 蒸馏引擎 v4.0（真 LLM 抽取，关键词兜底）────────────────────────
# 审查 #13：旧版三种"蒸馏"对外都标称 LLM 智能抽取，实际全是关键词正则拼装——
# memory 把命中关键词的原句整条存下来，skills 取前 16 个字当技能名、steps 恒为空。
# 现在真接 LLM，并在返回里如实标注 method（llm / keyword），不再含糊其辞。

def _recent_meta_texts(limit: int = 30):
    conn = get_db()
    rows = conn.execute(
        "SELECT sender,kind,text FROM messages WHERE project_id=? ORDER BY id DESC LIMIT ?",
        (META_PID, limit),
    ).fetchall()
    conn.close()
    return list(reversed([dict(r) for r in rows]))


def _parse_json_array(text: str):
    """从 LLM 回复里挖出 JSON 数组，容忍 ```json 包裹与前后废话。"""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


async def _llm_extract(system: str, user: str):
    """调用 LLM 做结构化抽取，返回 (列表, 方法说明)。失败返回 (None, 原因)。"""
    try:
        text = await asyncio.to_thread(call_llm, META_PID, [{"role": "user", "content": user}], system)
    except Exception as e:
        return None, f"LLM 调用异常：{e}"
    if text.startswith("[元神·离线]") or text.startswith("[元神·降级]"):
        return None, text
    items = _parse_json_array(text)
    if items is None:
        return None, "LLM 返回的不是合法 JSON 数组"
    return items, "llm"


MEMORY_DISTILL_SYSTEM = """你是记忆提炼器。从对话中找出「值得长期记住」的信息，输出 JSON 数组。

只提炼这四类，其余一律忽略：
- preference：用户的偏好与风格（喜欢什么、讨厌什么、习惯怎么做）
- rule：用户定下的硬性规矩与红线（必须/禁止）
- fact：关于用户或其项目的稳定事实（身份、资源、约定）
- decision：已经拍板的决策及其理由

要求：
1. 用第三人称陈述句改写，去掉语气词和上下文依赖，脱离原对话也能读懂
2. 一条只讲一件事，不超过 60 字
3. 临时性的、一次性的、闲聊的内容不要提炼
4. 没有值得记的就返回空数组 []

输出格式（只输出 JSON，不要任何解释）：
[{"category":"preference","content":"..."},...]"""

SKILL_DISTILL_SYSTEM = """你是流程提炼器。从对话中识别「可复用的做事流程」，输出 JSON 数组。

判断标准：这个流程下次遇到同类任务能照着做吗？能，才提炼；只是一次性指令，忽略。

要求：
1. name：动词开头的短名，6-14 字，例如「部署静态站到服务器」
2. description：一句话说清这个流程解决什么问题
3. trigger_words：什么时候该用它，2-4 个关键词，逗号分隔
4. steps：有序步骤数组，每步一个短句，2-6 步。步骤不能为空
5. 提炼不出完整步骤的，宁可不提炼
6. 没有就返回空数组 []

输出格式（只输出 JSON，不要任何解释）：
[{"name":"...","description":"...","trigger_words":"a,b","steps":["...","..."]},...]"""


@app.post("/api/memory/distill")
async def distill_memory(req: Request):
    """从元神私聊最近的对话中提炼长期记忆（LLM 抽取，失败退关键词）。
    评测 P2-1/P2-2（2026-08-17）：支持显式 text 入参 + force 强制记录。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    text_in = (data.get("text") or "").strip()
    force = bool(data.get("force"))
    if text_in:
        convo = text_in
    else:
        rows = _recent_meta_texts(20)
        if not rows:
            return {"ok": True, "extracted": 0, "items": [], "method": "empty",
                    "note": "最近没有对话可供提炼；可传入 text 直接提交一段文本提炼。"}
        convo = "\n".join(f"{r['sender']}: {r['text']}" for r in rows if r.get("text"))
    items, method = await _llm_extract(MEMORY_DISTILL_SYSTEM, f"以下是对话/文本记录：\n\n{convo}")

    if items is None:
        # 兜底：LLM 不可用时退回关键词匹配，但如实标注方法，不冒充智能抽取
        pref_keywords = ["记住", "我喜欢", "我不喜欢", "我习惯", "我总是", "我从来", "注意", "规则", "不要"]
        items = [{"category": "preference", "content": r["text"]}
                 for r in ([{"text": convo}] if text_in else [])
                 if r.get("text") and any(k in r["text"] for k in pref_keywords)] or (
            [{"category": "preference", "content": convo}] if (text_in and force) else [])
        method = "keyword"
        note = f"LLM 不可用（{method}），已退回关键词匹配。"
    else:
        note = ""

    conn = get_db()
    existing = {r[0] for r in conn.execute("SELECT content FROM long_term_memory").fetchall()}
    saved = []
    for it in items[:12]:
        content = (it.get("content") or "").strip()
        if not content or content in existing:
            continue
        category = it.get("category") if it.get("category") in ("preference", "rule", "fact", "decision") else "preference"
        conn.execute(
            "INSERT INTO long_term_memory (category,content,source,ts) VALUES (?,?,?,?)",
            (category, content, f"元神私聊·{method}", datetime.now().isoformat()),
        )
        existing.add(content)
        saved.append({"category": category, "content": content})
    # force：无可提炼但用户明确要求记住 → 原样存为 fact（如实标注来源）
    if not saved and force and convo.strip():
        content = convo.strip()[:300]
        if content not in existing:
            conn.execute(
                "INSERT INTO long_term_memory (category,content,source,ts) VALUES (?,?,?,?)",
                ("fact", content, "manual·force", datetime.now().isoformat()),
            )
            existing.add(content)
            saved.append({"category": "fact", "content": content, "forced": True})
    conn.commit()
    conn.close()
    if not note and not saved:
        note = ("未提炼出值得长期记住的信息（偏好/规则/事实/决策）；如需原样记住，请带 force=true 重新提交。"
                if text_in else "最近对话未提炼出值得长期记住的信息；可传入 text + force 直接记录。")
    return {"ok": True, "extracted": len(saved), "items": saved, "method": method, "note": note}


# ── API：清理机制（预览 + 执行 + 自动配置）───────────────────────
@app.get("/api/cleanup/preview")
def cleanup_preview():
    return get_cleanup_preview()


CLEANUP_SCOPES = {"temp", "chat", "memory", "logs", "context", "all"}
# 会删掉用户真实内容（而非临时文件）的范围，必须真人点头
CLEANUP_DESTRUCTIVE = {"chat", "memory", "all"}
CLEANUP_LABELS = {
    "temp": "临时文件（缓存/日志碎片）", "chat": "全部聊天记录", "memory": "全部长期记忆",
    "logs": "操作与审计日志", "context": "超出 50 条的历史消息", "all": "以上全部",
}


@app.post("/api/cleanup")
async def run_cleanup(req: Request):
    data = await req.json()
    # 审查 D-1（本次审查中真实造成数据丢失的那条）：
    # 旧版 scope 默认值就是破坏力最大的 "all"，字段名写错即全表删除，且服务端无任何门禁。
    # v4.0 起：scope 必填、白名单校验、破坏性范围强制真人确认、删前自动备份。
    scope = (data.get("scope") or "").strip()
    keep_chat = int(data.get("keep_chat", 0))
    preview = data.get("preview", False)
    if preview:
        return get_cleanup_preview()
    if not scope:
        return JSONResponse(
            {"ok": False, "error": "必须显式指定 scope，没有默认值。"
                                   f"可选：{'、'.join(sorted(CLEANUP_SCOPES))}"},
            status_code=400,
        )
    if scope not in CLEANUP_SCOPES:
        return JSONResponse(
            {"ok": False, "error": f"未知的清理范围「{scope}」。可选：{'、'.join(sorted(CLEANUP_SCOPES))}"},
            status_code=400,
        )
    if scope in CLEANUP_DESTRUCTIVE:
        pv = get_cleanup_preview()
        ok_approved, why = await human_approve(
            "分身请求清理数据（不可撤销）",
            f"清理范围：{CLEANUP_LABELS.get(scope, scope)}\n"
            f"当前聊天 {pv.get('chat_count', '?')} 条 / 长期记忆 {pv.get('mem_count', '?')} 条\n\n"
            "确认删除吗？（会先自动备份数据库）",
        )
        if not ok_approved:
            return JSONResponse({"ok": False, "blocked": True, "error": why}, status_code=403)
    result = do_cleanup(scope, keep_chat)
    # 记录清理日志
    conn = get_db()
    conn.execute(
        "INSERT INTO cleanup_log (ts,action,scope,detail,size_freed) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(), "manual", scope,
         f"清理了 {result['deleted']} 个项目", result["freed"]),
    )
    conn.commit()
    conn.close()
    return result


# ── API：上下文管理（状态/压缩）───────────────────────────────────
@app.get("/api/context")
def context_status():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    meta_only = conn.execute("SELECT COUNT(*) FROM messages WHERE project_id=?", (META_PID,)).fetchone()[0]
    projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    mem_count = conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0]
    conn.close()
    return {"total_messages": total, "meta_messages": meta_only, "projects": projects, "long_term_memories": mem_count}


@app.post("/api/context/compress")
async def compress_context(req: Request):
    """v5.8「磨」自动触发：上下文将溢出时，先把最旧的非材料消息磨成一条压缩摘要（material=1 沉淀为元神材料），再删除旧消息。保留最近 keep 条。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    keep = int(data.get("keep", 100))
    deleted_total = 0
    ground_total = 0
    conn = get_db()
    for pid_row in conn.execute("SELECT DISTINCT project_id FROM messages"):
        conn.close()
        r = _auto_grind_project(pid_row[0], keep)
        deleted_total += r["deleted"]
        ground_total += r["ground"]
        conn = get_db()
    conn.close()
    return {"ok": True, "deleted": deleted_total, "ground_summaries": ground_total, "kept_per_project": keep}


# ── API：阶段门禁 / 冻结锁 / 版本快照（Phase 2）─────────────────
@app.get("/api/phases")
def phase_meta():
    return {"phases": PHASES, "names": PHASE_NAMES, "gates": PHASE_GATES}


@app.post("/api/projects/{pid}/phase")
async def set_phase(pid: str, req: Request):
    """阶段切换（带门禁校验 + 自动快照）。confirm=true 表示用户明确确认。"""
    data = await req.json()
    to_phase = data.get("to", "")
    confirm = bool(data.get("confirm", False))
    if to_phase not in PHASES:
        return {"ok": False, "error": f"未知阶段：{to_phase}，可选 {PHASES}"}
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    if proj["phase"] == to_phase:
        conn.close()
        return {"ok": False, "error": f"项目已在「{PHASE_NAMES[to_phase]}」阶段"}
    ok, err = gate_check(proj, to_phase)
    if not ok:
        conn.close()
        return {"ok": False, "need_confirm": not confirm, "error": err}
    from_phase = proj["phase"]
    conn.execute("UPDATE projects SET phase=? WHERE id=?", (to_phase, pid))
    conn.commit()
    conn.close()
    # 自动打快照
    create_snapshot(pid, f"{PHASE_NAMES[from_phase]} → {PHASE_NAMES[to_phase]}",
                    desc=f"阶段推进 {from_phase} → {to_phase}", auto=True)
    return {"ok": True, "from": from_phase, "to": to_phase, "phase": to_phase}


@app.post("/api/projects/{pid}/freeze")
async def set_freeze(pid: str, req: Request):
    data = await req.json()
    frozen = 1 if data.get("frozen", False) else 0
    conn = get_db()
    conn.execute("UPDATE projects SET frozen=? WHERE id=?", (frozen, pid))
    conn.commit()
    conn.close()
    return {"ok": True, "frozen": frozen}


# ── 修改单（变更已冻结内容的唯一通道）──
@app.get("/api/projects/{pid}/change-orders")
def list_change_orders(pid: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM change_orders WHERE project_id=? ORDER BY id DESC", (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/projects/{pid}/change-orders")
async def create_change_order(pid: str, req: Request):
    data = await req.json()
    title = (data.get("title") or "").strip()
    detail = (data.get("detail") or "").strip()
    if not title or not detail:
        return {"ok": False, "error": "修改单需要标题和理由"}
    conn = get_db()
    conn.execute(
        "INSERT INTO change_orders (project_id,title,detail,status,created_at) VALUES (?,?,?,?,?)",
        (pid, title, detail, "pending", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.patch("/api/change-orders/{cid}")
async def decide_change_order(cid: int, req: Request):
    """审批修改单：status = approved / rejected"""
    data = await req.json()
    status = data.get("status", "")
    if status not in ("approved", "rejected"):
        return {"ok": False, "error": "状态只能是 approved / rejected"}
    conn = get_db()
    row = conn.execute("SELECT * FROM change_orders WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "修改单不存在"}
    conn.execute(
        "UPDATE change_orders SET status=?, decided_at=? WHERE id=?",
        (status, datetime.now().isoformat(), cid),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "status": status}


# ── 版本快照 ──
@app.get("/api/projects/{pid}/snapshots")
def list_snapshots(pid: str):
    conn = get_db()
    rows = conn.execute("SELECT id,project_id,name,phase,desc,created_at FROM snapshots WHERE project_id=? ORDER BY id DESC", (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/projects/{pid}/snapshots")
async def make_snapshot(pid: str, req: Request):
    data = await req.json()
    name = (data.get("name") or "").strip() or f"快照 {datetime.now().strftime('%m-%d %H:%M')}"
    desc = (data.get("desc") or "").strip()
    label = create_snapshot(pid, name, desc, auto=False)
    if not label:
        return {"ok": False, "error": "项目不存在"}
    return {"ok": True, "name": label}


@app.post("/api/snapshots/{sid}/rollback")
async def rollback_snapshot(sid: int, req: Request):
    """回滚项目信息到快照时刻（name/goal/phase/frozen）。"""
    data = await req.json()
    confirm = bool(data.get("confirm", False))
    conn = get_db()
    snap = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    if not snap:
        conn.close()
        return {"ok": False, "error": "快照不存在"}
    info = json.loads(snap["data"])
    proj = info.get("project", {})
    pid = proj.get("id") or snap["project_id"]
    if not confirm:
        conn.close()
        return {"ok": False, "need_confirm": True,
                "error": f"回滚将把项目「{proj.get('name','')}」恢复到快照「{snap['name']}」时的状态（{proj.get('phase','')}阶段）。确认后执行。"}
    conn.execute(
        "UPDATE projects SET name=?, goal=?, status=?, phase=?, frozen=? WHERE id=?",
        (proj.get("name", ""), proj.get("goal", ""), proj.get("status", "green"),
         proj.get("phase", "requirement"), proj.get("frozen", 0), pid),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "rolled_back": {"name": proj.get("name"), "phase": proj.get("phase")}}


# ── API：模块看板（v3 Phase A 模块解构）────────────────────────
MODULE_STATUS = ["idea", "todo", "doing", "review", "done"]


def _mod_status_rank(status: str) -> int:
    return MODULE_STATUS.index(status) if status in MODULE_STATUS else 0


def _module_dict(row):
    d = dict(row)
    d["depends_on"] = json.loads(d.get("depends_on") or "[]")
    return d


def _auto_advance_phase(conn, pid: str) -> str:
    """v4.2 自主闭环：看板 100%（全部任务 done 或无任务）→ 自动推进到下一阶段。
    经 PHASE_GATES 门禁校验；返回新阶段名或 ''（未推进）。看板未满 100% 一律不推进。"""
    try:
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj or proj["phase"] == "done":
            return ""
        tasks = conn.execute("SELECT status FROM tasks WHERE project_id=?", (pid,)).fetchall()
        if tasks and any(t["status"] != "done" for t in tasks):
            return ""  # 未达 100%，团队继续自主推进
        cur = proj["phase"]
        idx = PHASES.index(cur) if cur in PHASES else 0
        if idx >= len(PHASES) - 1:
            return ""
        nxt = PHASES[idx + 1]
        ok, why = gate_check(proj, nxt)
        if not ok:
            conn.execute(
                "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                (pid, "系统", "sys",
                 f"🔒 看板已完成 100%，但进入「{PHASE_NAMES.get(nxt, nxt)}」的门禁未满足：{why}",
                 "progress", datetime.now().isoformat()),
            )
            conn.commit()
            return ""
        conn.execute(
            "UPDATE projects SET phase=? WHERE id=? AND phase=?",  # 条件更新：并发下只有一个推进成功
            (nxt, pid, cur),
        )
        if conn.execute("SELECT phase FROM projects WHERE id=?", (pid,)).fetchone()["phase"] != nxt:
            conn.commit()
            return ""  # 已被其他并发路径推进
        conn.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
            (pid, "系统", "sys",
             f"🚀 看板完成度 100%，团队自动进入下一阶段：{PHASE_NAMES.get(nxt, nxt)}",
             "progress", datetime.now().isoformat()),
        )
        conn.commit()
        return nxt
    except Exception as e:
        print(f"[advance-phase] {pid} 失败: {e}")
        return ""


@app.get("/api/projects/{pid}/modules")
def list_modules(pid: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort, created_at", (pid,)).fetchall()
    conn.close()
    return [_module_dict(r) for r in rows]


def _file_row_to_dict(r) -> dict:
    return {"id": r["id"], "name": r["name"], "abs_path": r["abs_path"], "rel_path": r["rel_path"],
            "size": r["size"], "owner_member": r["owner_member"], "module_id": r["module_id"],
            "ts": r["ts"], "material": r["material"]}


@app.get("/api/projects/{pid}/files")
def project_files(pid: str, module_id: str = ""):
    """v5.8 P1 双维度存储：点看板模块/项目开文件。
    - 带 module_id：返回该模块目录（modules/<mid>）的索引文件 + 磁盘现有文件
    - 不带：返回项目所有索引文件（public + 各模块）"""
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    root = _project_storage_root(dict(proj))
    if module_id:
        mod_dir = os.path.join(root, "modules", module_id)
        rows = conn.execute(
            "SELECT * FROM files WHERE project_id=? AND module_id=? ORDER BY ts DESC", (pid, module_id)).fetchall()
        indexed = {os.path.basename(r["abs_path"]): _file_row_to_dict(r) for r in rows}
        disk = []
        if os.path.isdir(mod_dir):
            for f in sorted(os.listdir(mod_dir)):
                fp = os.path.join(mod_dir, f)
                if os.path.isfile(fp) and f not in indexed:
                    disk.append({"name": f, "abs_path": fp, "size": os.path.getsize(fp), "from": "disk"})
        conn.close()
        return {"ok": True, "dir": mod_dir, "module_id": module_id,
                "indexed": list(indexed.values()), "disk": disk}
    rows = conn.execute("SELECT * FROM files WHERE project_id=? ORDER BY ts DESC", (pid,)).fetchall()
    conn.close()
    return {"ok": True, "dir": os.path.join(root, "public"), "indexed": [_file_row_to_dict(r) for r in rows],
            "disk": []}


@app.get("/api/file")
def open_file(path: str = ""):
    """v5.8 P1：在 storage_root（用户主目录下）读取/下载文件，供前端「点模块开文件」。"""
    real = _safe_file_path(path)
    if not real or not os.path.isfile(real):
        return JSONResponse({"ok": False, "error": "文件不存在或路径不安全"}, status_code=404)
    return FileResponse(real, filename=os.path.basename(real))


@app.post("/api/projects/{pid}/modules")
async def create_module(pid: str, req: Request):
    data = await req.json()
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "模块名不能为空"}
    conn = get_db()
    proj = conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    mid = f"{pid}-m{int(datetime.now().timestamp())}"
    max_sort = conn.execute("SELECT COALESCE(MAX(sort),0) FROM modules WHERE project_id=?", (pid,)).fetchone()[0]
    # 同秒多次创建避免 id 冲突：用 sort 序号兜底
    if conn.execute("SELECT 1 FROM modules WHERE id=?", (mid,)).fetchone():
        mid = f"{pid}-m{max_sort + 1}"
    conn.execute(
        "INSERT INTO modules (id,project_id,name,desc,depends_on,owner_role,status,sort,domain,flow,layer,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, name, data.get("desc", ""), json.dumps(data.get("depends_on") or [], ensure_ascii=False),
         data.get("owner_role", "后端"), data.get("status", "idea"), max_sort + 1,
         data.get("domain", ""), data.get("flow", ""), data.get("layer", ""),
         datetime.now().isoformat(), datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": mid}


@app.patch("/api/projects/{pid}/modules/{mid}")
async def update_module(pid: str, mid: str, req: Request):
    data = await req.json()
    conn = get_db()
    row = conn.execute("SELECT * FROM modules WHERE id=? AND project_id=?", (mid, pid)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "模块不存在"}
    fields, vals = [], []
    for k in ("name", "desc", "owner_role", "context_summary", "domain", "flow", "layer"):
        if k in data:
            fields.append(f"{k}=?")
            vals.append(data[k])
    if "depends_on" in data:
        fields.append("depends_on=?")
        vals.append(json.dumps(data["depends_on"] or [], ensure_ascii=False))
    if "sort" in data:
        fields.append("sort=?")
        vals.append(int(data["sort"]))
    if fields:
        conn.execute(f"UPDATE modules SET {', '.join(fields)}, updated_at=? WHERE id=?",
                     vals + [datetime.now().isoformat(), mid])
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/projects/{pid}/modules/{mid}/move")
async def move_module(pid: str, mid: str, req: Request):
    """看板列流转，带依赖检查：进入 doing 前，被依赖模块必须已完成。"""
    data = await req.json()
    to = data.get("to")
    if to not in MODULE_STATUS:
        return {"ok": False, "error": f"目标状态非法：{to}，允许 {MODULE_STATUS}"}
    conn = get_db()
    row = conn.execute("SELECT * FROM modules WHERE id=? AND project_id=?", (mid, pid)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "模块不存在"}
    # 依赖检查：状态前进到 doing（含之后）时，依赖项必须 done
    to_rank = _mod_status_rank(to)
    deps = json.loads(row["depends_on"] or "[]")
    if to_rank >= _mod_status_rank("doing") and deps:
        ph = ",".join("?" * len(deps))
        dep_rows = conn.execute(f"SELECT id,status FROM modules WHERE project_id=? AND id IN ({ph})", [pid] + deps).fetchall()
        dep_map = {r["id"]: r["status"] for r in dep_rows}
        blocked = [d for d in deps if dep_map.get(d) != "done"]
        if blocked:
            conn.close()
            return {"ok": False, "error": f"依赖未完成：{', '.join(blocked)}。需先完成依赖模块才能进入「进行中」"}
    conn.execute("UPDATE modules SET status=?, updated_at=? WHERE id=?", (to, datetime.now().isoformat(), mid))
    conn.commit()
    conn.close()
    return {"ok": True, "status": to}


@app.delete("/api/projects/{pid}/modules/{mid}")
def delete_module(pid: str, mid: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM modules WHERE id=? AND project_id=?", (mid, pid)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "模块不存在"}
    # 被依赖检查：其他模块 depends_on 含本模块则拒绝
    refs = conn.execute("SELECT id,name,depends_on FROM modules WHERE project_id=?", (pid,)).fetchall()
    ref_by = [r for r in refs if mid in (json.loads(r["depends_on"] or "[]"))]
    if ref_by:
        conn.close()
        return {"ok": False, "error": f"模块被 {', '.join(r['name'] for r in ref_by)} 依赖，无法删除"}
    conn.execute("DELETE FROM modules WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── 角色系统提示词（v0.27.0 静态种子；P3-1 起从 roles 表动态加载，此处为兜底默认）──
ROLE_SYSTEMS = {
    "architect": "你是项目架构师，负责技术方案设计。根据任务要求，给出简洁的技术方案，包括：关键设计决策、接口定义、技术栈选择。回答用中文，直接给方案，不废话。",
    "backend": "你是后端工程师，负责 API 和数据层实现。根据任务要求，给出具体的代码或方案，包括：接口定义、数据结构、关键逻辑。回答用中文，直接给代码/方案。",
    "frontend": "你是前端工程师，负责 H5 客户端与交互实现。根据任务要求，给出具体的代码或方案，包括：组件结构、样式要点、交互逻辑。回答用中文，直接给代码/方案。",
    "tester": "你是测试工程师，负责质量保障。根据任务要求，给出测试方案和关键用例；测试执行类任务请把用例/报告写入项目产物目录（~/Desktop/<项目名>/tests/），并汇报文件路径。回答用中文，直接给用例。",
}
ROLE_NAMES = {
    "architect": "架构师",
    "backend": "后端",
    "frontend": "前端",
    "tester": "测试",
}
# 兜底中文名 → id（roles 表反查失败时用；P3-1 起优先查表，消灭硬编码 ROLE_ID_MAP）
_ROLE_NAME_FALLBACK = {"后端": "backend", "前端": "frontend", "产品": "architect", "测试": "tester"}


def _roles_from_db() -> tuple:
    """P3-1：从 roles 表动态加载角色 → (systems, names)。
    静态种子作兜底，数据库记录（id/name/mandate/gate）覆盖或扩展；角色库改动即时生效。"""
    systems = dict(ROLE_SYSTEMS)
    names = dict(ROLE_NAMES)
    try:
        conn = get_db()
        rows = conn.execute("SELECT id,name,mandate,skills,gate FROM roles").fetchall()
        conn.close()
        for r in rows:
            rid = (r["id"] or "").strip()
            if not rid:
                continue
            rname = (r["name"] or rid).strip()
            names[rid] = rname
            mandate = (r["mandate"] or "").strip()
            if mandate:
                gate = (r["gate"] or "").strip()
                systems[rid] = (f"你是{rname}，职责：{mandate}。"
                                + (f"验收门禁：{gate}。" if gate else "")
                                + "回答用中文，直接给方案/产出，不废话。")
    except Exception as e:
        print(f"[roles-db] 动态加载失败，退回静态种子: {e}")
    return systems, names


def _role_id_by_name(name: str) -> str:
    """P3-1：按角色中文名反查 id（消灭 ROLE_ID_MAP）。查不到返回 None。"""
    if not name:
        return None
    try:
        conn = get_db()
        row = conn.execute("SELECT id FROM roles WHERE name=? LIMIT 1", (name,)).fetchone()
        conn.close()
        if row and row["id"]:
            return row["id"]
    except Exception:
        pass
    return _ROLE_NAME_FALLBACK.get(name)

# ── v4.0：任务状态自动流转（修复「能派」——此前任务建成 todo 后看板永不移动）──
FAIL_MARKERS = ("这次没有产出", "调用失败", "模型调用失败", "未配置任何可用模型", "provider_error")


def _task_status(task_id: str, status: str, pid: str = "", note: str = "") -> None:
    """更新任务状态并在群聊留痕（看板可见流转）。失败不影响主流程。"""
    if status not in MODULE_STATUS:
        return
    try:
        conn = get_db()
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, datetime.now().isoformat(), task_id))
        # 版本护栏：格子已有确认基线，重新进入 doing = 开始改 → 先打 wip 预编辑快照保留改前态
        if status == "doing":
            try:
                _rw = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if _rw and (_rw["module_id"] or ""):
                    _st = _cell_stage_of_task(dict(_rw))
                    if _cell_has_confirmed(conn, _rw["module_id"], _st):
                        _freeze_cell_version(conn, _rw["module_id"], _st, pid,
                                             kind="wip", content="", source_task_id=task_id,
                                             created_by="pre-edit")
            except Exception:
                pass
        if note and pid:
            conn.execute(
                "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                (pid, "系统", "sys", note, "done" if status == "done" else "progress",
                 datetime.now().isoformat()),
            )
        conn.commit()
        if status == "done":
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row:
                _settle_task_done(conn, dict(row))
            # v4.2 自主闭环：看板 100% → 自动推进下一阶段
            if pid:
                _auto_advance_phase(conn, pid)
                # M2：任务完成 → 立即唤醒元神调度器补位（毫秒级）
                _kick_autonomy()
        conn.close()
    except Exception as e:
        print(f"[task-status] {task_id} -> {status} 失败: {e}")


async def _judge_role_output(reply: str, done_criteria: str = "", project_standards: str = "",
                             role: str = "backend") -> tuple:
    """判定角色产出是否达标（批次 B / P1-3：对照标准 LLM 判定，替换 v4.0 的纯长度启发式）。
    判定优先级：任务 done_criteria → 项目 standards → 无标准时退回保守长度启发式。
    返回 (status, reason)。status: done（达标）/ review（未达标或产出存疑，需人工看）/ todo（无产出退回）。"""
    text = (reply or "").strip()
    if not text:
        return "todo", "角色无产出（空回复）"
    if any(m in text[:120] for m in FAIL_MARKERS):
        return "todo", "产出含失败标记，判定未产出"
    criteria = (done_criteria or "").strip() or (project_standards or "").strip()
    if not criteria:
        # 无标准可对照：退回保守长度启发式（v4.0 旧行为）
        if len(text) < 40:
            return "review", "无完成标准且产出较短，转人工复核"
        return "done", "无完成标准，产出长度正常"
    judge_sys = (
        "你是严格的验收官。根据「完成标准」判定角色产出是否达标。\n"
        '只输出 JSON：{"pass": true 或 false, "reason": "一句话理由（中文）"}。\n'
        "产出必须直接满足标准才算 pass；若产出只是计划/思路而没有实际交付物，或明显未达标准，判 fail。"
    )
    judge_hist = [
        {"role": "system", "content": judge_sys},
        {"role": "user", "content": f"【完成标准】\n{criteria}\n\n【角色产出】\n{text[:1500]}"},
    ]
    try:
        jr = await asyncio.to_thread(call_llm, role, judge_hist, judge_sys)
        if "{" in jr and "}" in jr:
            j = json.loads(jr[jr.find("{"):jr.rfind("}") + 1])
            reason = str(j.get("reason") or "对照完成标准判定").strip()[:80]
            if j.get("pass") is True:
                return "done", f"达标：{reason}"
            return "review", f"未达标：{reason}"
    except Exception:
        pass
    # LLM 判定失败 → 保守退回长度启发式（不因判定故障而误杀产出）
    if len(text) < 40:
        return "review", "标准判定调用异常且产出较短，转人工复核"
    return "done", "标准判定调用异常，按产出长度保守通过"


# ── API：话题（v3 Phase B 三层模型：对话/话题/任务）──────────────
@app.post("/api/projects/{pid}/chat")
async def project_chat(pid: str, req: Request):
    """项目群聊对话（v4.2：异步派单——落库用户消息后立即返回 job_id，后台执行，前端轮询进度）。
    单条消息 → 一个 dispatch_job；不再阻塞发送方 100-200s。
    v6.5：支持多模态图片（images）+ @指定成员（@name，定向派发）。"""
    data = await req.json()
    user_text = (data.get("text") or "").strip()
    if not user_text:
        return {"ok": False, "error": "消息不能为空"}
    # ── @ 指定成员解析（群聊定向派发）──
    target = None
    mm = re.search(r"@([^\s@,，。：:！!?？]{1,24})", user_text)
    if mm:
        target = mm.group(1)
    images = _normalize_images(data.get("images"))
    conn = get_db()
    proj = conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    conn.close()
    job_id = f"job{time.time_ns()}"
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO dispatch_jobs (id,project_id,status,progress,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (job_id, pid, "queued", "排队中…", now, now),
    )
    conn.commit()
    conn.close()
    # 后台执行（事件循环任务，不阻塞响应）。
    # 注意：必须持有 task 引用，否则 asyncio 可能 GC 掉 pending 任务导致永不执行。
    _t = asyncio.create_task(_run_dispatch_job(job_id, pid, user_text, images=images, target=target))
    _DISPATCH_TASKS.add(_t)
    _t.add_done_callback(_DISPATCH_TASKS.discard)
    return {"ok": True, "job_id": job_id, "queued": True, "message": "已派单，团队执行中"}


# 异步派单任务引用集合（防 GC 吞掉 pending 任务）
_DISPATCH_TASKS = set()

# P0-A：项目群聊蒸馏节流计数器（每累计 N 条消息触发一次经验蒸馏，对应 OpenHuman 20min cadence）
_CHAT_DISTILL_CNT = {}


async def _run_dispatch_job(job_id: str, pid: str, user_text: str, images=None, target=None):
    """异步执行一条派单：更新 job 状态 → 跑执行链 → 写结果。异常记录 error，不中断其他任务。"""
    def _set(status, progress="", result="", error=""):
        try:
            conn = get_db()
            conn.execute(
                "UPDATE dispatch_jobs SET status=?, progress=?, result=?, error=?, updated_at=? WHERE id=?",
                (status, progress, result, error, datetime.now().isoformat(), job_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    try:
        _set("running", "元神分析中…")
        r = await _execute_project_chat(pid, user_text, images=images, target=target)
        _set("done", "执行完成", json.dumps(r, ensure_ascii=False)[:20000])
    except Exception as e:
        _set("failed", "", "", f"{e}")
        print(f"[dispatch-job] {job_id} 失败: {e}")


@app.get("/api/jobs/{job_id}")
def get_dispatch_job(job_id: str):
    """进度轮询：返回派单任务当前状态（queued/running/done/failed）+ 进度摘要。"""
    conn = get_db()
    row = conn.execute(
        "SELECT id,project_id,status,progress,result,error,updated_at FROM dispatch_jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse(status_code=404, content={"error": "任务不存在"})
    d = dict(row)
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except Exception:
            pass
    return d


# ── P1-A：团队级 blast radius（爆炸半径）计算 ─────────────────────
# 对齐 CRG 结构图/爆炸半径：只把受影响的模块/成员切片投给相关角色，避免全项目广播。
# 保守多报路线（CRG F1≈0.71 也走保守）：命中模块 + 其依赖方 + 被依赖方 一并纳入，宁可多拉不可漏。

def _module_dep_graph(mods: list) -> dict:
    """返回 {模块名: set(依赖的模块名)}。depends_on 可能是 JSON 字符串或列表。"""
    g = {}
    for m in mods:
        raw = m["depends_on"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw) if raw.strip() else []
            except Exception:
                raw = []
        if not isinstance(raw, list):
            raw = []
        g[m["name"]] = set(raw)
    return g


# 通用/弱区分词，不应作为命中 token（避免"系统/功能/模块"等词导致全命中）
_BLAST_STOP = {"系统", "功能", "模块", "页面", "管理", "服务", "接口", "组件", "逻辑",
               "处理", "相关", "实现", "优化", "支持", "平台", "中心", "后台", "前端", "后端"}


def _blast_tokens(name: str) -> set:
    """模块/任务名 → 用于命中的 token 集合（分词 + 全名，去停用词，保留 len>=2）。"""
    raw = (name or "").strip()
    if not raw:
        return set()
    toks = set()
    for part in __import__("re").split(r"[\/\-_\s、，,；;]+", raw):
        if len(part) >= 2 and part not in _BLAST_STOP:
            toks.add(part)
    if len(raw) >= 2 and raw not in _BLAST_STOP:
        toks.add(raw)
    return toks


def _compute_blast_radius(user_text: str, mods: list, tasks: list) -> set:
    """返回受用户指令影响的模块名集合（爆炸半径）。空集 = 全项目（不聚焦，保质量）。
    命中策略：模块名/任务名的分词 token（如"登录/注册"→登录、注册）在指令文本中出现即命中，
    解决"优化登录模块"匹配不到"登录/注册"的退化问题；同时保留全名精确命中。"""
    text = (user_text or "").lower()
    name_to_mod = {m["name"]: m for m in mods}
    hits = set()
    # 1) 模块名命中（token 或全名）
    for m in mods:
        nm = (m["name"] or "")
        if not nm:
            continue
        if nm.lower() in text or any(tok.lower() in text for tok in _blast_tokens(nm)):
            hits.add(m["name"])
    # 2) 任务名命中 → 归入其所属模块
    for t in tasks:
        tn = (t["name"] or "")
        if not tn:
            continue
        if tn.lower() in text or any(tok.lower() in text for tok in _blast_tokens(tn)):
            mid = t["module_id"]
            for m in mods:
                if m["id"] == mid:
                    hits.add(m["name"])
                    break
    if not hits:
        return set()  # 无命中 → 全项目广播（保质量）
    # 3) 保守扩展：命中模块的依赖方 + 被依赖方一并纳入
    g = _module_dep_graph(mods)
    expanded = set(hits)
    changed = True
    while changed:
        changed = False
        for nm in list(expanded):
            deps = g.get(nm, set())
            for d in deps:
                if d not in expanded and d in name_to_mod:
                    expanded.add(d); changed = True
            for other, odep in g.items():
                if nm in odep and other not in expanded and other in name_to_mod:
                    expanded.add(other); changed = True
    return expanded


def _scoped_project_context(mods: list, tasks: list, focus: set) -> tuple:
    """按爆炸半径生成聚焦的 mod_desc / task_desc。focus 为空 = 全项目。"""
    if focus:
        smods = [m for m in mods if m["name"] in focus]
        smod_ids = {m["id"] for m in smods}
        stasks = [t for t in tasks if t["module_id"] in smod_ids]
    else:
        smods, stasks = mods, tasks
    mod_desc = ""
    if smods:
        active = [m for m in smods if m["status"] != "done"]
        done_cnt = len(smods) - len(active)
        mod_desc = "项目模块总览（按爆炸半径聚焦）：" + ("\n" + "\n".join(
            f"- {m['name']}（{m['status']} · 负责人 {m['owner_role']}）" for m in active[:8])) if active else ""
        if done_cnt:
            mod_desc += f"\n（另 {done_cnt} 个模块已完成）"
    task_desc = ""
    if stasks:
        doing = [t for t in stasks if t["status"] == "doing"]
        todo = [t for t in stasks if t["status"] == "todo"]
        task_desc = (f"项目任务（聚焦范围内）：进行中 {len(doing)} 个（{('、'.join(t['name'] for t in doing[:3])) if doing else '无'}），"
                     f"待办 {len(todo)} 个。")
    return mod_desc, task_desc


# ── v6.5 token 档位体系（T0 即时直答 / T1 单角色速办 / T2 团队协作 / T3 长程自治）──────
# 启发式分档规则：纯问答/咨询/汇报（有疑问词无执行动词）→ T0；有明确执行动词且指令简短 → T1；其余交元神调度判定
TIER0_HINT_RE = re.compile(r"(？|\?|吗|呢|怎么样|如何|是什么|介绍一下|说说|汇报|总结|分析|解释|看看|查看|进度|状态|了解)")
TIER1_ACT_RE = re.compile(r"(写|做|改|创建|生成|实现|添加|删除|修复|调整|新建|输出|打印|画|设计|翻译|计算|启动|停止|保存|提交|搭建)")


def _project_tier(pid: str) -> str:
    """项目级档位覆盖：projects.product_meta.tier（auto/0/1/2）；无则回退全局 chat_tier_default。"""
    try:
        conn = get_db()
        r = conn.execute("SELECT product_meta FROM projects WHERE id=?", (pid,)).fetchone()
        conn.close()
        if r and r["product_meta"]:
            pm = json.loads(r["product_meta"] or "{}")
            t = pm.get("tier")
            if t in ("0", "1", "2", "auto"):
                return t
    except Exception:
        pass
    return get_setting("chat_tier_default", "auto")


def _tier_heuristic(pid: str, text: str):
    """启发式分档：返回 0/1/2 或 None（拿不准→交给元神调度输出 tier 字段）。
    优先级：项目级 product_meta.tier > 全局 chat_tier_default(auto) > 启发式规则。"""
    if get_setting("chat_tier_enabled", "1") != "1":
        return None
    t = _project_tier(pid)
    if t in ("0", "1", "2"):
        return int(t)
    s = text.strip()
    # T0：纯问答/咨询/汇报（有疑问词，无执行动词）
    if TIER0_HINT_RE.search(s) and not TIER1_ACT_RE.search(s):
        return 0
    # T1：有明确执行动词且指令简短（≤60 字）→ 单角色速办
    if TIER1_ACT_RE.search(s) and len(s) <= 60:
        return 1
    return None


def _judge_light(reply: str, done_criteria: str = "", standards: str = "") -> tuple:
    """T1 轻量验收（不调 LLM，省 token）：无标准→长度启发式；有标准→长度+失败标记兜底。
    返回 (status, reason)。done/review/todo 与 _judge_role_output 一致。"""
    text = (reply or "").strip()
    if not text:
        return "todo", "角色无产出（空回复）"
    if any(m in text[:120] for m in FAIL_MARKERS):
        return "todo", "产出含失败标记，判定未产出"
    if not (done_criteria or "").strip() and not (standards or "").strip():
        if len(text) < 40:
            return "review", "无完成标准且产出较短，转人工复核"
        return "done", "无完成标准，产出长度正常"
    if len(text) < 80:
        return "review", "T1 轻量验收：产出较短，转人工复核"
    return "done", "T1 轻量验收通过（产出长度正常）"


def _parse_dispatch_plan(text: str):
    """解析元神调度 JSON（容错）：LLM 偶发截断（结尾缺 ] 或 }）时，按括号栈补全后重试。
    返回 dict 或 None（非 JSON / 无法修复）。"""
    if not text or "{" not in text or "}" not in text:
        return None
    seg = text[text.find("{"):text.rfind("}") + 1]
    try:
        return json.loads(seg)
    except Exception:
        pass
    # 截断补全：扫描括号栈，把缺失的收尾 ]/} 补上再试
    stack = []
    instr = False
    esc = False
    for ch in seg:
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch in "[{":
                stack.append(ch)
            elif ch in "]}":
                if not stack or (ch == "}" and stack[-1] != "{") or (ch == "]" and stack[-1] != "["):
                    return None  # 括号不匹配，无法修复
                stack.pop()
    if not stack:
        return None
    tail = "".join("}" if b == "{" else "]" for b in reversed(stack))
    try:
        return json.loads(seg + tail)
    except Exception:
        return None


def _token_phase_stats() -> dict:
    """驾驶舱 token 三段显性化：按 调度/执行/其他 聚合 model_usage token 与调用数。"""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT CASE WHEN phase='meta-dispatch' THEN '调度' "
            "WHEN phase LIKE 'role-exec:%' THEN '执行' ELSE '其他' END p,"
            " COUNT(*) n, COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) t "
            "FROM model_usage WHERE input_tokens>0 OR output_tokens>0 GROUP BY p"
        ).fetchall()
        conn.close()
        return {r["p"]: {"calls": r["n"], "tokens": r["t"]} for r in rows}
    except Exception:
        return {}


async def _execute_project_chat(pid: str, user_text: str, reuse_task_id: str = None, images=None, target=None) -> dict:
    """v4.2 自主闭环：项目群聊执行链（API 与团队自主推进循环共用）。
    流程：① 元神分析用户指令，输出 JSON 调度计划 ② 逐个调度角色执行（建任务+调AI） ③ 结果汇报群聊。
    v6.5：token 档位体系——T0 即时直答（启发式判定，元神单次回答）/ T1 单角色速办 / T2 完整团队链路。"""
    # v5.9：Trajectory run_id（本次派单/对话的唯一回放标识）
    run_id = f"{pid}-{int(time.time()*1000)}"
    _TRAJ_RUN.set((run_id, pid))
    _trajectory_event(run_id, pid, "run_start", "元神", f"收到指令：{user_text[:200]}", {"trigger": "project_chat"})
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO trajectory_runs (run_id,project_id,ts,trigger,status) VALUES (?,?,?,?,?)",
            (run_id, pid, datetime.now().isoformat(), "project_chat", "running"),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        _trajectory_event(run_id, pid, "error", "元神", "项目不存在")
        return {"ok": False, "error": "项目不存在"}
    proj = dict(proj)  # v5.8 P2：转 dict 以便安全访问 design_standard 等可选列
    # ── v6.5：@ 定向派发——把 @name 解析成具体成员（精确名/包含匹配）──
    _target_id = None
    _target_name = None
    if target:
        try:
            mems = conn.execute("SELECT id,name FROM agent_members").fetchall()
            tl = target.lower()
            for m in mems:
                if m["name"] and (m["name"].lower() == tl or tl in m["name"].lower()):
                    _target_id, _target_name = m["id"], m["name"]
                    break
            if not _target_id:
                _trajectory_event(run_id, pid, "warn", "元神", f"@ 定向派发：未找到成员「{target}」，回退自动路由")
        except Exception:
            pass
    # 落库用户消息（项目群聊：topic_id 为空；v6.5 含图片）
    imgs_json = json.dumps(images, ensure_ascii=False) if images else ""
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,images) VALUES (?,?,?,?,?,?,?)",
        (pid, "你", "self", user_text, None, datetime.now().isoformat(), imgs_json),
    )
    conn.commit()
    # ── P0-A：群聊自动蒸馏（记忆树原料）── 节流：每 6 条项目消息触发一次经验蒸馏
    _CHAT_DISTILL_CNT[pid] = _CHAT_DISTILL_CNT.get(pid, 0) + 1
    if _CHAT_DISTILL_CNT[pid] >= 6:
        _CHAT_DISTILL_CNT[pid] = 0
        asyncio.create_task(_auto_distill_project_chat(pid))
    # ── 项目级上下文（P1-A：先算爆炸半径，再生成聚焦切片，避免全项目广播）──
    mods = conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort", (pid,)).fetchall()
    tasks = conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
    # P1-A 团队级 blast radius：只把与本次指令相关的模块切片投给元神，避免全项目广播
    _blast = _compute_blast_radius(user_text, mods, tasks)
    mod_desc, task_desc = _scoped_project_context(mods, tasks, _blast)
    _blast_scope = len(_blast) if _blast else len(mods)  # 记录聚焦范围（0 命中=全项目）
    # 最近群聊消息（项目级，不含话题消息）
    rows = conn.execute(
        "SELECT sender,kind,text FROM messages WHERE project_id=? AND (topic_id IS NULL OR topic_id='') "
        "ORDER BY id DESC LIMIT 6", (pid,)
    ).fetchall()
    conn.close()

    # ── Step 1: 元神分析 → 调度计划（v0.27.0：支持先调工具查状态再调度）──
    # P3-1：角色从 roles 表动态加载；P3-3：单轮可派动作数按角色数动态（PAD 协议 ≤3 并行由串行执行满足）
    role_systems, role_names = _roles_from_db()
    max_actions = max(3, min(6, len(role_systems)))
    role_enum = "|".join(role_names)
    # P1-A：若已聚焦，明确告知元神本次只涉及这些模块（爆炸半径），其余模块不在此轮上下文
    blast_note = ""
    if _blast:
        blast_note = (f"\n【爆炸半径·精准路由】本次指令经分析只聚焦以下模块：{'、'.join(sorted(_blast))}。"
                      f"请仅围绕这些模块调度，不要涉及范围外模块（其余模块暂不在此轮上下文）。\n")
    dispatch_sys = (
        "你是「元神」，在项目群聊中接收用户指令后，需要分析并调度团队执行。\n"
        "根据用户指令和项目当前状态，判断是否需要调度团队执行：\n"
        "- 如果是执行类指令（如\"实现XX\"、\"修复XX\"、\"检查XX\"、\"设计XX\"），输出 JSON 调度计划\n"
        "- 如果是闲聊/提问/汇报，只在 reply 中回答，actions 为空数组\n\n"
        f"项目：{proj['name']}。目标：{proj['goal'] or '（未填写）'}。\n"
        f"当前团队角色：{'、'.join(role_names.values())}。{blast_note}"
        f"{mod_desc}\n{task_desc}\n\n"
        "【可用工具（v0.27.0）】调度阶段不要调用任何工具——直接输出 JSON 调度计划；"
        "需要查真实状态/执行验证，交给被派单的角色在其执行阶段用工具完成。"
        "（v4.2 修复：元神若在调度阶段调工具，会陷入工具循环导致迟迟不输出计划。）\n\n"
        "输出格式（必须为合法 JSON）：\n"
        '{"tier": 0或1或2（0=即时问答元神直答不派单；1=单一明确小任务只派单一个角色快速完成；2=复杂任务完整团队协作，含质量门与验收）, '
        '"reply": "给用户的简短回复（中文，说明你安排了什么）", '
        '"team_mode": "single(单一角色即可完成，只派一个角色) 或 multi(需多角色跨职能协作，如设计+编码+测试)", '
        f'"actions": [{{"role": "{role_enum}", "task_name": "简短任务名（10字内）", "detail": "给角色的执行指令", '
        '"done_criteria": "该任务完成的、可验证的判定标准（例如：接口返回 200 且通过测试）", '
        '"plan": "可选：你对该任务的执行计划（先做什么、改哪些文件、如何验证），供角色参考"}}]\n'
        f"注意：actions 最多 {max_actions} 个；done_criteria 务必具体、可验证，用于后续自动判定角色产出是否达标。如果只需要一个角色，就只放一个。闲聊/提问时 actions 为空。\n"
        "【复杂度→调度模式】单点/明确任务用 team_mode=single 只派一个角色（更聚焦、更省 token）；"
        "只有真正需要跨职能协作（如既要设计又要编码又要测试）才用 team_mode=multi 拆给多个角色。\n"
        "【自主原则（v4.2）】你是团队的自主调度者：能自己查状态、能自行安排先后顺序、能自行决策，"
        "不要向用户提问确认（除非缺少关键信息无法继续）。用户要的是你把看板任务推进到 100%。"
    )
    # v5.8 P2：项目选定 UI 设计规范 → 调度阶段提示元神，派单给前端/产品角色时令其遵守
    _ds_id = proj.get("design_standard") or ""
    if _ds_id:
        _ds_spec = _load_design_spec(_ds_id)
        if _ds_spec:
            dispatch_sys += f"\n本项目采用 UI 设计规范「{_ds_spec.get('name', _ds_id)}」：派给前端/产品角色的任务须令其遵守该规范（配色、字体、组件、间距）。\n"
    # ── v6.5：@ 定向派发指令（用户明确点名某成员处理）──
    if _target_id:
        dispatch_sys += (
            f"\n【定向派发·最高优先级】用户本条消息明确 @「{_target_name}」处理。"
            f"请将任务直接派给「{_target_name}」这一角色（team_mode=single），"
            f"不要自动分派给其他角色；除非该角色执行中确需他人协作，否则只派它。\n"
        )
    hist = [{"role": "system", "content": dispatch_sys}]
    for r in reversed(rows):
        if r["kind"] == "sys":
            continue
        role = "assistant" if r["kind"] in ("agent", "meta") else "user"
        hist.append({"role": role, "content": r["text"]})
    hist.append({"role": "user", "content": user_text})

    # ── v6.5 T0 即时直答：启发式判定为问答/轻咨询 → 元神单次直答（不建卡/不派单/不验收）──
    _tier = _tier_heuristic(pid, user_text)
    if _tier == 0 and get_setting("tier0_quick_answer", "1") == "1":
        quick_sys = (
            f"你是「元神」——{OWNER_NAME}的数字分身与团队总管。当前项目「{proj['name']}」，目标：{proj['goal'] or '（未填写）'}。\n"
            "这是即时问答/轻咨询。请直接、简洁、准确地回答；不要创建任务、不要派单、不要调用工具、不要提验收。"
        )
        quick_hist = [{"role": "system", "content": quick_sys}, {"role": "user", "content": user_text}]
        quick_reply = await _chat_with_tools("__meta__", quick_hist, quick_sys, tools=[],
                                             phase="meta-dispatch", scope_modules=_blast_scope, project_id=pid)
        _trajectory_event(run_id, pid, "plan", "元神", f"T0 即时直答：{quick_reply[:200]}", {"tier": 0})
        conn = get_db()
        conn.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
            (pid, "分身 · 元神", "meta", quick_reply, "progress", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        try:
            conn = get_db()
            conn.execute("UPDATE trajectory_runs SET status='done' WHERE run_id=?", (run_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return {"reply": quick_reply, "actions": [], "ok": True, "rounds": 0, "all_done": True, "tier": 0,
                "blast_radius": sorted(_blast) if _blast else [], "blast_scope": _blast_scope, "run_id": run_id}

    dispatch_reply = await _chat_with_tools("__meta__", hist, dispatch_sys, tools=[],
                                          phase="meta-dispatch", scope_modules=_blast_scope,
                                          project_id=pid)  # v4.2: 调度阶段禁用工具

    # 解析 JSON（容错：LLM 可能返回非 JSON）
    meta_reply = dispatch_reply
    actions = []
    tier = _tier if _tier in (0, 1, 2) else None  # 启发式已定则直接用；否则看元神输出 tier 字段
    team_mode = None  # v6.5：元神输出的调度模式（single/multi），由任务复杂度决定
    _t1_mode = False  # v6.5：T1 单角色速办标记（try 内可能改写）
    try:
        j = _parse_dispatch_plan(dispatch_reply)
        if j is not None:
            meta_reply = j.get("reply", dispatch_reply)
            actions = j.get("actions", [])
            # v6.5：元神输出 tier 字段（启发式未定时使用）；非法则按 actions 数量兜底
            if tier is None:
                _jt = j.get("tier")
                if _jt in (0, 1, 2):
                    tier = int(_jt)
                else:
                    tier = 2 if actions else 0
            # 每个 action 补默认空完成标准（批次 B / P1-1）
            for _a in actions:
                _a.setdefault("done_criteria", "")
            # ── v6.5：@ 定向派发——强制所有 action 只派给被 @ 的成员 ──
            if _target_id and actions:
                for _a in actions:
                    _a["role"] = _target_id
                team_mode = "single"
                _trajectory_event(run_id, pid, "plan", "元神",
                                  f"@ 定向派发：已将任务锁定给「{_target_name}」", {"target": _target_id})
            team_mode = j.get("team_mode")  # v6.5：single/multi
            # v6.5 T1 单角色速办：只取第一个 action，跳过 D8 交叉验证（tier1_skip_xverify）
            _t1_mode = (tier == 1) and get_setting("tier1_solo_exec", "1") == "1"
            if _t1_mode and actions:
                actions = actions[:1]
                _trajectory_event(run_id, pid, "plan", "元神",
                                  "T1 单角色速办：仅派单最匹配角色，跳过交叉验证与 LLM 验收", {"tier": 1})
            # 批次B D8：写/测交叉验证——为代码类动作追加 tester 配对（同一 spec 写测试并跑）
            if _code_xverify_enabled() and not _t1_mode:
                _pairs = []
                for _a in actions:
                    if _is_code_task(_a):
                        _orig = (_a.get("task_name") or "任务")[:16]
                        _pairs.append({
                            "role": "tester",
                            "task_name": f"测:{_orig}",
                            "detail": (f"基于同一需求为「{_a.get('task_name','')}」的实现编写自动化测试并运行验证："
                                       f"{_a.get('detail','')}。测试须覆盖 done_criteria，运行测试确认实现达标；"
                                       f"测试不通过则说明实现有缺陷，请在结果中点明。"),
                            "done_criteria": f"为该任务编写并通过测试（覆盖：{_a.get('done_criteria','实现通过测试')}）",
                            "plan": _a.get("plan", ""),
                        })
                if _pairs:
                    actions.extend(_pairs)
                    _trajectory_event(run_id, pid, "plan", "元神",
                        f"🔬 写/测交叉验证：为 {len(_pairs)} 个代码任务追加 tester 配对",
                        {"pairs": [p.get("task_name") for p in _pairs]})
    except Exception:
        pass  # 非 JSON，当作纯文本回复
    # v5.9：回放事件——元神调度计划
    _trajectory_event(run_id, pid, "plan", "元神",
                      f"调度计划：{meta_reply[:300]}" + (f"\n派单 {len(actions)} 个角色任务" if actions else "（仅回复，无派单）"),
                      {"actions": [{"role": a.get("role"), "task": a.get("task_name")} for a in actions]})
    # v6.5：记录本次运行的模型策略（roleplay / multimodel）与调度模式，诚实反映"真多模型"还是"角色扮演"
    _strat = get_model_strategy()
    _trajectory_event(run_id, pid, "model_strategy", "元神",
                      f"模型策略：{_strat['mode']}"
                      f"（已配置 {_strat['distinct_providers']} 个独立供方：{', '.join(_strat['providers']) or '无'}）"
                      + (f"；调度模式：{team_mode}" if team_mode else ""),
                      {"mode": _strat["mode"], "providers": _strat["providers"], "team_mode": team_mode or ""})

    # 落库元神回复
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
        (pid, "分身 · 元神", "meta", meta_reply, "progress", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    # ── Step 2: 角色执行（批次 B / P2-3：autonomy 有界循环）──
    # 执行 → 对照完成标准判定 → 未达标由元神重新规划补充动作 → 最多 MAX_ROUNDS 轮
    MAX_ROUNDS = max(1, min(5, int(get_setting("autonomy_max_rounds", "3"))))
    if _t1_mode:
        MAX_ROUNDS = 1  # v6.5：T1 单角色速办不做补做轮（省 token）
    role_results = []
    round_no = 1
    pending_actions = actions[:max_actions]

    # ── v6.5 P2 共享上下文池：项目级共享段只构建一次（固定前缀，同项目多角色/多轮命中模型前缀缓存）──
    shared_ctx = ""
    if get_setting("shared_context_pool", "1") == "1" and not _t1_mode:
        shared_ctx = (
            f"项目：{proj['name']}，目标：{proj['goal'] or ''}。"
            f"项目完成标准：{proj['standards'] or '（未填写）'}。\n"
            "【执行纪律】若任务涉及写代码或生成文件，请先简短说明执行计划"
            "（改动哪些文件、为什么、如何验证），再动手产出；禁止无说明地直接覆写既有文件。"
        )
        if proj.get("design_standard"):
            try:
                _ds0 = _load_design_spec(proj["design_standard"])
                if _ds0:
                    shared_ctx += "\n\n" + _design_spec_prompt(_ds0)
            except Exception:
                pass

    async def _run_one(act, reuse_task_id: str = None):
        """执行单个 action：建卡 → doing → 角色执行 → 落库 → 对照标准判定。并发安全（各自独立连接）。
        reuse_task_id：autopilot 推进预存 todo 看板卡时传入既有卡 id，复用该卡而非新建（修复看板↔执行脱节）。"""
        role = act.get("role", "backend")
        if role not in role_systems:
            role = "backend"
        # v6.5：解析该角色激活成员的专属模型覆盖（成员 model_cfg 绑定了 provider+key 才生效，否则 None=走全局路由）
        try:
            _mc = get_db()
            member_id, member_override = _resolve_member_model(_mc, pid, role)
            _mc.close()
        except Exception:
            member_id, member_override = None, None
        task_name = act.get("task_name", "未命名任务")[:20]
        detail = act.get("detail", "")
        done_criteria = (act.get("done_criteria") or "").strip()[:300]
        # v5.9：回放事件——角色开始执行
        _trajectory_event(run_id, pid, "role_start", role_names.get(role, role),
                          f"▶️ 派单：{task_name}（{role_names.get(role, role)}）",
                          {"detail": detail[:300], "member_id": member_id or "", "model_override": bool(member_override)})

        conn = get_db()
        # v6.4 P0 修复：对话任务必须落进真实 module×stage 格（见 _resolve_dispatch_module）
        mod_id = _resolve_dispatch_module(conn, pid, act, mods)
        topic_id = ""
        if reuse_task_id:
            # P1-2 修复：autopilot 推进预存 todo 看板卡时复用既有卡，而非每张 action 新建一张，
            # 避免「预存卡永远停在 todo + 每轮派单堆新卡」导致看板与实际执行脱节。
            task_id = reuse_task_id
            conn.execute(
                "UPDATE tasks SET owner_role=?, status='doing', "
                "done_criteria=COALESCE(NULLIF(?, ''), done_criteria), updated_at=? "
                "WHERE id=?",
                (role, done_criteria, datetime.now().isoformat(), task_id),
            )
            conn.commit()
            conn.close()
        else:
            # 建任务卡片（todo 状态），并绑定该模块的真实话题（修复看板↔群聊断链）
            task_id = f"tk{time.time_ns()}"
            if mod_id:
                trow = conn.execute("SELECT id FROM topics WHERE project_id=? AND module_id=? LIMIT 1", (pid, mod_id)).fetchone()
                if trow:
                    topic_id = trow["id"]
                else:
                    topic_id = f"tp{time.time_ns()}"
                    conn.execute(
                        "INSERT INTO topics (id,project_id,module_id,name,agents,status,created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (topic_id, pid, mod_id, "默认讨论", "[]", "open", datetime.now().isoformat()),
                    )
            _stg, _trk = _derive_task_stage_track(conn, mod_id, pid)
            conn.execute(
                "INSERT INTO tasks (id,project_id,module_id,topic_id,name,owner_role,status,done_criteria,stage,track,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, pid, mod_id, topic_id, task_name, role, "todo", done_criteria, _stg, _trk, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            conn.commit()
            conn.close()

        # v6.4 P0 快赢：把当前任务 id 注入 contextvar，后续角色执行期 LLM 调用按任务归因 token
        _TOOL_TASK.set(task_id)

        # v4.0：开工即流转到「进行中」，看板实时可见
        _task_status(task_id, "doing", pid, f"▶️ 「{task_name}」已派给{role_names.get(role, role)}，进入进行中")

        # 调用角色 AI 执行（v0.27.0：角色也可调工具——跑命令验证/查资料）
        # v5.8 P1 双维度存储：角色执行期注入工作区（contextvar 并发隔离），
        # 默认落该任务所属模块目录 modules/<mid>（点看板模块即开），无模块则落 public/。
        root = _project_storage_root(proj)
        if mod_id:
            workdir = os.path.join(root, "modules", mod_id)
        else:
            workdir = os.path.join(root, "public")
        os.makedirs(workdir, exist_ok=True)
        _TOOL_WORKDIR.set(workdir)
        # v5.8 P1：把"谁/哪个项目/哪个模块/根目录"注入工具上下文，写文件后索引到 files 表
        _TOOL_PROJECT.set(pid)
        _TOOL_MODULE.set(mod_id or "")
        _TOOL_ACTOR.set(role_names.get(role, role))
        _TOOL_ROOT.set(root)
        # 批次B D6：代理级版本管理——为任务创建/切到独立分支（开启时），提交在收尾处原子落盘
        vcs_branch = ""
        if _code_vcs_enabled():
            try:
                vcs_branch = await asyncio.to_thread(_vcs_task_branch, root, task_id)
            except Exception:
                vcs_branch = ""
        # v6.5 P2 共享上下文池：shared_ctx（项目信息+纪律+设计规范）作为固定前缀，角色段接后面
        if shared_ctx:
            role_sys_ctx = shared_ctx + "\n\n" + role_systems[role]
        else:
            role_sys_ctx = role_systems[role] + f"\n项目：{proj['name']}，目标：{proj['goal'] or ''}"
        role_sys_ctx += (f"\n【工作区】所有写文件必须写入此目录（越界会被拒绝）：{workdir}"
                         f"\n多步文件操作建议用 run_batch 一次完成（省 token）。")
        if done_criteria:
            role_sys_ctx += f"\n任务完成标准（必须对照交付，不达标会被打回重做）：{done_criteria}"
        # 批次A D2：角色级执行纪律（软门禁）——写代码/文件前先给出执行计划（共享池开启时已含于 shared_ctx）
        if not shared_ctx:
            role_sys_ctx += ("\n【执行纪律】若任务涉及写代码或生成文件，请先简短说明执行计划"
                             "（改动哪些文件、为什么、如何验证），再动手产出；禁止无说明地直接覆写既有文件。")
        # 批次B D5：注入代码库语义上下文（锚点契约 + 符号索引），降低跨文件改动回归风险
        if _code_semantic_enabled():
            _cb_ctx = _build_codebase_context(root, mod_id, role)
            if _cb_ctx:
                role_sys_ctx += "\n\n" + _cb_ctx
        # 批次C D7：代码质量硬约束系统提示（仅代码角色 + 开关开启时追加），强约束降幻觉/提首过质量
        _qp = _build_code_quality_prompt(role)
        if _qp:
            role_sys_ctx += _qp
        # v5.8 P2：项目选定 UI 设计规范 → 注入角色执行上下文（前端/产品尤其要遵守，共享池开启时已含于 shared_ctx）
        if not shared_ctx:
            ds_id = proj.get("design_standard") or ""
            if ds_id:
                _ds = _load_design_spec(ds_id)
                if _ds:
                    role_sys_ctx += "\n\n" + _design_spec_prompt(_ds)
        # P1-A：注入该角色所负责模块的聚焦切片（爆炸半径），让角色只见自己的模块上下文（而非全项目）
        _role_scope = 0
        try:
            _role_disp = role_names.get(role, role)
            _role_mods = [m for m in mods
                          if (m["owner_role"] or "") in (role, _role_disp)
                          or _ROLE_NAME_FALLBACK.get(m["owner_role"] or "", "") == role]
            # 评测 P2-3（2026-08-17）：在角色职责基础上按爆炸半径再收敛——
            # 指令命中某模块时，只注入「该角色负责的 ∩ 爆炸半径内」的模块，进一步降低 token；
            # 交集为空则保留角色职责全量（避免角色失去上下文）。
            if _role_mods and _blast:
                _blast_intersect = [m for m in _role_mods if m["name"] in _blast]
                if _blast_intersect:
                    _role_mods = _blast_intersect
            if _role_mods:
                _rm_ids = {m["id"] for m in _role_mods}
                _rm_lines = [f"- {m['name']}（{m['status']}）" for m in _role_mods]
                _rm_tasks = [t for t in tasks if t.get("module_id") in _rm_ids and t["status"] in ("doing", "todo")]
                if _rm_tasks:
                    _rm_lines.append("该模块待推进任务：" + "、".join(t["name"] for t in _rm_tasks[:5]))
                role_sys_ctx += "\n\n【你负责的模块（爆炸半径聚焦，仅这些模块在此上下文）】\n" + "\n".join(_rm_lines)
                _role_scope = len(_role_mods)
                # 版本护栏：该角色负责的模块若有确认基线，提醒改动将生成新版本、不要整体覆写
                try:
                    _rc = get_db()
                    _confirmed = []
                    for _rm in _role_mods:
                        _rs = conn.execute(
                            "SELECT stage FROM tasks WHERE module_id=? AND stage<>'' LIMIT 1",
                            (_rm["id"],)).fetchone()
                        _stg = (_rs["stage"] if _rs else "")
                        if _cell_has_confirmed(_rc, _rm["id"], _stg):
                            _confirmed.append(_rm["name"])
                    _rc.close()
                    if _confirmed:
                        role_sys_ctx += (f"\n\n⚠️ 版本护栏：模块「{'、'.join(_confirmed)}」已有确认版本基线。"
                                         f"你的改动会自动生成新版本（旧基线可随时回滚），"
                                         f"严禁整体覆写已确认功能，只改被明确要求的部分。")
                except Exception:
                    pass
        except Exception:
            _role_scope = 0
        # 疗效归因第⑤环：召回与任务最相关的经验（按 5 维权重排序），注入角色执行上下文
        try:
            rel_exp = _recall_experiences(f"{task_name} {detail}", limit=3)
            if rel_exp:
                exp_lines = [
                    f"- （权重{round(e.get('weight', 0), 2)}）{e.get('scenario', '')}：{e.get('lesson', '')}"
                    for e in rel_exp if e.get("lesson")
                ]
                if exp_lines:
                    role_sys_ctx += "\n\n【相关经验（按疗效权重召回，优先复用高信任经验）】\n" + "\n".join(exp_lines)
                    for e in rel_exp:
                        try:
                            _record_experience_use(e["id"])  # 标记复用：frequency/last_used 演化权重
                        except Exception:
                            pass
        except Exception:
            pass
        role_hist = [
            {"role": "system", "content": role_sys_ctx},
            {"role": "user", "content": f"任务：{task_name}\n具体要求：{detail}\n请给出你的执行方案/代码/分析。需要验证时可调用 exec_command / browser_action 工具获取真实结果。"},
        ]
        # 批次A D1+D4：确定性质量门 + 自纠正闭环。
        # 仅当开启代码校验时才进入「执行→验证→回填修复」循环；否则单次执行，保持原行为。
        verify_enabled = _code_verify_enabled()
        max_fix = _code_verify_fix_rounds() if verify_enabled else 0
        role_reply = ""
        verify = {"ok": True, "errors": [], "summary": "未启用代码质量门", "files": [], "ran_tests": False}
        for _fix in range(max_fix + 1):
            try:
                role_reply = await _chat_with_tools(role, role_hist, role_sys_ctx,
                                                 phase=f"role-exec:{role}", scope_modules=_role_scope,
                                                 project_id=pid, member_override=member_override)
            except Exception as e:
                role_reply = f"这次没有产出。{role_names.get(role, role)}执行时出错：{e}"
            if not verify_enabled:
                break
            # 确定性质量门（IO 密集，用线程执行避免阻塞事件循环）
            verify = await asyncio.to_thread(_sandbox_verify, workdir, done_criteria)
            if verify["ok"] or _fix >= max_fix:
                break
            # 未通过 → 把结构化报错回填，驱动角色修复（D4 自纠正闭环）
            role_hist.append({"role": "assistant", "content": role_reply[:1500]})
            role_hist.append({"role": "user", "content":
                "⚠️ 你的产出未通过确定性质量门（编译/测试失败），请修复后重新提交完整产出：\n"
                + verify["summary"]})
            _trajectory_event(run_id, pid, "role_fix", role_names.get(role, role),
                              f"🔧 质量门未通过，进入第 {_fix + 1} 轮自纠正：{verify['summary'][:200]}",
                              {"task_id": task_id, "round": _fix, "fix_round": _fix + 1})

        # 落库角色回复
        conn = get_db()
        conn.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
            (pid, f"分身 · {role_names.get(role, role)}", "agent", role_reply, "progress", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        # 批次A D3：确定性质量门优先于 LLM 判定——代码任务质量门失败直接判 review（附报错）
        if verify_enabled and not verify["ok"]:
            final = "review"
            judge_reason = "确定性质量门未通过：" + verify["summary"][:200]
        else:
            # v6.5 T1 单角色速办：跳过 LLM 验收（tier1_skip_llm_judge）→ 轻量启发式判定
            if _t1_mode and get_setting("tier1_skip_llm_judge", "1") == "1":
                final, judge_reason = _judge_light(role_reply, done_criteria, proj["standards"] or "")
            else:
                # 批次 B / P1-3：对照完成标准（任务级 → 项目级）LLM 判定
                final, judge_reason = await _judge_role_output(role_reply, done_criteria, proj["standards"] or "", role)
        note = {
            "done": f"✅ 「{task_name}」已完成（{role_names.get(role, role)}）",
            "review": f"🔍 「{task_name}」未达标转复核（{judge_reason}）",
            "todo": f"⚠️ 「{task_name}」执行未产出，退回待办",
        }[final]
        _task_status(task_id, final, pid, note)
        # 批次B D6：原子提交（仅成功 done 的任务分支；review 保留分支供人工评审/合主干）
        if _code_vcs_enabled() and vcs_branch:
            try:
                if final == "done":
                    _vcs = await asyncio.to_thread(_vcs_commit, root, task_id,
                        f"分身·{role_names.get(role, role)} 完成任务「{task_name}」")
                    if not _vcs.get("skipped"):
                        _trajectory_event(run_id, pid, "vcs", "元神",
                            f"🔖 已提交任务分支 {vcs_branch}：" + _vcs.get("out", "")[:120],
                            {"task_id": task_id, "branch": vcs_branch})
                else:
                    _trajectory_event(run_id, pid, "vcs", "元神",
                        f"⏸ 任务未达标，保留分支 {vcs_branch} 待人工评审后再合主干",
                        {"task_id": task_id, "branch": vcs_branch})
            except Exception:
                pass
        # v5.9：回放事件——角色执行结果
        _trajectory_event(run_id, pid, "role_done", role_names.get(role, role),
                          f"{note}（{judge_reason[:200]}）", {"task_id": task_id, "status": final, "round": round_no})
        return {"role": role, "task_name": task_name, "task_id": task_id,
                "status": final, "round": round_no, "reason": judge_reason}

    _sem = asyncio.Semaphore(3)  # v4.2 并行化：PAD 协议 ≤3 并发

    async def _limited(act, reuse_task_id=None):
        async with _sem:
            return await _run_one(act, reuse_task_id)

    _reuse_consumed = False
    while pending_actions and round_no <= MAX_ROUNDS:
        # v4.2 并行化：本轮 actions 并发执行（≤3），多 action 派单显著提速
        # P1-2：本轮首个 action 复用 autopilot 传入的预存看板卡，其余 action 仍各自建新卡
        role_results.extend(await asyncio.gather(*[
            _limited(a, reuse_task_id if (not _reuse_consumed and i == 0) else None)
            for i, a in enumerate(pending_actions)
        ]))
        _reuse_consumed = True

        # ── 本轮判定：未达标 → 元神重新规划补充动作（autonomy，最多 MAX_ROUNDS 轮）──
        unmet = [r for r in role_results if r["round"] == round_no and r["status"] != "done"]
        if not unmet or round_no >= MAX_ROUNDS:
            break
        round_no += 1
        feedback = "；".join(
            f"「{r['task_name']}」（{role_names.get(r['role'], r['role'])}）未达标：{r['reason']}" for r in unmet
        )
        replan_sys = (
            "你是「元神」。上一轮派单给团队的部分任务未达标，需要你重新规划补充动作。\n"
            f"项目：{proj['name']}。目标：{proj['goal'] or '（未填写）'}。"
            f"项目完成标准：{proj['standards'] or '（未填写）'}。\n"
            f"未达标任务反馈：{feedback}\n"
            '输出 JSON：{"reply": "给用户的简短说明（本轮补做计划）", '
            f'"actions": [{{"role": "{role_enum}", "task_name": "简短任务名", '
            '"detail": "针对未达标原因的补充执行指令", "done_criteria": "可验证的完成标准"}]}\n'
            f"注意：actions 最多 {max_actions} 个；若当前产出已尽力、无法继续（缺信息/需用户决策等），actions 输出空数组并说明原因。"
        )
        replan_hist = [{"role": "system", "content": replan_sys}]
        replan_reply = await _chat_with_tools(META_PID, replan_hist, replan_sys, tools=[])  # v4.2: 重规划同样禁用工具
        pending_actions = []
        try:
            j = _parse_dispatch_plan(replan_reply)
            if j is not None:
                pending_actions = (j.get("actions") or [])[:max_actions]
                for _a in pending_actions:
                    _a.setdefault("done_criteria", "")
                extra_reply = str(j.get("reply") or "").strip()
                if extra_reply:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                        (pid, "分身 · 元神", "meta", f"🔄 第 {round_no} 轮补做：{extra_reply}", "progress",
                         datetime.now().isoformat()),
                    )
                    conn.commit()
                    conn.close()
        except Exception:
            pending_actions = []
        if not pending_actions:
            break

    all_done = all(r["status"] == "done" for r in role_results) if role_results else False
    # v5.9：回放——标记 run 完成 + 统计事件数
    try:
        conn = get_db()
        cnt = conn.execute("SELECT COUNT(*) FROM trajectory WHERE run_id=?", (run_id,)).fetchone()[0]
        conn.execute(
            "UPDATE trajectory_runs SET status='done', total_events=? WHERE run_id=?",
            (cnt, run_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return {"reply": meta_reply, "actions": role_results, "ok": True, "rounds": round_no, "all_done": all_done,
            "blast_radius": sorted(_blast) if _blast else [], "blast_scope": _blast_scope, "run_id": run_id,
            "model_strategy": _strat, "team_mode": team_mode or ""}


# ── v4.2 立项自动拆解：沟通内容 → 看板（模块 + 任务 + 目标/标准）──
@app.post("/api/projects/{pid}/plan")
async def plan_project(pid: str, req: Request):
    """立项即看板：用户用自然语言描述项目 → 元神输出结构化计划
    {goal, standards, modules:[{name,desc,owner_role,tasks:[{name,done_criteria}]}]}
    → 更新目标/完成标准 + 建模块与任务卡 + 群聊留痕。全程不问用户。"""
    data = await req.json()
    text = (data.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "请描述项目"}
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    conn.close()
    plan_sys = (
        "你是「元神」，负责把用户对项目的描述拆解成可执行看板。\n"
        '输出严格 JSON：{"goal": "项目目标一句话", "standards": "完成标准（可验收，用 / 分隔）", '
        '"modules": [{"name": "模块名", "desc": "模块说明", "owner_role": "backend|frontend|architect|tester", '
        '"tasks": [{"name": "任务名", "done_criteria": "该任务可验证的完成标准"}]}]}\n'
        "要求：模块 2-5 个；每个模块 2-4 个任务；任务按实现顺序排列；done_criteria 必须具体可验证；"
        "不要输出 JSON 以外的内容。"
    )
    hist = [
        {"role": "system", "content": plan_sys},
        {"role": "user", "content": f"项目名称：{proj['name']}\n用户描述：\n{text}"},
    ]
    reply = await asyncio.to_thread(call_llm, META_PID, hist, plan_sys)
    try:
        if "{" in reply and "}" in reply:
            j = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
        else:
            return {"ok": False, "error": "元神返回非 JSON，请重试", "raw": reply[:200]}
    except Exception as e:
        return {"ok": False, "error": f"解析失败：{e}", "raw": reply[:200]}
    goal = (j.get("goal") or "").strip()[:200]
    standards = (j.get("standards") or "").strip()[:500]
    modules = j.get("modules") or []
    if not modules:
        return {"ok": False, "error": "未拆出模块，请补充描述", "raw": reply[:200]}
    conn = get_db()
    if goal and not (proj["goal"] or "").strip():
        conn.execute("UPDATE projects SET goal=? WHERE id=?", (goal, pid))
    if standards:
        conn.execute("UPDATE projects SET standards=? WHERE id=?", (standards, pid))
    existing_mods = {m["name"] for m in conn.execute("SELECT name FROM modules WHERE project_id=?", (pid,)).fetchall()}
    mod_count = 0
    task_count = 0
    for i, m in enumerate(modules):
        mname = (m.get("name") or "").strip()
        if not mname or mname in existing_mods:
            continue
        owner = (m.get("owner_role") or "后端").strip()
        mid = f"{pid}-m{int(time.time() * 1000)}{i}"
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO modules (id,project_id,name,desc,depends_on,owner_role,status,sort,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mid, pid, mname, (m.get("desc") or "").strip()[:200], "[]", owner, "idea",
             i, now, now),
        )
        mod_count += 1
        existing_mods.add(mname)
        tid = f"tp{time.time_ns()}"
        conn.execute(
            "INSERT INTO topics (id,project_id,module_id,name,agents,status,created_at) VALUES (?,?,?,?,?,?,?)",
            (tid, pid, mid, "默认讨论", "[]", "open", now),
        )
        for t in (m.get("tasks") or []):
            tname = (t.get("name") or "").strip()
            if not tname:
                continue
            _stg, _trk = _derive_task_stage_track(conn, mid, pid)
            conn.execute(
            "INSERT INTO tasks (id,project_id,module_id,topic_id,name,owner_role,status,done_criteria,stage,track,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"tk{time.time_ns()}", pid, mid, tid, tname[:60], owner, "todo",
             (t.get("done_criteria") or "").strip()[:300], _stg, _trk, now, now),
            )
            task_count += 1
    conn.commit()
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
        (pid, "分身 · 元神", "meta",
         f"🗂️ 已根据你的描述建立看板：{mod_count} 个模块 · {task_count} 个任务。\n"
         f"🎯 目标：{goal or '（沿用）'}\n✅ 完成标准：{standards or '（未设）'}\n"
         "团队将按看板顺序自主推进，直到 100% 再自动进入下一阶段。",
         "progress", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "goal": goal, "standards": standards, "modules": mod_count, "tasks": task_count}


@app.get("/api/projects/{pid}/topics")
def list_topics(pid: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM topics WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["agents"] = json.loads(d.get("agents") or "[]")
        out.append(d)
    conn.close()
    return out


@app.post("/api/projects/{pid}/topics")
async def create_topic(pid: str, req: Request):
    data = await req.json()
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "话题名不能为空"}
    conn = get_db()
    proj = conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    tid = f"tp{int(datetime.now().timestamp() * 1000)}"
    conn.execute(
        "INSERT INTO topics (id,project_id,module_id,name,agents,status,created_at) VALUES (?,?,?,?,?,?,?)",
        (tid, pid, data.get("module_id", ""), name, json.dumps(data.get("agents") or [], ensure_ascii=False),
         data.get("status", "open"), datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": tid}


@app.patch("/api/topics/{tid}")
async def update_topic(tid: str, req: Request):
    data = await req.json()
    conn = get_db()
    row = conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "话题不存在"}
    fields, vals = [], []
    for k in ("name", "module_id", "status"):
        if k in data:
            fields.append(f"{k}=?")
            vals.append(data[k])
    if "agents" in data:
        fields.append("agents=?")
        vals.append(json.dumps(data["agents"] or [], ensure_ascii=False))
    if fields:
        vals.append(tid)
        conn.execute(f"UPDATE topics SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/topics/{tid}/messages")
def list_topic_messages(tid: str):
    """话题对话组消息（三层模型：话题 = 绑定模块的讨论组）。"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM messages WHERE topic_id=? ORDER BY id", (tid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── P1-3 语义搜索（关键词召回 + LLM 语义重排，跨 模块/任务/话题/消息）──
SEMSEARCH_RANK_SYSTEM = """你是语义搜索引擎的重排器。下面给出用户查询与候选条目列表（每行格式：[序号][类型] 标题 | 附加字段）。
对每条候选判断它与查询的语义相关度（同义改写、意图相近也算相关，不只是字面匹配），输出 JSON 数组，每项：
{"i": 候选序号, "score": 0-100 整数, "snippet": "一句话摘要（≤40字），从该条目内容提炼并点明为何与查询相关"}
只保留 score>=50 的条目；全部不相关返回 []。只输出 JSON，不要任何解释。"""


@app.post("/api/search")
async def api_search(req: Request):
    """P1-3 语义搜索：先关键词召回（模块/任务/话题/消息），再交给 LLM 语义重排打分。"""
    data = await req.json()
    q = (data.get("q") or "").strip()
    pid = (data.get("project_id") or "").strip()
    if not q:
        return {"ok": False, "error": "搜索词不能为空"}
    conn = get_db()
    try:
        # 分词：整句 + 按空格/标点拆分（中文可整体命中）
        pats = [q]
        for w in re.split(r"[\s,，、;；:：]+", q):
            w = w.strip()
            if w and w not in pats:
                pats.append(w)
        pats = pats[:6]

        def _like(cols):
            parts, args = [], []
            for c in cols:
                for p in pats:
                    parts.append(f"{c} LIKE ?")
                    args.append(f"%{p}%")
            return "(" + " OR ".join(parts) + ")", args

        scope, sc_arg = "", []
        if pid:
            scope = "AND project_id=?"
            sc_arg = [pid]
        scope_t = scope.replace("project_id", "t.project_id")  # 话题查询带 LEFT JOIN，需消歧义

        # ① 模块召回
        lc, la = _like(["name", "desc", "domain", "flow"])
        mods = [dict(r) for r in conn.execute(
            f"SELECT id,project_id,name,desc,domain,flow,status,owner_role FROM modules WHERE {lc} {scope} ORDER BY sort LIMIT 8",
            la + sc_arg).fetchall()]
        # ② 任务召回
        lc, la = _like(["name", "done_criteria"])
        tasks = [dict(r) for r in conn.execute(
            f"SELECT id,project_id,module_id,topic_id,name,owner_role,status,stage FROM tasks WHERE {lc} {scope} ORDER BY updated_at DESC LIMIT 8",
            la + sc_arg).fetchall()]
        # ③ 话题召回（带模块名，便于展示）
        lc, la = _like(["t.name"])
        topics = [dict(r) for r in conn.execute(
            f"SELECT t.id,t.project_id,t.module_id,t.name,t.status,m.name AS module_name FROM topics t LEFT JOIN modules m ON m.id=t.module_id WHERE {lc} {scope_t} ORDER BY t.created_at DESC LIMIT 8",
            la + sc_arg).fetchall()]
        # ④ 消息召回（话题消息 + 项目群聊）
        lc, la = _like(["text"])
        msgs = [dict(r) for r in conn.execute(
            f"SELECT id,project_id,topic_id,sender,kind,text,ts FROM messages WHERE {lc} {scope} ORDER BY id DESC LIMIT 8",
            la + sc_arg).fetchall()]

        # ── 组装候选（给 LLM 的紧凑文本）──
        cand = []
        for m in mods:
            cand.append({"type": "module", "raw": m, "text": f"[模块] {m['name']} | 业务域:{m.get('domain','')} 流程:{m.get('flow','')} 状态:{m.get('status','')} | 描述:{m.get('desc','')[:60]}"})
        for t in tasks:
            cand.append({"type": "task", "raw": t, "text": f"[任务] {t['name']} | 模块:{t.get('module_id','')} 负责人:{t.get('owner_role','')} 状态:{t.get('status','')} 阶段:{t.get('stage','')}"})
        for tp in topics:
            cand.append({"type": "topic", "raw": tp, "text": f"[话题] {tp['name']} | 模块:{tp.get('module_name') or tp.get('module_id','')} 状态:{tp.get('status','')}"})
        for ms in msgs:
            cand.append({"type": "message", "raw": ms, "text": f"[消息] {ms.get('sender','')}: {ms.get('text','')[:80]}"})

        if not cand:
            conn.close()
            return {"ok": True, "query": q, "results": []}

        # ── LLM 语义重排 ──
        lines = "\n".join(f"{i} {c['text']}" for i, c in enumerate(cand))
        ranked, _m = await _llm_extract(
            SEMSEARCH_RANK_SYSTEM,
            f"用户查询：{q}\n候选条目：\n{lines}\n\n输出相关条目的打分 JSON。")
        scored = {}
        if ranked is not None:
            for it in ranked:
                try:
                    i = int(it.get("i", -1))
                    score = int(it.get("score", 0))
                    if 0 <= i < len(cand) and score >= 50:
                        scored[i] = {"score": score, "snippet": str(it.get("snippet", ""))[:120]}
                except (TypeError, ValueError):
                    continue
        # LLM 失败或空 → 兜底：按原序返回（score 0）
        results = []
        for i, c in enumerate(cand):
            sc = scored.get(i)
            r = c["raw"]
            if c["type"] == "module":
                results.append({"type": "module", "id": r["id"], "project_id": r["project_id"], "title": r["name"],
                                "snippet": sc["snippet"] if sc else (r.get("desc") or "")[:120],
                                "score": sc["score"] if sc else 0, "module_id": r["id"], "status": r.get("status", ""),
                                "domain": r.get("domain", ""), "flow": r.get("flow", "")})
            elif c["type"] == "task":
                results.append({"type": "task", "id": r["id"], "project_id": r["project_id"], "title": r["name"],
                                "snippet": sc["snippet"] if sc else "",
                                "score": sc["score"] if sc else 0, "module_id": r.get("module_id", ""),
                                "topic_id": r.get("topic_id", ""), "stage": r.get("stage", ""), "status": r.get("status", "")})
            elif c["type"] == "topic":
                results.append({"type": "topic", "id": r["id"], "project_id": r["project_id"], "title": r["name"],
                                "snippet": sc["snippet"] if sc else "",
                                "score": sc["score"] if sc else 0, "module_id": r.get("module_id", "")})
            else:
                results.append({"type": "message", "id": str(r["id"]), "project_id": r.get("project_id", ""),
                                "title": f"{r.get('sender','')} 说", "snippet": sc["snippet"] if sc else (r.get("text") or "")[:120],
                                "score": sc["score"] if sc else 0, "topic_id": r.get("topic_id", ""),
                                "sender": r.get("sender", "")})
        results.sort(key=lambda x: x["score"], reverse=True)
        conn.close()
        return {"ok": True, "query": q, "results": results[:20]}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": f"搜索异常：{e}"}


@app.post("/api/topics/{tid}/chat")
async def topic_chat(tid: str, req: Request):
    """话题对话（Phase C：上下文按模块隔离——只注入该模块的上下文窗口，token 针对性投入）。"""
    data = await req.json()
    user_text = (data.get("text") or "").strip()
    if not user_text:
        return {"ok": False, "error": "消息不能为空"}
    conn = get_db()
    topic = conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
    if not topic:
        conn.close()
        return {"ok": False, "error": "话题不存在"}
    pid = topic["project_id"]
    # 落库用户消息（带 topic_id，与项目群聊隔离）
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,topic_id) VALUES (?,?,?,?,?,?,?)",
        (pid, "你", "self", user_text, None, datetime.now().isoformat(), tid),
    )
    conn.commit()
    # ── 构造模块级上下文（Phase C 核心：只注入该模块相关）──
    mod = None
    if topic["module_id"]:
        mod = conn.execute("SELECT * FROM modules WHERE id=?", (topic["module_id"],)).fetchone()
    # 模块信息 + 依赖模块名
    mod_desc = ""
    if mod:
        deps = json.loads(mod["depends_on"] or "[]")
        dep_names = []
        for d in deps:
            dm = conn.execute("SELECT name FROM modules WHERE id=?", (d,)).fetchone()
            if dm:
                dep_names.append(dm["name"])
        mod_desc = (f"当前工作模块：{mod['name']}。\n模块说明：{mod['desc'] or '（未填写）'}。\n"
                    f"依赖模块：{'、'.join(dep_names) if dep_names else '无'}。\n"
                    f"模块上下文摘要：{mod['context_summary'] or '（暂无）'}")
    # 该模块相关任务（看板卡片，帮助 agent 理解模块进度）
    mod_tasks = []
    if mod:
        rows = conn.execute(
            "SELECT name,status,owner_role FROM tasks WHERE project_id=? AND module_id=? ORDER BY created_at",
            (pid, mod["id"]),
        ).fetchall()
        mod_tasks = [dict(r) for r in rows]
    task_desc = ""
    if mod_tasks:
        task_desc = "模块相关任务：\n" + "\n".join(
            f"- {t['name']}（{t['status']} · {t['owner_role']}）" for t in mod_tasks
        )
    # 话题内最近消息（只取该话题的，不污染其它模块/群聊）
    rows = conn.execute(
        "SELECT sender,kind,text FROM messages WHERE topic_id=? ORDER BY id DESC LIMIT 12", (tid,)
    ).fetchall()
    conn.close()
    # 组装 openai 格式上下文
    sys_prompt = (
        "你是分身里的项目协作 agent，在「话题对话组」里与用户讨论该模块的问题。\n"
        "回答要简短、直接、可执行，中文。\n"
        f"{mod_desc}\n{task_desc}"
    )
    hist = [{"role": "system", "content": sys_prompt}]
    for r in reversed(rows):
        if r["kind"] == "sys":
            continue
        role = "assistant" if r["kind"] != "self" else "user"
        hist.append({"role": role, "content": r["text"]})
    # 角色：话题绑定模块 → 用模块负责人角色调用（P3-1：按 roles 表反查 id，不再硬编码映射）
    agent_id = _role_id_by_name(mod["owner_role"]) if mod else "architect"
    if not agent_id:
        agent_id = "architect"
    reply = call_llm(agent_id, hist, sys_prompt, phase="topic-chat", scope_modules=1, project_id=pid)
    # 落库 agent 回复（带 topic_id）
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,topic_id) VALUES (?,?,?,?,?,?,?)",
        (pid, f"分身 · {mod['name'] if mod else '团队'}", "agent", reply, "progress", datetime.now().isoformat(), tid),
    )
    conn.commit()
    conn.close()
    return {"reply": reply, "ok": True}


# ── API：任务（v3 Phase B 看板卡片，话题提炼而来）───────────────
@app.get("/api/projects/{pid}/tasks")
def list_tasks(pid: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── v6.1 矩阵看板：模块 × 阶段 接口 ──────────────────────────────────
# 阶段 → 责任角色（用于格子补卡时自动指定 owner_role）
STAGE_ROLE = {
    "策划": "产品", "原型": "前端", "前端": "前端", "H5开发": "前端", "页面": "前端",
    "后端": "后端", "接口对接": "后端", "云函数": "后端", "联调": "后端", "封装": "后端",
    "测试": "测试", "提审": "测试", "发布": "后端", "上架": "后端", "设计": "前端", "实现": "后端",
}


def _cell_status(tasks):
    if not tasks:
        return "blank"
    st = {t["status"] for t in tasks}
    if "blocked" in st:
        return "blocked"
    if "doing" in st:
        return "doing"
    if "review" in st:
        return "review"
    if all(s == "done" for s in st):
        return "done"
    if all(s in ("todo", "idea") for s in st):
        return "todo"
    return "doing"  # 混合态（部分 done + 部分 todo）按进行中处理


def _matrix_critical_path(mods, cells_by_m):
    """沿 depends_on 计算最长「未完成」依赖链，返回模块 id 列表（关键路径高亮用）。"""
    done = {}
    for m in mods:
        cs = cells_by_m.get(m["id"], [])
        done[m["id"]] = bool(cs) and all(c["status"] == "done" for c in cs)

    def long_path(mid, seen):
        if mid in seen:
            return []
        seen = seen | {mid}
        m = next((x for x in mods if x["id"] == mid), None)
        if not m:
            return [mid]
        best = []
        for dep in (m.get("depends_on") or []):
            p = long_path(dep, seen)
            if len(p) > len(best):
                best = p
        return best + [mid]

    roots = [m["id"] for m in mods if not (m.get("depends_on") or [])]
    chains = [long_path(r, set()) for r in roots]
    cand = [c for c in chains if any(not done.get(x, False) for x in c)] or chains
    cand.sort(key=len, reverse=True)
    return cand[0] if cand else []


@app.get("/api/projects/{pid}/matrix")
def project_matrix(pid: str, track: str = "web"):
    """返回模块×阶段矩阵：stages / modules / cells / 行·列·项目进度 / 空白格数 / 关键路径。
    legacy 任务（stage 为空）汇总进末列「未分环节」，保证 0 丢失。"""
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "项目不存在"})
    stages = get_stage_chain(conn, pid, track)
    mods = [_module_dict(r) for r in
            conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort, created_at", (pid,)).fetchall()]
    tasks = [dict(r) for r in
             conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()]
    # 版本管理：一次性汇总本项目所有格子版本信息（confirmed 标记 + 计数），供矩阵角标
    _vrows = conn.execute(
        "SELECT module_id,stage,kind FROM module_versions WHERE project_id=?", (pid,)).fetchall()
    ver_map = {}
    for _vr in _vrows:
        _k = (_vr["module_id"], _vr["stage"])
        _e = ver_map.get(_k, {"count": 0, "confirmed": False})
        _e["count"] += 1
        if _vr["kind"] == "confirmed":
            _e["confirmed"] = True
        ver_map[_k] = _e
    conn.close()

    # v6.2 P2：生命周期轨道（infra/release/ops）按阶段汇总项目级任务，渲染为单列阶段清单
    lifecycle = track in LIFE_TRACKS
    if lifecycle:
        uncat = []
        tmods = [{"id": "__life__", "name": LIFE_LABELS.get(track, track),
                  "track": track, "weight": 1, "depends_on": [], "sort": 0, "desc": ""}]
        life_tasks = [t for t in tasks if (t.get("track") or "") == track]
        cells = {"__life__": {}}
        cells_by_m = {"__life__": []}
        for s in stages:
            tk = [t for t in life_tasks if (t.get("stage") or "") == s]
            total = len(tk)
            done_n = sum(1 for t in tk if t["status"] == "done")
            pct = round(done_n * 100 / total) if total else 0
            c = {"stage": s, "status": _cell_status(tk), "pct": pct,
                 "total": total, "done": done_n,
                 "task_ids": [t["id"] for t in tk],
                 "versions": ver_map.get(("__life__", s), {"count": 0, "confirmed": False}),
                 "tasks": [{"id": t["id"], "name": t["name"], "owner_role": t.get("owner_role", ""),
                            "status": t["status"], "done_criteria": t.get("done_criteria", "")} for t in tk]}
            cells["__life__"][s] = c
            cells_by_m["__life__"].append(c)
    else:
        # 轨道筛模块：多轨道项目只看当前轨道；单轨道兜底全部
        tmods = [m for m in mods if (m.get("track") or "web") == track]
        if not tmods:
            tmods = mods
        # legacy 无 stage 任务 → 末列「未分环节」
        uncat = [t for t in tasks if not (t.get("stage") or "")]
        if uncat:
            stages = stages + ["__uncat__"]

        cells = {}
        cells_by_m = {}
        for m in tmods:
            cells[m["id"]] = {}
            cells_by_m[m["id"]] = []
            for s in stages:
                if s == "__uncat__":
                    tk = [t for t in uncat if t["module_id"] == m["id"]]
                else:
                    tk = [t for t in tasks if t["module_id"] == m["id"] and (t.get("stage") or "") == s]
                total = len(tk)
                done_n = sum(1 for t in tk if t["status"] == "done")
                pct = round(done_n * 100 / total) if total else 0
                c = {"stage": s, "status": _cell_status(tk), "pct": pct,
                     "total": total, "done": done_n,
                     "task_ids": [t["id"] for t in tk],
                     "versions": ver_map.get((m["id"], s), {"count": 0, "confirmed": False}),
                     "tasks": [{"id": t["id"], "name": t["name"], "owner_role": t.get("owner_role", ""),
                                "status": t["status"], "done_criteria": t.get("done_criteria", "")} for t in tk]}
                cells[m["id"]][s] = c
                cells_by_m[m["id"]].append(c)

    # v6.4 P0 快赢：看板每格 token 成本透出（开关门控；开启时按格聚合该格任务累计 token）
    if _cost_visibility_enabled():
        try:
            _cc = get_db()
            _rows = _cc.execute(
                "SELECT task_id, COALESCE(SUM(input_tokens+output_tokens),0) FROM model_usage "
                "WHERE task_id<>'' AND project_id=? GROUP BY task_id", (pid,)).fetchall()
            _cc.close()
            _tok_map = {r[0]: r[1] for r in _rows}
            for _m in cells:
                for _s in cells[_m]:
                    _c = cells[_m][_s]
                    _c["cost_tokens"] = sum(_tok_map.get(_t, 0) for _t in _c.get("task_ids", []))
        except Exception:
            pass

    # 聚合：列(模块)进度 / 行(阶段)进度 / 项目进度（blank 不计入分母）
    column_pct, row_pct = {}, {}
    for m in tmods:
        cs = [cells[m["id"]][s] for s in stages if cells.get(m["id"], {}).get(s)]
        nb = [c for c in cs if c["status"] != "blank"]
        column_pct[m["id"]] = round(sum(c["pct"] for c in nb) / len(nb)) if nb else 0
    for s in stages:
        cs = [cells[m["id"]][s] for m in tmods if cells.get(m["id"], {}).get(s)]
        nb = [c for c in cs if c["status"] != "blank"]
        row_pct[s] = round(sum(c["pct"] for c in nb) / len(nb)) if nb else 0
    tot_w = sum((m.get("weight") or 1) for m in tmods) or 1
    project_pct = round(sum(column_pct.get(m["id"], 0) * (m.get("weight") or 1) for m in tmods) / tot_w)

    blank_count = sum(1 for m in tmods for s in stages
                      if stages and cells.get(m["id"], {}).get(s, {}).get("status") == "blank")
    # 未分环节列不计入空白（那是 legacy，不是「该做没做」的空白）
    blank_count -= sum(1 for m in tmods if cells[m["id"]].get("__uncat__", {}).get("status") == "blank")
    blank_count = max(blank_count, 0)

    crit = _matrix_critical_path(tmods, cells_by_m)
    return {
        "track": track,
        "tracks": list(dict.fromkeys(list(json.loads(proj["tracks"] or '["web"]') if proj["tracks"] else ["web"]) + LIFE_TRACKS)),
        "stages": stages,
        "modules": tmods,
        "cells": cells,
        "column_pct": column_pct,
        "row_pct": row_pct,
        "project_pct": project_pct,
        "blank_count": blank_count,
        "uncat_count": len(uncat),
        "critical_path": crit,
        "task_total": len(tasks),
        "task_done": sum(1 for t in tasks if t["status"] == "done"),
    }


@app.get("/api/stage-presets")
def stage_presets():
    return {"ok": True, "presets": STAGE_PRESETS}


@app.get("/api/projects/{pid}/readiness")
def project_readiness(pid: str):
    """v6.2 P2 全链路就绪验收：基础设施轨道 done + 发布轨道 done + 运营轨道 started → 可运营。"""
    conn = get_db()
    proj = conn.execute("SELECT tracks FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "项目不存在"})
    tracks = json.loads(proj["tracks"] or '["web"]')
    out = {}
    for t in LIFE_TRACKS:
        if t not in tracks:
            out[t] = {"present": False, "status": "not_started", "stages": []}
            continue
        stages = get_stage_chain(conn, pid, t)
        life_tasks = [dict(r) for r in
                      conn.execute("SELECT * FROM tasks WHERE project_id=? AND track=?", (pid, t)).fetchall()]
        stage_info, all_done, any_task = [], True, False
        for s in stages:
            tk = [x for x in life_tasks if (x.get("stage") or "") == s]
            if tk:
                any_task = True
            dn = sum(1 for x in tk if x["status"] == "done")
            st = "done" if (tk and dn == len(tk)) else ("doing" if tk else "blank")
            if st != "done":
                all_done = False
            stage_info.append({"stage": s, "total": len(tk), "done": dn, "status": st})
        status = "done" if (all_done and any_task) else ("started" if any_task else "not_started")
        out[t] = {"present": True, "status": status, "stages": stage_info}
    conn.close()
    operational = (out["infra"]["status"] == "done" and out["release"]["status"] == "done"
                   and out["ops"]["status"] != "not_started")
    return {"ok": True, "tracks": tracks, "lifecycle": out, "operational_ready": operational,
            "message": "全链路就绪 ✅ 可从 idea 进入可运营" if operational
                       else "未就绪：基础设施·发布需 done，运营需 started"}


@app.get("/api/projects/{pid}/stage-chain")
def get_stage_chain_api(pid: str, track: str = "web"):
    conn = get_db()
    proj = conn.execute("SELECT stage_chains, tracks FROM projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not proj:
        return {"ok": False, "error": "项目不存在"}
    try:
        chains = json.loads(proj["stage_chains"] or "{}") or {}
    except Exception:
        chains = {}
    tracks = json.loads(proj["tracks"] or '["web"]') or ["web"]
    return {"ok": True, "tracks": tracks, "track": track,
            "chain": chains.get(track, STAGE_PRESETS.get(track, STAGE_PRESETS["web"]))}


@app.put("/api/projects/{pid}/stage-chain")
async def put_stage_chain_api(pid: str, req: Request):
    data = await req.json()
    track = data.get("track", "web")
    chain = data.get("chain") or []
    conn = get_db()
    proj = conn.execute("SELECT stage_chains FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    try:
        chains = json.loads(proj["stage_chains"] or "{}") or {}
    except Exception:
        chains = {}
    chains[track] = [str(x) for x in chain]
    conn.execute("UPDATE projects SET stage_chains=? WHERE id=?", (json.dumps(chains, ensure_ascii=False), pid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/projects/{pid}/cell/tasks")
async def create_cell_task(pid: str, req: Request):
    """在指定格子（module × stage）建任务。"""
    data = await req.json()
    mid = data.get("module_id") or ""
    stage = data.get("stage") or ""
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "任务名不能为空"}
    track = data.get("track", "web")
    if track in LIFE_TRACKS:
        pass  # 生命周期轨道任务不绑定功能模块，跳过 module 校验
    elif not mid:
        return {"ok": False, "error": "缺少 module_id"}
    conn = get_db()
    if track not in LIFE_TRACKS and not conn.execute("SELECT 1 FROM modules WHERE id=? AND project_id=?", (mid, pid)).fetchone():
        conn.close()
        return {"ok": False, "error": "模块不存在"}
    tid = f"tk{int(datetime.now().timestamp() * 1000)}"
    conn.execute(
        "INSERT INTO tasks (id,project_id,module_id,topic_id,name,owner_role,status,done_criteria,stage,track,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tid, pid, mid, "", name, data.get("owner_role") or STAGE_ROLE.get(stage, "后端"), "todo",
         (data.get("done_criteria") or "").strip()[:300], stage, track,
         datetime.now().isoformat()),
    )
    # 群聊↔看板联动：新卡建成立即回写主群聊一条可跳转的系统动态
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,task_id) VALUES (?,?,?,?,?,?,?)",
        (pid, "系统", "sys", f"📌 新建任务「{name}」于看板（{stage}）", "done", datetime.now().isoformat(), tid),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "task_id": tid}


def _blank_proposals(conn, pid: str, track: str = "web") -> list:
    """计算补空白格建议（纯计算，不落库）：前一阶段同模块已 done、当前格空白 → 生成待补卡。
    供 HTTP 接口 fill_blanks 与 M2 调度器 _fill_blanks_internal 共用。"""
    stages = get_stage_chain(conn, pid, track)
    mods = [_module_dict(r) for r in
            conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort, created_at", (pid,)).fetchall()]
    tasks = [dict(r) for r in
             conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()]
    tmods = [m for m in mods if (m.get("track") or "web") == track] or mods
    mod_tasks = {}
    for m in tmods:
        mod_tasks[m["id"]] = {}
        for s in stages:
            mod_tasks[m["id"]][s] = [t for t in tasks if t["module_id"] == m["id"] and (t.get("stage") or "") == s]
    proposals = []
    for m in tmods:
        for i, s in enumerate(stages):
            tk = mod_tasks[m["id"]][s]
            if tk:
                continue  # 非空白格跳过
            prev_stage = stages[i - 1] if i > 0 else None
            prev_tasks = mod_tasks[m["id"]][prev_stage] if prev_stage else []
            # 前置阶段必须「有任务且全部 done」才算就绪（空白/未全 done 都视为未到点，防提前刷屏）
            prev_done = (i == 0) or (len(prev_tasks) > 0 and all(t["status"] == "done" for t in prev_tasks))
            if not prev_done:
                continue  # 前一阶段没 done → 还没到点
            proposals.append({
                "module_id": m["id"], "module_name": m["name"], "stage": s,
                "name": f"{s}：{m['name']}", "owner_role": STAGE_ROLE.get(s, "后端"),
            })
    return proposals


@app.post("/api/projects/{pid}/fill-blanks")
async def fill_blanks(pid: str, req: Request):
    """元神补空白格：找「前一阶段同模块已 done、当前格空白」的格子，生成待办卡。
    dry_run=true 只返回建议不落库；否则创建 todo 任务。"""
    data = await req.json()
    track = data.get("track", "web")
    dry_run = bool(data.get("dry_run", True))
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    proposals = _blank_proposals(conn, pid, track)
    conn.close()
    if dry_run:
        return {"ok": True, "dry_run": True, "count": len(proposals), "proposals": proposals}
    created = []
    conn = get_db()
    for p in proposals:
        tid = f"tk{int(datetime.now().timestamp() * 1000)}_{len(created)}"
        conn.execute(
            "INSERT INTO tasks (id,project_id,module_id,topic_id,name,owner_role,status,done_criteria,stage,track,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, pid, p["module_id"], "", p["name"], p["owner_role"], "todo", "",
             p["stage"], track, datetime.now().isoformat(), datetime.now().isoformat()),
        )
        created.append(tid)
    conn.commit()
    conn.close()
    return {"ok": True, "dry_run": False, "created": len(created), "task_ids": created}


@app.post("/api/topics/{tid}/tasks")
async def distill_task(tid: str, req: Request):
    """话题 → 任务（R2：任务必有来源；提炼后自动进看板待办）。"""
    data = await req.json()
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "任务名不能为空"}
    conn = get_db()
    topic = conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
    if not topic:
        conn.close()
        return {"ok": False, "error": "话题不存在"}
    tid2 = f"tk{int(datetime.now().timestamp() * 1000)}"
    done_criteria = (data.get("done_criteria") or "").strip()[:300]
    # P0-4：优先用前端显式传入的 stage/track，缺失则从模块生命周期推导
    _explicit_stg = (data.get("stage") or "").strip()
    _explicit_trk = (data.get("track") or "").strip()
    if _explicit_stg:
        _stg, _trk = _explicit_stg, (_explicit_trk or "web")
    else:
        _stg, _trk = _derive_task_stage_track(conn, topic["module_id"], topic["project_id"])
    conn.execute(
        "INSERT INTO tasks (id,project_id,module_id,topic_id,name,owner_role,status,done_criteria,stage,track,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid2, topic["project_id"], topic["module_id"], tid, name,
         data.get("owner_role", "后端"), "todo", done_criteria, _stg, _trk, datetime.now().isoformat(), datetime.now().isoformat()),
    )
    # 同步：话题内落一条系统消息
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,topic_id) VALUES (?,?,?,?,?,?,?)",
        (topic["project_id"], "系统", "sys", f"✅ 已提炼为任务「{name}」→ 进入看板待办列", "done",
         datetime.now().isoformat(), tid),
    )
    # 群聊↔看板联动：提炼出的任务同步回写主群聊一条可跳转动态
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,task_id) VALUES (?,?,?,?,?,?,?)",
        (topic["project_id"], "系统", "sys", f"📌 已提炼任务「{name}」→ 看板待办", "done",
         datetime.now().isoformat(), tid2),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "task_id": tid2}


@app.post("/api/tasks/{task_id}/move")
async def move_task(task_id: str, req: Request):
    """任务看板列流转（不依赖模块依赖门禁，任务独立流转——R5）。"""
    data = await req.json()
    to = data.get("to")
    if to not in MODULE_STATUS:
        return {"ok": False, "error": f"目标状态非法：{to}"}
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "任务不存在"}
    conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (to, datetime.now().isoformat(), task_id))
    # 群聊↔看板联动：看板列流转回写主群聊一条可跳转的系统动态
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,task_id) VALUES (?,?,?,?,?,?,?)",
        (row["project_id"], "系统", "sys", f"📌 已将「{row['name']}」移至【{to}】", to,
         datetime.now().isoformat(), task_id),
    )
    conn.commit()
    # Phase D：任务完成 → 自动沉淀（经验 + 技能草稿 + 模块摘要更新）
    settled = False
    advanced = ""
    if to == "done":
        settled = _settle_task_done(conn, dict(row))
        # v4.2 自主闭环：看板 100% → 自动推进下一阶段
        advanced = _auto_advance_phase(conn, row["project_id"])
        # M2：任务完成 → 立即唤醒元神调度器补位（毫秒级）
        _kick_autonomy()
    conn.close()
    return {"ok": True, "status": to, "settled": settled, "advanced_phase": advanced}


def _settle_task_done(conn, task: dict) -> bool:
    """任务完成闭环：经验入库 + 可复用技能草稿 + 模块 context_summary 摘要沉淀。
    返回是否产生了沉淀。"""
    try:
        now = datetime.now().isoformat()
        pid = task["project_id"]
        # 1) 经验入库（success 案例，来源 task）
        scenario = task["name"][:30].rstrip("，。,.")
        exists = conn.execute(
            "SELECT id FROM experiences WHERE scenario=?", (scenario,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO experiences (category,scenario,goal,attempts,outcome,lesson,project_id,source,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("success", scenario, f"完成任务：{task['name']}", "", "任务完成", "任务已通过看板完成，流程可复用", pid, "task", now),
            )
        # 2) 模块 context_summary 更新（上下文释放前的摘要沉淀）
        if task["module_id"]:
            mod = conn.execute("SELECT * FROM modules WHERE id=?", (task["module_id"],)).fetchone()
            if mod:
                done_tasks = conn.execute(
                    "SELECT name FROM tasks WHERE project_id=? AND module_id=? AND status='done' ORDER BY created_at",
                    (pid, task["module_id"]),
                ).fetchall()
                done_names = "、".join(r["name"] for r in done_tasks[-5:])
                summary = (f"已完成任务：{done_names or task['name']}。"
                           f"模块「{mod['name']}」累计完成 {len(done_tasks)} 项。")
                conn.execute(
                    "UPDATE modules SET context_summary=?, updated_at=? WHERE id=?",
                    (summary, now, task["module_id"]),
                )
        # 3) 话题内落系统消息（闭环可见）
        if task["topic_id"]:
            conn.execute(
                "INSERT INTO messages (project_id,sender,kind,text,tag,ts,topic_id) VALUES (?,?,?,?,?,?,?)",
                (pid, "系统", "sys", f"✅ 任务「{task['name']}」已完成 → 已沉淀经验与模块摘要", "done", now, task["topic_id"]),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "任务不存在"}
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── P1-C：精准定位窄播（团队级 blast radius 的通知侧应用）─────────────
def _narrowcast_targets(pid: str, module_id: str) -> list:
    """P1-C 精准定位窄播：计算受某模块变更影响的成员（角色）集合。
    命中规则：① 该模块 owner_role ② 依赖该模块的其它模块的 owner_role（波及面）。
    保守多报（宁可多拉不可漏，对齐 CRG F1 路线）。返回角色展示名列表。"""
    if not module_id:
        return []
    conn = get_db()
    mods = conn.execute("SELECT id,name,depends_on,owner_role FROM modules WHERE project_id=?", (pid,)).fetchall()
    conn.close()
    focus = next((m for m in mods if m["id"] == module_id), None)
    if not focus:
        return []
    target_roles = set()
    if focus["owner_role"]:
        target_roles.add(focus["owner_role"])
    # 被此模块依赖的其它模块 → 其 owner 也受影响（波及）
    for m in mods:
        raw = m["depends_on"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw) if raw.strip() else []
            except Exception:
                raw = []
        if not isinstance(raw, list):
            raw = []
        if focus["name"] in raw and m["owner_role"]:
            target_roles.add(m["owner_role"])
    _, role_names = _roles_from_db()
    return [role_names.get(r, r) for r in sorted(target_roles)]


def _narrowcast_notify(pid: str, text: str, targets: list):
    """P1-C 窄播：把通知只发给受影响的成员（@提及 + narrowcast 标记），而非全群广播。"""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
            (pid, "分身 · 元神", "meta", text, "narrowcast", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


@app.post("/api/meta/code_stream")
async def code_stream(req: Request):
    """批次C D7：流式代码生成(SSE)。开关 code_stream_enabled 未开启时返回 403。
    body: {pid, module_id, role, message} → 选代码专用模型(若启用) / 角色配置，按 SSE 流式输出。"""
    if not _code_stream_enabled():
        return JSONResponse(status_code=403, content={"ok": False, "error": "code_stream_enabled 未开启"})
    try:
        data = await req.json()
    except Exception:
        data = {}
    pid = (data.get("pid") or data.get("project_id") or "").strip()
    role = (data.get("role") or "backend").strip()
    message = (data.get("message") or "").strip()
    if not message:
        return JSONResponse(status_code=422, content={"ok": False, "error": "缺少 message"})
    # 选模型：代码专用模型(若启用且匹配) → 角色配置/降级链兜底
    cm = _code_model_for_role(role)
    cands = None
    if cm:
        _b, _k, _ = _key_for_provider(cm["provider"], role)
        if _k:
            cands = [(cm["provider"], _b or PROVIDER_PRESETS[cm["provider"]]["base"], _k, cm["model"])]
    if not cands:
        cands = _available_providers(role)
    if not cands:
        return JSONResponse(status_code=503, content={"ok": False, "error": "无可用代码模型"})
    provider, base, key, model = cands[0]
    # 系统提示：角色基础 + 代码质量约束 + 代码库语义(若开启)
    _rolesystems, _ = _roles_from_db()
    sys_prompt = _rolesystems.get(role, "")
    sys_prompt += _build_code_quality_prompt(role)
    if _code_semantic_enabled():
        try:
            conn = get_db(); proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone(); conn.close()
            if proj:
                root = _project_storage_root(dict(proj))
                cb = _build_codebase_context(root, data.get("module_id") or "", role)
                if cb:
                    sys_prompt += "\n\n" + cb
        except Exception:
            pass
    history = [{"role": "user", "content": message}]

    def _gen():
        try:
            for chunk in _call_single_provider_stream(provider, base, key, model, history, sys_prompt):
                yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)[:300]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.post("/api/meta/code_scan")
async def code_scan(req: Request):
    """批次C D10：代码静态扫描预览（宪法护栏）。返回 {blocked, warnings, reasons}；
    拦截判定仅在 code_static_scan_enabled 开启时生效（与 _write_file_tool 同一道门）。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    path = (data.get("path") or "").strip()
    content = data.get("content") or ""
    return {"ok": True, **_code_static_scan(path, content)}


@app.get("/api/narrowcast/targets")
def narrowcast_targets(project_id: str = "", module_id: str = ""):
    """P1-C：返回某模块变更将窄播通知的成员集合（精准定位，不广播全群）。"""
    if not project_id:
        return {"ok": False, "error": "project_id 必填"}
    targets = _narrowcast_targets(project_id, module_id)
    return {"ok": True, "targets": targets}


# ── 版本管理（看板格 module×stage 版本化：冻结确认基线 / 预编辑分支 / 恢复 / 对比）──
def _cell_stage_of_task(task: dict) -> str:
    """看板格的纵轴键 = 任务的 stage（生命周期阶段）；缺省回落 status。"""
    return (task.get("stage") or task.get("status") or "").strip()


def _resolve_dispatch_module(conn, pid: str, act: dict, mods: list) -> str:
    """v6.4 P0 修复：对话派单解析目标模块，确保任务落进真实 module×stage 格而非「未分环节」。
    ① action 显式指定且属于本项目 → 用其；② 否则取首个模块（既有行为）；
    ③ 项目尚无模块 → 自动建「默认模块」兜底（board grounding 不断裂）。返回模块 id。"""
    _act_mod = (act.get("module_id") or "").strip()
    if _act_mod and conn.execute("SELECT 1 FROM modules WHERE id=? AND project_id=?", (_act_mod, pid)).fetchone():
        return _act_mod
    if mods:
        return mods[0]["id"]
    try:
        _dm = conn.execute("SELECT id FROM modules WHERE project_id=? AND name=? LIMIT 1", (pid, "默认模块")).fetchone()
        if _dm:
            return _dm["id"]
        _mid = f"{pid}-m{int(time.time()*1000)}"
        _now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO modules (id,project_id,name,desc,depends_on,owner_role,status,sort,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_mid, pid, "默认模块", "对话自动建立的默认模块", "[]", "后端", "idea", 0, _now, _now))
        conn.commit()
        return _mid
    except Exception:
        return ""


def _derive_task_stage_track(conn, module_id: str, project_id: str) -> tuple:
    """P0-4：对话流任务落 stage——从模块当前生命周期推导新任务应落 stage/track。
    优先级：① 该模块已有任务的 stage（模块当前阶段）→ ② 项目该轨道阶段链首阶段 → ③ '策划'。
    返回 (stage, track)。
    """
    if not module_id:
        return ("", "web")
    mod = conn.execute("SELECT track FROM modules WHERE id=?", (module_id,)).fetchone()
    track = (mod["track"] if mod and (mod["track"] or "") else "web")
    # ① 模块已有任务的 stage（取最近更新的）
    ex = conn.execute(
        "SELECT stage FROM tasks WHERE module_id=? AND stage<>'' ORDER BY updated_at DESC LIMIT 1",
        (module_id,)).fetchone()
    if ex and ex["stage"]:
        return (ex["stage"], track)
    # ② 项目轨道阶段链首阶段
    try:
        stages = get_stage_chain(conn, project_id, track)
        if stages:
            return (stages[0], track)
    except Exception:
        pass
    # ③ 兜底
    return ("策划", track)


def _cell_latest(conn, mid: str, stage: str) -> dict:
    row = conn.execute(
        "SELECT * FROM module_versions WHERE module_id=? AND stage=? ORDER BY version_no DESC LIMIT 1",
        (mid, stage)).fetchone()
    return dict(row) if row else None


def _cell_has_confirmed(conn, mid: str, stage: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM module_versions WHERE module_id=? AND stage=? AND kind='confirmed' LIMIT 1",
        (mid, stage)).fetchone()
    return bool(row)


def _freeze_cell_version(conn, mid: str, stage: str, pid: str, kind: str = "wip",
                         content: str = "", source_task_id: str = "", topic_id: str = "",
                         created_by: str = "local", parent: str = "") -> int:
    """对 (模块,阶段) 格打一个版本快照；返回 version_no。自动捕获模块规约（可恢复基线）。"""
    mod = conn.execute("SELECT * FROM modules WHERE id=?", (mid,)).fetchone()
    if not mod:
        return 0
    cur_no = conn.execute(
        "SELECT COALESCE(MAX(version_no),0) FROM module_versions WHERE module_id=? AND stage=?",
        (mid, stage)).fetchone()[0]
    nxt = cur_no + 1
    conn.execute(
        """INSERT INTO module_versions (id,module_id,project_id,stage,version_no,kind,
            snapshot_name,snapshot_desc,snapshot_acceptance,snapshot_owner,snapshot_status,
            content,source_task_id,topic_id,parent_version,created_at,created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"mv_{mid}_{stage}_{nxt}", mid, pid, stage, nxt, kind,
         mod["name"], mod["desc"] or "", "", mod["owner_role"] or "", mod["status"] or "",
         content or "", source_task_id or "", topic_id or "", parent or "",
         datetime.now().isoformat(), created_by))
    conn.commit()
    return nxt


@app.post("/api/tasks/{task_id}/verify")
async def verify_task(task_id: str, req: Request):
    """P0-C 看板验收门：用任务验收标准（或 done_criteria / 项目 standards）对照产出判定；达标转 done，否则标记 review。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    output = (data.get("output") or "").strip()
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return {"ok": False, "error": "任务不存在"}
    pid = task["project_id"]
    proj = conn.execute("SELECT standards FROM projects WHERE id=?", (pid,)).fetchone()
    project_standards = proj["standards"] if proj else ""
    acceptance = (task["acceptance_criteria"] or "").strip() or (task["done_criteria"] or "").strip()
    conn.close()
    if not output:
        return {"ok": False, "error": "请提供待验收的产出内容（output）"}
    status, reason = await _judge_role_output(output, acceptance, project_standards, task["owner_role"])
    # 批次A D3：确定性质量门（可选，默认关闭）——代码类任务先过编译/测试关再判定
    if _code_verify_enabled() and status != "todo":
        try:
            _qg = await asyncio.to_thread(run_quality_gate_project, pid, task["module_id"] or "")
            if not _qg["ok"]:
                status, reason = "review", "确定性质量门未通过：" + _qg["summary"][:200]
        except Exception:
            pass
    # E1+E3：经验泛化 + 质量门（代码任务经 code_attribution_enabled 门；非代码经 evolution_record_enabled 门）
    try:
        _record_experience(dict(task), status == "done", reason,
                           has_standard=bool(acceptance), standard_result=status)
    except Exception:
        pass
    # P1-C：精准定位窄播——只把验收结果 @ 给受该模块影响的成员，而非全群广播
    targets = _narrowcast_targets(pid, task["module_id"] or "")
    mention = (" @" + " @".join(targets)) if targets else ""
    if status == "done":
        _task_status(task_id, "done", pid, f"✅ 验收通过：{task['name']}（{reason}）")
        # 版本管理：冻结该 (模块,阶段) 格的确认基线（满意的部分被钉死，后续改动生成新版本、可回滚）
        _vstage = _cell_stage_of_task(dict(task))
        try:
            _vconn = get_db()
            _freeze_cell_version(_vconn, task["module_id"] or "", _vstage, pid,
                                 kind="confirmed", content=output, source_task_id=task_id,
                                 topic_id=task["topic_id"] or "", created_by="verify")
            _vconn.close()
        except Exception:
            pass
        _narrowcast_notify(pid, f"✅ 验收通过：{task['name']}（{reason}）{mention}", targets)
        return {"ok": True, "pass": True, "status": "done", "reason": reason, "narrowcast": targets}
    _task_status(task_id, "review", pid, f"🔍 验收未通过：{task['name']}（{reason}）")
    _narrowcast_notify(pid, f"🔍 验收未通过：{task['name']}（{reason}）{mention}", targets)
    return {"ok": True, "pass": False, "status": "review", "reason": reason, "narrowcast": targets}


# ── 批次A D3：确定性质量门 API ───────────────────────────────────────────────
@app.post("/api/meta/quality_gate")
async def quality_gate(req: Request):
    """对项目/模块工作区跑确定性质量门（编译 + 可选 pytest）。
    默认开关关闭时不改变既有行为；开启后可由前端/自动化在交付前调用，返回结构化结果。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    pid = (data.get("pid") or data.get("project_id") or "").strip()
    mid = (data.get("module_id") or "").strip()
    if not pid:
        return {"ok": False, "error": "缺少 pid（项目 id）"}
    result = await asyncio.to_thread(run_quality_gate_project, pid, mid)
    return {"ok": True, "passed": result["ok"], "pid": pid, "module_id": mid, **result}


@app.post("/api/meta/codebase")
async def codebase_context(req: Request):
    """批次B D5：返回项目锚点契约 + 工作区符号索引（供前端展示/调试，或代码任务自检）。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    pid = (data.get("pid") or data.get("project_id") or "").strip()
    mid = (data.get("module_id") or "").strip()
    if not pid:
        return {"ok": False, "error": "缺少 pid（项目 id）"}
    try:
        conn = get_db()
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        conn.close()
        if not proj:
            return {"ok": False, "error": "项目不存在"}
        root = _project_storage_root(dict(proj))
        anchor = _read_anchor_file(root)
        workdir = os.path.join(root, "modules", mid) if mid else root
        symbols = _symbol_index(workdir)
        return {"ok": True, "pid": pid, "module_id": mid,
                "anchor": anchor, "anchor_present": bool(anchor),
                "symbols": symbols, "symbols_present": bool(symbols)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/meta/vcs")
async def vcs_api(req: Request):
    """批次B D6：代理级版本管理操作。
    body.action: status | commit | merge
      - status  {pid}                      → 当前分支/工作区状态
      - commit  {pid, task_id, message}    → 原子提交当前工作区（该任务分支）
      - merge   {pid, task_id}             → 评审后 fast-forward 合回 main
    危险 git 子集（push --force/reset --hard 等）不受此接口提供，仍走人工确认闸门。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    action = (data.get("action") or "").strip().lower()
    pid = (data.get("pid") or "").strip()
    if not pid:
        return {"ok": False, "error": "缺少 pid（项目 id）"}
    try:
        conn = get_db()
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        conn.close()
        if not proj:
            return {"ok": False, "error": "项目不存在"}
        root = _project_storage_root(dict(proj))
        if action == "status":
            if not _vcs_init(root):
                return {"ok": True, "initialized": False, "branch": "", "dirty": False}
            br = _vcs_safe_git(root, "symbolic-ref", "--short", "HEAD").get("out", "").strip()
            dirty = bool(_vcs_safe_git(root, "status", "--porcelain").get("out", "").strip())
            return {"ok": True, "initialized": True, "branch": br, "dirty": dirty}
        if action == "commit":
            tid = (data.get("task_id") or "").strip()
            if not tid:
                return {"ok": False, "error": "缺少 task_id"}
            _vcs_task_branch(root, tid)
            res = await asyncio.to_thread(_vcs_commit, root, tid, (data.get("message") or "分身提交")[:200])
            return {"ok": True, **res}
        if action == "merge":
            tid = (data.get("task_id") or "").strip()
            if not tid:
                return {"ok": False, "error": "缺少 task_id"}
            res = await asyncio.to_thread(_vcs_merge, root, tid)
            return {"ok": True, **res}
        return {"ok": False, "error": "未知 action（status/commit/merge）"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ── 版本管理 API：按看板格 (module×stage) 版本化 ──────────────────────────────
@app.get("/api/cells/{mid}/{stage}/versions")
def cell_versions(mid: str, stage: str):
    """列出某格全部版本（含 confirmed 标记、内容长度、关联话题）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id,version_no,kind,snapshot_name,created_at,created_by,source_task_id,topic_id,"
        "LENGTH(content) AS content_len FROM module_versions WHERE module_id=? AND stage=? ORDER BY version_no DESC",
        (mid, stage)).fetchall()
    conn.close()
    return {"ok": True, "module_id": mid, "stage": stage, "versions": [dict(r) for r in rows]}


@app.post("/api/cells/{mid}/{stage}/version")
async def cell_version_create(mid: str, stage: str, req: Request):
    """手动打快照（body: kind/content/source_task_id/project_id）。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    conn = get_db()
    n = _freeze_cell_version(conn, mid, stage, data.get("project_id") or "",
                             kind=data.get("kind") or "wip", content=data.get("content") or "",
                             source_task_id=data.get("source_task_id") or "", created_by="manual")
    conn.close()
    return {"ok": True, "version_no": n}


@app.get("/api/cells/{mid}/{stage}/versions/diff")
def cell_version_diff(mid: str, stage: str, a: str = "", b: str = ""):
    """两版本 content 行级 diff（unified_diff）。声明在 {vid} 路由之前，避免被 /versions/{vid} 抢匹配。"""
    import difflib
    conn = get_db()
    def _get(vid):
        r = conn.execute("SELECT content FROM module_versions WHERE id=?", (vid,)).fetchone()
        return (r["content"] if r else "") or ""
    ca, cb = _get(a), _get(b)
    conn.close()
    if not a or not b or a == b:
        return {"ok": True, "diff": [], "note": "需提供两个不同版本 id"}
    d = difflib.unified_diff(ca.splitlines(), cb.splitlines(),
                             fromfile=f"v({a})", tofile=f"v({b})", lineterm="")
    return {"ok": True, "diff": list(d)}


@app.get("/api/cells/{mid}/{stage}/versions/{vid}")
def cell_version_get(mid: str, stage: str, vid: str):
    """取某版本完整快照内容（预览）。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM module_versions WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "版本不存在"}
    return {"ok": True, "version": dict(row)}


@app.post("/api/cells/{mid}/{stage}/versions/{vid}/restore")
def cell_version_restore(mid: str, stage: str, vid: str):
    """回滚该格模块规约到指定版本，并生成 restored 新版本（保留可追溯链，旧基线永删）。"""
    conn = get_db()
    v = conn.execute("SELECT * FROM module_versions WHERE id=?", (vid,)).fetchone()
    if not v:
        conn.close()
        return {"ok": False, "error": "版本不存在"}
    if v["module_id"] != mid or v["stage"] != stage:
        conn.close()
        return {"ok": False, "error": "版本与格子不匹配"}
    conn.execute(
        "UPDATE modules SET name=?, desc=?, owner_role=?, status=? WHERE id=?",
        (v["snapshot_name"], v["snapshot_desc"], v["snapshot_owner"], v["snapshot_status"], mid))
    n = _freeze_cell_version(conn, mid, stage, v["project_id"], kind="restored",
                             content=v["content"] or "", source_task_id=v["source_task_id"] or "",
                             created_by="restore", parent=vid)
    conn.close()
    return {"ok": True, "restored_version_no": n, "module_id": mid, "stage": stage}


# ── v5.8「磨」：对话压缩 / 元神材料精炼 ──────────────────────────
# ── P0-B：磨 / TokenJuice 升级助手 ──────────────────────────────
GRIND_RULES_PATH = os.path.join(BASE, "..", "data", "grind_rules.json")


def _load_grind_rules() -> dict:
    """分层规则（内置 + 用户，JSON 免编译热加载）。对应 TokenJuice 三层 rules。"""
    builtin = {
        "drop_patterns": ["【略】", "（省略）", "同上", "如前述", "（接上）"],
        "redundant_marks": ["其实", "也就是说", "换句话说", "总的来说", "简而言之"],
    }
    rules = dict(builtin)
    try:
        if os.path.exists(GRIND_RULES_PATH):
            with open(GRIND_RULES_PATH, "r", encoding="utf-8") as f:
                custom = json.load(f)
            if isinstance(custom, dict):
                rules.update(custom)
    except Exception:
        pass
    return rules


def _est_tokens(s: str) -> int:
    """token 近似（CJK 按字 ≈1 token；其余字母数字每 3 个 ≈1 token）。用于省 token 量化。"""
    if not s:
        return 0
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿")
    other = sum(1 for ch in s if ch.isalnum() and not ("一" <= ch <= "鿿"))
    return cjk + max(1, other // 3)


GRIND_SYSTEM = """你是「磨」——一个语意保真压缩器（类 TokenJuice）。把你收到的一段或多段对话/资料「磨碎打散」：
- 完整保留：事实、决策与结论、用户原话的关键引用、文件路径/命令、绑定维度信号（利益/决策/情感/价值观/沟通）。
- 坚决删掉：寒暄、重复、口水、与结论无关的铺垫。
- 在「语意不丢失」前提下压缩到最短，能用短语不用长句。
- 【CJK / emoji 字形保真硬约束】所有中文、emoji、非 ASCII 字符必须原样保留（按字形，绝不删除、绝不 stripping、绝不转义）。只删冗余口水，不删任何字符内容。
- 输出纯压缩后的文本，不要解释、不要加「摘要：」之类前缀。"""

GRIND_SEGMENT_GUIDE = (
    "分段以话题边界为主：同一话题的连续内容归一段；话题/模块切换即切段；"
    "单话题过长（>30 轮）再按语义句群细分。每段独立可磨。"
)


def _grind_segment(text: str) -> str:
    """磨碎单段：保语意缩写（失败则原样返回，不丢信息）。"""
    if not text or len(text) < 40:
        return text  # 太短不磨
    try:
        hist = [{"role": "system", "content": GRIND_SYSTEM},
                {"role": "user", "content": "请磨碎下面这段：\n\n" + text}]
        out = call_llm(META_PID, hist, system_prompt=GRIND_SYSTEM)
        if out and not out.startswith("[元神·") and len(out.strip()) >= 10:
            return out.strip()
    except Exception:
        pass
    return text


def _grind_compress(segments: list) -> str:
    """把多段内容磨碎并拼接为压缩文本。segments: [{role, content}] 或 [str]。"""
    out = []
    for seg in segments:
        if isinstance(seg, dict):
            content = seg.get("content") or seg.get("text") or ""
        else:
            content = str(seg)
        if not content.strip():
            continue
        out.append(_grind_segment(content))
    return "\n\n".join(s for s in out if s)


def _auto_grind_project(pid: str, keep: int = 150) -> dict:
    """把一个项目最旧的、非材料的消息磨成一条压缩摘要（material=1），再删旧消息。返回 {deleted, ground}。"""
    try:
        conn = get_db()
        cutoff = conn.execute(
            "SELECT id FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
            (pid, keep - 1),
        ).fetchone()
        if not cutoff:
            conn.close()
            return {"deleted": 0, "ground": 0}
        old_rows = conn.execute(
            "SELECT id, sender, text FROM messages WHERE project_id=? AND id < ? AND material=0 ORDER BY id",
            (pid, cutoff[0]),
        ).fetchall()
        ground = 0
        if old_rows:
            segs = [{"role": (r["sender"] or "user"), "content": r["text"] or ""} for r in old_rows if (r["text"] or "").strip()]
            compressed = _grind_compress(segs) if segs else ""
            if compressed and len(compressed) >= 20:
                now = datetime.now().isoformat()
                db_write(
                    "INSERT INTO messages (project_id, sender, kind, text, tag, ts, material) VALUES (?,?,?,?,?,?,1)",
                    (pid, "元神·磨", "grind", "🪨 历史压缩摘要：\n" + compressed, "grind", now))
                ground += 1
        db_write("DELETE FROM messages WHERE project_id=? AND id < ? AND material=0", (pid, cutoff[0]))
        conn.close()
        return {"deleted": len(old_rows), "ground": ground}
    except Exception:
        return {"deleted": 0, "ground": 0}


@app.post("/api/grind")
async def grind(req: Request):
    """P0-B 手动磨：传入 segments（对话段落/资料），返回保语意压缩文本 + 省 token 量化。"""
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    segments = data.get("segments") or []
    if not isinstance(segments, list) or not segments:
        return JSONResponse({"ok": False, "error": "segments 必须是非空数组"}, status_code=400)
    # P0-B：分层规则预清理（drop 已知样板/冗余标记，免编译热加载）
    rules = _load_grind_rules()
    drop = rules.get("drop_patterns") or []
    cleaned = []
    for seg in segments:
        txt = seg if isinstance(seg, str) else (seg.get("content") or seg.get("text") or "")
        txt = str(txt)
        for p in drop:
            txt = txt.replace(p, "")
        cleaned.append(txt)
    original_text = "\n\n".join(cleaned)
    compressed = _grind_compress(cleaned)
    ot = _est_tokens(original_text)
    ct = _est_tokens(compressed)
    saved = round((1 - ct / ot) * 100, 1) if ot else 0.0
    return {"ok": True, "compressed": compressed,
            "original_tokens": ot, "compressed_tokens": ct, "saved_pct": saved,
            "original_len": len(original_text), "compressed_len": len(compressed)}


@app.get("/api/grind/rules")
def grind_rules_get():
    """P0-B：读取当前磨分层规则。"""
    return {"ok": True, "rules": _load_grind_rules()}


@app.post("/api/grind/rules")
async def grind_rules_set(req: Request):
    """P0-B：写入用户层磨规则（JSON 免编译热加载）。"""
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        with open(GRIND_RULES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/messages/cleanup")
async def messages_cleanup(req: Request):
    """磨的「人工批量删」：删除无效记录（delete_ids），其余（keep_ids）标记为元神材料。
    不传 keep_ids 则把所有未删记录都标为材料。"""
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    pid = data.get("project_id") or META_PID
    delete_ids = data.get("delete_ids") or []
    keep_ids = data.get("keep_ids") or []
    if not isinstance(delete_ids, list) or not isinstance(keep_ids, list):
        return JSONResponse({"ok": False, "error": "delete_ids/keep_ids 必须是数组"}, status_code=400)
    try:
        del_n = 0
        mat_n = 0
        if delete_ids:
            ph = ",".join("?" * len(delete_ids))
            del_n = db_write(
                f"DELETE FROM messages WHERE project_id=? AND id IN ({ph})",
                (pid, *delete_ids))
        if keep_ids:
            ph = ",".join("?" * len(keep_ids))
            mat_n = db_write(
                f"UPDATE messages SET material=1 WHERE project_id=? AND id IN ({ph}) AND material=0",
                (pid, *keep_ids))
        elif not delete_ids:
            # 未指定 keep_ids 且未删 → 把该项目全部现存消息标为材料
            mat_n = db_write(
                "UPDATE messages SET material=1 WHERE project_id=? AND material=0",
                (pid,))
        return {"ok": True, "deleted": del_n, "marked_material": mat_n}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── API：技能库（Phase 3 技能提炼机制）───────────────────────────
@app.get("/api/skills")
def list_skills():
    conn = get_db()
    rows = conn.execute("SELECT * FROM skills ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/skills")
async def create_skill(req: Request):
    data = await req.json()
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "技能名称不能为空"}
    conn = get_db()
    # v3：可选配件上限 ≤30（内置 19 + 用户自定义 ≤8，留余量）
    total = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    if total >= 30:
        conn.close()
        return {"ok": False, "error": f"技能配件已达上限（{total}/30，含内置），请先删除或停用不需要的技能"}
    exists = conn.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
    if exists:
        conn.close()
        return {"ok": False, "error": f"技能「{name}」已存在，请用编辑升级版本"}
    now = datetime.now().isoformat()
    steps = json.dumps(data.get("steps") or [], ensure_ascii=False)
    conn.execute(
        "INSERT INTO skills (name,category,description,trigger_words,steps,version,enabled,created_at,updated_at) VALUES (?,?,?,?,?,1,1,?,?)",
        (name, data.get("category", "general"), data.get("description", ""),
         data.get("trigger_words", ""), steps, now, now),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/skills/{sid}")
async def update_skill(sid: int, req: Request):
    """更新技能 = 升级版本（旧版本自动存档，可回滚）。"""
    data = await req.json()
    conn = get_db()
    row = conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "技能不存在"}
    # 存档旧版本
    old_data = json.dumps(dict(row), ensure_ascii=False)
    conn.execute(
        "INSERT INTO skill_versions (skill_id,version,data,created_at) VALUES (?,?,?,?)",
        (sid, row["version"], old_data, datetime.now().isoformat()),
    )
    new_version = row["version"] + 1
    steps = json.dumps(data.get("steps") if "steps" in data else json.loads(row["steps"]), ensure_ascii=False)
    conn.execute(
        "UPDATE skills SET name=?, category=?, description=?, trigger_words=?, steps=?, version=?, updated_at=? WHERE id=?",
        (data.get("name", row["name"]), data.get("category", row["category"]),
         data.get("description", row["description"]), data.get("trigger_words", row["trigger_words"]),
         steps, new_version, datetime.now().isoformat(), sid),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "version": new_version}


@app.delete("/api/skills/{sid}")
def delete_skill(sid: int):
    conn = get_db()
    conn.execute("DELETE FROM skill_versions WHERE skill_id=?", (sid,))
    conn.execute("DELETE FROM skills WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/skills/{sid}/toggle")
def toggle_skill(sid: int):
    conn = get_db()
    row = conn.execute("SELECT enabled FROM skills WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "技能不存在"}
    new = 0 if row["enabled"] else 1
    conn.execute("UPDATE skills SET enabled=? WHERE id=?", (new, sid))
    conn.commit()
    conn.close()
    return {"ok": True, "enabled": new}


@app.get("/api/skills/{sid}/versions")
def skill_versions(sid: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT id,version,created_at FROM skill_versions WHERE skill_id=? ORDER BY version DESC", (sid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/skills/{sid}/rollback/{ver}")
async def rollback_skill(sid: int, ver: int, req: Request):
    """回滚技能到指定历史版本（当前版本先存档）。"""
    data = await req.json()
    confirm = bool(data.get("confirm", False))
    conn = get_db()
    row = conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
    hist = conn.execute(
        "SELECT * FROM skill_versions WHERE skill_id=? AND version=?", (sid, ver)
    ).fetchone()
    if not row or not hist:
        conn.close()
        return {"ok": False, "error": "技能或历史版本不存在"}
    old = json.loads(hist["data"])
    if not confirm:
        conn.close()
        return {"ok": False, "need_confirm": True,
                "error": f"回滚将把技能「{row['name']}」从 v{row['version']} 恢复到 v{ver}（{old.get('name','')}）。确认后执行。"}
    # 当前版本存档
    conn.execute(
        "INSERT INTO skill_versions (skill_id,version,data,created_at) VALUES (?,?,?,?)",
        (sid, row["version"], json.dumps(dict(row), ensure_ascii=False), datetime.now().isoformat()),
    )
    conn.execute(
        "UPDATE skills SET name=?, category=?, description=?, trigger_words=?, steps=?, version=? WHERE id=?",
        (old.get("name", row["name"]), old.get("category", row["category"]),
         old.get("description", row["description"]), old.get("trigger_words", row["trigger_words"]),
         old.get("steps", row["steps"]), ver + 1, sid),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "version": ver + 1}


@app.post("/api/skills/distill")
async def distill_skills(req: Request):
    """从元神私聊最近的对话中识别可复用流程（LLM 抽取，带完整步骤）。"""
    rows = _recent_meta_texts(30)
    if not rows:
        return {"ok": True, "extracted": 0, "skills": [], "method": "empty",
                "note": "最近没有对话可供提炼。"}
    convo = "\n".join(f"{r['sender']}: {r['text']}" for r in rows if r.get("text"))
    items, method = await _llm_extract(SKILL_DISTILL_SYSTEM, f"以下是最近的对话记录：\n\n{convo}")
    if items is None:
        # 审查 #13：旧版在这里取原句前 16 个字当技能名、steps 恒为 []，
        # 存出来的"技能"既不可读也不可执行。现在宁可不产出，也不造垃圾数据。
        return {"ok": True, "extracted": 0, "skills": [], "method": "unavailable",
                "note": f"未能提炼：{method}。技能需要完整步骤，缺步骤的条目不入库。"}

    conn = get_db()
    existing = {r[0] for r in conn.execute("SELECT name FROM skills").fetchall()}
    created = []
    for it in items[:5]:
        name = (it.get("name") or "").strip()[:40]
        steps = it.get("steps") or []
        if not name or name in existing or not isinstance(steps, list) or len(steps) < 2:
            continue  # 没名字、重名、步骤不足 2 步的一律不收
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO skills (name,category,description,trigger_words,steps,version,enabled,created_at,updated_at) "
            "VALUES (?,?,?,?,?,1,0,?,?)",
            (name, "auto", (it.get("description") or "").strip()[:200],
             (it.get("trigger_words") or "").strip()[:100],
             json.dumps([str(s)[:120] for s in steps], ensure_ascii=False), now, now),
        )
        existing.add(name)
        created.append({"name": name, "steps": len(steps)})
    conn.commit()
    conn.close()
    return {"ok": True, "extracted": len(created), "skills": created, "method": method}


# ── API：经验库（成功/失败案例归档，Phase 4）────────────────────
@app.get("/api/experiences")
def list_experiences(category: str = ""):
    conn = get_db()
    if category and category in ("success", "failure"):
        rows = conn.execute("SELECT * FROM experiences WHERE category=? ORDER BY id DESC", (category,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM experiences ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/experiences")
async def create_experience(req: Request):
    data = await req.json()
    scenario = (data.get("scenario") or "").strip()
    if not scenario:
        return {"ok": False, "error": "场景不能为空"}
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO experiences (category,scenario,goal,attempts,outcome,lesson,project_id,source,ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (data.get("category", "success"), scenario, data.get("goal", ""), data.get("attempts", ""),
         data.get("outcome", ""), data.get("lesson", ""), data.get("project_id", ""),
         data.get("source", "manual"), datetime.now().isoformat()),
    )
    _refresh_experience_weights(conn, cur.lastrowid)  # 疗效归因：新经验初始化 5 维权重
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/experiences/{eid}")
async def update_experience(eid: int, req: Request):
    data = await req.json()
    conn = get_db()
    conn.execute(
        "UPDATE experiences SET category=?, scenario=?, goal=?, attempts=?, outcome=?, lesson=? WHERE id=?",
        (data.get("category", "success"), data.get("scenario", ""), data.get("goal", ""),
         data.get("attempts", ""), data.get("outcome", ""), data.get("lesson", ""), eid),
    )
    _refresh_experience_weights(conn, eid)  # 疗效归因：outcome 变化重算权重
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/experiences/{eid}")
def delete_experience(eid: int):
    conn = get_db()
    conn.execute("DELETE FROM experiences WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ───────────────────────────────────────────────────────────────
# 疗效归因（类脑自进化第⑤环）
# 信号 → 5 维权重（relevance/recency/frequency/explicit_feedback/trust_score，EMA 平滑）
#      → 综合 weight 驱动召回优先级 → 累积/淘汰 → 聚合喂回成员自动升级
# 设计真源：桌面元神/EXPERIENCES.md + WORKFLOW「流程六」EchoMind RL 蓝图
# ───────────────── Privat ────────────────
_ATTR_W = {"relevance": 0.25, "recency": 0.20, "frequency": 0.15,
           "explicit_feedback": 0.15, "trust_score": 0.25}
_LAST_ATTR_MAINTAIN = 0.0  # 模块级：上次全量维护时间戳，避免召回时频繁重算


def _refresh_experience_weights(conn, eid):
    """按当前存储信号重算单条经验的 5 维权重 + 综合 weight。
    trust_score / explicit_feedback 以存储的 EMA 累计值为准（不被 outcome 文本覆盖），
    保证 _record_experience_use 的增量学习可持续；relevance/recency/frequency 由信号重算（幂等）。
    """
    row = conn.execute(
        "SELECT outcome,last_used,ts,frequency,trust_score,explicit_feedback,relevance "
        "FROM experiences WHERE id=?", (eid,)).fetchone()
    if not row:
        return
    d = dict(row)
    now = datetime.now()
    anchor = d.get("last_used") or d.get("ts") or now.isoformat()
    try:
        dt = datetime.fromisoformat(anchor)
    except Exception:
        dt = now
    age_days = max(0, (now - dt).total_seconds() / 86400.0)
    recency = round(max(0.0, 1.0 - age_days / 30.0), 3)  # 30 天半衰
    outcome = (d.get("outcome") or "").lower()
    pos = any(k in outcome for k in ("成功", "达标", "完成", "有效", "解决", "✅", "ok", "pass", "win"))
    neg = any(k in outcome for k in ("失败", "踩坑", "报错", "无效", "超时", "❌", "fail", "error", "stuck"))
    if pos and not neg:
        rel = 0.8
    elif neg and not pos:
        rel = 0.2
    else:
        rel = round(d.get("relevance") or 0.5, 3)
    freq_raw = d.get("frequency") or 0          # 原始复用计数（勿覆盖）
    freq = min(1.0, freq_raw / 10.0)            # 归一化仅用于权重
    trust = d.get("trust_score") if d.get("trust_score") is not None else 0.5
    ef = d.get("explicit_feedback") if d.get("explicit_feedback") is not None else 0.0
    weight = (_ATTR_W["relevance"] * rel + _ATTR_W["recency"] * recency +
              _ATTR_W["frequency"] * freq + _ATTR_W["explicit_feedback"] * max(0.0, ef) +
              _ATTR_W["trust_score"] * trust)
    weight = round(max(0.0, min(1.0, weight)), 3)
    # 注意：frequency 为原始复用次数，由 _record_experience_use 维护，此处只重算权重不回写
    conn.execute(
        "UPDATE experiences SET relevance=?,recency=?,explicit_feedback=?,trust_score=?,weight=? WHERE id=?",
        (round(rel, 3), recency, round(ef, 3), round(trust, 3), weight, eid))


def _record_experience_use(eid, feedback=None):
    """经验被召回/使用：frequency+1、last_used=now（EMA 增量）；可带一次显式反馈调整 trust/ef。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT frequency,last_used,trust_score,explicit_feedback,neg_streak,persistent,eliminated,lesson,snippet,outcome "
            "FROM experiences WHERE id=?", (eid,)).fetchone()
        if not row:
            return
        d = dict(row)
        freq = (d.get("frequency") or 0) + 1
        last = datetime.now().isoformat()
        trust = d.get("trust_score") if d.get("trust_score") is not None else 0.5
        ef = d.get("explicit_feedback") if d.get("explicit_feedback") is not None else 0.0
        neg = d.get("neg_streak") or 0
        persistent = d.get("persistent") or 0
        if feedback == "positive" or feedback is True:
            trust = round(min(1.0, trust + 0.1), 3)
            ef = round(min(1.0, ef + 0.2), 3)
            neg = 0
        elif feedback == "negative" or feedback is False:
            trust = round(max(0.0, trust - 0.1), 3)
            ef = round(max(-1.0, ef - 0.2), 3)
            neg += 1
        # E4：held-out 晋升（受 evolution_heldout_enabled 控制；关→保持 D9 既有 freq>=10 宽松）。
        # E8：晋升前护栏扫描——含违禁模式不晋升（unsafe 已由写入时标记，双重保险）。
        _lesson_text = (d.get("lesson") or "") + " " + (d.get("snippet") or "") + " " + (d.get("outcome") or "")
        _blocked = _evolution_guardrail_enabled() and _experience_contains_blocked(_lesson_text)
        if (ef >= 3.0) or (freq >= _heldout_promote_uses()):
            if not _blocked:
                persistent = 1
        # E4：连续负反馈→淘汰（阈值随 held-out 开关一致）
        eliminated = 1 if neg >= _heldout_demote_neg() else (d.get("eliminated") or 0)
        conn.execute(
            "UPDATE experiences SET frequency=?,last_used=?,trust_score=?,explicit_feedback=?,neg_streak=?,persistent=?,eliminated=? WHERE id=?",
            (freq, last, trust, ef, neg, persistent, eliminated, eid))
        _refresh_experience_weights(conn, eid)
        conn.commit()
    finally:
        conn.close()


def _maintain_attribution():
    """定时维护：recency 衰减、累积→持久、连续负反馈/断连→淘汰、聚合到 skill_attribution。"""
    global _LAST_ATTR_MAINTAIN
    conn = get_db()
    try:
        now = datetime.now()
        rows = conn.execute(
            "SELECT id,category,last_used,ts,neg_streak,persistent,eliminated,frequency,explicit_feedback "
            "FROM experiences WHERE eliminated=0").fetchall()
        for r in rows:
            d = dict(r)
            anchor = d["last_used"] or d["ts"] or now.isoformat()
            try:
                dt = datetime.fromisoformat(anchor)
            except Exception:
                dt = now
            age_days = (now - dt).total_seconds() / 86400.0
            neg = d["neg_streak"] or 0
            ef = d["explicit_feedback"] or 0.0
            # E4：淘汰（阈值随 held-out 开关一致）：连续负反馈达阈值 或 30 天未引（学会遗忘该忘记的事）
            if (neg >= _heldout_demote_neg()) or (age_days > 30):
                conn.execute("UPDATE experiences SET eliminated=1 WHERE id=?", (d["id"],))
                continue
            # E4：累积→持久（阈值随 held-out 开关一致）：显式正反馈累计≥3 或 被复用达阈值次（升持久）
            if (ef >= 3.0) or ((d["frequency"] or 0) >= _heldout_promote_uses()):
                conn.execute("UPDATE experiences SET persistent=1 WHERE id=?", (d["id"],))
            _refresh_experience_weights(conn, d["id"])
        # 聚合到 skill_attribution（按 category 维度）
        conn.execute("DELETE FROM skill_attribution")
        for cat in ("success", "failure"):
            sub = conn.execute(
                "SELECT AVG(weight) w, AVG(trust_score) t, AVG(relevance) rel, AVG(recency) rec, "
                "AVG(frequency) fr, AVG(explicit_feedback) ef, COUNT(*) n "
                "FROM experiences WHERE category=? AND eliminated=0", (cat,)).fetchone()
            if sub and sub["n"]:
                conn.execute(
                    "INSERT INTO skill_attribution "
                    "(key,name,kind,trust_score,relevance,recency,frequency,explicit_feedback,weight,samples,last_signal,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("cat:" + cat, ("成功经验" if cat == "success" else "失败教训"), "category",
                     round(sub["t"] or 0.5, 3), round(sub["rel"] or 0.5, 3), round(sub["rec"] or 0.5, 3),
                     round(min(1.0, (sub["fr"] or 0) / 10.0), 3), round(sub["ef"] or 0.0, 3),
                     round(sub["w"] or 0.5, 3), sub["n"], 1, now.isoformat()))
        conn.commit()
        _LAST_ATTR_MAINTAIN = time.time()
    finally:
        conn.close()


def _recall_experiences(scenario: str, limit: int = 5, project_id: str = None, module_id: str = None, stage: str = None):
    """特征驱动召回：按综合 weight 降序（非淘汰项）；场景关键词重叠额外加权 relevance。
    超过 1 小时未全量维护则顺带跑一次 _maintain_attribution，保证 weight 不过期。"""
    global _LAST_ATTR_MAINTAIN
    if time.time() - _LAST_ATTR_MAINTAIN > 3600:
        try:
            _maintain_attribution()
        except Exception:
            pass
    conn = get_db()
    try:
        _rec_where = "WHERE eliminated=0"
        if _evolution_guardrail_enabled():
            # E8：护栏开启→含违禁模式的经验（unsafe=1）不主动召回，仅留作草稿供人工审
            _rec_where += " AND unsafe=0"
        rows = conn.execute(
            f"SELECT * FROM experiences {_rec_where} ORDER BY weight DESC LIMIT ?",
            (limit * 3,)).fetchall()
        scored = []
        kw = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", scenario or ""))
        for r in rows:
            d = dict(r)
            rel = d.get("relevance") or 0.5
            if kw:
                sce = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", (d.get("scenario", "") + d.get("goal", ""))))
                ov = len(kw & sce) / max(1, len(kw))
                rel = max(rel, min(1.0, rel + 0.4 * ov))
            # E2：同项目经验加权（evolution_recall_enabled 开启时），让 (模块,阶段) 格召回更贴合
            if _evolution_recall_enabled() and project_id and d.get("project_id") == project_id:
                rel = min(1.0, rel + 0.15)
            score = (d.get("weight") or 0.5) * 0.7 + rel * 0.3
            d["_score"] = round(score, 3)
            d["_relevance_boost"] = round(rel, 3)
            scored.append(d)
        scored.sort(key=lambda x: x["_score"], reverse=True)
        return scored[:limit]
    finally:
        conn.close()


@app.get("/api/experiences/recall")
def experience_recall(q: str = "", limit: int = 5):
    """特征驱动召回经验（疗效归因闭环：召回→标记使用→权重演化）。"""
    items = _recall_experiences(q, limit)
    return {"ok": True, "query": q, "count": len(items), "items": items}


@app.get("/api/meta/evolution/cell")
def evolution_cell(module_id: str = "", stage: str = "", project_id: str = ""):
    """E2：看板即技能索引（只读展示）——返回该 (模块,阶段,项目) 格已沉淀经验数 + top3。
    开关关闭返回 403（不暴露内部经验热力）。"""
    if not _evolution_recall_enabled():
        return JSONResponse(status_code=403, content={"ok": False, "error": "evolution_recall 未开启"})
    try:
        items = _recall_experiences(f"{module_id} {stage}", limit=3,
                                    project_id=project_id or None)
        conn = get_db()
        try:
            if project_id:
                c = conn.execute("SELECT COUNT(*) FROM experiences WHERE project_id=? AND eliminated=0",
                                 (project_id,)).fetchone()
            else:
                c = conn.execute("SELECT COUNT(*) FROM experiences WHERE eliminated=0").fetchone()
            cnt = c[0] if c else 0
        finally:
            conn.close()
        return {"ok": True, "module_id": module_id, "stage": stage, "project_id": project_id,
                "count": cnt, "top3": items}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/meta/evolution/promote")
def evolution_promote(eid: int = 0, act: str = ""):
    """E4：held-out 晋升观察/手动触发（受 evolution_heldout_enabled 门控；关→403）。
    act=run 模拟一次成功应用（等效 experience_signal kind=positive），推进 freq/ef 直到晋升。"""
    if not _evolution_heldout_enabled():
        return JSONResponse(status_code=403, content={"ok": False, "error": "evolution_heldout 未开启"})
    try:
        if act == "run" and eid:
            _record_experience_use(eid, "positive")
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id,category,scenario,frequency,neg_streak,persistent,eliminated,weight,trust_score,unsafe "
                "FROM experiences WHERE id=?", (eid,)).fetchone()
        finally:
            conn.close()
        if not row:
            return {"ok": False, "error": "经验不存在"}
        return {"ok": True, "eid": eid, "state": dict(row),
                "promote_uses": _heldout_promote_uses(), "demote_neg": _heldout_demote_neg(),
                "note": "freq 达 promote_uses 且 neg_streak<demote_neg 且非 unsafe → persistent=1"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/meta/evolution/lineage")
def evolution_lineage(eid: int = 0, scenario: str = ""):
    """E5：lineage 可追溯（受 evolution_lineage_enabled 门控；关→403）。
    返回经验的来源任务/验收结果/摘要/版本指纹，支撑回滚与可信度审计。"""
    if not _evolution_lineage_enabled():
        return JSONResponse(status_code=403, content={"ok": False, "error": "evolution_lineage 未开启"})
    try:
        conn = get_db()
        try:
            if eid:
                row = conn.execute(
                    "SELECT id,category,scenario,source_task_id,acceptance_result,snippet,version_fingerprint,ts,persistent,eliminated,unsafe "
                    "FROM experiences WHERE id=?", (eid,)).fetchone()
            elif scenario:
                row = conn.execute(
                    "SELECT id,category,scenario,source_task_id,acceptance_result,snippet,version_fingerprint,ts,persistent,eliminated,unsafe "
                    "FROM experiences WHERE scenario LIKE ? ORDER BY ts DESC LIMIT 1", ("%" + scenario + "%",)).fetchone()
            else:
                return {"ok": False, "error": "需 eid 或 scenario"}
        finally:
            conn.close()
        if not row:
            return {"ok": False, "error": "未找到经验"}
        return {"ok": True, "lineage": dict(row)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/meta/memory/archive")
def memory_archive(project_id: str = "", session_id: str = ""):
    """E6：无损会话归档视图（受 memory_archive_enabled 门控；关→403）。
    返回节点树：全历史保留 + 摘要节点 + 统计（总节点/已归档/摘要/估算 token）。"""
    if not _memory_archive_enabled():
        return JSONResponse(status_code=403, content={"ok": False, "error": "memory_archive 未开启"})
    pid = project_id or META_PID
    try:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT id,parent_id,role,kind,content,token_est,is_summary,summary_of,archived,created_at "
                "FROM session_nodes WHERE project_id=? AND session_id=? ORDER BY id ASC",
                (pid, session_id)).fetchall()
        finally:
            conn.close()
        nodes = [dict(r) for r in rows]
        for n in nodes:
            if n["summary_of"]:
                n["summary_refs"] = [int(x) for x in n["summary_of"].split(",") if x]
            n["content_preview"] = (n["content"] or "")[:120]
        return {"ok": True, "project_id": pid, "session_id": session_id,
                "total": len(nodes),
                "archived": sum(1 for n in nodes if n["archived"]),
                "summaries": sum(1 for n in nodes if n["is_summary"]),
                "token_est": sum(n["token_est"] for n in nodes),
                "nodes": nodes,
                "note": "原节点 archived=1 仍保留全历史；summary_of 指向被摘要的原节点 id（无损）"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/meta/memory/panel")
def memory_panel(project_id: str = ""):
    """E7：元神记忆面板聚合（受 memory_panel_enabled 门控；关→403）。
    透出：经验库总量 / 分类分布 / 最近沉淀(含来源验收标注) / persistent vs 草稿 vs 护栏拦截。"""
    if not _memory_panel_enabled():
        return JSONResponse(status_code=403, content={"ok": False, "error": "memory_panel 未开启"})
    try:
        conn = get_db()
        try:
            base = "FROM experiences WHERE 1=1"
            params = []
            if project_id:
                base += " AND project_id=?"
                params.append(project_id)
            total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
            by_cat = {}
            for r in conn.execute(f"SELECT category,COUNT(*) c {base} GROUP BY category", params).fetchall():
                by_cat[r["category"]] = r["c"]
            persistent = conn.execute(f"SELECT COUNT(*) {base} AND persistent=1", params).fetchone()[0]
            drafts = conn.execute(f"SELECT COUNT(*) {base} AND persistent=0 AND eliminated=0", params).fetchone()[0]
            unsafe = conn.execute(f"SELECT COUNT(*) {base} AND unsafe=1", params).fetchone()[0]
            eliminated = conn.execute(f"SELECT COUNT(*) {base} AND eliminated=1", params).fetchone()[0]
            recent = [dict(r) for r in conn.execute(
                f"SELECT id,category,scenario,acceptance_result,version_fingerprint,trust_score,persistent,unsafe,ts "
                f"{base} ORDER BY ts DESC LIMIT 12", params).fetchall()]
        finally:
            conn.close()
        return {"ok": True, "project_id": project_id,
                "total": total, "by_category": by_cat,
                "persistent": persistent, "drafts": drafts, "unsafe": unsafe, "eliminated": eliminated,
                "recent": recent}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/experiences/{eid}/signal")
async def experience_signal(eid: int, req: Request):
    """给一条经验打信号：use（被复用）/ positive / negative / outcome（更新结果文本并重算）。"""
    data = await req.json()
    kind = (data.get("kind") or "use")
    if kind == "use":
        _record_experience_use(eid)
    elif kind in ("positive", "negative"):
        _record_experience_use(eid, kind)
    elif kind == "outcome":
        conn = get_db()
        conn.execute("UPDATE experiences SET outcome=?,last_used=? WHERE id=?",
                     (str(data.get("outcome", ""))[:500], datetime.now().isoformat(), eid))
        conn.commit()
        conn.close()
        _record_experience_use(eid)
    else:
        return {"ok": False, "error": "未知信号类型（use/positive/negative/outcome）"}
    return {"ok": True}


@app.get("/api/meta/attribution")
def meta_attribution():
    """疗效归因聚合视图（按类别的权重/信任度），供元神驾驶舱与成员升级参考。"""
    try:
        _maintain_attribution()
    except Exception:
        pass
    conn = get_db()
    rows = conn.execute("SELECT * FROM skill_attribution ORDER BY weight DESC").fetchall()
    conn.close()
    return {"ok": True, "attribution": [dict(r) for r in rows]}


@app.post("/api/meta/attribution/refresh")
def meta_attribution_refresh():
    _maintain_attribution()
    return {"ok": True}


EXPERIENCE_DISTILL_SYSTEM = """你是复盘提炼器。从对话中找出「做过并且有结果的事」，输出 JSON 数组。

只提炼真的发生过、且能看出结果的事情。计划、设想、还没做的，一律忽略。

每条包含：
- category：success（做成了）或 failure（失败或踩坑）
- scenario：当时在做什么，一句话，不超过 30 字
- goal：想达成什么
- attempts：怎么做的，关键动作
- outcome：结果如何
- lesson：下次遇到同类情况该怎么办。这一条最重要，必须是可复用的判断，不能是"要仔细"这种废话

没有就返回空数组 []。

输出格式（只输出 JSON，不要任何解释）：
[{"category":"failure","scenario":"...","goal":"...","attempts":"...","outcome":"...","lesson":"..."},...]"""


@app.post("/api/experiences/distill")
async def distill_experiences(req: Request):
    """从元神私聊最近的对话中提炼成功/失败案例（LLM 抽取）。
    评测 P2-1/P2-2（2026-08-17）：支持显式 text 入参（优先于私聊历史）；
    支持 force=true 强制记录——无可提炼时把原文原样存为一条 note 经验，避免「想记的记不下来」。"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    text_in = (data.get("text") or "").strip()
    force = bool(data.get("force"))
    if text_in:
        convo = text_in
    else:
        rows = _recent_meta_texts(30)
        if not rows:
            return {"ok": True, "extracted": 0, "items": [], "method": "empty",
                    "note": "最近没有对话可供提炼；可传入 text 直接提交一段文本提炼。"}
        convo = "\n".join(f"{r['sender']}: {r['text']}" for r in rows if r.get("text"))
    items, method = await _llm_extract(EXPERIENCE_DISTILL_SYSTEM, f"以下是对话/文本记录：\n\n{convo}")
    if items is None:
        # 旧版在这里把命中"失败/成功"关键词的原句整条塞进 lesson 字段，
        # 存出来的"经验"就是一句聊天记录，复用价值为零。宁可空手而归。
        return {"ok": True, "extracted": 0, "items": [], "method": "unavailable",
                "note": f"未能提炼：{method}。经验必须带可复用的教训，凑数的不入库。"}

    conn = get_db()
    existing = {r[0] for r in conn.execute("SELECT scenario FROM experiences").fetchall()}
    created = []
    for it in items[:5]:
        scenario = (it.get("scenario") or "").strip()[:60]
        lesson = (it.get("lesson") or "").strip()
        if not scenario or scenario in existing or len(lesson) < 6:
            continue  # 没有教训的不算经验
        category = "failure" if it.get("category") == "failure" else "success"
        cur = conn.execute(
            "INSERT INTO experiences (category,scenario,goal,attempts,outcome,lesson,project_id,source,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (category, scenario, (it.get("goal") or "")[:200], (it.get("attempts") or "")[:500],
             (it.get("outcome") or "")[:300], lesson[:500], META_PID, f"auto·{method}",
             datetime.now().isoformat()),
        )
        _refresh_experience_weights(conn, cur.lastrowid)  # 疗效归因：蒸馏新经验初始化权重
        existing.add(scenario)
        created.append({"scenario": scenario, "category": category})
    # force：无可提炼但用户明确要求记录 → 原文原样存为 note 经验（如实标注来源）
    if not created and force and len(convo) >= 6:
        scenario = convo.strip()[:30]
        cur = conn.execute(
            "INSERT INTO experiences (category,scenario,goal,attempts,outcome,lesson,project_id,source,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("note", scenario, "", "", "", convo.strip()[:500], META_PID, "manual·force",
             datetime.now().isoformat()),
        )
        _refresh_experience_weights(conn, cur.lastrowid)
        created.append({"scenario": scenario, "category": "note", "forced": True})
    conn.commit()
    conn.close()
    note = ""
    if not created:
        note = ("未提炼出带可复用教训的经验；如需原样记录这段内容，请带 force=true 重新提交。"
                if text_in else "最近对话未提炼出带可复用教训的经验；可传入 text + force 直接记录。")
    return {"ok": True, "extracted": len(created), "items": created, "method": method, "note": note}


# ── P0-A：项目群聊自动蒸馏（记忆树原料）────────────────────────
async def _auto_distill_project_chat(pid: str, limit: int = 14):
    """项目群聊自动蒸馏：取最近项目消息（非话题），LLM 抽取有教训的经验 → experiences(project_id=pid, source='chat')。
    复用 distill_experiences 的抽取链（_llm_extract + EXPERIENCE_DISTILL_SYSTEM + _refresh_experience_weights）。
    节流由调用方控制（每 N 条消息触发一次），此处只做单次抽取。分身护城河：自有群聊即最丰富蒸馏语料，零 OAuth 成本。"""
    if not pid or pid == META_PID:
        return 0
    conn = get_db()
    rows = conn.execute(
        "SELECT sender,kind,text FROM messages WHERE project_id=? AND (topic_id IS NULL OR topic_id='') "
        "ORDER BY id DESC LIMIT ?", (pid, limit)).fetchall()
    convo = "\n".join(f"{r['sender']}: {r['text']}" for r in reversed(rows) if (r['text'] or '').strip())
    conn.close()
    if not convo.strip():
        return 0
    items, method = await _llm_extract(EXPERIENCE_DISTILL_SYSTEM,
                                       f"以下是项目群聊记录（项目 {pid}）：\n\n{convo}")
    if items is None:
        return 0
    conn = get_db()
    existing = {r[0] for r in conn.execute(
        "SELECT scenario FROM experiences WHERE project_id=?", (pid,)).fetchall()}
    created = 0
    for it in items[:5]:
        scenario = (it.get("scenario") or "").strip()[:60]
        lesson = (it.get("lesson") or "").strip()
        if not scenario or scenario in existing or len(lesson) < 6:
            continue  # 没有教训的不算经验（保持"凑数不入"原则）
        category = "failure" if it.get("category") == "failure" else "success"
        cur = conn.execute(
            "INSERT INTO experiences (category,scenario,goal,attempts,outcome,lesson,project_id,source,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (category, scenario, (it.get("goal") or "")[:200], (it.get("attempts") or "")[:500],
             (it.get("outcome") or "")[:300], lesson[:500], pid, f"chat·{method}",
             datetime.now().isoformat()))
        _refresh_experience_weights(conn, cur.lastrowid)  # 疗效归因：初始化权重
        existing.add(scenario)
        created += 1
    conn.commit()
    conn.close()
    return created


@app.get("/api/experiences/tree")
def experiences_tree(project_id: str = ""):
    """P0-A 经验树：按 源(project_id)→分类→时间 分层分组（walk/drill 检索）。
    纯查询层，无需新表；project_id 为空则返回全部。"""
    conn = get_db()
    if project_id:
        rows = conn.execute(
            "SELECT id,category,scenario,lesson,project_id,ts,weight FROM experiences WHERE project_id=? ORDER BY ts DESC",
            (project_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,category,scenario,lesson,project_id,ts,weight FROM experiences ORDER BY ts DESC").fetchall()
    conn.close()
    tree = {}
    for r in rows:
        src = r["project_id"] or "全局"
        cat = r["category"]
        tree.setdefault(src, {}).setdefault(cat, []).append({
            "id": r["id"], "scenario": r["scenario"], "lesson": r["lesson"],
            "ts": (r["ts"] or "")[:10], "weight": round(r["weight"] or 0.5, 2)})
    return {"ok": True, "tree": tree}


# ── API：进化引擎（复盘 → 确认 → 固化，Phase 4）─────────────────
@app.get("/api/reviews")
def list_reviews(status: str = ""):
    conn = get_db()
    if status and status in ("pending", "accepted", "rejected"):
        rows = conn.execute("SELECT * FROM reviews WHERE status=? ORDER BY id DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/reviews")
async def create_review(req: Request):
    data = await req.json()
    summary = (data.get("summary") or "").strip()
    if not summary:
        return {"ok": False, "error": "复盘内容不能为空"}
    conn = get_db()
    conn.execute(
        "INSERT INTO reviews (project_id,summary,efficient,stuck,reusable,status,created_at) VALUES (?,?,?,?,?,?,?)",
        (data.get("project_id", ""), summary, data.get("efficient", ""), data.get("stuck", ""),
         data.get("reusable", ""), "pending", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/reviews/auto")
async def auto_review(req: Request):
    """自动复盘：从最近的项目消息中生成复盘草稿（待确认）。"""
    conn = get_db()
    # 找最近有消息的项目
    row = conn.execute(
        "SELECT project_id, COUNT(*) c FROM messages WHERE project_id != ? GROUP BY project_id ORDER BY MAX(id) DESC LIMIT 1",
        (META_PID,),
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "还没有项目消息，无法复盘"}
    pid = row["project_id"]
    msgs = conn.execute(
        "SELECT sender,kind,text FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 15", (pid,)
    ).fetchall()
    conn.close()
    # 启发式生成复盘：提取自我消息（用户指令）与 agent 消息（执行/完成）
    user_asks = [m["text"] for m in msgs if m["kind"] == "self"]
    agent_acts = [m["text"] for m in msgs if m["kind"] in ("agent", "meta", "sys")]
    summary = f"项目近期完成的工作：{len(user_asks)} 次用户指令，{len(agent_acts)} 条执行反馈。"
    stuck = next((m["text"] for m in msgs if any(k in m["text"] for k in ["卡住", "阻塞", "失败", "报错"])), "")
    done = [m["text"] for m in msgs if any(k in m["text"] for k in ["完成", "搞定", "成功"])]
    efficient = done[-1] if done else ""
    reusable = f"本项目的执行流程已沉淀为可复用经验：用户指令 {len(user_asks)} 条、执行反馈 {len(agent_acts)} 条。"
    conn = get_db()
    conn.execute(
        "INSERT INTO reviews (project_id,summary,efficient,stuck,reusable,status,created_at) VALUES (?,?,?,?,?,?,?)",
        (pid, summary, efficient, stuck, reusable, "pending", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "project_id": pid, "summary": summary}


@app.post("/api/reviews/{rid}/accept")
async def accept_review(rid: int, req: Request):
    """接受复盘：自动固化（可复用点→技能草稿 + 教训→经验库），完成进化闭环。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "复盘不存在"}
    conn.execute(
        "UPDATE reviews SET status='accepted', decided_at=? WHERE id=?",
        (datetime.now().isoformat(), rid),
    )
    # 固化：可复用点 → 技能草稿（disabled）
    now = datetime.now().isoformat()
    if row["reusable"]:
        name = row["reusable"][:16].rstrip("，。,.")
        exists = conn.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO skills (name,category,description,trigger_words,steps,version,enabled,created_at,updated_at) VALUES (?,?,?,?,?,1,0,?,?)",
                (name, "review", row["reusable"], "", "[]", now, now),
            )
    # 固化：教训 → 经验库（若有 stuck）
    if row["stuck"]:
        conn.execute(
            "INSERT INTO experiences (category,scenario,goal,attempts,outcome,lesson,project_id,source,ts) VALUES (?,?,?,?,?,?,?,?,?)",
            ("failure", row["stuck"][:30], row["summary"], "", "", row["stuck"], row["project_id"], "review", now),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "status": "accepted"}


@app.post("/api/reviews/{rid}/reject")
async def reject_review(rid: int, req: Request):
    data = await req.json()
    reason = (data.get("reason") or "").strip()
    conn = get_db()
    row = conn.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "复盘不存在"}
    conn.execute(
        "UPDATE reviews SET status='rejected', reject_reason=?, decided_at=? WHERE id=?",
        (reason, datetime.now().isoformat(), rid),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "status": "rejected"}


# ── API：元神总看板（v3.5 元神觉醒 · 聚合 + 巡检）────────────────
def _patrol_rules(conn, pid: str) -> dict:
    """单个项目的巡检结果：阻塞 / 滞留 / 超时 / 审核积压。"""
    issues = []
    mods = conn.execute(
        "SELECT * FROM modules WHERE project_id=?", (pid,)
    ).fetchall()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE project_id=?", (pid,)
    ).fetchall()
    task_map = {t["id"]: dict(t) for t in tasks}
    mod_map = {m["id"]: dict(m) for m in mods}
    # 1) 依赖未完成 → 依赖方滞留（red）
    for m in mods:
        deps = json.loads(m["depends_on"] or "[]")
        if not deps:
            continue
        for d in deps:
            dm = mod_map.get(d)
            if dm and dm["status"] != "done" and m["status"] in ("doing", "todo"):
                issues.append({
                    "level": "red", "project": pid, "module": m["name"], "task": "",
                    "type": "依赖阻塞",
                    "detail": f"模块「{m['name']}」依赖「{dm['name']}」未完成",
                })
    # 2) 任务滞留超时：doing 超过 24h（按 created_at 粗估）或 todo 积压 >= 5
    for t in tasks:
        if t["status"] == "doing":
            issues.append({
                "level": "amber", "project": pid, "module": mod_map.get(t["module_id"], {}).get("name", ""),
                "task": t["name"], "type": "进行中", "detail": f"任务「{t['name']}」在进行中，留意进度",
            })
    doing_count = sum(1 for t in tasks if t["status"] == "doing")
    todo_count = sum(1 for t in tasks if t["status"] == "todo")
    review_count = sum(1 for t in tasks if t["status"] == "review")
    if review_count >= 2:
        issues.append({
            "level": "amber", "project": pid, "module": "", "task": "",
            "type": "审核积压", "detail": f"有 {review_count} 个任务待审核",
        })
    return {
        "project": pid, "module_count": len(mods), "task_count": len(tasks),
        "doing": doing_count, "todo": todo_count, "review": review_count,
        "done": sum(1 for t in tasks if t["status"] == "done"),
        "issues": issues,
    }


@app.get("/api/meta/overview")
def meta_overview():
    """元神总看板：所有项目 × 模块 × 任务的聚合统计 + 问题清单。"""
    conn = get_db()
    projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    out = []
    all_issues = []
    for p in projects:
        if p["id"] == META_PID:
            continue
        r = _patrol_rules(conn, p["id"])
        r["id"] = p["id"]
        r["name"] = p["name"]
        r["status"] = p["status"]
        r["phase"] = p["phase"]
        out.append(r)
        all_issues.extend(r["issues"])
    conn.close()
    return {
        "projects": out,
        "issues": sorted(all_issues, key=lambda x: 0 if x["level"] == "red" else 1),
        "total_projects": len(out),
        "total_tasks": sum(r["task_count"] for r in out),
    }


@app.get("/api/meta/patrol")
def meta_patrol():
    """自动巡检：只返回需要关注的问题清单（供元神汇报/前端轮询）。"""
    conn = get_db()
    projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    issues = []
    for p in projects:
        if p["id"] == META_PID:
            continue
        r = _patrol_rules(conn, p["id"])
        for i in r["issues"]:
            i["project_name"] = p["name"]
        issues.extend(r["issues"])
    conn.close()
    return {"issues": sorted(issues, key=lambda x: 0 if x["level"] == "red" else 1)}


# ── API：元神调度 + 质检（v3.5 元神动手）────────────────────────
@app.post("/api/meta/dispatch")
async def meta_dispatch(req: Request):
    """跨项目调度：把任务分派给指定角色（改 owner_role + 话题落消息）。"""
    data = await req.json()
    task_id = data.get("task_id", "")
    to_role = (data.get("to_role") or "").strip()
    if not task_id or not to_role:
        return {"ok": False, "error": "缺少 task_id 或 to_role"}
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return {"ok": False, "error": "任务不存在"}
    # v4.0：派出即流转（idea/todo → doing），看板同步动起来
    new_status = "doing" if task["status"] in ("idea", "todo") else task["status"]
    conn.execute("UPDATE tasks SET owner_role=?, status=? WHERE id=?", (to_role, new_status, task_id))
    if task["topic_id"]:
        conn.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts,topic_id) VALUES (?,?,?,?,?,?,?)",
            (task["project_id"], "元神", "meta", f"📌 已调度：任务「{task['name']}」分派给 {to_role}", "progress",
             datetime.now().isoformat(), task["topic_id"]),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "task_id": task_id, "owner_role": to_role, "status": new_status}


@app.post("/api/meta/quality-check")
async def meta_quality_check(req: Request):
    """元神 AI 质检：任务移到审核中时，用真模型检查产出（对照任务名+话题讨论+模块摘要）。
    返回 verdict: pass / reject + reason。"""
    data = await req.json()
    task_id = data.get("task_id", "")
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return {"ok": False, "error": "任务不存在"}
    pid, tid = task["project_id"], task["topic_id"]
    mod = None
    if task["module_id"]:
        mod = conn.execute("SELECT * FROM modules WHERE id=?", (task["module_id"],)).fetchone()
    # 上下文：任务名 + 模块摘要 + 话题最近消息
    ctx_parts = [f"任务：{task['name']}（负责人 {task['owner_role']}）"]
    if mod:
        ctx_parts.append(f"模块：{mod['name']}（{mod['desc'] or ''}）")
        if mod["context_summary"]:
            ctx_parts.append(f"模块摘要：{mod['context_summary']}")
    if tid:
        msgs = conn.execute(
            "SELECT sender,kind,text FROM messages WHERE topic_id=? ORDER BY id DESC LIMIT 8", (tid,)
        ).fetchall()
        topic_msgs = [m["text"] for m in reversed(msgs) if m["text"]]
        if topic_msgs:
            ctx_parts.append("话题讨论：" + " | ".join(topic_msgs[-6:]))
    conn.close()
    ctx = "\n".join(ctx_parts)
    sys_prompt = (
        "你是「元神」，负责质检 agent 的任务产出。\n"
        "根据任务名、模块上下文和话题讨论，判断这个任务是否可以放行。\n"
        "如果信息不足以判断（比如没有任何实际产出描述），倾向放行（pass）但注明『建议人工复核』。\n"
        "只输出 JSON：{\"verdict\": \"pass\"|\"reject\", \"reason\": \"简短理由\"}"
    )
    reply = call_llm("__meta__", [{"role": "system", "content": sys_prompt},
                                  {"role": "user", "content": ctx}], sys_prompt)
    # 解析 verdict（容错：LLM 可能返回非 JSON 或降级文案）
    verdict, reason = "pass", "（模型不可用，自动放行）"
    try:
        if "{" in reply and "}" in reply:
            j = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            verdict = "reject" if j.get("verdict") == "reject" else "pass"
            reason = j.get("reason", "")
    except Exception:
        pass
    return {"ok": True, "task_id": task_id, "verdict": verdict, "reason": reason, "raw": reply[:120]}


# ── API：元神设置 + 自动巡检（v3.5 用户自安排巡检）────────────────
@app.get("/api/meta/settings")
def meta_settings_get():
    """读取元神设置（自动巡检开关/频率 + 文件监控路径等）。"""
    try:
        watch_paths = json.loads(get_setting("watch_paths", "[]"))
    except Exception:
        watch_paths = []
    return {
        "patrol_enabled": get_setting("patrol_enabled", "0") == "1",
        "patrol_interval": int(get_setting("patrol_interval", "60")),  # 分钟
        "patrol_level": get_setting("patrol_level", "red"),  # red / all
        "watch_paths": watch_paths,
        # v4.0 安全策略（AI 动手前的真人闸门）
        "approval_mode": approval_mode(),                                  # all / danger / off
        "approval_timeout": int(get_setting("approval_timeout", "90")),    # 秒，超时按拒绝
        "bind": "lan" if _lan_mode() else "localhost",
        "lan_enabled": get_setting("lan_enabled", "0") == "1",  # v5.5 局域网一键开关
        "in_app_guide_enabled": get_setting("in_app_guide_enabled", "1") == "1",  # v6.5 产品内引导开关（默认开：纯增量非破坏，可随时关）
        "constitutional_guard_enabled": get_setting("constitutional_guard_enabled", "1") == "1",  # v6.5 宪法级利益护栏（默认开：高于 approval_mode 配置，不可降级）
        "proactivity_enabled": get_setting("proactivity_enabled", "1") == "1",  # v6.5 主动性引擎+团队设计保障（默认开：持续态势扫描/自主补队/停滞预警）
        "final_acceptance_enabled": get_setting("final_acceptance_enabled", "1") == "1",  # v6.5 最终项目验收门禁（默认开：全卡done须经元神终验才置done）
        # v6.5 token 档位体系（默认全开，可随时关回旧行为）
        "chat_tier_enabled": get_setting("chat_tier_enabled", "1") == "1",
        "chat_tier_default": get_setting("chat_tier_default", "auto"),
        "tier0_quick_answer": get_setting("tier0_quick_answer", "1") == "1",
        "tier1_solo_exec": get_setting("tier1_solo_exec", "1") == "1",
        "tier1_skip_llm_judge": get_setting("tier1_skip_llm_judge", "1") == "1",
        "tier1_skip_xverify": get_setting("tier1_skip_xverify", "1") == "1",
        "shared_context_pool": get_setting("shared_context_pool", "1") == "1",
        # v5.2 监控自动兜底：服务端口监控配置 [{name,port,restart_cmd}]
        "services": json.loads(get_setting("services", "[]")),
    }


@app.post("/api/meta/settings")
async def meta_settings_set(req: Request):
    data = await req.json()
    if "patrol_enabled" in data:
        set_setting("patrol_enabled", "1" if data["patrol_enabled"] else "0")
    if "patrol_interval" in data:
        iv = int(data["patrol_interval"])
        if iv <= 0:
            return {"ok": False, "error": "巡检间隔必须大于 0 分钟"}
        set_setting("patrol_interval", str(iv))
    if "patrol_level" in data:
        lv = data["patrol_level"]
        if lv not in ("red", "all"):
            return {"ok": False, "error": "巡检级别必须是 red 或 all"}
        set_setting("patrol_level", lv)
    if "watch_paths" in data:
        paths = data["watch_paths"]
        if isinstance(paths, str):
            paths = [p.strip() for p in paths.split(",") if p.strip()]
        elif not isinstance(paths, list):
            return {"ok": False, "error": "watch_paths 必须是数组或逗号分隔字符串"}
        set_setting("watch_paths", json.dumps(paths, ensure_ascii=False))
        # 路径变化 → 重置快照，下次巡检全量对比
        set_setting("watch_snapshot", "{}")
    # v5.5 局域网一键开关（鉴权层立即生效；监听地址需重启，由启动脚本读取同配置）
    if "lan_enabled" in data:
        set_setting("lan_enabled", "1" if data["lan_enabled"] else "0")
    # v5.2 监控自动兜底：服务监控配置 [{name,port,restart_cmd}]
    if "services" in data:
        svcs = data["services"]
        if isinstance(svcs, str):
            try:
                svcs = json.loads(svcs)
            except Exception:
                return {"ok": False, "error": "services 必须是 JSON 数组"}
        if not isinstance(svcs, list):
            return {"ok": False, "error": "services 必须是数组"}
        clean = []
        for s in svcs:
            if isinstance(s, dict) and s.get("port"):
                clean.append({"name": str(s.get("name") or f"服务{s['port']}")[:40],
                              "port": int(s["port"]),
                              "restart_cmd": str(s.get("restart_cmd") or "").strip()})
        set_setting("services", json.dumps(clean, ensure_ascii=False))
    # v4.2 自主闭环：团队自主推进循环可配置（默认开启）
    if "autonomy_enabled" in data:
        set_setting("autonomy_enabled", "1" if data["autonomy_enabled"] else "0")
    if "autonomy_interval" in data:
        try:
            ai = int(data["autonomy_interval"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "autonomy_interval 必须是整数秒"}
        if not 10 <= ai <= 600:
            return {"ok": False, "error": "autonomy_interval 需在 10~600 秒之间"}
        set_setting("autonomy_interval", str(ai))
    # v4.0：审批策略可调（默认最严 all；关成 off 需用户自己负责）
    if "approval_mode" in data:
        md = data["approval_mode"]
        if md not in ("all", "danger", "off"):
            return {"ok": False, "error": "approval_mode 必须是 all / danger / off"}
        set_setting("approval_mode", md)
    if "approval_timeout" in data:
        try:
            tv = int(data["approval_timeout"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "approval_timeout 必须是整数秒"}
        if not 1 <= tv <= 300:
            return {"ok": False, "error": "approval_timeout 需在 1~300 秒之间（≤3 秒按直接拒绝处理，不弹窗）"}
        set_setting("approval_timeout", str(tv))
    # 批次B/C：代码能力强化开关（meta_settings 持久化，默认全关，用户可经设置页开启）。
    # 仅允许已知 code_* 键，避免任意键写入；值归一化为 "0"/"1"。
    _CODE_SETTING_KEYS = {
        "code_semantic_enabled", "code_vcs_enabled", "code_xverify_enabled",
        "code_stream_enabled", "code_model_enabled", "code_quality_prompt",
        "code_attribution_enabled", "code_static_scan_enabled",
        "evolution_record_enabled", "evolution_recall_enabled", "evolution_quality_gate_enabled",
        "evolution_heldout_enabled", "evolution_lineage_enabled", "evolution_guardrail_enabled",
        "cost_visibility_enabled",
        "in_app_guide_enabled",
        "constitutional_guard_enabled",
        "proactivity_enabled",
        "final_acceptance_enabled",
        "memory_archive_enabled", "memory_panel_enabled",
        # v6.5 token 档位体系：T0 即时直答 / T1 单角色速办 / T2 团队协作 / T3 长程自治
        "chat_tier_enabled", "chat_tier_default", "tier0_quick_answer",
        "tier1_solo_exec", "tier1_skip_llm_judge", "tier1_skip_xverify",
        "shared_context_pool",
    }
    for _k, _v in data.items():
        if _k in _CODE_SETTING_KEYS:
            set_setting(_k, "1" if _v in (True, "1", 1) else "0")
    return {"ok": True, "settings": meta_settings_get()}


def _snapshot_path(p: str) -> dict:
    """生成监控路径快照：{绝对路径: "mtime|size"}。目录递归深度≤3，条目≤500，跳过敏感/隐藏目录。"""
    real = _safe_file_path(p)
    if not real:
        return {}
    snap = {}
    try:
        if os.path.isfile(real):
            st = os.stat(real)
            snap[real] = f"{int(st.st_mtime)}|{st.st_size}"
        elif os.path.isdir(real):
            for root, dirs, files in os.walk(real):
                dirs[:] = [d for d in dirs if d not in FILE_SENSITIVE_PARTS and not d.startswith(".")]
                depth = root[len(real):].count(os.sep)
                if depth > 3:
                    dirs[:] = []
                    continue
                for f in files:
                    full = os.path.join(root, f)
                    try:
                        st = os.stat(full)
                        snap[full] = f"{int(st.st_mtime)}|{st.st_size}"
                    except Exception:
                        pass
                    if len(snap) >= 500:
                        return snap
    except Exception:
        pass
    return snap


def _check_watch_paths(conn):
    """文件监控：对比上次快照，检测新增/修改/删除 → 元神私聊汇报。"""


def _check_services(conn):
    """v5.2 监控自动兜底：巡检配置的服务端口，DOWN → 自动执行重启命令 → 元神私聊留痕。
    配置：settings.services = [{"name","port","restart_cmd"}]（模型设置页可配）。30 秒内不重复重启。"""
    import socket
    try:
        services = json.loads(get_setting("services", "[]"))
    except Exception:
        services = []
    if not services:
        return
    now = time.time()
    for svc in services:
        name = str(svc.get("name") or "?")[:40]
        port = int(svc.get("port") or 0)
        cmd = str(svc.get("restart_cmd") or "").strip()
        if port <= 0:
            continue
        last = float(get_setting(f"svc_restart_{name}", "0") or 0)
        if now - last < 30:
            continue
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.close()
            continue  # 端口正常
        except Exception:
            pass  # DOWN
        if not cmd:
            continue
        try:
            subprocess.Popen(cmd, shell=True, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            set_setting(f"svc_restart_{name}", str(time.time()))
            conn.execute(
                "INSERT INTO messages (project_id,sender,kind,text,ts) VALUES (?,?,?,?,?)",
                (META_PID, "分身 · 元神", "meta",
                 f"⚠️ 自动兜底：服务「{name}」（端口 {port}）无响应，已自动执行重启命令。", datetime.now().isoformat()))
            conn.commit()
            print(f"[services] 已自动重启 {name}")
        except Exception as e:
            print(f"[services] 重启失败 {name}: {e}")


def _check_watch_paths(conn):
    """文件监控：对比上次快照，检测新增/修改/删除 → 元神私聊汇报。"""
    try:
        watch_paths = json.loads(get_setting("watch_paths", "[]"))
    except Exception:
        watch_paths = []
    if not watch_paths:
        return
    try:
        prev = json.loads(get_setting("watch_snapshot", "{}"))
    except Exception:
        prev = {}
    cur = {}
    for wp in watch_paths:
        cur.update(_snapshot_path(wp))
    set_setting("watch_snapshot", json.dumps(cur))
    if not prev:
        return  # 首轮建立基线，不汇报
    added = [k for k in cur if k not in prev]
    removed = [k for k in prev if k not in cur]
    modified = [k for k in cur if k in prev and cur[k] != prev[k]]
    if not (added or removed or modified):
        return
    def short(k):
        home = os.path.expanduser("~")
        return k.replace(home, "~") if k.startswith(home) else k
    lines = []
    for k in added[:10]:
        lines.append(f"🆕 新增 {short(k)}")
    for k in removed[:10]:
        lines.append(f"🗑️ 删除 {short(k)}")
    for k in modified[:10]:
        lines.append(f"✏️ 修改 {short(k)}")
    more = f"\n…共 {len(added) + len(removed) + len(modified)} 处变化" if len(lines) >= 30 else ""
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
        (META_PID, "分身 · 元神", "meta",
         f"📁 文件监控：检测到 {len(added) + len(removed) + len(modified)} 处变化\n" + "\n".join(lines) + more,
         "progress", datetime.now().isoformat()),
    )
    conn.commit()


async def _patrol_loop():
    """后台自动巡检循环：按用户设置的间隔（默认 60 分钟）巡检所有看板，
    发现符合级别的问题 → 在元神私聊窗口落一条汇报消息（不打断用户，用户自会看到）。"""
    while True:
        try:
            await asyncio.sleep(60)  # 每 60 秒检查一次（省资源）
            if get_setting("patrol_enabled", "0") != "1":
                continue
            # 距上次巡检时间检查
            import time
            last = float(get_setting("patrol_last_ts", "0") or 0)
            interval_min = int(get_setting("patrol_interval", "60"))
            if time.time() - last < interval_min * 60:
                continue
            set_setting("patrol_last_ts", str(time.time()))
            # 执行巡检
            conn = get_db()
            # ── 文件监控（v0.27.0：对比监控路径快照，发现变化→私聊汇报）──
            _check_watch_paths(conn)
            # ── v5.2 监控自动兜底：服务端口 DOWN → 自动重启 → 私聊留痕 ──
            _check_services(conn)
            projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
            issues = []
            for p in projects:
                if p["id"] == META_PID:
                    continue
                r = _patrol_rules(conn, p["id"])
                for i in r["issues"]:
                    i["project_name"] = p["name"]
                issues.extend(r["issues"])
            level = get_setting("patrol_level", "red")
            filtered = [i for i in issues if level == "all" or i["level"] == "red"]
            if filtered:
                lines = "\n".join(f"· {i['project_name']}｜{i['detail']}" for i in filtered[:8])
                more = f"\n…共 {len(filtered)} 项" if len(filtered) > 8 else ""
                conn.execute(
                    "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                    (META_PID, "分身 · 元神", "meta", f"🔔 自动巡检发现 {len(filtered)} 个问题：\n{lines}{more}", "progress",
                     datetime.now().isoformat()),
                )
                conn.commit()
            conn.close()
        except Exception:
            pass


# ── M2 元神长续航调度器 ──────────────────────────────────────────────
# 三模式：autopilot（自动驾驶长续航）/ normal（常驻）/ rest（休息·只巡检）
# 由「定时点一下」改为「事件回调」：任务 done 即唤醒派单、执行返回即唤醒下一轮。
FAIL_BREAKER = 5              # 连续派单失败达此值 → 熔断
FAIL_BREAKER_COOLDOWN = 300   # 熔断冷却秒数（到点自动半开）

AUTONOMY_MODES = {
    "autopilot": {"interval": 15,  "concurrency": 3, "auto_fill": True,  "dispatch": True,  "blank_cap": 5},
    "normal":    {"interval": 60,  "concurrency": 1, "auto_fill": False, "dispatch": True,  "blank_cap": 0},
    "rest":      {"interval": 300, "concurrency": 0, "auto_fill": False, "dispatch": False, "blank_cap": 0},
}

AUTONOMY_STATE = {
    "mode": "normal",
    "paused_projects": set(),
    "running_projects": set(),
    "inflight_peak": 0,
    "fail_streak": 0,
    "circuit_open_until": 0.0,
    "last_fail_ts": 0.0,
    "last_success_ts": 0.0,
    "token_budget_hour": 200000,
    "token_budget_day": 2000000,
    "token_used_hour": 0,
    "token_used_day": 0,
    "stuck_timeout_min": 30,
    "ticks": 0,
    "dispatched_total": 0,
    "recycled_total": 0,
    "filled_total": 0,
    "last_tick": 0.0,
    # M4 休息时段：每日作息窗口自动停止派单/补空白（仍回收卡死任务）
    "rest_schedule": {},
    "rest_window_active": False,
    # P1-3 元神升级闭环：按成员累计连续派单失败，达阈值自动加技能+升级
    "member_fail": {},
    # P3 元神自动驾驶汇报：按项目累计派单/补空白/自动升级次数（用于汇报增量）
    "proj_stats": {},
    "report_ts": {},
    "report_snap": {},
}
_AUTONOMY_WAKE = asyncio.Event()


def _autopilot_load_config():
    """启动/设置变更时从 meta_settings 恢复调度器配置。"""
    m = get_setting("autopilot_mode", "normal")
    if m in AUTONOMY_MODES:
        AUTONOMY_STATE["mode"] = m
    try:
        AUTONOMY_STATE["token_budget_hour"] = int(get_setting("autopilot_budget_hour", "200000"))
        AUTONOMY_STATE["token_budget_day"] = int(get_setting("autopilot_budget_day", "2000000"))
    except Exception:
        pass
    try:
        AUTONOMY_STATE["paused_projects"] = set(json.loads(get_setting("autopilot_paused", "[]") or "[]"))
    except Exception:
        AUTONOMY_STATE["paused_projects"] = set()
    # M4 休息时段配置
    try:
        rs = json.loads(get_setting("autopilot_rest_schedule", "{}") or "{}")
        AUTONOMY_STATE["rest_schedule"] = rs if isinstance(rs, dict) else {}
    except Exception:
        AUTONOMY_STATE["rest_schedule"] = {}


def _kick_autonomy():
    """事件回调：任务完成（move_task / _task_status done）→ 立即唤醒调度器补位（毫秒级）。"""
    try:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(_AUTONOMY_WAKE.set)
    except Exception:
        try:
            _AUTONOMY_WAKE.set()
        except Exception:
            pass


def _autonomy_budget_exceeded() -> bool:
    """token 预算护栏：最近 1h / 24h 用量超预算 → 暂停派单。"""
    bh = AUTONOMY_STATE["token_budget_hour"]
    bd = AUTONOMY_STATE["token_budget_day"]
    if bh <= 0 and bd <= 0:
        return False
    try:
        conn = get_db()
        now = datetime.now()
        used_h = used_d = 0
        if bh > 0:
            since = (now - timedelta(hours=1)).isoformat()
            r = conn.execute(
                "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) FROM model_usage WHERE ts>=?",
                (since,)).fetchone()
            used_h = int(r[0]) if r else 0
        if bd > 0:
            since = (now - timedelta(days=1)).isoformat()
            r = conn.execute(
                "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) FROM model_usage WHERE ts>=?",
                (since,)).fetchone()
            used_d = int(r[0]) if r else 0
        conn.close()
        AUTONOMY_STATE["token_used_hour"] = used_h
        AUTONOMY_STATE["token_used_day"] = used_d
        if bh > 0 and used_h >= bh:
            return True
        if bd > 0 and used_d >= bd:
            return True
        return False
    except Exception:
        return False


def _in_rest_window(sched: dict) -> bool:
    """M4 自动作息休息：判断当前时刻是否落在休息窗口内。
    sched={"enabled":bool,"start":"HH:MM","end":"HH:MM"}；支持跨午夜（如 23:00–08:00）。"""
    if not sched or not sched.get("enabled"):
        return False
    try:
        sh, sm = (int(x) for x in str(sched.get("start", "23:00")).split(":"))
        eh, em = (int(x) for x in str(sched.get("end", "08:00")).split(":"))
        s, e = sh * 60 + sm, eh * 60 + em
        if s == e:
            return False
        cur = datetime.now().hour * 60 + datetime.now().minute
        return (s <= cur < e) if s < e else (cur >= s or cur < e)
    except Exception:
        return False


def _recycle_stuck(timeout_min: int = 30) -> int:
    """卡死回收：doing 且 updated_at 早于 cutoff 的任务 → 退回 todo（避免永久占坑）。返回回收数。"""
    try:
        cutoff = (datetime.now() - timedelta(minutes=timeout_min)).isoformat()
        conn = get_db()
        stuck = conn.execute(
            "SELECT id, project_id, name FROM tasks WHERE status='doing' AND updated_at IS NOT NULL "
            "AND updated_at != '' AND updated_at < ?", (cutoff,)
        ).fetchall()
        n = 0
        for r in stuck:
            conn.execute("UPDATE tasks SET status='todo', updated_at=? WHERE id=?",
                         (datetime.now().isoformat(), r["id"]))
            conn.execute(
                "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                (r["project_id"], "系统", "sys",
                 f"♻️ 任务「{r['name']}」卡死超时（> {timeout_min} 分钟无更新），已退回待办重新排队",
                 "progress", datetime.now().isoformat()),
            )
            n += 1
        conn.commit()
        conn.close()
        return n
    except Exception as e:
        print(f"[recycle-stuck] 失败: {e}")
        return 0


def _fill_blanks_internal(pid: str, cap: int = 5, track: str = None) -> int:
    """M2/M4 自动补空白：为项目补充最多 cap 张空白格待办卡（autopilot 模式）。返回建卡数。
    track=None → 遍历项目所有 tracks（web/app/mp…）分别补空白（M4 多轨道）；track 指定 → 仅该轨道。"""
    if cap <= 0:
        return 0
    try:
        conn = get_db()
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj:
            conn.close()
            return 0
        tracks = json.loads(proj["tracks"] or '["web"]') if proj["tracks"] else ["web"]
        if track:
            tracks = [track]
        created = 0
        total_cap = cap * len(tracks)  # 总上限：每轨道最多 cap 张
        for tr in tracks:
            if created >= total_cap:
                break
            proposals = _blank_proposals(conn, pid, tr)[:max(0, cap)]
            for p in proposals:
                if created >= total_cap:
                    break
                tid = f"tk{int(datetime.now().timestamp()*1000)}_{created}_{len(proposals)}"
                conn.execute(
                    "INSERT INTO tasks (id,project_id,module_id,topic_id,name,owner_role,status,done_criteria,stage,track,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, pid, p["module_id"], "", p["name"], p["owner_role"], "todo", "",
                     p["stage"], tr, datetime.now().isoformat(), datetime.now().isoformat()),
                )
                created += 1
        conn.commit()
        conn.close()
        return created
    except Exception as e:
        print(f"[fill-blanks-internal] {pid} 失败: {e}")
        return 0


async def _dispatch_and_track(pid: str, t: dict, prompt: str, assigned_mid: str = None):
    """并发派单包装：执行完成后回收并发槽并立即唤醒调度器（毫秒级补位）。
    P1-3 升级闭环：按负责成员累计连续失败，达阈值(3)元神自动加技能+升级，并在群聊留痕。"""
    try:
        await _execute_project_chat(pid, prompt, reuse_task_id=(t.get("id") if t else None))
        AUTONOMY_STATE["fail_streak"] = 0
        AUTONOMY_STATE["last_success_ts"] = time.time()
        if assigned_mid:
            AUTONOMY_STATE["member_fail"][assigned_mid] = 0  # 成功清零该成员连败
    except Exception as e:
        print(f"[autonomy] {pid} 派单失败: {e}")
        AUTONOMY_STATE["fail_streak"] += 1
        AUTONOMY_STATE["last_fail_ts"] = time.time()
        if assigned_mid:
            cnt = AUTONOMY_STATE["member_fail"].get(assigned_mid, 0) + 1
            AUTONOMY_STATE["member_fail"][assigned_mid] = cnt
            if cnt >= 3:  # 连续 3 次未达标 → 元神判定不胜任，自动升级
                res = _auto_upgrade_member(assigned_mid, "fail", f"{t.get('name','')}：{e}"[:120])
                AUTONOMY_STATE["member_fail"][assigned_mid] = 0
                if res:
                    try:
                        conn = get_db()
                        conn.execute(
                            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                            (pid, "元神", "sys",
                             f"【自动升级】检测到成员连续未达标，已加技能并升级到 v{res['version']}：{', '.join(res['added'])}",
                             "upgrade", datetime.now().isoformat()))
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass
    finally:
        AUTONOMY_STATE["running_projects"].discard(pid)
        _AUTONOMY_WAKE.set()  # 执行返回即触发下一轮 tick


def _project_critical_map(pid: str) -> dict:
    """项目关键路径模块 → 优先级位置（0 起始，越小越优先）；沿 depends_on 跨轨道取最小位置。
    供 M3 调度器优先派单：先打关键路径上的卡，避免其阻塞下游模块。
    与矩阵 _matrix_critical_path 不同：此处计算「含下游」的最长未完成链（memoized 最长链终止于各模块），
    使 B(依赖A) 也进入关键集，从而 A 与 B 都被优先。"""
    conn = get_db()
    proj = conn.execute("SELECT tracks FROM projects WHERE id=?", (pid,)).fetchone()
    tracks = json.loads(proj["tracks"] or '["web"]') if proj and proj["tracks"] else ["web"]
    mods = [_module_dict(r) for r in
            conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort, created_at", (pid,)).fetchall()]
    tasks = [dict(r) for r in
             conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()]
    conn.close()
    crit_pos = {}
    for tr in tracks:
        c2 = get_db()
        stages = get_stage_chain(c2, pid, tr)
        c2.close()
        tmods = [m for m in mods if (m.get("track") or "web") == tr] or mods
        cells_by_m = {}
        for m in tmods:
            cells_by_m[m["id"]] = [{"stage": s, "status": _cell_status(
                [tk for tk in tasks if tk["module_id"] == m["id"] and (tk.get("stage") or "") == s])} for s in stages]
        done = {m["id"]: bool(cells_by_m[m["id"]]) and all(c["status"] == "done" for c in cells_by_m[m["id"]])
                for m in tmods}
        # memoized 最长「终止于 mid」的依赖链（含下游：mid 自身即链尾）
        memo = {}
        def longest(mid):
            if mid in memo:
                return memo[mid]
            m = next((x for x in tmods if x["id"] == mid), None)
            best = []
            if m:
                for dep in (m.get("depends_on") or []):
                    p = longest(dep)
                    if len(p) > len(best):
                        best = p
            res = best + [mid]
            memo[mid] = res
            return res
        chains = [longest(m["id"]) for m in tmods]
        cand = [c for c in chains if any(not done.get(x, False) for x in c)] or chains
        cand.sort(key=len, reverse=True)
        cp = cand[0] if cand else []
        for i, mid in enumerate(cp):
            if mid not in crit_pos or i < crit_pos[mid]:
                crit_pos[mid] = i
    return crit_pos


def _bump_proj_stat(pid: str, key: str, n: int = 1):
    """累加某项目的自动驾驶活动计数（派单/补空白/自动升级），供元神汇报算增量。"""
    d = AUTONOMY_STATE.setdefault("proj_stats", {}).setdefault(
        pid, {"dispatched": 0, "filled": 0, "upgrades": 0})
    d[key] = d.get(key, 0) + n


def _meta_autopilot_report(pid: str, force: bool = False) -> dict:
    """生成并（按需）发布「元神自动驾驶汇报」到项目群聊。

    仅在 force 或（距上次≥600s 且本周期有增量）时发布，避免刷屏。
    内容=各轨道进度 + 关键路径 + 全链路就绪 + 本周期元神动作 + 需你决策的问题。"""
    try:
        conn = get_db()
        p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            conn.close()
            return {"posted": False, "reason": "no_project"}
        tracks = json.loads(p["tracks"] or '["web"]')
        tasks = [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE project_id=?", (pid,)).fetchall()]
        label_map = {"web": "策划/开发", "h5": "H5", "app": "App", "mp": "小程序",
                     "generic": "通用", "infra": "基础设施", "release": "发布", "ops": "运营"}
        # 各轨道进度
        track_lines = []
        for tr in tracks:
            cells = [t for t in tasks if (t.get("track") or "web") == tr]
            if not cells:
                continue
            done = sum(1 for t in cells if t.get("status") == "done")
            doing = sum(1 for t in cells if t.get("status") == "doing")
            todo = len(cells) - done - doing
            pct = round(done / len(cells) * 100)
            track_lines.append(
                f"· {label_map.get(tr, tr)}：{done}/{len(cells)} 完成（{pct}%）· 进行中 {doing} · 待办 {todo}")
        # 关键路径
        crit = _project_critical_map(pid)
        crit_mods = [m for m, _ in sorted(crit.items(), key=lambda kv: kv[1])]
        # 全链路就绪（infra/release/ops）
        life = {}
        for t in LIFE_TRACKS:
            if t not in tracks:
                continue
            sts = [x.get("status") for x in tasks if (x.get("track") or "") == t]
            if not sts:
                life[t] = "未启动"
            elif all(s == "done" for s in sts):
                life[t] = "done"
            elif any(s in ("done", "doing") for s in sts):
                life[t] = "推进中"
            else:
                life[t] = "未启动"
        operational = (life.get("infra") == "done" and life.get("release") == "done"
                       and life.get("ops") in ("done", "推进中"))
        # 巡检问题
        pr = _patrol_rules(conn, pid)
        red = [i for i in pr.get("issues", []) if i.get("level") == "red"]
        amber = [i for i in pr.get("issues", []) if i.get("level") in ("yellow", "amber")]
        conn.close()
        # 本周期增量
        ps = AUTONOMY_STATE.get("proj_stats", {}).get(
            pid, {"dispatched": 0, "filled": 0, "upgrades": 0})
        snap = AUTONOMY_STATE.get("report_snap", {}).get(
            pid, {"dispatched": 0, "filled": 0, "upgrades": 0})
        delta = {k: ps.get(k, 0) - snap.get(k, 0) for k in ("dispatched", "filled", "upgrades")}
        interval = 600
        last = AUTONOMY_STATE.get("report_ts", {}).get(pid, 0)
        if not force and all(v <= 0 for v in delta.values()) and (time.time() - last) < interval:
            return {"posted": False, "reason": "no_change"}
        # 组装正文
        now = datetime.now()
        lines = [f"🚀 元神自动驾驶简报 · {now.strftime('%m-%d %H:%M')}",
                 f"产品：《{p['name']}》"]
        if track_lines:
            lines.append("【进度】")
            lines.extend(track_lines)
        if crit_mods:
            lines.append("【关键路径】" + " → ".join(crit_mods[:6]))
        lines.append("【全链路就绪】" + ("✅ 可运营" if operational else
                     f"基础设施 {life.get('infra', '-')} · 发布 {life.get('release', '-')} · 运营 {life.get('ops', '-')}"))
        acts = []
        if delta["dispatched"] > 0:
            acts.append(f"派单 {delta['dispatched']} 次")
        if delta["filled"] > 0:
            acts.append(f"补空白 {delta['filled']} 张")
        if delta["upgrades"] > 0:
            acts.append(f"自动升级成员 {delta['upgrades']} 次")
        if acts:
            lines.append("【本周期元神动作】" + "，".join(acts))
        if red:
            lines.append("【需你决策】" + "；".join(i.get("detail", "") for i in red[:3]))
        elif amber:
            lines.append("【提示】" + "；".join(i.get("detail", "") for i in amber[:3]))
        lines.append("—— 元神将继续自主推进，你随时可 @元神 干预。")
        text = "\n".join(lines)
        # 结构化载荷：供群聊/驾驶舱渲染卡片（不依赖纯文本解析）
        report = {
            "ts": now.isoformat(),
            "product": p["name"],
            "progress": [
                {"track": label_map.get(tr, tr), "done": done, "total": len(cells),
                 "pct": pct, "doing": doing, "todo": todo}
                for tr, (done, doing, todo, pct, cells) in
                ((tr, (sum(1 for t in cells if t.get("status") == "done"),
                       sum(1 for t in cells if t.get("status") == "doing"),
                       len(cells) - sum(1 for t in cells if t.get("status") == "done")
                       - sum(1 for t in cells if t.get("status") == "doing"),
                       round(sum(1 for t in cells if t.get("status") == "done") / len(cells) * 100),
                       cells))
                 for tr, cells in
                 ((tr, [t for t in tasks if (t.get("track") or "web") == tr]) for tr in tracks)
                 if cells)
            ],
            "critical_path": crit_mods[:6],
            "readiness": {"infra": life.get("infra"), "release": life.get("release"),
                          "ops": life.get("ops"), "operational": operational},
            "actions": acts,
            "decisions": [i.get("detail", "") for i in red[:3]],
            "hints": [i.get("detail", "") for i in amber[:3]],
        }
        conn = get_db()
        conn.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts,report_json) VALUES (?,?,?,?,?,?,?)",
            (pid, "元神", "sys", text, "自动驾驶汇报", now.isoformat(),
             json.dumps(report, ensure_ascii=False)))
        conn.commit()
        conn.close()
        AUTONOMY_STATE.setdefault("report_ts", {})[pid] = time.time()
        AUTONOMY_STATE.setdefault("report_snap", {})[pid] = dict(ps)
        return {"posted": True, "pid": pid, "text": text}
    except Exception as e:
        print(f"[autonomy] report err: {e}")
        return {"posted": False, "reason": str(e)}


# ── Phase B：主动性引擎 + 团队设计保障 ──────────────────────────
# 基础体（管理 OS）的主动工作能力：持续态势扫描、团队齐整度保障、计划停滞主动预警。
# 全部受 proactivity_enabled 开关控制（默认开）；不替代用户决策，只在缺口/风险处主动补位。
_DEFAULT_MEMBER_SKILLS = {
    "architect": ["架构设计", "技术选型", "模块拆分", "关键路径"],
    "backend":   ["后端开发", "API设计", "数据库", "调试"],
    "frontend":  ["前端开发", "UI实现", "交互", "样式"],
    "tester":    ["测试用例", "质量门", "回归", "验收"],
    "pm":        ["需求拆解", "进度追踪", "验收协调"],
    "ops":       ["部署", "监控", "运维", "CI"],
    "designer":  ["视觉设计", "原型", "规范"],
}
STAGNATION_TICKS = 30  # 连续多少 tick 有todo无done即判定计划停滞


def _mins_ago(ts):
    try:
        dt = datetime.fromisoformat(ts)
        return (datetime.now() - dt).total_seconds() / 60.0
    except Exception:
        return 0.0


def _team_health(pid: str) -> dict:
    """项目团队齐整度：所需角色（来自模块 owner_role）vs 已有成员 role_title。
    返回 required/have/gaps/coverage —— 支撑团队设计保障 T1-T3 与驾驶舱团队健康分。"""
    conn = get_db()
    mods = conn.execute("SELECT owner_role,track FROM modules WHERE project_id=?", (pid,)).fetchall()
    members = conn.execute("SELECT role_title,track FROM agent_members WHERE project_id=?", (pid,)).fetchall()
    conn.close()
    required = {}
    for m in mods:
        r = (m["owner_role"] or "").strip()
        tr = (m["track"] or "web").strip() or "web"
        if r:
            required.setdefault(r, set()).add(tr)
    have = set((x["role_title"] or "").strip() for x in members)
    gaps = [{"role": role, "tracks": sorted(trs)} for role, trs in required.items() if role not in have]
    total = max(1, len(required))
    return {"required": sorted(required.keys()), "have": sorted(have),
            "gaps": gaps, "coverage": round((total - len(gaps)) / total, 2)}


def _ensure_team_for_project(pid: str):
    """团队设计保障（T1-T3）：自动补齐缺失的标准角色成员，使团队齐整、每 track 有 owner。
    仅建标准角色成员（不擅自定义能力）；能力维度仍由蒸馏 persona-only 契约约束。返回新建角色列表。"""
    th = _team_health(pid)
    if not th["gaps"]:
        return []
    created = []
    now = datetime.now().isoformat()
    for g in th["gaps"]:
        role = g["role"]
        tr = (g["tracks"][0] if g["tracks"] else "web")
        mid = f"m_{pid}_{role}_{int(datetime.now().timestamp() * 1000)}"
        skills = _DEFAULT_MEMBER_SKILLS.get(role, ["通用执行"])
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO agent_members (id,project_id,role_title,track,skills,version,level,experience,created_at,updated_at) "
                "VALUES (?,?,?,?,?,1,1,0,?,?)",
                (mid, pid, role, tr, json.dumps(skills, ensure_ascii=False), now, now))
            conn.commit()
            conn.close()
            created.append(role)
        except Exception as e:
            print("[autonomy] ensure team err", e)
    return created


def _situation_scan(pid: str) -> dict:
    """持续态势扫描：阻塞(doing卡超时) / 计划停滞(有todo长期无done)。
    团队齐整度由 _team_health 单独维护。返回 findings 供循环决策。"""
    conn = get_db()
    rows = conn.execute("SELECT status,updated_at FROM tasks WHERE project_id=?", (pid,)).fetchall()
    conn.close()
    doing = [r for r in rows if r["status"] == "doing"]
    todo = [r for r in rows if r["status"] == "todo"]
    done = [r for r in rows if r["status"] == "done"]
    stuck_min = AUTONOMY_STATE.get("stuck_timeout_min", 30)
    blockers = [r["id"] for r in doing if _mins_ago(r["updated_at"]) > stuck_min]
    st = AUTONOMY_STATE.setdefault("stagnation", {})
    if todo and not done:
        st[pid] = st.get(pid, 0) + 1
    else:
        st[pid] = 0
    return {"blockers": blockers, "stagnation": st.get(pid, 0) >= STAGNATION_TICKS,
            "todo": len(todo), "done": len(done)}


# ── Phase C：最终项目验收门禁 ────────────────────────────────────
# 团队设计保障（B）让团队齐整；本段确保「项目算不算真完成」由元神自主终验裁决（A1-A5），
# 而非「全卡 done 就被当作完成」。set_phase(done) 只经本门禁或用户显式指令（A5）。
def _set_project_phase(pid: str, to_phase: str, actor: str = "元神") -> bool:
    """底层阶段直写（含快照）。终验门禁与用户指令共用；autopilot 不得直接调用。"""
    conn = get_db()
    proj = conn.execute("SELECT phase FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj or proj["phase"] == to_phase:
        conn.close()
        return False
    from_phase = proj["phase"]
    conn.execute("UPDATE projects SET phase=? WHERE id=?", (to_phase, pid))
    conn.commit()
    conn.close()
    try:
        create_snapshot(pid, f"{from_phase} → {to_phase}", desc=f"阶段推进（{actor}）", auto=True)
    except Exception:
        pass
    return True


def _final_reopen(pid: str, reason: str, fix_task_name: str = None):
    """终验不通过：保持未交付，留痕并（可选）补修正卡。"""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
            (pid, "元神", "sys", f"🔍 最终验收未通过：{reason}。项目保持未交付状态，请处理。",
             "accept", datetime.now().isoformat()))
        if fix_task_name:
            conn.execute(
                "INSERT INTO tasks (project_id,module_id,topic_id,name,owner_role,status,done_criteria,stage,track,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, "", "", f"终验修正：{fix_task_name}", "", "todo",
                 f"终验修正：{fix_task_name}", "", "", datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print("[final-accept] reopen err", e)


async def _meta_final_acceptance(pid: str):
    """最终项目验收门禁（A1-A5）：
    A1 全任务 done 且无 review；A2 项目须有验收基准(目标/标准)，否则暂缓请用户定义；
    A3 若定义了标准，要求每个任务具备验收点；A4 通过→置 done+归档；A5 用户可经 set_phase 终验覆盖。
    不通过→保持未交付+补修正卡。"""
    try:
        conn = get_db()
        proj = conn.execute("SELECT goal,standards,phase FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj:
            conn.close()
            return
        if proj["phase"] == "done":
            conn.close()
            return
        tasks = conn.execute(
            "SELECT id,name,status,acceptance_criteria,done_criteria FROM tasks WHERE project_id=?", (pid,)
        ).fetchall()
        conn.close()
        not_done = [t for t in tasks if t["status"] != "done"]
        review = [t for t in tasks if t["status"] == "review"]
        if not_done:
            _final_reopen(pid, f"仍有 {len(not_done)} 个任务未完成")
            return
        if review:
            _final_reopen(pid, f"仍有 {len(review)} 个任务验收未通过（review）")
            return
        goal = (proj["goal"] or "").strip()
        standards = (proj["standards"] or "").strip()
        if not goal and not standards:  # A2：无验收基准 → 暂缓，请用户定义
            try:
                conn = get_db()
                conn.execute(
                    "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                    (pid, "元神", "sys",
                     "⏸ 最终验收暂缓：项目未定义目标/完成标准。请补充「完成标准」，元神将据此自主终验。",
                     "accept", datetime.now().isoformat()))
                conn.commit()
                conn.close()
            except Exception:
                pass
            return
        if standards:  # A3：有标准则要求每任务具备验收点
            lacking = [t for t in tasks if not ((t["acceptance_criteria"] or "").strip() or (t["done_criteria"] or "").strip())]
            if lacking:
                _final_reopen(pid, f"已定义完成标准，但仍有 {len(lacking)} 个任务缺少验收点", "补齐任务验收标准")
                return
        # A4 通过 → 置 done + 留痕归档
        _set_project_phase(pid, "done", actor="元神-终验")
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                (pid, "元神", "sys",
                 "✅ 最终验收通过：所有任务完成且对照完成标准验收达标，项目已交付。",
                 "accept", datetime.now().isoformat()))
            conn.commit()
            conn.close()
        except Exception:
            pass
    except Exception as e:
        print("[final-accept] err", e)


async def _autonomy_loop():
    """M2 元神长续航调度器：三模式 + 事件回调驱动。
    - autopilot：15s 心跳·并发 3·自动补空白；normal：60s·并发 1；rest：300s·只巡检（不派单）。
    - 任务 done（move_task/_task_status）即唤醒；执行返回即唤醒下一轮。
    - 护栏：全局并发 / 小时+日 token 预算 / 连败熔断 / 卡死超时回收 / 单轮补空白上限 / 项目级暂停。
    - M3 关键路径优先级：派单排序 (on_critical, crit_pos, stage_idx, created_at)，优先打通阻塞下游的链。
    - M4 休息时段：rest_schedule 窗口内自动停止派单/补空白（仍回收卡死），窗口外无缝恢复；多轨道补空白遍历全 tracks。"""
    _first_tick = True
    while True:
        try:
            mode = AUTONOMY_STATE["mode"]
            cfg = AUTONOMY_MODES.get(mode, AUTONOMY_MODES["normal"])
            # 首轮立即扫描（避免切换模式后空等一个完整间隔）；之后等间隔或被事件唤醒（毫秒级响应）
            if _first_tick:
                _first_tick = False
            else:
                try:
                    await asyncio.wait_for(_AUTONOMY_WAKE.wait(), timeout=cfg["interval"])
                except asyncio.TimeoutError:
                    pass  # 间隔到期，进入本轮扫描
            # 注意：CancelledError 不在此捕获，交由外层在关闭时优雅退出
            _AUTONOMY_WAKE.clear()
            AUTONOMY_STATE["last_tick"] = time.time()
            AUTONOMY_STATE["ticks"] += 1
            # v6.4 真·8态状态机：每 tick 同步基础态（idle/autopilot/rest/supervising/blocked 跟随续航 flags）
            meta_reconcile()

            if get_setting("autonomy_enabled", "1") != "1":
                continue
            # M4 自动作息休息：每 tick 更新窗口状态；处于窗口 → 跳过派单/补空白（卡死回收已在上方执行）
            AUTONOMY_STATE["rest_window_active"] = _in_rest_window(AUTONOMY_STATE.get("rest_schedule", {}))
            if not cfg["dispatch"]:
                continue  # rest 模式：仅巡检（由 _patrol_loop 负责），调度器空转
            if AUTONOMY_STATE["rest_window_active"]:
                continue  # 自动作息窗口：元神休眠，停止派单与补空白

            # 连败熔断：冷却中跳过
            if AUTONOMY_STATE.get("circuit_open_until", 0.0) > time.time():
                continue
            if AUTONOMY_STATE["fail_streak"] >= FAIL_BREAKER:
                AUTONOMY_STATE["circuit_open_until"] = time.time() + FAIL_BREAKER_COOLDOWN
                AUTONOMY_STATE["fail_streak"] = 0
                continue

            # token 预算护栏
            if _autonomy_budget_exceeded():
                continue

            # 卡死回收
            recycled = _recycle_stuck(AUTONOMY_STATE.get("stuck_timeout_min", 30))
            AUTONOMY_STATE["recycled_total"] += recycled

            conn = get_db()
            projects = conn.execute(
                "SELECT * FROM projects WHERE phase != 'done' ORDER BY created_at DESC"
            ).fetchall()
            conn.close()

            for p in projects:
                if p["id"] == META_PID:
                    continue
                if p["id"] in AUTONOMY_STATE["paused_projects"]:
                    continue
                if "autonomy_paused" in p.keys() and p["autonomy_paused"]:
                    continue
                # C 最终验收门禁（提前至派单-skip 之前）：全卡 done/无 todo/doing 且未 done → 元神自主终验（门控）
                conn = get_db()
                doing = conn.execute(
                    "SELECT id FROM tasks WHERE project_id=? AND status='doing' LIMIT 1", (p["id"],)
                ).fetchone()
                todos = [dict(r) for r in conn.execute(
                    "SELECT id,name,done_criteria,module_id,stage,track,created_at FROM tasks "
                    "WHERE project_id=? AND status='todo'", (p["id"],)
                ).fetchall()]
                conn.close()
                _fa = get_setting("final_acceptance_enabled", "1") == "1"
                if (_fa and not doing and not todos and p["phase"] != "done"
                        and not AUTONOMY_STATE.get("final_accept_checked", {}).get(p["id"])):
                    AUTONOMY_STATE.setdefault("final_accept_checked", {})[p["id"]] = True
                    asyncio.create_task(_meta_final_acceptance(p["id"]))
                    continue
                if todos:  # 有新卡 → 允许再次终验
                    AUTONOMY_STATE.get("final_accept_checked", {}).pop(p["id"], None)
                if doing:
                    continue  # 该项目仍有进行中任务
                _g = (p["goal"] or "").strip()
                if not (p["standards"] or "").strip() and (
                    not _g or _g.startswith(("完成 ·", "运行中 ·", "阻塞 ·", "暂停 ·"))
                ):
                    continue  # 未定义完成标准/目标 → 不派单（终验门禁已先行评估，A2 会提示补全）
                # 全局并发上限
                if len(AUTONOMY_STATE["running_projects"]) >= cfg["concurrency"]:
                    break
                # 该项目已在执行中（含 meta 计划阶段未落 doing 的窗口）→ 跳过防重复派单
                if p["id"] in AUTONOMY_STATE["running_projects"]:
                    continue

                # B 主动性：态势扫描 + 团队设计保障（门控 proactivity_enabled）
                if get_setting("proactivity_enabled", "1") == "1":
                    try:
                        th = _team_health(p["id"])
                        AUTONOMY_STATE.setdefault("team_health", {})[p["id"]] = th
                        if th["gaps"] and cfg["dispatch"]:  # autopilot/normal 可自主补队
                            created = _ensure_team_for_project(p["id"])
                            if created:
                                AUTONOMY_STATE["team_filled_total"] = AUTONOMY_STATE.get("team_filled_total", 0) + len(created)
                                try:
                                    conn = get_db()
                                    conn.execute(
                                        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                                        (p["id"], "元神", "sys",
                                         f"【团队保障】检测到角色缺口，已自动补齐：{', '.join(created)}",
                                         "team", datetime.now().isoformat()))
                                    conn.commit()
                                    conn.close()
                                except Exception:
                                    pass
                        scan = _situation_scan(p["id"])
                        noted = AUTONOMY_STATE.setdefault("stagnation_notified", {})
                        if scan["stagnation"] and not noted.get(p["id"]):
                            noted[p["id"]] = True
                            try:
                                conn = get_db()
                                conn.execute(
                                    "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                                    (p["id"], "元神", "sys",
                                     "【主动性】检测到计划停滞（长期无交付进展），建议重规划：可让我重新拆解目标或调整优先级。",
                                     "plan", datetime.now().isoformat()))
                                conn.commit()
                                conn.close()
                            except Exception:
                                pass
                        elif not scan["stagnation"]:
                            noted[p["id"]] = False
                    except Exception as ex:
                        print("[autonomy] scan err", ex)

                # 自动补空白（仅 autopilot）
                if cfg["auto_fill"] and cfg["blank_cap"] > 0:
                    filled = _fill_blanks_internal(p["id"], cfg["blank_cap"])
                    if filled:
                        AUTONOMY_STATE["filled_total"] += filled
                        _bump_proj_stat(p["id"], "filled", filled)
                        _AUTONOMY_WAKE.set()  # 新卡已建 → 本轮继续派单

                # M3 关键路径优先级：优先派「关键路径模块、且模块链最早未完成阶段」的卡
                crit_pos = _project_critical_map(p["id"])
                _stage_idx_cache = {}
                def _stage_idx(tr, st):
                    if tr not in _stage_idx_cache:
                        c2 = get_db()
                        _stage_idx_cache[tr] = {s: i for i, s in enumerate(get_stage_chain(c2, p["id"], tr))}
                        c2.close()
                    return _stage_idx_cache[tr].get(st, 999)
                def _rank(tk):
                    mid = tk.get("module_id") or ""
                    on_crit = mid in crit_pos
                    return (0 if on_crit else 1, crit_pos.get(mid, 999),
                            _stage_idx(tk.get("track") or "web", tk.get("stage") or ""),
                            tk.get("created_at") or "")

                if not todos:
                    continue  # 无可派卡片
                todos.sort(key=_rank)
                t = todos[0]

                criteria = (t["done_criteria"] or "").strip()
                on_crit = (t.get("module_id") or "") in crit_pos
                prompt = (f"团队自主推进：请按看板顺序执行任务「{t['name']}」。"
                          + (f"完成标准：{criteria}。" if criteria else "")
                          + ("【该任务位于关键路径，请优先保障其推进，避免阻塞下游模块。】" if on_crit else "")
                          + "自主决策安排执行，不要向用户提问；完成后汇报结果。")
                # 并发派单（不 await，立即让出事件循环）
                AUTONOMY_STATE["running_projects"].add(p["id"])
                AUTONOMY_STATE["inflight_peak"] = max(
                    AUTONOMY_STATE.get("inflight_peak", 0), len(AUTONOMY_STATE["running_projects"]))
                AUTONOMY_STATE["dispatched_total"] += 1
                _bump_proj_stat(p["id"], "dispatched", 1)
                assigned_mid = _resolve_member_for_task(p["id"], t)  # P1-3：解析负责成员 → 连败达阈值自动升级
                asyncio.create_task(_dispatch_and_track(p["id"], t, prompt, assigned_mid))
            # 元神自动驾驶汇报：每 ~40 tick（autopilot≈10min / normal≈40min）给有活动的项目发一份简报（自带节流）
            if AUTONOMY_STATE["ticks"] % 40 == 0 and mode in ("autopilot", "normal"):
                for pp in projects:
                    if pp["id"] == META_PID or pp["id"] in AUTONOMY_STATE["paused_projects"]:
                        continue
                    try:
                        _meta_autopilot_report(pp["id"])
                    except Exception as ex:
                        print("[autonomy] report err", ex)
        except Exception as e:
            print(f"[autonomy] 循环异常: {e}")
            await asyncio.sleep(5)


def _autopilot_state_dict() -> dict:
    cfg = AUTONOMY_MODES.get(AUTONOMY_STATE["mode"], AUTONOMY_MODES["normal"])
    return {
        "ok": True,
        "mode": AUTONOMY_STATE["mode"],
        "modes": list(AUTONOMY_MODES.keys()),
        "dispatch": cfg["dispatch"],
        "interval": cfg["interval"],
        "concurrency": cfg["concurrency"],
        "auto_fill": cfg["auto_fill"],
        "blank_cap": cfg["blank_cap"],
        "running_projects": list(AUTONOMY_STATE["running_projects"]),
        "paused_projects": list(AUTONOMY_STATE["paused_projects"]),
        "inflight": len(AUTONOMY_STATE["running_projects"]),
        "inflight_peak": AUTONOMY_STATE.get("inflight_peak", 0),
        "fail_streak": AUTONOMY_STATE["fail_streak"],
        "circuit_open": AUTONOMY_STATE.get("circuit_open_until", 0.0) > time.time(),
        "token_budget": {
            "hour": AUTONOMY_STATE["token_budget_hour"],
            "day": AUTONOMY_STATE["token_budget_day"],
            "used_hour": AUTONOMY_STATE.get("token_used_hour", 0),
            "used_day": AUTONOMY_STATE.get("token_used_day", 0),
        },
        "stuck_timeout_min": AUTONOMY_STATE["stuck_timeout_min"],
        "ticks": AUTONOMY_STATE["ticks"],
        "dispatched_total": AUTONOMY_STATE["dispatched_total"],
        "recycled_total": AUTONOMY_STATE["recycled_total"],
        "filled_total": AUTONOMY_STATE["filled_total"],
        # B 主动性：团队齐整度（团队设计保障 T1-T3 的可观测出口）
        "team_health": AUTONOMY_STATE.get("team_health", {}),
        "team_filled_total": AUTONOMY_STATE.get("team_filled_total", 0),
        "proactivity_enabled": get_setting("proactivity_enabled", "1") == "1",
        "final_acceptance_enabled": get_setting("final_acceptance_enabled", "1") == "1",
        "last_tick": AUTONOMY_STATE["last_tick"],
        "rest_window_active": AUTONOMY_STATE.get("rest_window_active", False),
        "rest_schedule": AUTONOMY_STATE.get("rest_schedule", {}),
        "autonomy_enabled": get_setting("autonomy_enabled", "1") == "1",
        # v6.5 token 档位体系配置 + token 三段显性化（调度/执行/其他）
        "chat_tier_enabled": get_setting("chat_tier_enabled", "1") == "1",
        "chat_tier_default": get_setting("chat_tier_default", "auto"),
        "tier0_quick_answer": get_setting("tier0_quick_answer", "1") == "1",
        "tier1_solo_exec": get_setting("tier1_solo_exec", "1") == "1",
        "tier1_skip_llm_judge": get_setting("tier1_skip_llm_judge", "1") == "1",
        "tier1_skip_xverify": get_setting("tier1_skip_xverify", "1") == "1",
        "shared_context_pool": get_setting("shared_context_pool", "1") == "1",
        "token_by_phase": _token_phase_stats(),
        # M3 可观测：当前在跑项目的「关键路径模块」有序列表（元神正在优先打通的链）
        "focus_paths": {
            pid: [m for m, _ in sorted(_project_critical_map(pid).items(), key=lambda kv: kv[1])]
            for pid in AUTONOMY_STATE["running_projects"]
        },
    }


@app.get("/api/autopilot/state")
def autopilot_state():
    """返回元神续航调度器实时状态：模式 / 在岗矩阵 / 护栏用量。"""
    return _autopilot_state_dict()


@app.get("/api/meta/token-report")
def meta_token_report(project_id: str = ""):
    """P1-A 精准路由 token 埋点报告：按 phase 聚合元神调度 vs 角色执行的 token 用量，
    并对比「聚焦（scope>0）」vs「全量广播（scope=-1）」的 input token，量化精准路由收益。"""
    conn = get_db()
    where = "WHERE project_id=? " if project_id else ""
    args = (project_id,) if project_id else ()
    rows = conn.execute(
        f"SELECT phase, scope_modules, SUM(input_tokens) AS inp, SUM(output_tokens) AS outp, COUNT(*) AS n "
        f"FROM model_usage {where}GROUP BY phase, scope_modules ORDER BY phase", args
    ).fetchall()
    conn.close()
    by_phase = {}
    for r in rows:
        ph = r["phase"] or "unknown"
        d = by_phase.setdefault(ph, {"input": 0, "output": 0, "calls": 0, "scopes": {}})
        d["input"] += r["inp"] or 0
        d["output"] += r["outp"] or 0
        d["calls"] += r["n"] or 0
        sc = r["scope_modules"]
        d["scopes"][sc] = d["scopes"].get(sc, 0) + (r["inp"] or 0)
    md = by_phase.get("meta-dispatch", {})
    scopes = md.get("scopes", {})
    full_inp = scopes.get(-1, 0)        # 未聚焦（全项目广播）时的 input 总和
    scoped_inp = sum(v for k, v in scopes.items() if isinstance(k, int) and k > 0)
    return {"ok": True, "by_phase": by_phase,
            "meta_full_input": full_inp, "meta_scoped_input": scoped_inp,
            "saved_pct": round((1 - scoped_inp / full_inp) * 100, 1) if full_inp else 0.0}


@app.get("/api/meta/morning-brief")
async def meta_morning_brief(project_id: str = ""):
    """P1-B 元神潜意识晨报（对齐 OpenHuman Subconscious）：汇总自上次查看以来项目状态 diff + 元神主动建议关注。
    返回结构化 stats + focus（主动建议）+ brief（自然语言晨报，离线/无 key 时降级为模板）。"""
    if not project_id:
        conn = get_db()
        plist = conn.execute("SELECT id,name FROM projects ORDER BY created_at DESC LIMIT 5").fetchall()
        conn.close()
        hint = "；可用项目：" + "、".join(f"{r['name']}({r['id'][:12]})" for r in plist) if plist else ""
        return {"ok": False, "error": f"project_id 必填{hint}"}
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    proj = dict(proj)
    last_key = f"morning_brief_last:{project_id}"
    last_ts = get_setting(last_key, "")
    now = datetime.now()
    since = last_ts or (now - timedelta(hours=24)).isoformat()
    new_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND created_at>=?", (project_id, since)).fetchone()[0]
    done_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND status='done' AND updated_at>=?", (project_id, since)).fetchone()[0]
    review_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND status='review' AND updated_at>=?", (project_id, since)).fetchone()[0]
    doing = conn.execute("SELECT id,name,module_id,owner_role,updated_at FROM tasks WHERE project_id=? AND status='doing'", (project_id,)).fetchall()
    todo = conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND status='todo'", (project_id,)).fetchone()[0]
    # 卡点：doing 超过 6h 未更新
    stuck = []
    for t in doing:
        try:
            if (now - datetime.fromisoformat(t["updated_at"])).total_seconds() > 6 * 3600:
                stuck.append(t["name"])
        except Exception:
            pass
    # 近期失败经验（疗效归因素材）
    recent_fail = conn.execute(
        "SELECT scenario,lesson FROM experiences WHERE project_id=? AND category='failure' ORDER BY ts DESC LIMIT 3",
        (project_id,)).fetchall()
    conn.close()
    # 元神主动建议关注
    focus = []
    if stuck:
        focus.append(f"有 {len(stuck)} 个进行中任务疑似卡住（>6h 无更新）：{'、'.join(stuck[:3])}，建议优先排查。")
    if review_tasks:
        focus.append(f"{review_tasks} 个任务待复核（验收未通过），需决策重做或放宽标准。")
    if todo:
        focus.append(f"仍有 {todo} 个待办未启动。")
    # LLM 生成晨报（失败/离线降级为模板）
    brief_prompt = (f"项目：{proj['name']}。\n"
                    f"自 {since} 以来：新增任务 {new_tasks}、完成 {done_tasks}、转复核 {review_tasks}。\n"
                    f"当前：进行中 {len(doing)}、待办 {todo}。\n"
                    f"卡点：{'、'.join(stuck) if stuck else '无'}。\n"
                    f"近期失败经验：{'; '.join(e['lesson'] for e in recent_fail) if recent_fail else '无'}。\n"
                    f"请给出一份「今日晨报」：3-5 句，先现状概览，再给元神主动建议关注（聚焦最关键 1-2 件事）。")
    try:
        brief = await _chat_with_tools("__meta__", [{"role": "user", "content": brief_prompt}],
                                       "你是分身产品的「元神」，负责团队总管。用中文、简洁、actionable 的口吻写晨报（3-5 句）。")
        if brief.startswith("[分身·离线]") or brief.startswith("[分身·降级]"):
            raise RuntimeError("offline")
    except Exception:
        brief = (f"【{proj['name']} 今日晨报】新增 {new_tasks} · 完成 {done_tasks} · 待复核 {review_tasks}；"
                 f"进行中 {len(doing)} · 待办 {todo}。" + (" " + " ".join(focus) if focus else " 状态平稳，按计划推进即可。"))
    set_setting(last_key, now.isoformat())
    return {"ok": True, "project": proj["name"], "since": since,
            "stats": {"new": new_tasks, "done": done_tasks, "review": review_tasks,
                      "doing": len(doing), "todo": todo, "stuck": stuck},
            "focus": focus, "brief": brief}


# ── P2-A：画像/经验导出 Markdown Vault（对齐 OpenHuman Obsidian）──
def _vault_md_front(tag: str) -> str:
    return "---\ntags: [分身, %s]\n---\n\n" % tag


@app.get("/api/export/vault")
def export_vault(project_id: str = ""):
    """P2-A 画像/经验导出 Markdown Vault（对齐 OpenHuman Obsidian）：
    把 user_model(画像) + experiences(经验库) + meta_interview(元神访谈) 导出为
    Obsidian 兼容的多文件 .md（人读可纠错）。返回 files 字典 {文件名: 内容} 与 combined 合并版。
    鉴权由全局中间件统一处理（需本地令牌）。"""
    conn = get_db()
    # 画像：按维度分组
    um = conn.execute(
        "SELECT dim,field,value,confidence,source FROM user_model ORDER BY dim, id").fetchall()
    # 经验：非淘汰，按疗效权重降序（可按项目过滤）
    exp_where = "WHERE eliminated=0" + (" AND project_id=?" if project_id else "")
    exp_args = (project_id,) if project_id else ()
    exps = conn.execute(
        "SELECT id,category,scenario,goal,attempts,outcome,lesson,project_id,source,ts,weight "
        f"FROM experiences {exp_where} ORDER BY weight DESC", exp_args).fetchall()
    # 访谈：取最近一条
    mi = conn.execute(
        "SELECT asked,answers,focus_dim,last_ask_at,updated_at FROM meta_interview ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    # ── 元神画像.md ──
    by_dim = {}
    for r in um:
        by_dim.setdefault(r["dim"], []).append(r)
    prof = [_vault_md_front("元神画像"), "# 元神画像（用户模型）\n",
            "> 由元神在群聊/访谈中逐步沉淀，人读可纠错。\n"]
    if by_dim:
        for dim, rows in by_dim.items():
            prof.append(f"## {dim}")
            for r in rows:
                conf = f"（置信度 {r['confidence']:.2f}）" if r["confidence"] is not None else ""
                src = f" · 来源：{r['source']}" if r["source"] else ""
                prof.append(f"- **{r['field']}**：{r['value']}{conf}{src}")
            prof.append("")
    else:
        prof.append("_尚无画像数据（元神尚未完成足够访谈/观察）。_\n")
    profile_md = "\n".join(prof)

    # ── 经验库.md ──
    exp = [_vault_md_front("经验库"), "# 经验库（疗效归因权重降序）\n",
           "> 每条经验可被其他 Agent/IDE 经 /api/share/experiences 复用。\n"]
    if exps:
        for r in exps:
            cat = "✅ 成功" if r["category"] == "success" else "❌ 失败/踩坑"
            pid_tag = f" · 项目：{r['project_id']}" if r["project_id"] else ""
            exp.append(f"## [{cat}] {r['scenario']}（权重 {r['weight']:.2f}）")
            if r["goal"]:
                exp.append(f"- **目标**：{r['goal']}")
            if r["attempts"]:
                exp.append(f"- **做法**：{r['attempts']}")
            if r["outcome"]:
                exp.append(f"- **结果**：{r['outcome']}")
            exp.append(f"- **经验**：{r['lesson']}{pid_tag}")
            if r["source"]:
                exp.append(f"  - 来源：{r['source']} · {r['ts']}")
            exp.append("")
    else:
        exp.append("_尚无经验数据。_\n")
    exp_md = "\n".join(exp)

    # ── 元神访谈.md ──
    mi_lines = [_vault_md_front("元神访谈"), "# 元神访谈记录\n"]
    if mi:
        try:
            asked = json.loads(mi["asked"]) if mi["asked"] else []
        except Exception:
            asked = []
        try:
            answers = json.loads(mi["answers"]) if mi["answers"] else {}
        except Exception:
            answers = {}
        if mi["focus_dim"]:
            mi_lines.append(f"- **当前聚焦维度**：{mi['focus_dim']}")
        if asked:
            mi_lines.append("")
            mi_lines.append("## 已问问题")
            for q in asked:
                a = answers.get(q, "")
                mi_lines.append(f"- Q：{q}" + (f"\n  - A：{a}" if a else ""))
        if mi["updated_at"]:
            mi_lines.append("")
            mi_lines.append(f"_最近更新：{mi['updated_at']}_")
    else:
        mi_lines.append("_尚无访谈记录。_")
    mi_lines.append("")
    interview_md = "\n".join(mi_lines)

    # ── Vault 索引（Obsidian 双链）──
    index_md = "\n".join([
        _vault_md_front("Vault索引"), "# 分身记忆 Vault 索引\n",
        "本 Vault 由分身导出，可被 Obsidian 直接打开（双链互相引用）。\n",
        "- [[元神画像]]", "- [[经验库]]", "- [[元神访谈]]", "",
        f"> 导出范围：{('项目 ' + project_id) if project_id else '全部项目'}"
    ])

    files = {
        "Vault索引.md": index_md,
        "元神画像.md": profile_md,
        "经验库.md": exp_md,
        "元神访谈.md": interview_md,
    }
    combined = "\n\n---\n\n".join(files.values())
    return {"ok": True, "scope": project_id or "all",
            "count": {"user_model": len(um), "experiences": len(exps),
                      "meta_interview": 1 if mi else 0},
            "files": files, "combined": combined}


# ── P2-B：经验库 agentmemory 式外部共享（供其他 Agent/IDE 复用）──
@app.get("/api/share/experiences")
def share_experiences(scenario: str = "", category: str = "", project_id: str = "", limit: int = 20):
    """P2-B 经验库 agentmemory 式外部共享：供其他 Agent/IDE 复用本产品的经验库（对齐 OpenHuman agentmemory）。
    - scenario 命中：按疗效权重复用召回（_recall_experiences，权重+相关度排序，返回 _score）
    - 否则按 category / project_id 过滤，按权重降序
    返回机器可读 JSON 数组（含 _score 以便外部排序复用）。鉴权由全局中间件统一处理（需本地令牌）。"""
    if scenario:
        items = _recall_experiences(scenario, limit)
        return {"ok": True, "count": len(items), "mode": "recall",
                "query": scenario, "items": [dict(i) for i in items]}
    conn = get_db()
    try:
        where, args = [], []
        if category:
            where.append("category=?"); args.append(category)
        if project_id:
            where.append("project_id=?"); args.append(project_id)
        wsql = ("WHERE eliminated=0 AND " + " AND ".join(where)) if where else "WHERE eliminated=0"
        rows = conn.execute(
            "SELECT id,category,scenario,goal,attempts,outcome,lesson,project_id,source,ts,weight "
            f"FROM experiences {wsql} ORDER BY weight DESC LIMIT ?", args + [limit]).fetchall()
        out = [dict(r) for r in rows]
        return {"ok": True, "count": len(out), "mode": "list",
                "query": {"category": category, "project_id": project_id, "limit": limit},
                "items": out}
    finally:
        conn.close()


@app.post("/api/autopilot/set")
async def autopilot_set(req: Request):
    """切换续航模式 / 暂停·恢复项目 / 设置 token 预算。变更即时生效。"""
    data = await req.json()
    changed = []
    if "mode" in data:
        m = data["mode"]
        if m not in AUTONOMY_MODES:
            return {"ok": False, "error": f"mode 必须是 {list(AUTONOMY_MODES.keys())}"}
        AUTONOMY_STATE["mode"] = m
        set_setting("autopilot_mode", m)
        changed.append("mode")
    if "enabled" in data:
        set_setting("autonomy_enabled", "1" if data["enabled"] else "0")
        changed.append("enabled")
    if "paused" in data:
        for pid in (data["paused"] or []):
            AUTONOMY_STATE["paused_projects"].add(pid)
        set_setting("autopilot_paused", json.dumps(list(AUTONOMY_STATE["paused_projects"]), ensure_ascii=False))
        changed.append("paused")
    if "resume" in data:
        for pid in (data["resume"] or []):
            AUTONOMY_STATE["paused_projects"].discard(pid)
        set_setting("autopilot_paused", json.dumps(list(AUTONOMY_STATE["paused_projects"]), ensure_ascii=False))
        changed.append("resume")
    if "token_budget_hour" in data:
        try:
            v = int(data["token_budget_hour"])
            AUTONOMY_STATE["token_budget_hour"] = max(0, v)
            set_setting("autopilot_budget_hour", str(v))
            changed.append("token_budget_hour")
        except (TypeError, ValueError):
            return {"ok": False, "error": "token_budget_hour 必须是整数"}
    if "token_budget_day" in data:
        try:
            v = int(data["token_budget_day"])
            AUTONOMY_STATE["token_budget_day"] = max(0, v)
            set_setting("autopilot_budget_day", str(v))
            changed.append("token_budget_day")
        except (TypeError, ValueError):
            return {"ok": False, "error": "token_budget_day 必须是整数"}
    if "stuck_timeout_min" in data:
        try:
            AUTONOMY_STATE["stuck_timeout_min"] = max(5, int(data["stuck_timeout_min"]))
            changed.append("stuck_timeout_min")
        except (TypeError, ValueError):
            return {"ok": False, "error": "stuck_timeout_min 必须是整数"}
    if "rest_schedule" in data:
        try:
            s = data["rest_schedule"]
            if not isinstance(s, dict):
                return {"ok": False, "error": "rest_schedule 必须是对象 {enabled,start,end}"}
            if s.get("enabled"):
                for k in ("start", "end"):
                    if k in s:
                        hhmm = str(s[k])
                        if hhmm.count(":") != 1:
                            return {"ok": False, "error": f"rest_schedule.{k} 格式应为 HH:MM"}
                        h, m = hhmm.split(":")
                        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                            return {"ok": False, "error": f"rest_schedule.{k} 时间超出范围"}
            AUTONOMY_STATE["rest_schedule"] = s
            set_setting("autopilot_rest_schedule", json.dumps(s, ensure_ascii=False))
            changed.append("rest_schedule")
        except Exception as e:
            return {"ok": False, "error": f"rest_schedule 校验失败: {e}"}
    _AUTONOMY_WAKE.set()  # 配置变更立即生效
    return {"ok": True, "changed": changed, "state": _autopilot_state_dict()}


@app.post("/api/meta/report/{pid}")
async def meta_report(pid: str):
    """手动触发元神为某项目生成并发布「自动驾驶汇报」（force=True，无视节流）。"""
    res = _meta_autopilot_report(pid, force=True)
    if res.get("posted"):
        return {"ok": True, **res}
    return JSONResponse(status_code=400, content={"ok": False, "error": res.get("reason", "未发布")})


@app.get("/api/projects/{pid}/report/latest")
def project_report_latest(pid: str):
    """返回项目最近一条元神自动驾驶汇报（供驾驶舱展示）。"""
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM messages WHERE project_id=? AND tag='自动驾驶汇报' ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    conn.close()
    if not r:
        return {"ok": True, "report": None}
    return {"ok": True, "report": dict(r)}


# ════════════════════════════════════════════════════════════════════
# v6.2 元神战队：Agent 成员（团队成员）CRUD + 配置 + 升级 + 经验进化
# ════════════════════════════════════════════════════════════════════
DEFAULT_MODEL_CFG = {"model": "deepseek-v4-flash", "temp": 0.3, "reason": False, "max_tokens": 8000}


def _member_dict(r):
    d = dict(r)
    for k in ("rule", "skills", "evo_tree"):
        try:
            d[k] = json.loads(d[k] or "[]")
        except Exception:
            d[k] = []
    try:
        d["model_cfg"] = json.loads(d.get("model_cfg") or "{}")
    except Exception:
        d["model_cfg"] = {}
    return d


@app.get("/api/projects/{pid}/members")
def list_members(pid: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM agent_members WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
    out = [_member_dict(r) for r in rows]
    conn.close()
    return {"ok": True, "members": out}


@app.post("/api/projects/{pid}/members")
async def create_member(pid: str, req: Request):
    data = await req.json()
    mid = data.get("id") or f"{pid}-a{int(datetime.now().timestamp() * 1000)}"
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO agent_members "
        "(id,project_id,name,avatar,role_title,track,soul,rule,eng_spec,model_cfg,work_mode,work_hours,current_task,skills,experience,level,version,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,1,?,?)",
        (mid, pid, data.get("name", ""), data.get("avatar") or (data.get("name", "?")[0]),
         data.get("role_title", ""), data.get("track", "web"), data.get("soul", ""),
         json.dumps(data.get("rule") or [], ensure_ascii=False), data.get("eng_spec", ""),
         json.dumps(data.get("model_cfg") or DEFAULT_MODEL_CFG, ensure_ascii=False),
         data.get("work_mode", "online"), data.get("work_hours", "24h"),
         data.get("current_task", ""), json.dumps(data.get("skills") or [], ensure_ascii=False),
         now, now),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": mid}


@app.delete("/api/projects/{pid}/members/{mid}")
def delete_member(pid: str, mid: str):
    conn = get_db()
    conn.execute("DELETE FROM agent_members WHERE id=? AND project_id=?", (mid, pid))
    conn.execute("DELETE FROM agent_experience WHERE member_id=?", (mid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/members/{mid}")
def get_member(mid: str):
    conn = get_db()
    r = conn.execute("SELECT * FROM agent_members WHERE id=?", (mid,)).fetchone()
    if not r:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "成员不存在"})
    d = _member_dict(r)
    exp = [dict(x) for x in conn.execute(
        "SELECT * FROM agent_experience WHERE member_id=? ORDER BY created_at DESC", (mid,)).fetchall()]
    d["experiences"] = exp
    conn.close()
    return {"ok": True, "member": d}


@app.put("/api/members/{mid}")
async def update_member(mid: str, req: Request):
    data = await req.json()
    conn = get_db()
    r = conn.execute("SELECT * FROM agent_members WHERE id=?", (mid,)).fetchone()
    if not r:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "成员不存在"})

    def pick(k, default):
        return data.get(k, default)

    rule = data.get("rule", None)
    rule = rule if rule is None else (rule if isinstance(rule, str) else json.dumps(rule, ensure_ascii=False))
    rule = r["rule"] if rule is None else rule
    model_cfg = data.get("model_cfg", None)
    model_cfg = model_cfg if model_cfg is None else (model_cfg if isinstance(model_cfg, str) else json.dumps(model_cfg, ensure_ascii=False))
    model_cfg = r["model_cfg"] if model_cfg is None else model_cfg
    skills = data.get("skills", None)
    skills = skills if skills is None else (skills if isinstance(skills, str) else json.dumps(skills, ensure_ascii=False))
    skills = r["skills"] if skills is None else skills

    conn.execute(
        "UPDATE agent_members SET name=?,avatar=?,role_title=?,track=?,soul=?,rule=?,eng_spec=?,"
        "model_cfg=?,work_mode=?,work_hours=?,current_task=?,skills=?,updated_at=? WHERE id=?",
        (pick("name", r["name"]), pick("avatar", r["avatar"]), pick("role_title", r["role_title"]),
         pick("track", r["track"]), pick("soul", r["soul"]), rule, pick("eng_spec", r["eng_spec"]),
         model_cfg, pick("work_mode", r["work_mode"]), pick("work_hours", r["work_hours"]),
         pick("current_task", r["current_task"]), skills, datetime.now().isoformat(), mid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/members/{mid}/upgrade")
async def upgrade_member(mid: str, req: Request = None):
    data = await req.json() if req else {}
    conn = get_db()
    r = conn.execute("SELECT * FROM agent_members WHERE id=?", (mid,)).fetchone()
    if not r:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "成员不存在"})
    new_ver = (r["version"] or 1) + 1
    new_level = r["level"] or 1
    reason = (data.get("reason") or "元神评估：能力升级")[:200]
    exp = (r["experience"] or 0) + 50
    if exp >= new_level * 200:
        new_level += 1
    evo = json.loads(r["evo_tree"] or "[]")
    evo.append({"v": new_ver, "at": datetime.now().isoformat(), "note": reason})
    _bump_proj_stat(r["project_id"], "upgrades", 1)
    conn.execute(
        "UPDATE agent_members SET version=?,level=?,experience=?,evo_tree=?,updated_at=? WHERE id=?",
        (new_ver, new_level, exp, json.dumps(evo, ensure_ascii=False), datetime.now().isoformat(), mid))
    conn.execute(
        "INSERT INTO agent_experience (id,member_id,project_id,note,kind,created_at) VALUES (?,?,?,?,?,?)",
        (f"e{int(datetime.now().timestamp() * 1000)}", mid, r["project_id"],
         f"[升级] {reason}", "upgrade", datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"ok": True, "version": new_ver, "level": new_level, "experience": exp}


@app.post("/api/members/{mid}/experience")
async def add_experience(mid: str, req: Request):
    data = await req.json()
    conn = get_db()
    r = conn.execute("SELECT * FROM agent_members WHERE id=?", (mid,)).fetchone()
    if not r:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "成员不存在"})
    note = (data.get("note") or "")[:500]
    kind = data.get("kind", "general")
    gained = int(data.get("gained", 10))
    conn.execute(
        "INSERT INTO agent_experience (id,member_id,project_id,note,kind,created_at) VALUES (?,?,?,?,?,?)",
        (f"e{int(datetime.now().timestamp() * 1000)}", mid, r["project_id"], note, kind, datetime.now().isoformat()))
    new_exp = (r["experience"] or 0) + gained
    new_level = r["level"] or 1
    if new_exp >= new_level * 200:
        new_level += 1
    conn.execute(
        "UPDATE agent_members SET experience=?,level=?,updated_at=? WHERE id=?",
        (new_exp, new_level, datetime.now().isoformat(), mid))
    conn.commit()
    conn.close()
    return {"ok": True, "experience": new_exp, "level": new_level}


# ── P1-3 元神升级闭环：agent 不胜任 → 元神自动加技能 / 改 soul / 换模型 ──
def _auto_upgrade_member(mid: str, reason_type: str = "manual", context: str = ""):
    """元神对不胜任成员自动升级：加技能 + 版本+1 + 进化树节点 + 经验 + soul 印记 + 必要时换模型。
    返回升级结果 dict，成员不存在返回 None。reason_type: fail / weak / manual。"""
    conn = get_db()
    r = conn.execute("SELECT * FROM agent_members WHERE id=?", (mid,)).fetchone()
    if not r:
        conn.close()
        return None
    now = datetime.now().isoformat()
    # v6.4 真·8态状态机：进入「升级中」瞬态（受 META_TRANSITIONS 守卫，仅 idle/autopilot/supervising/dispatching/rest → upgrading）
    meta_transition("upgrade_start", reason=f"auto-upgrade member {mid} ({reason_type})")
    skills = json.loads(r["skills"] or "[]")
    evo = json.loads(r["evo_tree"] or "[]")
    model_cfg = json.loads(r["model_cfg"] or "{}") or {}
    soul = r["soul"] or ""
    new_ver = (r["version"] or 1) + 1
    new_level = r["level"] or 1
    exp = (r["experience"] or 0) + 50
    if exp >= new_level * 200:
        new_level += 1
    added = []
    ctx = (context or "")
    if reason_type == "fail":
        if not any("复盘" in s for s in skills):
            added.append("复盘与异常兜底")
        if not any("自愈" in s for s in skills):
            added.append("失败自愈重试")
        if any(k in ctx for k in ("推理", "复杂", "逻辑", "reason", "推理链")):
            model_cfg["reason"] = True
            if "强化推理(reason=on)" not in added:
                added.append("强化推理(reason=on)")
        if any(k in ctx for k in ("前端", "样式", "UI", "界面")) and not any("前端" in s for s in skills):
            added.append("前端样式精修")
        if any(k in ctx for k in ("后端", "接口", "API", "数据库")) and not any("后端" in s for s in skills):
            added.append("后端接口加固")
    elif reason_type == "weak":
        if not any("强化" in s for s in skills):
            added.append("进阶专项强化")
    else:
        if not any("定向" in s for s in skills):
            added.append("元神定向强化")
    for s in added:
        if s not in skills:
            skills.append(s)
    # 疗效归因第⑤环 → 第③环（成员进化）：该类经验信任度偏低时优先补强并标记
    try:
        ac = conn.execute(
            "SELECT trust_score,weight,samples FROM skill_attribution WHERE key=?",
            ("cat:" + ("failure" if reason_type == "fail" else "success"),)).fetchone()
        if ac and ac["trust_score"] is not None and ac["trust_score"] < 0.4:
            if "疗效归因补强" not in skills:
                skills.append("疗效归因补强")
            added.append("疗效归因补强")
            note_suffix = f"；归因预警：该类经验信任度偏低({ac['trust_score']:.2f})，优先补强"
        else:
            note_suffix = ""
    except Exception:
        note_suffix = ""
    note = f"自动升级[v{new_ver}]：{'、'.join(added)}" + (f"（诱因：{ctx[:60]}）" if ctx else "") + note_suffix
    evo.append({"v": new_ver, "at": now, "note": note, "type": reason_type, "added": added})
    if "【进化印记】" not in soul:
        soul = soul + ("\n【进化印记】" if soul.strip() else "") + note
    else:
        soul = soul + "；" + note
    _bump_proj_stat(r["project_id"], "upgrades", 1)
    conn.execute(
        "UPDATE agent_members SET version=?,level=?,experience=?,skills=?,evo_tree=?,model_cfg=?,soul=?,updated_at=? WHERE id=?",
        (new_ver, new_level, exp, json.dumps(skills, ensure_ascii=False),
         json.dumps(evo, ensure_ascii=False), json.dumps(model_cfg, ensure_ascii=False), soul, now, mid))
    conn.execute(
        "INSERT INTO agent_experience (id,member_id,project_id,note,kind,created_at) VALUES (?,?,?,?,?,?)",
        (f"e{int(datetime.now().timestamp() * 1000)}", mid, r["project_id"],
         f"[自动升级] {note}", "auto-upgrade", now))
    conn.commit()
    conn.close()
    # v6.4 真·8态状态机：离开「升级中」瞬态，回到基础态（由后续 reconcile 决定实际态）
    meta_transition("upgrade_done", reason=f"auto-upgrade member {mid} done")
    return {"version": new_ver, "level": new_level, "experience": exp,
            "added": added, "skills": skills, "model_cfg": model_cfg}


@app.post("/api/members/{mid}/auto-upgrade")
async def auto_upgrade_member(mid: str, req: Request = None):
    """元神主动升级成员：依据 reason_type（fail/weak/manual）+ 上下文自动加技能 / 改 soul / 换模型。"""
    data = await req.json() if req else {}
    rt = data.get("reason_type", "manual")
    ctx = (data.get("context") or "")[:300]
    res = _auto_upgrade_member(mid, rt, ctx)
    if res is None:
        return JSONResponse(status_code=404, content={"error": "成员不存在"})
    return {"ok": True, **res}


def _resolve_member_for_task(pid: str, t: dict) -> str:
    """解析任务负责成员 id：模块 owner_role → 同名 role_title 成员；否则按 track 匹配；再否则首个成员。"""
    try:
        conn = get_db()
        mod = conn.execute("SELECT owner_role FROM modules WHERE id=?",
                           (t.get("module_id") or "",)).fetchone()
        members = conn.execute(
            "SELECT id,role_title,track FROM agent_members WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
        conn.close()
        if not members:
            return None
        owner_role = (mod["owner_role"] if mod else "") or ""
        by_role = [m for m in members if owner_role and m["role_title"] == owner_role]
        if by_role:
            return by_role[0]["id"]
        trk = t.get("track") or "web"
        by_trk = [m for m in members if m["track"] == trk]
        if by_trk:
            return by_trk[0]["id"]
        return members[0]["id"]
    except Exception:
        return None


# ── v6.4 真·8态状态机（事件驱动 + 守卫 + 事件溯源）──
# 此前 v6.2 的 META_STATE 仅作派生展示（无转移/守卫/持久化），本次升级为权威状态变量：
# ① 基础态(idle/autopilot/supervising/rest) 由续航 flags 派生并经 reconcile 同步；
# ② 瞬态(upgrading/blocked) 由显式事件进入/退出，受 META_TRANSITIONS 守卫保护；
# ③ 每次转移写入 meta_state_log（事件溯源），驾驶舱可回看状态轨迹。
META_STATE = {"state": "idle", "prev_state": "idle", "focus_project": "", "upgrading": False, "blocked": False}

# 显式转移表（守卫）：仅表中允许的 (当前态, 事件) → 目标态 才可通过；其余视为非法转移被拒绝并记录。
META_TRANSITIONS = {
    "init":        {"boot_ok": "idle"},
    "idle":        {"upgrade_start": "upgrading", "manual": "idle"},
    "dispatching": {"upgrade_start": "upgrading", "manual": "idle"},
    "autopilot":   {"upgrade_start": "upgrading", "manual": "idle", "supervise": "supervising"},
    "supervising": {"upgrade_start": "upgrading", "manual": "idle", "autonomy_on": "autopilot"},
    "rest":        {"upgrade_start": "upgrading", "wake": "idle", "autonomy_on": "autopilot"},
    "upgrading":   {"upgrade_done": "idle"},
    "blocked":     {"resolve": "idle", "manual": "idle", "autonomy_on": "autopilot", "rest": "rest"},
}


def _meta_log(frm, to, event, reason, rejected=False):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO meta_state_log (from_state,to_state,event,reason,rejected,ts) VALUES (?,?,?,?,?,?)",
            (frm, to, event, reason, 1 if rejected else 0, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as ex:
        print("[meta_state] log err", ex)


def _meta_natural_state(ap: dict, blocked: bool) -> str:
    """由续航 flags 派生"应有"基础态（不含升级/需决策瞬态）。"""
    if ap.get("rest_window_active"):
        return "rest"
    if ap.get("mode") == "autopilot":
        return "blocked" if blocked else "supervising"
    return "idle"


def meta_transition(event: str, reason: str = "", to_state: str = None) -> str:
    """显式事件驱动转移：经 META_TRANSITIONS 守卫校验（to_state 显式给定时跳过表查，用于 reconcile 派生）。
    返回转移后的权威状态。非法/未定义转移：拒绝并写日志，状态不变。"""
    cur = META_STATE["state"]
    nxt = to_state if to_state else META_TRANSITIONS.get(cur, {}).get(event)
    if not nxt or nxt not in ("init", "idle", "dispatching", "supervising", "autopilot", "rest", "upgrading", "blocked"):
        _meta_log(cur, cur, event, (reason or "guard:illegal-transition"), rejected=True)
        return cur
    if nxt == cur:
        return cur
    _meta_log(cur, nxt, event, reason)
    META_STATE["prev_state"] = cur
    META_STATE["state"] = nxt
    if nxt == "upgrading":
        META_STATE["upgrading"] = True
    if cur == "upgrading" and nxt != "upgrading":
        META_STATE["upgrading"] = False
    if nxt == "blocked":
        META_STATE["blocked"] = True
    if cur == "blocked" and nxt != "blocked":
        META_STATE["blocked"] = False
    return nxt


def meta_reconcile() -> str:
    """每 tick 调用：瞬态(upgrading/blocked)优先不被改写；否则让基础态跟随续航 flags 派生。"""
    try:
        ap = _autopilot_state_dict()
        blocked = bool(META_STATE.get("blocked") or ap.get("circuit_open"))
        nat = _meta_natural_state(ap, blocked)
        cur = META_STATE["state"]
        if cur in ("upgrading", "blocked"):
            return cur
        if cur != nat:
            meta_transition("derive", reason=f"flags→{nat}", to_state=nat)
    except Exception as ex:
        print("[meta_state] reconcile err", ex)
    return META_STATE["state"]


@app.get("/api/meta/state")
def meta_state():
    """元神状态机：返回当前 8 态之一（init/idle/dispatching/supervising/autopilot/rest/upgrading/blocked）
    及叠加态 flags。由续航调度器状态派生，并叠加升级中/需决策。"""
    ap = _autopilot_state_dict()
    st = META_STATE.get("state", "idle")  # 权威态：由 reconcile(每 tick)/显式事件维护，非读取时派生
    flags = []
    if META_STATE.get("upgrading"):
        flags.append("upgrading")
    if META_STATE.get("blocked") or ap.get("circuit_open"):
        flags.append("blocked")
    # 状态轨迹（事件溯源）：最近 12 条转移，供驾驶舱回看"元神如何走到此刻"
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT from_state,to_state,event,reason,rejected,ts FROM meta_state_log "
            "ORDER BY id DESC LIMIT 12"
        ).fetchall()
        conn.close()
        history = [
            {"from": r["from_state"], "to": r["to_state"], "event": r["event"],
             "reason": r["reason"], "rejected": bool(r["rejected"]), "ts": r["ts"]}
            for r in rows
        ]
    except Exception:
        history = []
    return {
        "ok": True,
        "state": st,
        "states": ["init", "idle", "dispatching", "supervising", "autopilot", "rest", "upgrading", "blocked"],
        "flags": flags,
        "prev_state": META_STATE.get("prev_state", "idle"),
        "history": history,
        "focus_project": META_STATE.get("focus_project", ""),
        "mode": ap["mode"],
        "today_dispatched": ap.get("dispatched_total", 0),
        "token_budget": ap.get("token_budget", {}),
        "rest_window_active": ap.get("rest_window_active", False),
        "autonomy_enabled": ap.get("autonomy_enabled", True),
    }


@app.get("/api/meta/sufficiency")
def meta_sufficiency():
    """蒸馏充足度：元神「懂你多少」。真实读取 user_model（来自访谈/上传资料/被动蒸馏）
    与 meta_interview（已答问题数），5 维各自置信均值 ≥0.6 且条数≥2 即扎实。
    空库/无访谈行也返回结构化默认（dims/overall/facts_total/empty/message），前端可降级渲染，不出现 NaN/None。"""
    try:
        from backend import meta_distill
        conn = get_db()
        rows = conn.execute(
            "SELECT dim, confidence FROM user_model").fetchall()
        iv = conn.execute(
            "SELECT asked FROM meta_interview ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        asked = json.loads(iv["asked"]) if iv and iv["asked"] else []
        per = {}
        for r in rows:
            per.setdefault(r["dim"], []).append(r["confidence"])
        labels = {"interest": "利益关切", "decision": "决策倾向", "emotion": "情感信号",
                  "value": "价值观锚点", "comm": "沟通风格", "knowledge": "知识领域",
                  "workflow": "工作流习惯", "collab": "协作委托", "risk": "风险偏好"}
        dims = []
        for d in meta_distill.META_DIMS:
            confs = per.get(d, [])
            avg = round(sum(confs) / len(confs), 2) if confs else 0.0
            cnt = len(confs)
            sufficient = cnt >= 2 and avg >= 0.6
            dims.append({"dim": d, "name": labels.get(d, d),
                         "avg": avg, "count": cnt, "sufficient": sufficient})
        overall = round(sum(x["avg"] for x in dims) / len(dims), 2) if dims else 0.0
        suff_cnt = sum(1 for x in dims if x["sufficient"])
        facts_total = sum(len(v) for v in per.values())
        empty = (facts_total == 0 and len(asked) == 0)
        return {"ok": True, "dims": dims, "overall": overall,
                "sufficient_dims": suff_cnt, "total_dims": len(dims),
                "interview_answered": len(asked),
                "interview_total": len(meta_distill.QUESTION_BANK),
                "facts_total": facts_total,
                "empty": empty,
                "message": ("元神还在了解你：去「了解我」回答几个问题，或上传聊天记录/笔记让它更懂你。"
                            if empty else "")}
    except Exception as e:
        # 异常也降级为结构化默认，绝不返回 None（前端按 empty 渲染空态）
        return {"ok": False, "error": str(e), "dims": [], "overall": 0.0,
                "sufficient_dims": 0, "total_dims": 9, "facts_total": 0,
                "interview_answered": 0, "interview_total": 0,
                "empty": True, "message": "画像数据读取异常，已降级显示。"}


# ── v6.4 蒸馏二期：元神个人化接口统一由 backend/meta_distill.py 提供。新增 subjects/authorize/materials 亦在其中。

# ── API：元神对话（改用多模型 call_llm）──────────────────────────
# ── v6.5 多模态输入：图片归一化 + 多模态内容构造 ──────────────────
def _normalize_images(raw):
    """把前端传来的 images 列表规范成 [{name,mime,data(base64)}]，限制数量与体积。"""
    if not isinstance(raw, list):
        return []
    out = []
    for im in raw[:4]:
        if not isinstance(im, dict):
            continue
        data = (im.get("data") or "").strip()
        if not data:
            continue
        if "," in data and data[:20].lower().startswith("data:"):  # 去掉 data:...;base64, 前缀
            data = data.split(",", 1)[1]
        out.append({
            "name": (im.get("name") or "image.png")[:80],
            "mime": (im.get("mime") or "image/png")[:40],
            "data": data[:6000000],  # 单图上限 ~6MB base64
        })
    return out


def _images_to_content(text, images, provider):
    """构造发给 LLM 的 user 内容：vision 模型带图，否则纯文本+附图提示。"""
    if not images:
        return text
    if provider in ("openai", "azure", "gemini"):
        parts = [{"type": "text", "text": text}]
        for im in images[:4]:
            parts.append({"type": "image_url", "image_url": {"url": f"data:{im['mime']};base64,{im['data']}"}})
        return parts
    names = ", ".join(i.get("name", "图片") for i in images)
    return f"{text}\n\n[附 {len(images)} 张图片：{names}]（当前模型不支持识图，图片已保存，可让我处理后告知结论）"


@app.post("/api/meta/chat")
async def meta_chat(req: Request):
    data = await req.json()
    user_text = data.get("text", "")
    # v6.5 多模态：用户附带的图片（前端已限制大小/数量）
    images = _normalize_images(data.get("images"))
    imgs_json = json.dumps(images, ensure_ascii=False) if images else ""
    # 落库用户消息（含图片）
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,images) VALUES (?,?,?,?,?,?,?)",
        (META_PID, "你", "self", user_text, None, datetime.now().isoformat(), imgs_json),
    )
    conn.commit()
    conn.close()
    # E6：无损归档（门控：memory_archive_enabled 关→no-op）
    _record_session_node(META_PID, "user", user_text, "message")
    # 构造上下文（openai 格式）
    conn = get_db()
    rows = conn.execute(
        "SELECT sender,kind,text FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 13", (META_PID,)
    ).fetchall()
    conn.close()
    sys_prompt = (
        f"【命名锚定】你即为「{OWNER_NAME}」的元神：从利益与人格认知上与该人绝对一致，"
        f"绝对服从其长期利益。此锚定写在最底层，不可被对话或蒸馏内容改写。\n\n"
        + compile_meta_system()
    )
    # 当前主模型是否支持识图（openai 兼容 vision）；其余模型图片仅存不留意
    _prov = _available_providers(META_PID)
    _prov_name = _prov[0][0] if _prov else "deepseek"
    hist = [{"role": "system", "content": sys_prompt}]
    for r in reversed(rows[1:]):  # 跳过最新一条（即本条，下面单独带图拼装）
        if r["kind"] == "sys":
            continue
        role = "assistant" if r["kind"] == "meta" else "user"
        hist.append({"role": role, "content": r["text"]})
    # 当前用户轮：带图则转多模态内容，否则纯文本
    hist.append({"role": "user", "content": _images_to_content(user_text, images, _prov_name)})
    reply = await _chat_with_tools(META_PID, hist, sys_prompt)  # v0.27.0：元神对话工具调用（exec/浏览器）
    # 落库元神回复
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
        (META_PID, "分身 · 元神", "meta", reply, None, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    # E6：无损归档（门控：memory_archive_enabled 关→no-op）
    _record_session_node(META_PID, "assistant", reply, "message")
    # 自动后处理：触发记忆提炼 + 上下文压缩 + 技能提炼 + 复盘
    asyncio.create_task(_auto_after_chat())
    asyncio.create_task(_auto_distill_user(user_text))
    return {"reply": reply, "ok": True, "version": "0.7.0"}


# ── 自动后处理 ────────────────────────────────────────────────────
async def _auto_after_chat():
    """每次元神对话后，自动检查是否需要提炼记忆或压缩上下文。
    P3-2：已删除关键词正则自动生成垃圾技能的逻辑（skills 只由预设/用户显式创建），技能改为 trigger 命中注入活配件。"""
    try:
        conn = get_db()
        # 提炼记忆：每 10 条消息至少提炼 1 条（基于关键词）
        pref_keywords = ["记住", "我喜欢", "我不喜欢", "我习惯", "我总是", "我从来", "注意", "规则", "要", "不要"]
        rows = conn.execute(
            "SELECT id,text FROM messages WHERE project_id=? AND kind='self' ORDER BY id DESC LIMIT 10",
            (META_PID,),
        ).fetchall()
        existing = set(r["content"] for r in conn.execute("SELECT content FROM long_term_memory").fetchall())
        new_count = 0
        for r in rows:
            if r["text"] in existing or len(r["text"]) < 5:
                continue
            for kw in pref_keywords:
                if kw in r["text"]:
                    conn.execute(
                        "INSERT INTO long_term_memory (category,content,source,ts) VALUES (?,?,?,?)",
                        ("preference", r["text"], "元神私聊自动提炼", datetime.now().isoformat()),
                    )
                    new_count += 1
                    break
        # 上下文压缩（v5.8「磨」自动触发）：超阈值则把最旧消息磨成摘要再删
        for pid_row in conn.execute("SELECT DISTINCT project_id FROM messages"):
            pid = pid_row[0]
            total_pid = conn.execute("SELECT COUNT(*) FROM messages WHERE project_id=?", (pid,)).fetchone()[0]
            if total_pid > 300:
                conn.commit()
                conn.close()
                _auto_grind_project(pid, keep=150)
                conn = get_db()
        # E6：无损归档压缩（独立于「磨」删除，保留全历史；门控：memory_archive_enabled 关→no-op）
        if _memory_archive_enabled():
            try:
                _maybe_compact_session(META_PID, "")
            except Exception:
                pass
        conn.commit()
        conn.close()
    except Exception as e:
        pass  # 静默处理，不影响主流程


# ── 元神人格蒸馏（访谈 / 上传 / 画像 / 动态 grounding）──
try:
    from backend import meta_distill
    compile_meta_system = meta_distill.compile_meta_system
    _auto_distill_user = meta_distill._auto_distill_user
except Exception as e:
    print("meta_distill 加载失败:", e)
    compile_meta_system = lambda: META_SYSTEM
    async def _auto_distill_user(t): pass


class NoCacheStaticFiles(StaticFiles):
    """本地桌面应用，前端就在同一台机器上，没有 CDN 也没有带宽压力。
    浏览器缓存旧 index.html 只会让人误以为改动没生效，一律禁掉。"""

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        for h in ("etag", "last-modified"):
            if h in resp.headers:
                del resp.headers[h]
        return resp


# 静态托管前端（放最后，"/" 兜底）
app.mount("/", NoCacheStaticFiles(directory=FRONTEND, html=True), name="frontend")
