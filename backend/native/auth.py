"""分身原生标准库 · 账号与验证码模块（auth）。

所有产品通用的注册 / 登录 / 登出 / 找回密码 / 短信验证码流程。
- 纯标准库实现：sqlite3 + hashlib(pbkdf2) + secrets + hmac + base64 + urllib，零第三方 SDK；
- 不依赖任何 LLM / 蒸馏：天然确定性、可单测、可离线、可审计；
- 业务流接收 sqlite3 连接（conn）作为参数，由调用方（FastAPI 端点 / 元神原生工具）管理会话与网络线程；
- 网络（发短信）与线程编排留在 main.py，本模块只负责「逻辑 + 密码学 + 阿里云签名」。

契约：所有业务函数返回 dict；成功含 ok=True，失败含 ok=False + error + status(HTTP 码)。
"""
import os
import re
import json
import secrets
import hashlib
import hmac
import base64
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from sqlite3 import Connection

# ── 校验正则 ──
CN_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 短信验证码时效与限流
SMS_TTL_MIN = 5
SMS_COOLDOWN_SEC = 60
SMS_DAILY_LIMIT = 10
SMS_MAX_ATTEMPTS = 5
CODE_LEN = 6

# ── 阿里云短信（环境变量，由 launchd plist / 运行环境注入）──
_ALIYUN_SMS_AK = os.environ.get("ALIYUN_SMS_AK", "")
_ALIYUN_SMS_SK = os.environ.get("ALIYUN_SMS_SK", "")
_ALIYUN_SMS_SIGN = os.environ.get("ALIYUN_SMS_SIGN", "安徽叒叕创业投资有限公司")
_ALIYUN_SMS_TPL = os.environ.get("ALIYUN_SMS_TPL", "REDACTED_ALIYUN_SMS_TEMPLATE")
_DEV_SMS = os.environ.get("FENSHEN_DEV_SMS", "") == "1"
# 注册 / 密码重置 分别使用独立模板（旧 ALIYUN_SMS_TPL 仍作为注册模板兜底）
_ALIYUN_SMS_TPL_REGISTER = os.environ.get("ALIYUN_SMS_TPL_REGISTER", os.environ.get("ALIYUN_SMS_TPL", "SMS_511885275"))
_ALIYUN_SMS_TPL_RESET = os.environ.get("ALIYUN_SMS_TPL_RESET", "SMS_511935313")


# ── 密码学（stdlib）──
def gen_salt() -> str:
    return secrets.token_hex(16)


def hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256 + 每用户随机盐，stdlib 实现、零依赖。"""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()


# ── 阿里云短信签名（纯 HMAC-SHA1，无 SDK）──
def _aliyun_percent_encode(s: str) -> str:
    return quote(str(s), safe="-_.~")


def _aliyun_sms_sign(params: dict, secret: str) -> str:
    canonical = "&".join(
        f"{_aliyun_percent_encode(k)}={_aliyun_percent_encode(params[k])}"
        for k in sorted(params.keys())
    )
    string_to_sign = "GET&%2F&" + _aliyun_percent_encode(canonical)
    return base64.b64encode(
        hmac.new((secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")


def send_sms_code(phone: str, code: str, purpose: str = "register") -> dict:
    """调用阿里云短信发送验证码。purpose='register' 用注册模板，'reset' 用密码重置模板。
    返回阿里云原始响应（Code=='OK' 为成功）。"""
    if _DEV_SMS:
        return {"Code": "OK", "Message": "dev-skip", "dev_code": code}
    if not _ALIYUN_SMS_AK or not _ALIYUN_SMS_SK:
        return {"Code": "ERR", "Message": "短信服务未配置（缺少 ALIYUN_SMS_AK / ALIYUN_SMS_SK 环境变量）"}
    tpl = _ALIYUN_SMS_TPL_RESET if purpose == "reset" else _ALIYUN_SMS_TPL_REGISTER
    params = {
        "AccessKeyId": _ALIYUN_SMS_AK,
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": "cn-hangzhou",
        "SignName": _ALIYUN_SMS_SIGN,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "TemplateCode": tpl,
        "TemplateParam": json.dumps({"code": code}, ensure_ascii=False),
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2017-05-25",
    }
    params["Signature"] = _aliyun_sms_sign(params, _ALIYUN_SMS_SK)
    url = "https://dysmsapi.aliyuncs.com/?" + urlencode(params)
    try:
        import requests  # 仅发短信处用；stdlib 不覆盖网络，故此处局部导入
        resp = requests.get(url, timeout=10)
        return resp.json()
    except Exception as e:  # 网络/超时：返回错误，绝不泄露验证码
        return {"Code": "ERR", "Message": str(e)[:200]}


def _gen_sms_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_LEN):0{CODE_LEN}d}"


def _gen_recovery_key() -> str:
    """生成恢复密钥（脱离手机号的所有权证明，明文仅随注册返回一次）。"""
    raw = secrets.token_hex(16).upper()
    return "-".join(raw[i:i + 4] for i in range(0, 32, 4))


# ── 速率限制（标准流程：确定性）──
def check_rate_limit(conn: Connection, phone: str, now: datetime | None = None) -> dict:
    """60s 冷却 + 单手机号每日上限 10 条。返回 {ok:True} 或 {ok:False, error, status}。"""
    now = now or datetime.now()
    row = conn.execute("SELECT * FROM sms_codes WHERE phone=?", (phone,)).fetchone()
    if not row:
        return {"ok": True}
    last = datetime.fromisoformat(row["last_sent_at"]) if row["last_sent_at"] else None
    if last and (now - last).total_seconds() < SMS_COOLDOWN_SEC:
        return {"ok": False, "error": f"发送过于频繁，请 {SMS_COOLDOWN_SEC} 秒后再试", "status": 429}
    day = now.strftime("%Y-%m-%d")
    if row["day"] == day and (row["send_count"] or 0) >= SMS_DAILY_LIMIT:
        return {"ok": False, "error": "今日验证码发送次数已达上限", "status": 429}
    return {"ok": True}


# ── 业务流（纯逻辑，接收 conn）──
def register_user(conn: Connection, *, email: str | None = None, phone: str | None = None,
                  password: str = "", code: str | None = None) -> dict:
    """注册用户。email 通道免验证码；phone 通道需先 issue_code 并通过短信验证。
    成功返回 {ok:True, user_id, recovery_key}；失败 {ok:False, error, status}。"""
    is_email = bool(email)
    if is_email:
        if not EMAIL_RE.match(email):
            return {"ok": False, "error": "邮箱格式不正确", "status": 400}
        if len(password) < 8:
            return {"ok": False, "error": "密码至少 8 位", "status": 400}
    else:
        if not phone or not CN_MOBILE_RE.match(phone):
            return {"ok": False, "error": "手机号格式不正确（应为 11 位中国大陆手机号）", "status": 400}
        if len(password) < 6:
            return {"ok": False, "error": "密码至少 6 位", "status": 400}
    # 验证码校验（仅手机号通道）
    if not is_email:
        srow = conn.execute("SELECT code,expires_at,attempts FROM sms_codes WHERE phone=?", (phone,)).fetchone()
        if not srow or srow["code"] != code or datetime.now().isoformat() > srow["expires_at"]:
            return {"ok": False, "error": "验证码无效或已过期，请先获取短信验证码", "status": 400}
        if (srow["attempts"] or 0) >= SMS_MAX_ATTEMPTS:
            return {"ok": False, "error": "验证码尝试次数过多，请重新获取", "status": 429}
        if conn.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
            return {"ok": False, "error": "该手机号已注册，请直接登录", "status": 409}
    else:
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return {"ok": False, "error": "该邮箱已注册，请直接登录", "status": 409}
    if not is_email:
        conn.execute("UPDATE sms_codes SET attempts=attempts+1 WHERE phone=?", (phone,))
    uid = "u" + secrets.token_hex(8)
    salt = gen_salt()
    phash = hash_password(password, salt)
    now = datetime.now().isoformat()
    if is_email:
        conn.execute(
            "INSERT INTO users (id,email,phone,password_hash,salt,created_at,last_login) VALUES (?,?,?,?,?,?,?)",
            (uid, email, "", phash, salt, now, now),
        )
    else:
        conn.execute(
            "INSERT INTO users (id,phone,password_hash,salt,created_at,last_login) VALUES (?,?,?,?,?,?)",
            (uid, phone, phash, salt, now, now),
        )
        conn.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))  # 验证码一次性消费
    recovery_key = _gen_recovery_key()
    conn.execute("UPDATE users SET recovery_key_hash=? WHERE id=?",
                 (hashlib.sha256(recovery_key.encode()).hexdigest(), uid))
    conn.commit()
    return {"ok": True, "user_id": uid, "recovery_key": recovery_key}


def verify_login(conn: Connection, *, email: str | None = None, phone: str | None = None,
                 password: str = "") -> dict:
    """校验登录凭据。成功返回 {ok:True, user_id}；失败 {ok:False, error, status}。"""
    is_email = bool(email)
    if is_email:
        row = conn.execute("SELECT id,password_hash,salt FROM users WHERE email=?", (email,)).fetchone()
        err = "邮箱或密码错误"
    else:
        row = conn.execute("SELECT id,password_hash,salt FROM users WHERE phone=?", (phone,)).fetchone()
        err = "手机号或密码错误"
    if not row or hash_password(password, row["salt"]) != row["password_hash"]:
        return {"ok": False, "error": err, "status": 401}
    return {"ok": True, "user_id": row["id"]}


def reset_password(conn: Connection, *, phone: str, code: str, new_password: str) -> dict:
    """凭短信验证码重置密码。成功返回 {ok:True, user_id}；失败 {ok:False, error, status}。"""
    if not CN_MOBILE_RE.match(phone):
        return {"ok": False, "error": "手机号格式不正确", "status": 400}
    if len(new_password) < 6:
        return {"ok": False, "error": "新密码至少 6 位", "status": 400}
    row = conn.execute("SELECT code,expires_at,attempts FROM sms_codes WHERE phone=?", (phone,)).fetchone()
    if not row or row["code"] != code or datetime.now().isoformat() > row["expires_at"]:
        return {"ok": False, "error": "验证码无效或已过期", "status": 400}
    if (row["attempts"] or 0) >= SMS_MAX_ATTEMPTS:
        return {"ok": False, "error": "验证码尝试次数过多，请重新获取", "status": 429}
    salt = gen_salt()
    conn.execute("UPDATE sms_codes SET attempts=attempts+1 WHERE phone=?", (phone,))
    conn.execute("UPDATE users SET password_hash=?, salt=? WHERE phone=?",
                 (hash_password(new_password, salt), salt, phone))
    conn.commit()
    return {"ok": True, "user_id": row["id"] if "id" in row.keys() else None}


def issue_code(conn: Connection, *, phone: str, purpose: str = "register", now: datetime | None = None) -> dict:
    """生成并落库验证码（含速率限制）。返回 {ok:True, code} 或 {ok:False, error, status}。
    注意：本函数只负责「生成+限流+落库」，实际发短信由调用方经 send_sms_code 异步发送。"""
    if purpose not in ("register", "reset"):
        purpose = "register"
    if not CN_MOBILE_RE.match(phone):
        return {"ok": False, "error": "手机号格式不正确", "status": 400}
    now = now or datetime.now()
    rl = check_rate_limit(conn, phone, now)
    if not rl["ok"]:
        return rl
    code = _gen_sms_code()
    day = now.strftime("%Y-%m-%d")
    row = conn.execute("SELECT * FROM sms_codes WHERE phone=?", (phone,)).fetchone()
    cur_day_count = row["send_count"] if (row and row["day"] == day) else 0
    conn.execute(
        "INSERT INTO sms_codes (phone,code,expires_at,attempts,last_sent_at,send_count,day) "
        "VALUES (?,?,?,0,?,?,?) "
        "ON CONFLICT(phone) DO UPDATE SET code=excluded.code,expires_at=excluded.expires_at,"
        "attempts=0,last_sent_at=excluded.last_sent_at,day=excluded.day,send_count=excluded.send_count",
        (phone, code, (now + timedelta(minutes=SMS_TTL_MIN)).isoformat(), now.isoformat(), cur_day_count + 1, day),
    )
    conn.commit()
    return {"ok": True, "code": code}
