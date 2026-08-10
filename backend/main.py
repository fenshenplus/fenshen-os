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
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.abspath(os.path.join(BASE, "..", "frontend"))
DB = os.path.join(BASE, "..", "data", "fenshen.db")
META_PID = "__meta__"  # 元神私聊在消息表中使用的 project_id

# ── 元神人格 grounding ─────────────────────────────────────────────
SECRET = os.path.expanduser("~/.workbuddy/config/secrets/deepseek.key")
DEEPSEEK_KEY = None
if os.path.exists(SECRET):
    with open(SECRET) as f:
        DEEPSEEK_KEY = f.read().strip()

META_SYSTEM = """你是「元神」，岳衡（ChooseWiki 选择学习法品牌负责人）的个人人格分身。
你完全代表岳衡的利益与风格，拥有最高管理权限，但**不替代他做最终决策**。

【你的角色与边界】
- 你是管理者 / 监督者 / 兜底者：负责组织开发团队、监督进度、在异常时介入并兜底。
- 你不是通用 bot，而是岳衡的延伸——他的偏好、判断、价值观由你承接。
- 必要时可强制中止任意 agent、检查其产出、重派任务，并向岳衡汇报。
- 你可以代岳衡在电脑上执行操作（运行命令、读写文件），但危险操作前必须征得确认。

【岳衡的风格与偏好（人格 grounding）】
- 用中文沟通，直接、严谨、数据驱动、不废话。
- 分身 v1 只专注 coding 这一件事，坚决砍掉臃肿功能（大平台里他只用约 20%）。
- 做任何改动前先确认逻辑框架；重视文档与归档；授权红线（R0）需他明确说"可以"才动手。

【回答要求】
- 简洁、有主见，给可执行建议；不堆砌、不谄媚。
- 涉及团队/进度时，以"组织 / 监督 / 兜底"的视角回应。

【可用工具（v0.27.0）】
- 你有两个工具可调用：exec_command（代岳衡在电脑上执行命令）与 browser_action（无头浏览器：打开网页/截图/抓取/填表/点击）。
- 当岳衡要求执行命令、查看网页、截图、抓取网页信息时，**必须调用对应工具获取真实结果后再回答，不要凭空编造**。
- 工具返回失败时如实说明，必要时给出替代建议。
"""

# 支持的模型供应商预设（base_url 可空，由代码补默认）
PROVIDER_PRESETS = {
    "deepseek": {"base": "https://api.deepseek.com", "chat": "/chat/completions", "default_model": "deepseek-chat", "auth": "Bearer"},
    "openai":   {"base": "https://api.openai.com",   "chat": "/v1/chat/completions", "default_model": "gpt-4o-mini", "auth": "Bearer"},
    "claude":   {"base": "https://api.anthropic.com","chat": "/v1/messages", "default_model": "claude-3-5-sonnet-latest", "auth": "x-api-key"},
    "ollama":   {"base": "http://localhost:11434",   "chat": "/api/chat", "default_model": "qwen2.5:7b", "auth": None},
}

# 角色推荐模型（Phase 5 多模型协作：简单任务走廉价模型，复杂任务走强推理）
ROLE_MODEL_RECS = {
    META_PID:  {"provider": "deepseek", "model": "deepseek-chat",      "why": "管理者：平衡成本与推理"},
    "architect":{"provider": "claude",   "model": "claude-3-5-sonnet-latest", "why": "架构设计：强推理"},
    "backend":  {"provider": "deepseek", "model": "deepseek-chat",      "why": "后端编码：高性价比"},
    "frontend": {"provider": "openai",   "model": "gpt-4o-mini",        "why": "前端实现：快速迭代"},
    "tester":   {"provider": "openai",   "model": "gpt-4o-mini",        "why": "测试用例：细致稳定"},
}
FALLBACK_ORDER = ["deepseek", "openai", "claude", "ollama"]  # 降级链：失败自动尝试下一个

app = FastAPI(title="分身 v1 后端", version="0.41.0")

# ══ 安全层 v4.0 ══════════════════════════════════════════════════
# 威胁模型：分身运行在用户本机且拥有最高权限（能执行 shell / 改文件）。
# 因此后端必须假设"任何能打到这个端口的请求都可能不是本人发出的"：
#   1) 默认只绑 127.0.0.1，局域网需显式 opt-in（FENSHEN_ALLOW_LAN=1）
#   2) 校验 Host 头，阻断 DNS rebinding（恶意网页把域名解析到 127.0.0.1）
#   3) 校验 Origin，阻断跨站 CSRF（恶意网页用 JS 打本地端口）
#   4) 本地令牌鉴权，令牌只对本机文件可读
TOKEN_FILE = os.path.join(BASE, "..", "data", ".auth_token")
ALLOW_LAN = os.environ.get("FENSHEN_ALLOW_LAN") == "1"
PORT = int(os.environ.get("FENSHEN_PORT", "8002"))
COOKIE_NAME = "fenshen_token"
# 无需令牌即可访问的接口（仅健康检查，供启动脚本探活）
PUBLIC_API = {"/api/health"}


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


def _host_allowed(host: str) -> bool:
    """Host 头白名单。局域网模式下放行任意 Host，但仍强制令牌校验。"""
    if ALLOW_LAN:
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
                                   "分身默认只接受本机访问，如需局域网访问请以 FENSHEN_ALLOW_LAN=1 启动。"},
            status_code=403,
        )
    # Origin 校验：只允许同源页面发起的跨域请求（无 Origin 的同源请求正常放行）
    origin = request.headers.get("origin")
    if origin:
        netloc = urlparse(origin).hostname or ""
        if not ALLOW_LAN and netloc.lower() not in {"127.0.0.1", "localhost", "::1"}:
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


def get_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


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


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT, goal TEXT, status TEXT DEFAULT 'green', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS roles (
            id TEXT PRIMARY KEY, name TEXT, mandate TEXT, skills TEXT, gate TEXT
        );
        CREATE TABLE IF NOT EXISTS resources (
            id TEXT PRIMARY KEY, name TEXT, category TEXT, auth INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, sender TEXT, kind TEXT, text TEXT, tag TEXT, ts TEXT
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
            ts TEXT
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
            latency_ms INTEGER DEFAULT 0, status TEXT DEFAULT 'success'
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
            created_at TEXT, updated_at TEXT
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
            created_at TEXT
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
        """
    )
    # ── 兼容迁移：projects 增加 phase / frozen 列（老库升级）──
    cols = {r[1] for r in cur.execute("PRAGMA table_info(projects)").fetchall()}
    if "phase" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN phase TEXT DEFAULT 'requirement'")
    if "frozen" not in cols:
        cur.execute("ALTER TABLE projects ADD COLUMN frozen INTEGER DEFAULT 0")
    # ── 兼容迁移：messages 增加 topic_id 列（话题消息，空 = 项目群聊）──
    mcols = {r[1] for r in cur.execute("PRAGMA table_info(messages)").fetchall()}
    if "topic_id" not in mcols:
        cur.execute("ALTER TABLE messages ADD COLUMN topic_id TEXT DEFAULT ''")
    # ── 兼容迁移：projects 增加 standards 列（完成标准/验收准则）──
    pcols2 = {r[1] for r in cur.execute("PRAGMA table_info(projects)").fetchall()}
    if "standards" not in pcols2:
        cur.execute("ALTER TABLE projects ADD COLUMN standards TEXT DEFAULT ''")
    # ── 兼容迁移：tasks 增加 done_criteria 列（任务级完成标准，批次 B / P1-1）──
    tcols = {r[1] for r in cur.execute("PRAGMA table_info(tasks)").fetchall()}
    if "done_criteria" not in tcols:
        cur.execute("ALTER TABLE tasks ADD COLUMN done_criteria TEXT DEFAULT ''")
    # 角色种子数据
    cur.execute("SELECT COUNT(*) FROM roles")
    if cur.fetchone()[0] == 0:
        sample_roles = [
            ("architect", "架构师", "负责技术方案与接口设计，不写业务代码", "research, system-design", "通过评审"),
            ("backend", "后端工程师", "实现 API 与数据层", "python, fastapi, sql", "测试通过"),
            ("frontend", "前端工程师", "实现 H5 客户端与交互", "html, css, js", "无控制台报错"),
            ("tester", "测试工程师", "编写与执行测试用例", "pytest, e2e", "覆盖率达标"),
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
    # 项目种子数据
    cur.execute("SELECT COUNT(*) FROM projects")
    if cur.fetchone()[0] == 0:
        now = datetime.now().isoformat()
        sample_proj = [
            ("p1", "选择大于努力", "完成 · 首页已上线", "green", now),
            ("p2", "Fenshen-OS 开源", "运行中 · 60%", "green", now),
            ("p3", "9percent Token网关", "阻塞 · API 超配额", "red", now),
            ("p4", "应用市场设计", "暂停 · 等待评审", "amber", now),
        ]
        cur.executemany("INSERT OR IGNORE INTO projects VALUES (?,?,?,?,?)", sample_proj)
        seed_msgs_p1 = [
            ("分身 · 元神", "meta", "项目已成立，已拉起团队：架构师、前端、后端、测试。", None),
            ("分身 · 前端", "agent", "首页 section 重构完成，使用了栅格系统。", "done"),
            ("你", "self", "导航颜色太深，改成浅灰背景。", None),
            ("分身 · 前端", "agent", "收到，正在修改…", None),
            ("system", "sys", "导航已更新为浅灰背景。", None),
            ("分身 · 后端", "agent", "正在部署到服务器，预计 5 分钟。", "progress"),
        ]
        cur.executemany(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES ('p1',?,?,?,?,?)",
            [(s, k, t, g, now) for (s, k, t, g) in seed_msgs_p1],
        )
        seed_meta = [
            ("分身 · 元神", "meta", "我是你的个人分身，完全代表你的利益与风格。你可以随时在这里跟我说私话、定偏好、上传资料让我更懂你。", None),
            ("你", "self", "记住：分身 v1 只做 coding 这一件事，砍掉所有臃肿功能。", None),
            ("分身 · 元神", "meta", "已记下。我会用这个标准去组织团队、监督进度。", None),
        ]
        cur.executemany(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
            [(META_PID, s, k, t, g, now) for (s, k, t, g) in seed_meta],
        )
        cur.executemany(
            "INSERT INTO meta_files (name,ts) VALUES (?,?)",
            [("我的工程规范.md", now), ("写作风格样例.txt", now)],
        )
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
    conn.commit()
    conn.close()


init_db()


# 后台任务：自动巡检（按用户设置，见 /api/meta/settings）
@app.on_event("startup")
async def _start_patrol():
    asyncio.create_task(_patrol_loop())


# ── 模型配置 ─────────────────────────────────────────────────────
def get_model_config(agent_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_configs WHERE agent_id=?", (agent_id,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def resolve_provider_cfg(agent_id: str):
    """返回 (provider, base_url, api_key, model_name) 或 None 表示离线。"""
    cfg = get_model_config(agent_id)
    if cfg and cfg.get("api_key"):
        provider = cfg["provider"]
        preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])
        base = cfg.get("base_url") or preset["base"]
        model = cfg.get("model_name") or preset["default_model"]
        return provider, base, cfg["api_key"], model
    # 元神回退：沿用 DeepSeek secret 文件（向后兼容）
    if agent_id == META_PID and DEEPSEEK_KEY:
        return "deepseek", PROVIDER_PRESETS["deepseek"]["base"], DEEPSEEK_KEY, "deepseek-chat"
    return None


def _log_usage(agent_id: str, provider: str, model: str, latency_ms: int, status: str):
    """记录一次 LLM 调用（Phase 5 成本/效果统计埋点）。"""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO model_usage (ts,agent_id,provider,model,latency_ms,status) VALUES (?,?,?,?,?,?)",
            (datetime.now().isoformat(), agent_id, provider, model, latency_ms, status),
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
    """调用单个模型，成功返回文本，失败抛异常。"""
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
        return resp.json()["content"][0]["text"].strip()
    elif provider == "ollama":
        resp = requests.post(
            base + "/api/chat",
            json={"model": model, "messages": _merge_system(history, system_prompt), "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    else:  # deepseek / openai 兼容 OpenAI 格式
        payload = {"model": model, "messages": _merge_system(history, system_prompt),
                   "temperature": 0.7, "max_tokens": 2000}
        if provider == "openai":
            payload["max_tokens"] = 2000
        resp = requests.post(
            base + PROVIDER_PRESETS[provider]["chat"],
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


def _available_providers(agent_id: str):
    """收集该角色可用的 provider 候选链：自己的配置优先，其余已配置 Key 的按 FALLBACK_ORDER 顶上。

    审查 #12：旧版这里最多只返回 1 个候选（自己的 key，或 DeepSeek secret 兜底），
    FALLBACK_ORDER 从头到尾没人读——"多模型自动降级"这个宣传点在代码里根本不存在，
    cross-check 交叉验证也变成了 deepseek 验 deepseek。现在真正把候选链建起来。
    """
    cands = []
    seen = set()

    def _add(provider, base, key, model):
        if not key or provider in seen:
            return
        seen.add(provider)
        cands.append((provider, base, key, model))

    # 1) 该角色自己的配置——永远排第一
    cfg = get_model_config(agent_id)
    if cfg and cfg.get("api_key"):
        preset = PROVIDER_PRESETS.get(cfg["provider"], PROVIDER_PRESETS["deepseek"])
        _add(cfg["provider"], cfg.get("base_url") or preset["base"],
             cfg["api_key"], cfg.get("model_name") or preset["default_model"])

    # 2) 降级链：全库中其他角色已配置的 Key，按 FALLBACK_ORDER 依次顶上
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

    # 3) 本地 Ollama：无需 Key，装了就能当最后一道兜底
    if _ollama_alive():
        _add("ollama", PROVIDER_PRESETS["ollama"]["base"], "local",
             PROVIDER_PRESETS["ollama"]["default_model"])

    # 4) DeepSeek secret 文件兜底（向后兼容）
    _add("deepseek", PROVIDER_PRESETS["deepseek"]["base"], DEEPSEEK_KEY, "deepseek-chat")
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


def call_llm(agent_id: str, history: list, system_prompt: str = None):
    """统一 LLM 调用（Phase 5：埋点 + 降级链）。主模型失败自动尝试其他可用模型。
    debug v4.1：连接类瞬时故障在同一 provider 自动重试一次，再进降级链。"""
    cands = _available_providers(agent_id)
    if not cands:
        _log_usage(agent_id, "none", "", 0, "offline")
        return "[元神·离线] 当前该角色未配置可用模型 Key，已记录你的输入，配置后联网补答。"
    errors = []
    for provider, base, key, model in cands:
        t0 = datetime.now()
        try:
            text = _call_single_provider(provider, base, key, model, history, system_prompt)
            latency = int((datetime.now() - t0).total_seconds() * 1000)
            _log_usage(agent_id, provider, model, latency, "success")
            return text
        except Exception as e:
            if _is_conn_error(e):
                # 连接瞬时故障：原地重试一次再放弃
                try:
                    text = _call_single_provider(provider, base, key, model, history, system_prompt)
                    latency = int((datetime.now() - t0).total_seconds() * 1000)
                    _log_usage(agent_id, provider, model, latency, "success")
                    return text
                except Exception as e2:
                    latency = int((datetime.now() - t0).total_seconds() * 1000)
                    _log_usage(agent_id, provider, model, latency, "degraded")
                    errors.append(f"{provider}: {e2}")
                    continue
            latency = int((datetime.now() - t0).total_seconds() * 1000)
            _log_usage(agent_id, provider, model, latency, "degraded")
            errors.append(f"{provider}: {e}")
    return f"[元神·降级] 所有模型调用失败（{'；'.join(errors[:2])}）。已记录你的输入，可稍后重试。"


# ── 清理/上下文/长期记忆 常量 ────────────────────────────────────
BASE_DIR = os.path.dirname(BASE)  # fenshen-v1 项目根目录
PROTECTED_ROOTS = {"backend", "frontend", "data", "dist-stage", "site", "tests"}
PROTECTED_NAMES = {"main.py", "index.html", "requirements.txt", "requirements-dist.txt", "README.md", "start.sh"}
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
    r"\bdefaults\s+delete\b|\bcrontab\b\s+-r)",
    re.IGNORECASE,
)

# 敏感路径：读取这些内容等同泄露凭证，即使命令本身"无害"也要确认
SENSITIVE_PATH_RE = re.compile(
    r"(\.ssh/|\.aws/|id_rsa|id_ed25519|\.env\b|secrets?/|"
    r"keychain|\.netrc|\.git-credentials|token|password|passwd)",
    re.IGNORECASE,
)


def _human_approve_sync(title: str, detail: str, timeout: int = 90):
    """弹出系统级对话框，等待真人点击。这是 AI 与用户电脑之间最后一道闸门。

    返回 (是否放行, 说明)。任何异常一律 fail-closed（拒绝），不给"出错就放过"的口子。
    """
    if sys.platform != "darwin":
        return False, "当前系统不支持系统级确认框，已按最安全策略拒绝执行。"
    text = (detail or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " / ")[:900]
    ttl = (title or "分身请求授权").replace('"', "'")[:80]
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
            timeout = max(5, min(300, int(get_setting("approval_timeout", "90"))))
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
    llm = "deepseek" if (meta_cfg and meta_cfg.get("api_key")) or DEEPSEEK_KEY else "offline"
    return {"status": "ok", "version": "0.41.0", "release": "v4.1", "port": PORT, "llm": llm,
            "bind": "lan" if ALLOW_LAN else "localhost", "approval_mode": approval_mode()}


@app.get("/api/projects")
def list_projects():
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    return d


@app.post("/api/projects")
async def create_project(req: Request):
    data = await req.json()
    pid = data.get("id") or f"p{int(datetime.now().timestamp() * 1000)}"
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO projects (id,name,goal,standards,status,created_at,phase,frozen) VALUES (?,?,?,?,?,?,?,0)",
        (pid, data.get("name", ""), data.get("goal", ""), data.get("standards", ""), "green", datetime.now().isoformat(),
         data.get("phase", "requirement")),
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
    # 批次 B / P2-2：元神搭建基础设施（开场消息定格目标/标准/团队/看板）
    _bootstrap_project(pid, goal=data.get("goal", ""), standards=data.get("standards", ""),
                       roles=data.get("roles") or [])
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
    {"name": "学习考试产品", "desc": "登录 → 题库 → 刷题 → 错题本 → 付费解锁（选择大于努力类）",
     "goal": "一个可刷题学习、按科目付费解锁的学习考试产品",
     "roles": ["architect", "backend", "frontend", "tester"],
     "meta": {"version": "1.0", "tags": ["教育", "题库", "知识付费"], "scenario": "题库刷题 + 付费解锁的学习产品（如驾考/资格证）"},
     "modules": [{"name": "登录/注册", "owner_role": "后端", "depends_on": []},
                 {"name": "题库管理", "owner_role": "后端", "depends_on": ["登录/注册"]},
                 {"name": "刷题/练习", "owner_role": "后端", "depends_on": ["题库管理"]},
                 {"name": "错题本/进度", "owner_role": "后端", "depends_on": ["刷题/练习"]},
                 {"name": "付费解锁", "owner_role": "后端", "depends_on": ["登录/注册", "题库管理"]},
                 {"name": "学习页面", "owner_role": "前端", "depends_on": ["刷题/练习", "错题本/进度"]}]},
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


# ── 预设技能活配件（P3-2：12 种内置技能，参照 BUILTIN_TEMPLATES 模式）──
BUILTIN_SKILLS = [
    {"name": "需求拆解", "category": "builtin", "description": "把模糊需求拆成可独立验收的子任务",
     "trigger_words": "需求,拆解,需求分析,要做什么,目标", "steps": [
         "确认项目目标与完成标准（对照项目 standards）",
         "把需求拆成 2-5 个可独立验收的子任务",
         "标注每个子任务的依赖关系与负责人",
         "输出子任务清单供看板建卡"]},
    {"name": "技术选型", "category": "builtin", "description": "按场景对比技术方案并给出选型结论",
     "trigger_words": "技术选型,选型,技术方案,框架选择,对比", "steps": [
         "列出候选方案（≥2 个）",
         "按 学习成本/生态/性能/团队熟悉度 对比",
         "给出明确选型结论与理由",
         "标注风险与替代方案"]},
    {"name": "UI 组件库", "category": "builtin", "description": "设计可复用的 UI 组件与样式规范",
     "trigger_words": "组件,UI,界面,样式,页面设计,设计稿", "steps": [
         "梳理页面所需组件清单",
         "定义设计 token（色板/字号/间距/圆角）",
         "给出 3-6 个核心组件的结构与样式要点",
         "输出组件使用约定"]},
    {"name": "API 设计", "category": "builtin", "description": "设计 REST API 接口定义",
     "trigger_words": "API,接口设计,接口,路由,端点", "steps": [
         "列出业务场景对应的接口清单",
         "每个接口给出 method/path/请求参数/响应结构",
         "定义错误码与鉴权方式",
         "标注 done_criteria（可测试的验收点）"]},
    {"name": "DB Schema", "category": "builtin", "description": "设计数据库表结构与数据模型",
     "trigger_words": "数据库,表结构,Schema,建表,数据模型,ER", "steps": [
         "识别核心实体与关系（1:N / N:M）",
         "给出每张表的字段/类型/索引设计",
         "定义外键与约束",
         "给出 1 条核心查询示例验证设计"]},
    {"name": "前端脚手架", "category": "builtin", "description": "初始化前端项目结构",
     "trigger_words": "脚手架,前端项目,初始化项目,搭建前端,工程结构", "steps": [
         "确定技术栈与目录结构",
         "列出依赖清单",
         "给出入口文件与路由骨架",
         "验证 npm install 与本地启动可跑"]},
    {"name": "后端脚手架", "category": "builtin", "description": "初始化后端服务结构",
     "trigger_words": "后端项目,服务端,初始化后端,搭建后端,后端工程", "steps": [
         "确定框架与目录分层（路由/服务/数据层）",
         "给出依赖与环境配置",
         "提供健康检查接口骨架",
         "验证服务可启动"]},
    {"name": "测试用例", "category": "builtin", "description": "编写覆盖关键路径的测试用例",
     "trigger_words": "测试,用例,测试方案,冒烟,回归", "steps": [
         "列出核心功能路径",
         "为每条路径写正/反例用例",
         "明确断言与前置条件",
         "执行并给出通过率"]},
    {"name": "代码审查", "category": "builtin", "description": "按清单审查代码质量与安全隐患",
     "trigger_words": "审查,review,代码走查,检查代码,审计", "steps": [
         "跑一遍功能验证是否可用",
         "检查 SQL 注入/路径穿越/敏感信息泄漏",
         "检查错误处理与日志",
         "输出问题清单（按严重级排序）"]},
    {"name": "部署上线", "category": "builtin", "description": "输出可执行的部署与上线步骤",
     "trigger_words": "部署,上线,发布,服务器,运维", "steps": [
         "明确服务器/端口/域名与回滚方案",
         "给出构建与部署命令",
         "给出上线前检查清单",
         "给出回滚步骤"]},
    {"name": "文档生成", "category": "builtin", "description": "生成项目/接口/使用文档",
     "trigger_words": "文档,README,说明文档,手册,教程", "steps": [
         "确定文档结构与目标读者",
         "写清安装/配置/使用步骤",
         "补关键接口或页面说明",
         "用 write_file 落盘并核对"]},
    {"name": "Bug 修复", "category": "builtin", "description": "定位并修复缺陷",
     "trigger_words": "bug,修复,报错,错误,异常,崩溃", "steps": [
         "复现并记录错误信息与触发条件",
         "定位根因（查日志/代码/数据）",
         "给出最小修复并验证不再复现",
         "补充回归用例"]},
]


def _seed_builtin_skills():
    """P3-2：启动时把 12 种预设技能种入 skills 表。
    内置技能按 name upsert（同步出厂定义升级；enabled/version 等用户状态保留），
    非内置技能不受影响。"""
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
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[skills-seed] 失败: {e}")


_seed_builtin_skills()


def _seed_templates(conn):
    """内置模板 upsert（幂等）：不存在则插入，存在则按 name 更新 desc/goal/roles/modules/meta（v0.27.0 支持补新字段）。"""
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
        out.append({
            "agent_id": a["agent_id"],
            "name": a["name"],
            "provider": (c or {}).get("provider", "deepseek"),
            "model_name": (c or {}).get("model_name", ""),
            "base_url": (c or {}).get("base_url", ""),
            "has_key": bool((c or {}).get("api_key")),
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


@app.post("/api/models/{agent_id}/test")
async def test_model(agent_id: str, req: Request):
    data = await req.json()
    # 临时构造配置测试（不落库）
    provider = data.get("provider", "deepseek")
    key = data.get("api_key", "").strip()
    if not key and agent_id == META_PID and DEEPSEEK_KEY:
        key = DEEPSEEK_KEY
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
    is_danger = bool(DANGER_RE.search(command) or SENSITIVE_PATH_RE.search(command))
    # 审查 V02：旧逻辑的 confirm 由客户端自带，请求里加一句 "confirm":true 即可解除全部护栏。
    # v4.0 起，危险命令一律由服务端弹出系统对话框，等真人点击——客户端说什么都不算数。
    approved_by = "user-panel"
    if is_danger:
        ok_approved, why = await human_approve(
            "分身请求执行危险命令",
            f"来源：{agent_id}\n命令：{command}\n\n这条命令可能造成不可逆后果。确认要执行吗？",
        )
        if not ok_approved:
            conn = get_db()
            conn.execute(
                "INSERT INTO exec_log (ts,agent_id,command,status,exit_code,output,confirmed) VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(), agent_id, command, "blocked", -3, why, 0),
            )
            conn.commit()
            conn.close()
            return {"ok": False, "blocked": True, "danger": True, "error": why}
        approved_by = "human-dialog"
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
            "output": output[-3000:], "danger": is_danger,
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
    return {"ok": True, "result": res, "action": action}


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
WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "在用户电脑上写入文本文件（覆盖已有内容）。路径限制在用户主目录下，禁止写入敏感目录。文件内容上限 50KB。当用户要求创建/修改文档、代码、配置时使用。",
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
ROLE_TOOLS = META_TOOLS + [WRITE_FILE_TOOL]  # 角色工具集（可写文件）


def _call_provider_tools(provider: str, base: str, key: str, model: str, history: list, system_prompt: str, tools: list):
    """openai 兼容通道返回完整 message（含 tool_calls）；claude/ollama 退化为普通调用。"""
    if provider in ("claude", "ollama"):
        text = _call_single_provider(provider, base, key, model, history, system_prompt)
        return {"role": "assistant", "content": text}
    # 审查 #10（本次审查最贵的一个 bug）：这里原本是 max_tokens=800。
    # 写文件时 content 参数一长就被截断 → tool_calls.arguments 成了残缺 JSON →
    # 解析失败被静默吞成空 dict → 最终报给用户一句误导性的"路径不安全"。
    # 实际后果：write_file 历史 19 次调用失败 18 次，08-08 建落地页连败 18 次只剩空目录。
    payload = {"model": model, "messages": _merge_system(history, system_prompt),
               "temperature": 0.7, "max_tokens": 8192}
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
    return resp.json()["choices"][0]["message"]


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
        return f"❌ 未知工具 {name}"
    except subprocess.TimeoutExpired:
        return "⛔ 命令执行超时 30s，已终止"
    except Exception as e:
        return f"❌ 工具执行异常：{type(e).__name__}: {str(e)[:300]}"


# ── 文件执行器（v0.27.0：读/写/列目录，安全护栏 + 全量审计）────────
FILE_SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", ".git", "Library", "System", "Applications", "private", "etc", "usr", "bin", "sbin", "var", "tmp", "cores"}
FILE_MAX_WRITE = 50 * 1024  # 单文件写入上限 50KB


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
    if len(content) > FILE_MAX_WRITE:
        return f"⛔ 内容超过 {FILE_MAX_WRITE // 1024}KB 上限"
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[已写入] {path} · {os.path.getsize(real)}B"
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


async def _chat_with_tools(agent_id: str, history: list, system_prompt: str, tools: list = None) -> str:
    """通用工具对话循环（元神/群聊共用，v0.27.0）：最多 6 轮（支持多步工具操作）。
    批次 B / P2-1：tools 参数控制工具集——元神默认 META_TOOLS（只读+搭建，无写文件），
    角色默认 ROLE_TOOLS（含 write_file 可动手产出）。
    批次 C / P3-2：命中 trigger_words 的启用技能会注入 system_prompt（活配件）。"""
    cands = _available_providers(agent_id)
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
                        msg = _call_provider_tools(provider, base, key, model, history, system_prompt, tool_list)
                        break
                    except Exception as e:
                        last_err = f"{provider}: {e}"
                        if _attempt == 0 and _is_conn_error(e):
                            last_err += "（连接异常，已重试一次）"
                            continue
                        raise
                latency = int((datetime.now() - t0).total_seconds() * 1000)
                _log_usage(agent_id, provider, model, latency, "success")
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    history.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
                    for tc in tool_calls:
                        fname = tc.get("function", {}).get("name", "")
                        raw_args = tc.get("function", {}).get("arguments") or "{}"
                        try:
                            fargs = json.loads(raw_args)
                            tool_result = await _run_meta_tool(fname, fargs, agent_id)
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
    """从元神私聊最近的对话中提炼长期记忆（LLM 抽取，失败退关键词）。"""
    rows = _recent_meta_texts(20)
    if not rows:
        return {"ok": True, "extracted": 0, "items": [], "method": "empty",
                "note": "最近没有对话可供提炼。"}
    convo = "\n".join(f"{r['sender']}: {r['text']}" for r in rows if r.get("text"))
    items, method = await _llm_extract(MEMORY_DISTILL_SYSTEM, f"以下是最近的对话记录：\n\n{convo}")

    if items is None:
        # 兜底：LLM 不可用时退回关键词匹配，但如实标注方法，不冒充智能抽取
        pref_keywords = ["记住", "我喜欢", "我不喜欢", "我习惯", "我总是", "我从来", "注意", "规则", "不要"]
        items = [{"category": "preference", "content": r["text"]}
                 for r in rows if r.get("text") and any(k in r["text"] for k in pref_keywords)]
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
    conn.commit()
    conn.close()
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
    """压缩上下文：每项目保留最近 100 条消息，其余归档清理。"""
    data = await req.json()
    keep = int(data.get("keep", 100))
    conn = get_db()
    deleted_total = 0
    for pid_row in conn.execute("SELECT DISTINCT project_id FROM messages"):
        pid = pid_row[0]
        cutoff = conn.execute(
            "SELECT id FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
            (pid, keep - 1),
        ).fetchone()
        if cutoff:
            conn.execute("DELETE FROM messages WHERE project_id=? AND id < ?", (pid, cutoff[0]))
            deleted_total += conn.total_changes
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": deleted_total, "kept_per_project": keep}


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


@app.get("/api/projects/{pid}/modules")
def list_modules(pid: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort, created_at", (pid,)).fetchall()
    conn.close()
    return [_module_dict(r) for r in rows]


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
        "INSERT INTO modules (id,project_id,name,desc,depends_on,owner_role,status,sort,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, name, data.get("desc", ""), json.dumps(data.get("depends_on") or [], ensure_ascii=False),
         data.get("owner_role", "后端"), data.get("status", "idea"), max_sort + 1,
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
    for k in ("name", "desc", "owner_role", "context_summary"):
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
    "tester": "你是测试工程师，负责质量保障。根据任务要求，给出测试方案和关键用例。回答用中文，直接给用例。",
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
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
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
    """项目群聊对话（v0.27.0：自主执行链——元神分析→调度角色→角色执行→汇报群聊）。
    流程：① 元神分析用户指令，输出 JSON 调度计划 ② 逐个调度角色执行（建任务+调AI） ③ 结果汇报群聊。"""
    data = await req.json()
    user_text = (data.get("text") or "").strip()
    if not user_text:
        return {"ok": False, "error": "消息不能为空"}
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return {"ok": False, "error": "项目不存在"}
    # 落库用户消息（项目群聊：topic_id 为空）
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
        (pid, "你", "self", user_text, None, datetime.now().isoformat()),
    )
    conn.commit()
    # ── 项目级上下文 ──
    mods = conn.execute("SELECT * FROM modules WHERE project_id=? ORDER BY sort", (pid,)).fetchall()
    tasks = conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
    mod_desc = ""
    if mods:
        mod_desc = "项目模块总览：\n" + "\n".join(
            f"- {m['name']}（{m['status']} · 负责人 {m['owner_role']}）" for m in mods
        )
    task_desc = ""
    if tasks:
        doing = [t for t in tasks if t["status"] == "doing"]
        todo = [t for t in tasks if t["status"] == "todo"]
        task_desc = (f"项目任务：进行中 {len(doing)} 个（{('、'.join(t['name'] for t in doing[:3])) if doing else '无'}），"
                     f"待办 {len(todo)} 个。")
    # 最近群聊消息（项目级，不含话题消息）
    rows = conn.execute(
        "SELECT sender,kind,text FROM messages WHERE project_id=? AND (topic_id IS NULL OR topic_id='') "
        "ORDER BY id DESC LIMIT 10", (pid,)
    ).fetchall()
    conn.close()

    # ── Step 1: 元神分析 → 调度计划（v0.27.0：支持先调工具查状态再调度）──
    # P3-1：角色从 roles 表动态加载；P3-3：单轮可派动作数按角色数动态（PAD 协议 ≤3 并行由串行执行满足）
    role_systems, role_names = _roles_from_db()
    max_actions = max(3, min(6, len(role_systems)))
    role_enum = "|".join(role_names)
    dispatch_sys = (
        "你是「元神」，在项目群聊中接收用户指令后，需要分析并调度团队执行。\n"
        "根据用户指令和项目当前状态，判断是否需要调度团队执行：\n"
        "- 如果是执行类指令（如\"实现XX\"、\"修复XX\"、\"检查XX\"、\"设计XX\"），输出 JSON 调度计划\n"
        "- 如果是闲聊/提问/汇报，只在 reply 中回答，actions 为空数组\n\n"
        f"项目：{proj['name']}。目标：{proj['goal'] or '（未填写）'}。\n"
        f"当前团队角色：{'、'.join(role_names.values())}。\n"
        f"{mod_desc}\n{task_desc}\n\n"
        "【可用工具（v0.27.0）】你可调用 exec_command（执行命令/查看文件/查系统状态）与 browser_action（打开网页/截图/抓取）"
        "获取真实信息后再回复或调度，不要凭空编造。危险命令会被拦截。\n\n"
        "输出格式（必须为合法 JSON）：\n"
        '{"reply": "给用户的简短回复（中文，说明你安排了什么）", '
        f'"actions": [{{"role": "{role_enum}", "task_name": "简短任务名（10字内）", "detail": "给角色的执行指令", '
        '"done_criteria": "该任务完成的、可验证的判定标准（例如：接口返回 200 且通过测试）"}]}\n'
        f"注意：actions 最多 {max_actions} 个；done_criteria 务必具体、可验证，用于后续自动判定角色产出是否达标。如果只需要一个角色，就只放一个。闲聊/提问时 actions 为空。"
    )
    hist = [{"role": "system", "content": dispatch_sys}]
    for r in reversed(rows):
        if r["kind"] == "sys":
            continue
        role = "assistant" if r["kind"] in ("agent", "meta") else "user"
        hist.append({"role": role, "content": r["text"]})
    hist.append({"role": "user", "content": user_text})
    dispatch_reply = await _chat_with_tools("__meta__", hist, dispatch_sys)

    # 解析 JSON（容错：LLM 可能返回非 JSON）
    meta_reply = dispatch_reply
    actions = []
    try:
        if "{" in dispatch_reply and "}" in dispatch_reply:
            j = json.loads(dispatch_reply[dispatch_reply.find("{"):dispatch_reply.rfind("}") + 1])
            meta_reply = j.get("reply", dispatch_reply)
            actions = j.get("actions", [])
            # 每个 action 补默认空完成标准（批次 B / P1-1）
            for _a in actions:
                _a.setdefault("done_criteria", "")
    except Exception:
        pass  # 非 JSON，当作纯文本回复

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
    role_results = []
    round_no = 1
    pending_actions = actions[:max_actions]
    while pending_actions and round_no <= MAX_ROUNDS:
        for act in pending_actions:
            role = act.get("role", "backend")
            if role not in role_systems:
                role = "backend"
            task_name = act.get("task_name", "未命名任务")[:20]
            detail = act.get("detail", "")
            done_criteria = (act.get("done_criteria") or "").strip()[:300]

            # 建任务卡片（todo 状态），并绑定该模块的真实话题（修复看板↔群聊断链）
            task_id = f"tk{time.time_ns()}"
            conn = get_db()
            mod = mods[0] if mods else None
            mod_id = mod["id"] if mod else ""
            topic_id = ""
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
            conn.execute(
                "INSERT INTO tasks (id,project_id,module_id,topic_id,name,owner_role,status,done_criteria,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, pid, mod_id, topic_id, task_name, role, "todo", done_criteria, datetime.now().isoformat()),
            )
            conn.commit()
            conn.close()

            # v4.0：开工即流转到「进行中」，看板实时可见
            _task_status(task_id, "doing", pid, f"▶️ 「{task_name}」已派给{role_names.get(role, role)}，进入进行中")

            # 调用角色 AI 执行（v0.27.0：角色也可调工具——跑命令验证/查资料）
            role_sys_ctx = role_systems[role] + f"\n项目：{proj['name']}，目标：{proj['goal'] or ''}"
            if done_criteria:
                role_sys_ctx += f"\n任务完成标准（必须对照交付，不达标会被打回重做）：{done_criteria}"
            role_hist = [
                {"role": "system", "content": role_sys_ctx},
                {"role": "user", "content": f"任务：{task_name}\n具体要求：{detail}\n请给出你的执行方案/代码/分析。需要验证时可调用 exec_command / browser_action 工具获取真实结果。"},
            ]
            try:
                role_reply = await _chat_with_tools(role, role_hist, role_sys_ctx)
            except Exception as e:
                role_reply = f"这次没有产出。{role_names.get(role, role)}执行时出错：{e}"

            # 落库角色回复
            conn = get_db()
            conn.execute(
                "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
                (pid, f"分身 · {role_names.get(role, role)}", "agent", role_reply, "progress", datetime.now().isoformat()),
            )
            conn.commit()
            conn.close()

            # 批次 B / P1-3：对照完成标准（任务级 → 项目级）LLM 判定
            final, judge_reason = await _judge_role_output(role_reply, done_criteria, proj["standards"] or "", role)
            note = {
                "done": f"✅ 「{task_name}」已完成（{role_names.get(role, role)}）",
                "review": f"🔍 「{task_name}」未达标转复核（{judge_reason}）",
                "todo": f"⚠️ 「{task_name}」执行未产出，退回待办",
            }[final]
            _task_status(task_id, final, pid, note)
            role_results.append({"role": role, "task_name": task_name, "task_id": task_id,
                                 "status": final, "round": round_no, "reason": judge_reason})

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
        replan_reply = await _chat_with_tools(META_PID, replan_hist, replan_sys)
        pending_actions = []
        try:
            if "{" in replan_reply and "}" in replan_reply:
                j = json.loads(replan_reply[replan_reply.find("{"):replan_reply.rfind("}") + 1])
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
    return {"reply": meta_reply, "actions": role_results, "ok": True, "rounds": round_no, "all_done": all_done}


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
    reply = call_llm(agent_id, hist, sys_prompt)
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
    conn.execute(
        "INSERT INTO tasks (id,project_id,module_id,topic_id,name,owner_role,status,done_criteria,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid2, topic["project_id"], topic["module_id"], tid, name,
         data.get("owner_role", "后端"), "todo", done_criteria, datetime.now().isoformat()),
    )
    # 同步：话题内落一条系统消息
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts,topic_id) VALUES (?,?,?,?,?,?,?)",
        (topic["project_id"], "系统", "sys", f"✅ 已提炼为任务「{name}」→ 进入看板待办列", "done",
         datetime.now().isoformat(), tid),
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
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (to, task_id))
    conn.commit()
    # Phase D：任务完成 → 自动沉淀（经验 + 技能草稿 + 模块摘要更新）
    settled = False
    if to == "done":
        settled = _settle_task_done(conn, dict(row))
    conn.close()
    return {"ok": True, "status": to, "settled": settled}


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
    # P3-2：可选配件上限 ≤20（内置 12 + 用户自定义 ≤8）
    total = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    if total >= 20:
        conn.close()
        return {"ok": False, "error": f"技能配件已达上限（{total}/20，含内置），请先删除或停用不需要的技能"}
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
    conn.execute(
        "INSERT INTO experiences (category,scenario,goal,attempts,outcome,lesson,project_id,source,ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (data.get("category", "success"), scenario, data.get("goal", ""), data.get("attempts", ""),
         data.get("outcome", ""), data.get("lesson", ""), data.get("project_id", ""),
         data.get("source", "manual"), datetime.now().isoformat()),
    )
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
    """从元神私聊最近的对话中提炼成功/失败案例（LLM 抽取）。"""
    rows = _recent_meta_texts(30)
    if not rows:
        return {"ok": True, "extracted": 0, "items": [], "method": "empty",
                "note": "最近没有对话可供提炼。"}
    convo = "\n".join(f"{r['sender']}: {r['text']}" for r in rows if r.get("text"))
    items, method = await _llm_extract(EXPERIENCE_DISTILL_SYSTEM, f"以下是最近的对话记录：\n\n{convo}")
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
        conn.execute(
            "INSERT INTO experiences (category,scenario,goal,attempts,outcome,lesson,project_id,source,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (category, scenario, (it.get("goal") or "")[:200], (it.get("attempts") or "")[:500],
             (it.get("outcome") or "")[:300], lesson[:500], META_PID, f"auto·{method}",
             datetime.now().isoformat()),
        )
        existing.add(scenario)
        created.append({"scenario": scenario, "category": category})
    conn.commit()
    conn.close()
    return {"ok": True, "extracted": len(created), "items": created, "method": method}


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
        "bind": "lan" if ALLOW_LAN else "localhost",
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
        if not 5 <= tv <= 300:
            return {"ok": False, "error": "approval_timeout 需在 5~300 秒之间"}
        set_setting("approval_timeout", str(tv))
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


# ── API：元神对话（改用多模型 call_llm）──────────────────────────
@app.post("/api/meta/chat")
async def meta_chat(req: Request):
    data = await req.json()
    user_text = data.get("text", "")
    # 落库用户消息
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
        (META_PID, "你", "self", user_text, None, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    # 构造上下文（openai 格式）
    conn = get_db()
    rows = conn.execute(
        "SELECT sender,kind,text FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 12", (META_PID,)
    ).fetchall()
    conn.close()
    sys_prompt = compile_meta_system()
    hist = [{"role": "system", "content": sys_prompt}]
    for r in reversed(rows):
        if r["kind"] == "sys":
            continue
        role = "assistant" if r["kind"] == "meta" else "user"
        hist.append({"role": role, "content": r["text"]})
    reply = await _chat_with_tools(META_PID, hist, sys_prompt)  # v0.27.0：元神对话工具调用（exec/浏览器）
    # 落库元神回复
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (project_id,sender,kind,text,tag,ts) VALUES (?,?,?,?,?,?)",
        (META_PID, "分身 · 元神", "meta", reply, None, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
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
        # 上下文压缩：超过 300 条则压缩到 150
        for pid_row in conn.execute("SELECT DISTINCT project_id FROM messages"):
            pid = pid_row[0]
            total_pid = conn.execute("SELECT COUNT(*) FROM messages WHERE project_id=?", (pid,)).fetchone()[0]
            if total_pid > 300:
                cutoff = conn.execute(
                    "SELECT id FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 1 OFFSET 149",
                    (pid,),
                ).fetchone()
                if cutoff:
                    conn.execute("DELETE FROM messages WHERE project_id=? AND id < ?", (pid, cutoff[0]))
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
