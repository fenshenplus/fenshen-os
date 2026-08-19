"""
分身 · 移动端中转 WebSocket 桥接

手机 App（Flutter）←WebSocket(/ws)→ 本模块 → 引擎本地 SQLite（同进程直接读写）

说明：引擎的 /api/projects 等 REST 接口需要登录态，中转作为同进程内组件
直接读写作者的 SQLite（data/fenshen.db），绕过 HTTP 鉴权，避免 401，也更高效。

职责：
- 认证（共享 token，默认 fenshen_mobile_v1，可用 FENSHEN_RELAY_TOKEN 覆盖）
- list_rooms：把 projects 表映射成移动端 Room[]（含最近消息历史）
- message：直接 INSERT 到 messages 表（引擎自治循环消费后产生元神回复）
- stop_agent：直接 UPDATE projects 置 autonomy_paused=1
- 轮询回推：新消息(new_message) + 状态变化(room_updated)
"""
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

RELAY_TOKEN = os.environ.get("FENSHEN_RELAY_TOKEN", "fenshen_mobile_v1")
POLL_INTERVAL = float(os.environ.get("FENSHEN_RELAY_POLL", "2.5"))
MAX_HISTORY = int(os.environ.get("FENSHEN_RELAY_HISTORY", "50"))

# ── SQLite 路径（与 backend/main.py 的 DB 一致）──
_BASE = os.path.dirname(os.path.abspath(__file__))          # .../backend
_DB = os.path.abspath(os.path.join(_BASE, "..", "data", "fenshen.db"))
if getattr(sys, "_MEIPASS", None):
    _DB = os.path.join(os.path.expanduser("~"), ".fenshen", "fenshen.db")


def _conn():
    return sqlite3.connect(_DB, timeout=10)


def _load_projects():
    c = _conn()
    try:
        rows = c.execute(
            "SELECT id,name,goal,autonomy_paused,created_at "
            "FROM projects ORDER BY created_at DESC").fetchall()
        out = []
        for pid, name, goal, paused, created in rows:
            t = c.execute(
                "SELECT COUNT(*), SUM(status='done') FROM tasks WHERE project_id=?",
                (pid,)).fetchone()
            total = t[0] or 0
            done = t[1] or 0
            percent = round(done * 100 / total) if total else 0
            out.append({
                "id": pid, "name": name, "goal": goal or "",
                "autonomy_paused": paused,
                "created_at": created or "", "updated_at": "",
                "completion": {"percent": percent, "total": total, "done": done},
            })
        return out
    finally:
        c.close()


def _load_messages(pid, limit=MAX_HISTORY):
    c = _conn()
    try:
        rows = c.execute(
            "SELECT id,project_id,sender,text,ts FROM messages "
            "WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, limit)).fetchall()
        return [{"id": r[0], "project_id": r[1], "sender": r[2],
                 "text": r[3], "ts": r[4]} for r in reversed(rows)]
    finally:
        c.close()


def _insert_message(pid, text):
    c = _conn()
    try:
        c.execute(
            "INSERT INTO messages (project_id,sender,kind,text,tag,ts,topic_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, "你", "self", text, None, datetime.now().isoformat(), ""))
        c.commit()
    finally:
        c.close()


def _pause_project(pid):
    c = _conn()
    try:
        c.execute("UPDATE projects SET autonomy_paused=1 WHERE id=?", (pid,))
        c.commit()
    finally:
        c.close()


# ── 映射：引擎结构 → 移动端结构 ──
def _sender_type(sender: str) -> str:
    s = (sender or "").strip()
    if s in ("你", "我", "user", "User"):
        return "user"
    if s in ("系统", "system", "System"):
        return "system"
    return "agent"


def _to_mobile_message(m: dict) -> dict:
    return {
        "id": str(m.get("id")),
        "roomId": m.get("project_id"),
        "sender": {
            "name": m.get("sender") or "系统",
            "type": _sender_type(m.get("sender")),
        },
        "content": m.get("text") or "",
        "timestamp": m.get("ts") or datetime.now().isoformat(),
        "type": "text",
    }


def _status_of(p: dict) -> str:
    completion = p.get("completion") or {}
    percent = completion.get("percent", 0) if isinstance(completion, dict) else 0
    if p.get("autonomy_paused"):
        return "paused"
    if percent >= 100:
        return "completed"
    return "active"


def _to_mobile_room(p: dict, messages=None) -> dict:
    return {
        "id": p.get("id"),
        "name": p.get("name") or "未命名项目",
        "status": _status_of(p),
        "summary": (p.get("goal") or "")[:200],
        "lastUpdated": p.get("updated_at") or p.get("created_at")
        or datetime.now().isoformat(),
        "messages": messages if messages is not None else [],
    }


async def relay_websocket(websocket: WebSocket):
    await websocket.accept()
    seen = {}            # room_id -> {message_id,...}  已推送消息去重
    last_status = {}     # room_id -> 上次状态          room_updated 节流
    poll_task = None
    try:
        # ── 认证 ──
        raw = await websocket.receive_text()
        msg = json.loads(raw)
        if msg.get("type") != "auth" or msg.get("token") != RELAY_TOKEN:
            await websocket.send_json({"type": "error", "message": "认证失败"})
            await websocket.close()
            return
        await websocket.send_json({"type": "auth_ok"})

        async def push_room_list():
            try:
                projects = _load_projects()
            except Exception as e:
                print("[relay] list projects err:", e)
                return
            rooms = []
            for p in projects:
                pid = p.get("id")
                try:
                    mob = [_to_mobile_message(m) for m in _load_messages(pid)]
                    seen[pid] = {m["id"] for m in mob}
                except Exception:
                    mob = []
                rooms.append(_to_mobile_room(p, mob))
                last_status[pid] = _status_of(p)
            await websocket.send_json({"type": "room_list", "rooms": rooms})

        await push_room_list()

        async def poll_loop():
            while True:
                try:
                    projects = _load_projects()
                    for p in projects:
                        pid = p.get("id")
                        # 新消息
                        try:
                            msgs = _load_messages(pid)
                            known = seen.get(pid, set())
                            for m in msgs:
                                mid = str(m.get("id"))
                                if mid not in known:
                                    known.add(mid)
                                    await websocket.send_json(
                                        {"type": "new_message",
                                         "message": _to_mobile_message(m)})
                            seen[pid] = known
                        except Exception as e:
                            print("[relay] poll msgs err:", e)
                        # 状态变化才推 room_updated（节流）
                        try:
                            st = _status_of(p)
                            if last_status.get(pid) != st:
                                last_status[pid] = st
                                await websocket.send_json(
                                    {"type": "room_updated",
                                     "room": _to_mobile_room(p, [])})
                        except Exception as e:
                            print("[relay] poll proj err:", e)
                except Exception as e:
                    print("[relay] poll loop err:", e)
                await asyncio.sleep(POLL_INTERVAL)

        poll_task = asyncio.create_task(poll_loop())

        # ── 主消息循环 ──
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "list_rooms":
                await push_room_list()
            elif t == "message":
                pid = msg.get("roomId")
                content = (msg.get("content") or "").strip()
                if pid and content:
                    try:
                        _insert_message(pid, content)
                        # 把刚发的消息标记为已见，避免轮询回推造成重复
                        try:
                            msgs = _load_messages(pid, 1)
                            if msgs:
                                seen.setdefault(pid, set()).add(str(msgs[-1].get("id")))
                        except Exception:
                            pass
                    except Exception as e:
                        print("[relay] insert msg err:", e)
                        await websocket.send_json({"type": "error", "message": "发送失败"})
            elif t == "stop_agent":
                pid = msg.get("roomId")
                if pid:
                    try:
                        _pause_project(pid)
                    except Exception as e:
                        print("[relay] pause err:", e)
            # 其它类型忽略
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("[relay] ws err:", e)
    finally:
        if poll_task:
            poll_task.cancel()
