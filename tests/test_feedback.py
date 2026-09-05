#!/usr/bin/env python3
"""分身反馈功能回归测试（产品端 /api/feedback 闭环）

覆盖：① 提交反馈返回受理单号 ② 列表可见 ③ 六能力统计聚合 ④ 回复/改状态。
用法：python tests/test_feedback.py [--base http://127.0.0.1:8099]
退出码：0 = 全通过；1 = 有失败。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8002"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", ".auth_token")
PASS, FAIL = [], []


def token() -> str:
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    return ""


TOK = token()


def call(method, path, body=None, timeout=20):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOK:
        req.add_header("x-fenshen-token", TOK)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": str(e)}


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'  PASS' if ok else '! FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    BASE = ap.parse_args().base

    # ① 提交反馈（六能力维度 D4）
    st, body = call("POST", "/api/feedback", {
        "content": "【回归测试】产品端反馈闭环自检",
        "category": "bug", "severity": "high",
        "capability": "D4", "source": "app", "version": "0.64.61",
    })
    ok_submit = st == 200 and body.get("ok") is True and body.get("ticket")
    ticket = body.get("ticket", "")
    check("提交反馈返回受理单号", ok_submit, ticket or f"status={st}")

    # ② 列表可见该反馈
    st, body = call("GET", "/api/feedback?limit=10")
    ok_list = st == 200 and isinstance(body, list) and any(
        r.get("id") == ticket for r in body)
    check("反馈列表可见刚提交的反馈", ok_list, f"count={len(body) if isinstance(body, list) else 'NA'}")

    # ③ 六能力统计聚合（按 capability 分组）
    st, body = call("GET", "/api/feedback/stats")
    ok_stats = st == 200 and isinstance(body, dict) and "by_capability" in body
    cap = body.get("by_capability", {}) if isinstance(body, dict) else {}
    check("六能力统计聚合可用", ok_stats, f"by_capability={cap}")

    # ④ 回复 + 改状态
    if ticket:
        st, body = call("POST", f"/api/feedback/{ticket}/reply", {
            "reply": "已收到，将在下个版本处理。", "status": "resolved"})
        ok_reply = st == 200 and body.get("ok") is True
        check("回复并改状态 resolved", ok_reply, f"status={st}")
        # 复核状态已变更
        st, body = call("GET", "/api/feedback?limit=10")
        changed = isinstance(body, list) and any(
            r.get("id") == ticket and r.get("status") == "resolved" for r in body)
        check("状态变更已落库", changed)
    else:
        check("回复并改状态 resolved", False, "无 ticket 可回复")

    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        sys.exit(1)
    print("✓ 反馈功能回归全绿")


if __name__ == "__main__":
    main()
