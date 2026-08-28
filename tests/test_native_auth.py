"""分身原生标准库 · 账号模块确定性单测（不依赖 LLM / 网络 / 短信网关）。

目的：证明「标准流程 = 原生代码」的正确性、可重复、可离线。
运行：python -m pytest tests/test_native_auth.py -q   （或 python tests/test_native_auth.py）
"""
import os
import sys
import sqlite3

# 让 backend 包可被导入（源码模式）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.native import auth as A  # noqa: E402


def _new_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE users (
        id TEXT PRIMARY KEY, email TEXT DEFAULT '', phone TEXT DEFAULT '',
        password_hash TEXT, salt TEXT, created_at TEXT, last_login TEXT,
        nickname TEXT DEFAULT '', recovery_key_hash TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE sms_codes (
        phone TEXT PRIMARY KEY, code TEXT, expires_at TEXT, attempts INTEGER DEFAULT 0,
        last_sent_at TEXT, send_count INTEGER DEFAULT 0, day TEXT)""")
    conn.commit()
    return conn


def test_register_email_no_code():
    conn = _new_db()
    res = A.register_user(conn, email="a@b.com", password="secret123")
    assert res["ok"], res
    assert res["recovery_key"].count("-") == 7  # 格式 XXXX-XXXX-... (8 段)
    row = conn.execute("SELECT email,recovery_key_hash FROM users WHERE id=?", (res["user_id"],)).fetchone()
    assert row["email"] == "a@b.com"
    assert row["recovery_key_hash"] == __import__("hashlib").sha256(res["recovery_key"].encode()).hexdigest()


def test_register_email_dup_409():
    conn = _new_db()
    A.register_user(conn, email="a@b.com", password="secret123")
    res = A.register_user(conn, email="a@b.com", password="secret123")
    assert res["ok"] is False and res["status"] == 409


def test_register_email_short_password():
    conn = _new_db()
    res = A.register_user(conn, email="a@b.com", password="123")
    assert res["ok"] is False and res["status"] == 400


def test_verify_login_ok_and_wrong():
    conn = _new_db()
    r = A.register_user(conn, email="a@b.com", password="secret123")
    ok = A.verify_login(conn, email="a@b.com", password="secret123")
    assert ok["ok"] and ok["user_id"] == r["user_id"]
    bad = A.verify_login(conn, email="a@b.com", password="wrong")
    assert bad["ok"] is False and bad["status"] == 401


def test_phone_register_requires_valid_code():
    conn = _new_db()
    # 无验证码 → 失败
    no_code = A.register_user(conn, phone="13800138000", password="abc123", code=None)
    assert no_code["ok"] is False and no_code["status"] == 400
    # 走 issue_code 生成 → 注册
    iss = A.issue_code(conn, phone="13800138000", purpose="register")
    assert iss["ok"], iss
    reg = A.register_user(conn, phone="13800138000", password="abc123", code=iss["code"])
    assert reg["ok"], reg
    # 已注册：重新发码后再用该码注册 → 命中「该手机号已注册」(409)
    iss2 = A.issue_code(conn, phone="13800138000", purpose="register")
    dup = A.register_user(conn, phone="13800138000", password="abc123", code=iss2["code"])
    assert dup["ok"] is False and dup["status"] == 409


def test_rate_limit_cooldown():
    conn = _new_db()
    A.issue_code(conn, phone="13800138000", purpose="register")
    # 60s 内再次 → 429
    rl = A.check_rate_limit(conn, "13800138000")
    assert rl["ok"] is False and rl["status"] == 429


def test_reset_password_flow():
    conn = _new_db()
    A.register_user(conn, phone="13800138000", password="abc123",
                    code=A.issue_code(conn, phone="13800138000", purpose="register")["code"])
    iss = A.issue_code(conn, phone="13800138000", purpose="reset")
    res = A.reset_password(conn, phone="13800138000", code=iss["code"], new_password="newpass123")
    assert res["ok"], res
    assert A.verify_login(conn, phone="13800138000", password="newpass123")["ok"]
    assert A.verify_login(conn, phone="13800138000", password="abc123")["ok"] is False


def test_reset_password_wrong_code():
    conn = _new_db()
    A.register_user(conn, phone="13800138000", password="abc123",
                    code=A.issue_code(conn, phone="13800138000", purpose="register")["code"])
    res = A.reset_password(conn, phone="13800138000", code="000000", new_password="x")
    assert res["ok"] is False and res["status"] == 400


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"✓ {fn.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"✗ {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
