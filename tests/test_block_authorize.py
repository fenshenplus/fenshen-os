"""执行阻塞 → 用户可见 + 一次性授权（v0.75.3）· 确定性单测。

背景：写文件越界 / 宪法代码护栏 / 系统确认框不可用，此前只把一句话返给角色，
用户在界面上完全看不到 —— 表现就是「被拦住了就没声了」。本测试锁死整条链：
  拦截 → 落一条用户可见的 blocked 消息 → 授权 → 重试成功 → 授权即用即废。

运行：python -m pytest tests/test_block_authorize.py -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("FENSHEN_DB_PATH",
                      os.path.join(tempfile.gettempdir(), "fenshen_block_test.db"))

from backend import main as M  # noqa: E402

PID = "__meta__"
# 工作区（允许写）与安全区外的目标（应被拦）
WORKDIR = os.path.join(tempfile.gettempdir(), "fenshen_block_wd")
OUTSIDE = os.path.expanduser("~/fenshen_block_test_outside.txt")


def _reset():
    M._BLOCKS.clear()
    M._BLOCK_ALLOW.clear()
    M._TOOL_PROJECT.set(PID)
    M._TOOL_TASK.set("")
    M._TOOL_WORKDIR.set(WORKDIR)
    os.makedirs(WORKDIR, exist_ok=True)
    if os.path.exists(OUTSIDE):
        os.remove(OUTSIDE)


def _last_block_text() -> str:
    try:
        conn = M.get_db()
        row = conn.execute(
            "SELECT text FROM messages WHERE project_id=? AND tag='blocked' ORDER BY id DESC LIMIT 1",
            (PID,)).fetchone()
        conn.close()
        return (row["text"] if row else "") or ""
    except Exception:
        return ""


def test_out_of_workspace_is_visible_and_blocked():
    _reset()
    r = M._write_file_tool(OUTSIDE, "x")
    assert "越界" in r, f"越界必须被拦下，实际：{r[:80]}"
    txt = _last_block_text()
    assert "执行被拦下" in txt, "必须落一条用户可见的交代"
    assert "写文件越界" in txt
    assert "[block:" in txt, "必须带 block id 供前端/接口放行"
    assert not os.path.exists(OUTSIDE), "被拦下时绝不落盘"


def test_grant_once_then_retry_succeeds():
    _reset()
    M._write_file_tool(OUTSIDE, "x")           # 先被拦下
    bid = [k for k, v in M._BLOCKS.items() if v["project_id"] == PID]
    assert bid, "应记录一条待处理阻塞"
    bid = max(bid, key=lambda k: M._BLOCKS[k]["created"])
    assert M._grant_block(bid) is True, "放行应成功"

    r = M._write_file_tool(OUTSIDE, "hello")
    assert "越界" not in r and "⛔" not in r, f"授权后重试应成功，实际：{r[:120]}"
    assert os.path.exists(OUTSIDE), "应真正落盘"
    with open(OUTSIDE, encoding="utf-8") as f:
        assert f.read() == "hello"

    # 一次性：第二次应再度被拦
    r2 = M._write_file_tool(OUTSIDE, "y")
    assert "越界" in r2, "授权必须即用即废，第二次仍应被拦"
    try:
        os.remove(OUTSIDE)
    except Exception:
        pass


def test_chat_allow_intent_grants_and_hints_retry():
    _reset()
    M._write_file_tool(OUTSIDE, "x")           # 先造成一次阻塞
    hint = M._maybe_grant_from_chat(PID, "允许一次")
    assert hint and "用户已授权" in hint, "群聊放行意图必须产出重试提示"
    r = M._write_file_tool(OUTSIDE, "z")
    assert "越界" not in r, "放行后重试应成功"
    # 未命中放行意图时不应有任何副作用
    M._BLOCKS.clear()
    assert M._maybe_grant_from_chat(PID, "继续做别的") == ""
    try:
        os.remove(OUTSIDE)
    except Exception:
        pass


def test_expired_grant_is_refused():
    _reset()
    M._write_file_tool(OUTSIDE, "x")
    bid = max((k for k, v in M._BLOCKS.items() if v["project_id"] == PID),
              key=lambda k: M._BLOCKS[k]["created"])
    M._grant_block(bid)
    # 人为把授权改成已过期
    for k in list(M._BLOCK_ALLOW):
        M._BLOCK_ALLOW[k] = M.time.time() - 1
    assert M._block_allowed(M._BLOCKS[bid]["kind"], M._BLOCKS[bid]["target"]) is False, \
        "过期授权必须失效"
