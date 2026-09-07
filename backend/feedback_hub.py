"""反馈中枢：本地留档 + 云端汇聚 的统一视图与回复通道。

闭环：
  用户提交 → 本地留档 + 上报云端
    → 维护者在此统一查看 / 回复 / 标注修复版本
    → 用户端「我的反馈」看到回复与修复版本 → 一键更新到新版

云端读取需管理令牌（环境变量 FENSHEN_FB_TOKEN，或 ~/.fenshen/feedback_token）。
无令牌时优雅降级：只展示本地反馈，提交与本地回复照常工作。
"""
import os
import threading
import time

import requests

CLOUD_URL = os.environ.get("FENSHEN_FB_CLOUD", "https://fenshen.plus/api/feedback")
TOKEN_ENV = "FENSHEN_FB_TOKEN"
TOKEN_FILE = os.path.expanduser("~/.fenshen/feedback_token")

_watch = {"unreplied": 0, "checked_at": 0, "error": None}


def _db():
    """延迟导入：避免与 main.py 形成循环依赖。"""
    from backend.main import get_db
    return get_db()


def cloud_token() -> str:
    tok = (os.environ.get(TOKEN_ENV) or "").strip()
    if tok:
        return tok
    try:
        if os.path.exists(TOKEN_FILE):
            return open(TOKEN_FILE, encoding="utf-8").read().strip()
    except Exception:
        pass
    return ""


def cloud_enabled() -> bool:
    return bool(cloud_token())


def _cloud_get(path="", **params):
    """GET 云端；无令牌或不可达时返回 None（静默，不影响本地功能）。"""
    tok = cloud_token()
    if not tok:
        return None
    try:
        r = requests.get(CLOUD_URL + path, params=params,
                         headers={"X-Feedback-Token": tok}, timeout=8)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _local_rows(limit=100, status="", capability=""):
    conn = _db()
    sql = "SELECT * FROM user_feedback"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if capability:
        where.append("capability=?")
        args.append(capability)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        rows = []
    conn.close()
    for r in rows:
        r["origin"] = "local"
    return rows


def unified(limit=100, status="", capability="", unreplied=False):
    """本地 + 云端合并去重。

    同一条反馈在本机与云端单号相同；云端记录优先（它携带维护者回复与修复版本）。
    """
    merged = {}
    for r in _local_rows(limit=limit, status=status, capability=capability):
        merged[r["id"]] = r

    cloud = _cloud_get("", limit=limit, status=status, capability=capability,
                       unreplied="1" if unreplied else "")
    if isinstance(cloud, list):
        for c in cloud:
            fid = c.get("id") or c.get("ticket")
            if not fid:
                continue
            c = dict(c)
            c["origin"] = "both" if fid in merged else "cloud"
            merged[fid] = c

    rows = sorted(merged.values(), key=lambda x: x.get("ts") or 0, reverse=True)
    if unreplied:
        rows = [r for r in rows if not (r.get("reply") or "").strip()]
    return rows[:limit]


def stats():
    """合并统计：本地待处理数 + 云端总数/未回复/按状态与六能力分布。"""
    local_pending = 0
    try:
        conn = _db()
        local_pending = conn.execute(
            "SELECT COUNT(*) c FROM user_feedback WHERE status='pending'").fetchone()["c"]
        conn.close()
    except Exception:
        pass
    cloud = _cloud_get("/stats") or {}
    return {
        "cloud_enabled": cloud_enabled(),
        "local_pending": local_pending,
        "cloud_total": cloud.get("total"),
        "cloud_unreplied": cloud.get("unreplied"),
        "cloud_by_status": cloud.get("by_status", {}),
        "cloud_by_capability": cloud.get("by_capability", {}),
    }


def reply(ticket, text="", status="", fixed_version=""):
    """回复反馈：本地有则更新本地；有令牌则同步云端。两处都不存在才算失败。"""
    out = {"ok": False, "ticket": ticket, "local": False, "cloud": None}
    try:
        conn = _db()
        row = conn.execute("SELECT id FROM user_feedback WHERE id=?", (ticket,)).fetchone()
        if row:
            sets, args = [], []
            if text:
                sets += ["reply=?", "replied_at=?"]
                args += [text, time.time()]
            if status:
                sets.append("status=?")
                args.append(status)
            if sets:
                args.append(ticket)
                conn.execute(
                    "UPDATE user_feedback SET " + ",".join(sets) + " WHERE id=?", args)
                conn.commit()
            out["local"] = True
        conn.close()
    except Exception as e:
        out["local_error"] = str(e)[:200]

    tok = cloud_token()
    if tok:
        try:
            r = requests.post(
                CLOUD_URL + "/" + str(ticket) + "/reply",
                json={"reply": text, "status": status, "fixed_version": fixed_version},
                headers={"X-Feedback-Token": tok}, timeout=10)
            out["cloud"] = (r.json() if r.status_code == 200
                            else {"error": (r.text or "")[:200]})
        except Exception as e:
            out["cloud"] = {"error": str(e)[:200]}

    out["ok"] = bool(out["local"] or (isinstance(out["cloud"], dict) and out["cloud"].get("ok")))
    return out


def remote_status(ticket):
    """公开查询单个反馈的处理进展（无需令牌，云端只回状态/回复/修复版本）。"""
    try:
        r = requests.get(CLOUD_URL + "/ticket/" + str(ticket), timeout=6)
        if r.status_code != 200:
            return None
        j = r.json()
        return j if j.get("ok") else None
    except Exception:
        return None


def my_feedback(limit=20):
    """「我的反馈」：本地记录 + 云端处理进展合并。

    用户提交后，维护者的回复写在云端，本机库不会自动同步 —— 不回查一次的话
    用户在自己电脑上永远看不到回复。这里按单号回查云端，补齐 reply /
    status / fixed_version，让「反馈 → 回复 → 修复 → 升级」闭环到用户眼前。
    """
    rows = _local_rows(limit=limit)
    for r in rows:
        st = remote_status(r.get("id"))
        r["fixed_version"] = ""
        r["synced"] = False
        if st:
            r["status"] = st.get("status") or r.get("status")
            r["reply"] = st.get("reply") or r.get("reply") or ""
            r["fixed_version"] = st.get("fixed_version") or ""
            r["replied_at"] = st.get("replied_at") or r.get("replied_at") or 0
            r["synced"] = True
    return rows


def refresh_unreplied():
    """刷新云端未回复数到内存；无令牌返回 None。"""
    if not cloud_enabled():
        _watch.update({"unreplied": 0, "checked_at": time.time(), "error": "no_token"})
        return None
    s = _cloud_get("/stats")
    if not s:
        _watch.update({"checked_at": time.time(), "error": "unreachable"})
        return None
    _watch.update({"unreplied": s.get("unreplied", 0), "checked_at": time.time(),
                   "error": None})
    return _watch["unreplied"]


def unreplied_count(max_age=1800):
    """未回复数（默认 30 分钟内读缓存，避免频繁打云端）。"""
    if time.time() - _watch.get("checked_at", 0) > max_age:
        refresh_unreplied()
    return _watch.get("unreplied", 0), _watch.get("error")


def start_watch(interval=1800):
    """后台定时刷新未回复数（仅内存态，不落库、不打扰用户）。"""
    def _run():
        while True:
            try:
                refresh_unreplied()
            except Exception:
                pass
            time.sleep(interval)
    threading.Thread(target=_run, daemon=True).start()
