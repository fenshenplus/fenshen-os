#!/usr/bin/env python3
"""分身 · D0 大模型能力运行期回归测试（对应评估标准 v2 §8 #2~#5）

覆盖：
  D0.2 上下文管理：_context_window 在 token 预算内保留「系统锚点 + 首条需求 + 最近若干轮」，截断中间。
  D0.3 Token 预算：_set_budget / _get_budget / _check_budget 超预算即拦截（绝不静默放行）。
  D0.4 连接状态：_update_model_health 写入 model_health；/status 接口正确回填。
  D0.5 主备路由透明化：/route 返回候选链（不含 Key）。

全部走临时目录，绝不污染真实 ~/.fenshen / Application Support。
用法：python tests/test_d0_runtime.py
退出码：0 = 全通过；1 = 有失败。
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="fenshen-d0-")
DB_PATH = os.path.join(TMP, "fenshen.db")
STORE_DIR = os.path.join(TMP, "app_support", "com.fenshen.app")
os.environ["FENSHEN_DB_PATH"] = DB_PATH
os.environ["FENSHEN_MODEL_STORE"] = STORE_DIR
os.environ["HOME"] = TMP

PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name + ((" — " + info) if (info and not cond) else ""))


import backend.main as M  # noqa: E402  (导入即触发 init_db，使用上述临时路径)

AGENT = "__meta__"


def _seed_key():
    conn = M.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO model_configs (agent_id,provider,base_url,api_key,model_name) VALUES (?,?,?,?,?)",
        (AGENT, "deepseek", None, "sk-D0-TEST-KEY", "deepseek-chat"))
    conn.commit()
    conn.close()


# ── D0.2 上下文管理 ──
big = "内容" * 1200  # ~ 约 1800 token
hist = [{"role": "user", "content": "首条用户需求锚点必须保留"}]
for i in range(40):
    hist.append({"role": "user" if i % 2 == 0 else "assistant", "content": big})
win = M._context_window(hist, system_prompt="你是元神系统设定锚点", max_tokens=6000)
contents = [m.get("content", "") for m in win]
check("D0.2 上下文窗口保留系统锚点", any("元神系统设定锚点" in c for c in contents))
check("D0.2 上下文窗口保留首条需求锚点", any("首条用户需求锚点必须保留" in c for c in contents))
check("D0.2 上下文窗口已截断中间历史", len(win) < len(hist), f"win={len(win)} hist={len(hist)}")
check("D0.2 最近消息优先保留", contents[-1] == big, "末条应为最近大消息")


# ── D0.3 Token 预算护栏 ──
M._set_budget(AGENT, 1000)
check("D0.3 预算可设置", M._get_budget(AGENT) == 1000)
check("D0.3 未超预算不拦截", M._check_budget(AGENT, 500) == "")
block = M._check_budget(AGENT, 1500)
check("D0.3 超预算返回拦截提示", "预算" in block, block)

# 模拟今日已消耗 900 token → 再加 200 应超
conn = M.get_db()
conn.execute(
    "INSERT INTO model_usage (ts,agent_id,provider,model,latency_ms,status,input_tokens,output_tokens) "
    "VALUES (?,?,?,?,?,?,?,?)",
    (M.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), AGENT, "deepseek", "deepseek-chat", 10, "success", 400, 500))
conn.commit()
conn.close()
check("D0.3 累计今日用量已计入", M._usage_today(AGENT) == 900, str(M._usage_today(AGENT)))
check("D0.3 累计超预算拦截", "预算" in M._check_budget(AGENT, 200))
M._set_budget(AGENT, 0)  # 恢复不限
check("D0.3 预算为 0 表示不限", M._check_budget(AGENT, 999999) == "")


# ── D0.4 连接状态 ──
_seed_key()
M._update_model_health(AGENT, True, 37, "deepseek", "deepseek-chat")
st = M.model_status(AGENT)
check("D0.4 /status 已配 Key", st["configured"] is True)
check("D0.4 /status 记录最近连通成功", st["last_test_ok"] is True)
check("D0.4 /status 记录延迟", st["last_latency_ms"] == 37)
check("D0.4 /status 记录实际服务模型", st["served_model"] == "deepseek-chat")
M._update_model_health(AGENT, False)
st2 = M.model_status(AGENT)
check("D0.4 /status 记录连通失败", st2["last_test_ok"] is False)
ud = M.model_usage_detail(AGENT)
check("D0.4 /usage 返回今日 calls", ud["today_calls"] == 1, str(ud))


# ── D0.5 主备路由透明化 ──
_seed_key()
route = M.model_route(AGENT)
check("D0.5 /route 返回候选链", isinstance(route["route"], list) and len(route["route"]) >= 1)
check("D0.5 候选链不含明文 Key", "api_key" not in json.dumps(route["route"]))
check("D0.5 主模型为首候选", route["primary"] is not None and route["primary"]["provider"] == "deepseek")


print(f"\n=== D0 运行期回归测试：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL PASS")
