"""单次请求预算护栏 · 确定性单测（v0.75.1）。

背景：用户实测「让元神做个计划」烧掉 66 次调用 / 339k token。根因是
派单主通道 _chat_with_tools 完全绕过预算，且没有任何「单次用户请求」级别的硬闸。
本测试锁死这道闸，防止回退。

同时锁死「Goal-Mode 绝不静默失败」：所有 failed 出口必须走 _fail_and_notify
（置状态 + 落日志 + 通知用户三件事绑定），不得只改状态不通知。

运行：python -m pytest tests/test_request_budget.py -q
"""
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须在 import backend.main 之前指定隔离 DB，避免碰生产库
os.environ.setdefault("FENSHEN_DB_PATH",
                      os.path.join(tempfile.gettempdir(), "fenshen_budget_test.db"))

from backend import main as M  # noqa: E402
from backend import goal_mode as G  # noqa: E402


def test_budget_resets_each_request():
    """每次请求必须拿到全新额度，长驻循环（autonomy/goal）不得复用上一次的旧额度。"""
    b1 = M._begin_request_budget()
    M._charge_request(10)
    assert b1["calls"] == 1
    b2 = M._begin_request_budget()
    assert b2 is not b1, "必须是新对象"
    assert b2["calls"] == 0 and b2["tokens"] == 0, "新请求必须从零开始"


def test_budget_trips_on_call_count():
    b = M._begin_request_budget()
    b["max_calls"] = 3
    b["max_tokens"] = 10 ** 9
    assert [M._charge_request(1) for _ in range(3)] == [False, False, False]
    assert M._charge_request(1) is True, "超过 max_calls 必须置 exceeded"
    assert M._request_exceeded() is True
    assert "已达上限" in M._request_budget_note(), "必须给出用户可见的说明文案"


def test_budget_trips_on_tokens():
    b = M._begin_request_budget()
    b["max_calls"] = 10 ** 6
    b["max_tokens"] = 100
    assert M._charge_request(90) is False
    assert M._charge_request(20) is True, "超过 max_tokens 必须置 exceeded"


def test_no_budget_is_noop():
    """没有请求上下文时（如单测、脚本直调）记账必须静默无副作用。"""
    M._REQ_BUDGET.set(None)
    assert M._charge_request(999) is False
    assert M._request_exceeded() is False


def test_goal_mode_never_fails_silently():
    """Goal-Mode 所有 failed 出口都必须经 _fail_and_notify（= 置状态 + 日志 + 通知用户）。"""
    src = inspect.getsource(G.run_goal_loop)
    assert "_fail_and_notify" in src, "失败出口必须通知用户，不得静默停下"
    assert '_set_goal_status(task_id, "failed")' not in src, (
        "不得直接置 failed——必须走 _fail_and_notify，否则用户端看不到任何交代"
    )


def test_fail_and_notify_bundles_three_things():
    """_fail_and_notify 必须同时做三件事，防止有人只改状态不通知。"""
    src = inspect.getsource(G._fail_and_notify)
    assert "_set_goal_status" in src
    assert "_log_run" in src
    assert "_notify" in src
