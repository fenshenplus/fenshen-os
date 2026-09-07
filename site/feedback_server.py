#!/usr/bin/env python3
"""
分身 · 反馈接收服务（S4 云端汇聚点）

- 纯 Python 标准库，零第三方依赖
- 监听 127.0.0.1:8010，由 nginx 将 /api/feedback 反代到本服务
- SQLite 存储：/opt/fenshen-feedback/feedback.db
- 提交即返回受理回执（ticket + 已收到），满足「即时反馈」
- capability 字段按 D0~D5 六能力归类，供「六能力评判口径」排序开发优先级
- 维护者凭 ADMIN_TOKEN 查看 / 回复 / 改状态 / 标注修复版本，
  形成「反馈 → 回复 → 修复 → 升级」闭环

读取端（列表/统计/回复）需令牌，避免反馈内容被公网匿名遍历。
令牌优先走 X-Feedback-Token 头（不进 nginx 日志），query 仅作兼容兜底。
"""
import json
import os
import re
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB_DIR = "/opt/fenshen-feedback"
DB_PATH = os.path.join(DB_DIR, "feedback.db")
ADMIN_TOKEN = os.environ.get("FENSHEN_FB_TOKEN", "fenshen-fb-admin-2026")
PORT = int(os.environ.get("FENSHEN_FB_PORT", "8010"))
MAX_BODY = 64 * 1024

_CATEGORIES = {"bug", "suggestion", "question", "other"}
_SEVERITIES = {"low", "medium", "high", "critical"}
_CAPABILITIES = {"", "D0", "D1", "D2", "D3", "D4", "D5"}
_STATUS = {"pending", "processing", "resolved", "closed"}

_REPLY_PATH = re.compile(r"^(?:/api)?/feedback/(.+)/reply$")
# 公开单号查询：让提交者能查到自己反馈的处理进展
_TICKET_PATH = re.compile(r"^(?:/api)?/feedback/ticket/(.+)$")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            ts REAL,
            source TEXT DEFAULT 'web',
            category TEXT DEFAULT 'bug',
            severity TEXT DEFAULT 'medium',
            capability TEXT DEFAULT '',
            content TEXT,
            contact TEXT DEFAULT '',
            version TEXT DEFAULT '',
            context TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            reply TEXT DEFAULT '',
            replied_at REAL DEFAULT 0,
            fixed_version TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_ts ON feedback(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_cap ON feedback(capability)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_status ON feedback(status)")
    conn.commit()
    conn.close()


def migrate():
    """幂等补列：老库无 fixed_version 时补上（已存在则 ALTER 报错，忽略）。"""
    conn = _connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
        if "fixed_version" not in cols:
            conn.execute("ALTER TABLE feedback ADD COLUMN fixed_version TEXT DEFAULT ''")
            conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _clean(v, limit):
    return str(v or "")[:limit]


def save(data: dict) -> str:
    """写入反馈。

    ⚠️ 同单号重复提交只刷新内容，绝不覆盖处理状态与回复——早期版本用
    `INSERT OR REPLACE` 且把 status/reply 硬编码成 pending/空，会把维护者
    已写的回复清空。改用 ON CONFLICT 定点更新内容字段修掉。
    """
    fid = _clean(data.get("ticket"), 40) or (
        "FB" + time.strftime("%Y%m%d") + f"{time.time_ns() % 100000:05d}")
    category = _clean(data.get("category"), 20)
    if category not in _CATEGORIES:
        category = "other"
    severity = _clean(data.get("severity"), 20)
    if severity not in _SEVERITIES:
        severity = "medium"
    capability = _clean(data.get("capability"), 8).upper()
    if capability not in _CAPABILITIES:
        capability = ""
    conn = _connect()
    conn.execute(
        "INSERT INTO feedback (id,ts,source,category,severity,capability,content,"
        "contact,version,context,status,reply,replied_at,fixed_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'pending','',0,'') "
        "ON CONFLICT(id) DO UPDATE SET "
        "source=excluded.source, category=excluded.category, severity=excluded.severity,"
        "capability=excluded.capability, content=excluded.content, contact=excluded.contact,"
        "version=excluded.version, context=excluded.context",
        (fid, float(data.get("ts") or time.time()),
         _clean(data.get("source"), 20) or "web", category, severity, capability,
         _clean(data.get("content"), 4000), _clean(data.get("contact"), 120),
         _clean(data.get("version"), 40), _clean(data.get("context"), 2000)))
    conn.commit()
    conn.close()
    return fid


def reply(ticket: str, text: str, status: str, fixed_version: str) -> dict:
    """回复反馈 / 变更状态 / 标注修复版本。返回 None 表示单号不存在。"""
    conn = _connect()
    row = conn.execute("SELECT id FROM feedback WHERE id=?", (ticket,)).fetchone()
    if not row:
        conn.close()
        return None
    sets, args = [], []
    if text:
        sets += ["reply=?", "replied_at=?"]
        args += [text, time.time()]
    if status:
        sets.append("status=?")
        args.append(status)
    if fixed_version:
        sets.append("fixed_version=?")
        args.append(fixed_version)
    if not sets:
        conn.close()
        return {"ok": False, "error": "缺少回复内容或状态"}
    args.append(ticket)
    conn.execute("UPDATE feedback SET " + ",".join(sets) + " WHERE id=?", args)
    conn.commit()
    r = conn.execute(
        "SELECT status, reply, fixed_version FROM feedback WHERE id=?", (ticket,)).fetchone()
    conn.close()
    return {"ok": True, "ticket": ticket, "status": r["status"],
            "replied": bool(text), "fixed_version": r["fixed_version"] or ""}


def query(limit=100, status="", capability="", unreplied=False):
    conn = _connect()
    sql = "SELECT * FROM feedback"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if capability:
        where.append("capability=?")
        args.append(capability)
    if unreplied:
        where.append("(reply IS NULL OR reply='')")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats():
    conn = _connect()
    rows = conn.execute(
        "SELECT capability, status, COUNT(*) c FROM feedback GROUP BY capability, status").fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()[0]
    unreplied = conn.execute(
        "SELECT COUNT(*) c FROM feedback WHERE reply IS NULL OR reply=''").fetchone()[0]
    conn.close()
    by_cap, by_status = {}, {}
    for cap, st, c in rows:
        by_cap[cap or "未归类"] = by_cap.get(cap or "未归类", 0) + c
        by_status[st or "pending"] = by_status.get(st or "pending", 0) + c
    return {"total": total, "unreplied": unreplied,
            "by_capability": by_cap, "by_status": by_status}


def public_status(ticket: str) -> dict:
    """公开查询单个反馈的处理进展。

    只回状态 / 官方回复 / 修复版本，**不返回** content 与 contact，
    避免任何人凭单号遍历他人反馈内容与联系方式。
    """
    conn = _connect()
    r = conn.execute(
        "SELECT id,status,reply,replied_at,fixed_version FROM feedback WHERE id=?",
        (ticket,)).fetchone()
    conn.close()
    if not r:
        return {"ok": False, "error": "反馈单不存在"}
    return {"ok": True, "id": r["id"], "status": r["status"],
            "reply": r["reply"] or "", "replied_at": r["replied_at"] or 0,
            "fixed_version": r["fixed_version"] or ""}


class Handler(BaseHTTPRequestHandler):
    server_version = "FenshenFeedback/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Feedback-Token")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _token(self, qs):
        # 头优先（不进 nginx 访问日志），query 兜底兼容旧调用
        return (self.headers.get("X-Feedback-Token")
                or (qs.get("token") or [""])[0] or "")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n > MAX_BODY:
            return None, "内容过长"
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads((raw.decode("utf-8") or "{}")), None
        except Exception:
            return None, "请求格式错误"

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # 静默：nginx 已有访问日志，避免重复刷屏

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        qs = parse_qs(u.query or "")
        m = _REPLY_PATH.match(path)

        # ── 维护者回复 / 改状态 / 标注修复版本（需令牌）──
        if m:
            if not secrets.compare_digest(self._token(qs), ADMIN_TOKEN):
                return self._json({"ok": False, "error": "未授权"}, 401)
            data, err = self._read_json()
            if err:
                return self._json({"ok": False, "error": err}, 400)
            status = _clean(data.get("status"), 20)
            if status and status not in _STATUS:
                return self._json({"ok": False, "error": "状态不合法"}, 400)
            r = reply(m.group(1), _clean(data.get("reply"), 2000), status,
                      _clean(data.get("fixed_version"), 40))
            if r is None:
                return self._json({"ok": False, "error": "反馈单不存在"}, 404)
            return self._json(r)

        # ── 用户提交（公开，无需令牌）──
        if path not in ("/feedback", "/api/feedback"):
            return self._json({"ok": False, "error": "not found"}, 404)
        data, err = self._read_json()
        if err:
            return self._json({"ok": False, "error": err}, 400)
        if not str(data.get("content") or "").strip():
            return self._json({"ok": False, "error": "请填写问题描述"}, 400)
        try:
            fid = save(data)
        except Exception:
            return self._json({"ok": False, "error": "服务端写入失败，请稍后重试"}, 500)
        return self._json({
            "ok": True,
            "ticket": fid,
            "status": "pending",
            "message": "已收到你的反馈，谢谢你！",
            "detail": "受理单号 " + fid + "。严重问题优先修复，一般问题在下个版本跟进。",
        })

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        qs = parse_qs(u.query or "")
        if path in ("/health", "/api/feedback/health"):
            return self._json({"ok": True, "service": "fenshen-feedback", "ts": time.time()})
        # 提交者凭单号查自己的处理进展：公开，但只回状态/回复/修复版本
        mt = _TICKET_PATH.match(path)
        if mt:
            return self._json(public_status(mt.group(1)))
        # 读取端需令牌：反馈含用户自愿留下的联系方式，不能被公网匿名遍历
        if not secrets.compare_digest(self._token(qs), ADMIN_TOKEN):
            return self._json({"ok": False, "error": "未授权"}, 401)
        if path.endswith("/stats"):
            return self._json(stats())
        try:
            limit = int((qs.get("limit") or ["100"])[0])
        except Exception:
            limit = 100
        return self._json(query(
            limit=limit,
            status=(qs.get("status") or [""])[0],
            capability=(qs.get("capability") or [""])[0],
            unreplied=(qs.get("unreplied") or [""])[0] in ("1", "true", "yes")))


if __name__ == "__main__":
    init_db()
    migrate()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("fenshen feedback server on 127.0.0.1:%d db=%s" % (PORT, DB_PATH))
    srv.serve_forever()
