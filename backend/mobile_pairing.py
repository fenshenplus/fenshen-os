"""分身 · 移动端跨端配对（M 维度落地层）。

解决「移动端便利性」：手机/平板经局域网或中继连接本机分身后，无需输入主令牌，
而是通过一次**配对握手**获得一个**只读设备令牌**，再用该令牌访问分身的只读数据端点
（项目列表、消息回看等）。设备令牌仅可读，绝不暴露主令牌或写接口，构成「跨端便利 + 零提权」。

配对握手（用户可见流程）：
  1. 桌面端调用 POST /api/mobile/pair/init  → 得到一个 6 位配对码（5 分钟有效）。
  2. 用户在移动端输入配对码 → POST /api/mobile/pair/confirm
     → 服务端校验配对码 → 生成 device_token（仅展示一次）并登记设备。
  3. 移动端此后在所有只读请求头带 x-fenshen-device-token → 访问 /api/mobile/* 只读端点。

所有写操作（执行命令、改配置）仍必须走主令牌（localhost），移动端令牌无写权限。
"""
import json
import secrets
import hashlib
from datetime import datetime, timedelta
from sqlite3 import Connection

# 配对码有效期（秒）
PAIR_CODE_TTL = 5 * 60
# 设备令牌长度（bytes，token_urlsafe 后约 43 字符）
DEVICE_TOKEN_BYTES = 32
# 设备令牌前缀（便于日志识别，不进库）
DEVICE_TOKEN_PREFIX = "dev_"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_pair_request(conn: Connection) -> dict:
    """创建一个配对请求，返回 {code, expires_at}。旧/过期请求会被清理。"""
    code = f"{secrets.randbelow(10 ** 6):06d}"  # 6 位数字，便于手机输入
    pending = secrets.token_urlsafe(16)
    now = _now_iso()
    exp = (datetime.now() + timedelta(seconds=PAIR_CODE_TTL)).isoformat()
    # 清理过期/已消费的请求，避免无限增长
    conn.execute(
        "DELETE FROM mobile_pair_requests WHERE expires_at < ? OR consumed=1",
        (now,),
    )
    conn.execute(
        "INSERT INTO mobile_pair_requests (code, pending_token, created_at, expires_at, consumed) "
        "VALUES (?,?,?,?,0)",
        (code, pending, now, exp),
    )
    conn.commit()
    return {"code": code, "expires_at": exp}


def confirm_pair(conn: Connection, code: str, device_name: str, device_info: dict | None = None) -> dict:
    """用配对码完成配对：成功返回 {ok, device_id, device_token, scope}（令牌仅此一次）。"""
    row = conn.execute(
        "SELECT * FROM mobile_pair_requests WHERE code=? AND consumed=0 AND expires_at > ?",
        (code, _now_iso()),
    ).fetchone()
    if not row:
        return {"ok": False, "error": "配对码无效或已过期", "status": 400}
    conn.execute("UPDATE mobile_pair_requests SET consumed=1 WHERE code=?", (code,))
    raw = DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(DEVICE_TOKEN_BYTES)
    dev_id = "m" + secrets.token_hex(8)
    info = json.dumps(device_info or {}, ensure_ascii=False)
    name = (device_name or "未命名设备")[:60]
    conn.execute(
        "INSERT INTO mobile_devices (id,name,token_hash,device_info,paired_at,last_seen,scope) "
        "VALUES (?,?,?,?,?,?,?)",
        (dev_id, name, _hash_token(raw), info, _now_iso(), _now_iso(), "readonly"),
    )
    conn.commit()
    return {
        "ok": True,
        "device_id": dev_id,
        "device_token": raw,
        "scope": "readonly",
        "note": "device_token 仅展示一次，请立即在移动端保存。",
    }


def valid_device_token(conn: Connection, token: str) -> bool:
    """校验设备令牌是否有效；有效则刷新 last_seen。无效返回 False。"""
    if not token:
        return False
    row = conn.execute(
        "SELECT id FROM mobile_devices WHERE token_hash=?", (_hash_token(token),)
    ).fetchone()
    if row:
        conn.execute("UPDATE mobile_devices SET last_seen=? WHERE id=?", (_now_iso(), row["id"]))
        conn.commit()
        return True
    return False


def list_devices(conn: Connection) -> list:
    rows = conn.execute(
        "SELECT id,name,paired_at,last_seen,scope FROM mobile_devices ORDER BY paired_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_device(conn: Connection, device_id: str) -> dict:
    n = conn.execute("DELETE FROM mobile_devices WHERE id=?", (device_id,)).rowcount
    conn.commit()
    return {"ok": n > 0, "status": 200 if n > 0 else 404}


def status(conn: Connection, token: str | None = None) -> dict:
    if token:
        return {"paired": valid_device_token(conn, token)}
    return {"paired": False}
