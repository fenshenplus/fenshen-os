"""分身移动端跨端配对 · 确定性单测（不依赖 LLM / 网络 / 主程序）。

验证 M 维度落地层 backend/mobile_pairing.py 的核心不变量：
  - 配对码生成 / 5 分钟有效 / 一次性消费；
  - confirm 用正确码生成只读设备令牌，错误/过期码拒绝；
  - 设备令牌校验、设备列表、撤销、状态查询；
  - 设备令牌 scope 恒为 readonly（移动端零提权）。
运行：python -m pytest tests/test_mobile_pairing.py -q
      （或 python tests/test_mobile_pairing.py）
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import mobile_pairing as MP  # noqa: E402
from datetime import datetime, timedelta


def _new_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE mobile_pair_requests (
        code TEXT PRIMARY KEY, pending_token TEXT, created_at TEXT,
        expires_at TEXT, consumed INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE mobile_devices (
        id TEXT PRIMARY KEY, name TEXT DEFAULT '', token_hash TEXT,
        device_info TEXT DEFAULT '{}', paired_at TEXT, last_seen TEXT,
        scope TEXT DEFAULT 'readonly')""")
    conn.commit()
    return conn


def test_create_pair_request():
    conn = _new_db()
    r = MP.create_pair_request(conn)
    assert len(r["code"]) == 6 and r["code"].isdigit()
    exp = datetime.fromisoformat(r["expires_at"])
    assert exp > datetime.now()
    # 落库
    row = conn.execute("SELECT * FROM mobile_pair_requests WHERE code=?", (r["code"],)).fetchone()
    assert row and row["consumed"] == 0


def test_confirm_flow_issues_readonly_token():
    conn = _new_db()
    r = MP.create_pair_request(conn)
    res = MP.confirm_pair(conn, r["code"], "我的iPhone", {"os": "iOS"})
    assert res["ok"], res
    assert res["scope"] == "readonly"
    assert res["device_token"].startswith(MP.DEVICE_TOKEN_PREFIX)
    # 令牌可验证
    assert MP.valid_device_token(conn, res["device_token"]) is True
    # 配对请求已被消费，不能二次使用
    again = MP.confirm_pair(conn, r["code"], "x")
    assert again["ok"] is False and again["status"] == 400


def test_confirm_wrong_code():
    conn = _new_db()
    MP.create_pair_request(conn)
    res = MP.confirm_pair(conn, "000000", "x")
    assert res["ok"] is False and res["status"] == 400


def test_confirm_expired_code():
    conn = _new_db()
    r = MP.create_pair_request(conn)
    # 把过期时间改到过去
    past = (datetime.now() - timedelta(seconds=10)).isoformat()
    conn.execute("UPDATE mobile_pair_requests SET expires_at=? WHERE code=?", (past, r["code"]))
    conn.commit()
    res = MP.confirm_pair(conn, r["code"], "x")
    assert res["ok"] is False and res["status"] == 400


def test_device_token_invalid():
    conn = _new_db()
    assert MP.valid_device_token(conn, "garbage") is False
    assert MP.valid_device_token(conn, "") is False
    assert MP.valid_device_token(conn, None) is False


def test_list_and_revoke():
    conn = _new_db()
    c1 = MP.create_pair_request(conn)
    c2 = MP.create_pair_request(conn)
    d1 = MP.confirm_pair(conn, c1["code"], "iPhone")
    d2 = MP.confirm_pair(conn, c2["code"], "iPad")
    devs = MP.list_devices(conn)
    assert len(devs) == 2
    # 撤销一个
    rev = MP.revoke_device(conn, d1["device_id"])
    assert rev["ok"] is True
    assert len(MP.list_devices(conn)) == 1
    # 撤销未知设备
    assert MP.revoke_device(conn, "nope")["ok"] is False
    # 令牌随之失效
    assert MP.valid_device_token(conn, d1["device_token"]) is False
    assert MP.valid_device_token(conn, d2["device_token"]) is True


def test_status():
    conn = _new_db()
    assert MP.status(conn)["paired"] is False
    c = MP.create_pair_request(conn)
    tok = MP.confirm_pair(conn, c["code"], "iPhone")["device_token"]
    assert MP.status(conn, tok)["paired"] is True
    assert MP.status(conn, "wrong")["paired"] is False


def test_device_info_stored():
    conn = _new_db()
    c = MP.create_pair_request(conn)
    MP.confirm_pair(conn, c["code"], "Pixel", {"os": "Android", "model": "Pixel 8"})
    row = conn.execute("SELECT device_info FROM mobile_devices").fetchone()
    assert "Android" in row["device_info"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
