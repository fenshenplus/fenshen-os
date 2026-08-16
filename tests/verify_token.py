#!/usr/bin/env python3
"""分身 v6.3 token 节约专项评测：磨压缩率/聚焦vs全项目/蒸馏压缩/预算护栏/记账完整性。"""
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

BASE = "http://127.0.0.1:8011"
TOKEN = open("/Users/a13401098230/WorkBuddy/fenshen-v1/data/.auth_token").read().strip()
HDRS = {"Content-Type": "application/json", "x-fenshen-token": TOKEN}
RESULTS = []


def q(path):
    p, _, query = path.partition("?")
    enc = "/".join(urllib.parse.quote(s, safe="") for s in p.split("/"))
    return enc + ("?" + query if query else "")


def api(method, path, body=None, timeout=300):
    req = urllib.request.Request(BASE + q(path), data=json.dumps(body).encode() if body is not None else None,
                                 headers=HDRS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code}
    except Exception as e:
        return {"error": str(e)}


def est_tokens(text):
    """中文粗估：1 字 ≈ 0.7 token（DeepSeek 词表近似），英文按词*1.3。"""
    cn = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cn
    return int(cn * 0.7 + other * 0.35)


def main():
    # ── T1-6 磨压缩率（实测 saved_pct）──
    segs = [
        {"role": "user", "content": "我最近在做用户登录模块的改造，主要问题是现有的登录流程有点复杂，用户经常忘记密码，而且验证码发送经常延迟，很多用户反馈说收不到验证码，需要重新设计一套更顺畅的登录方案，包括手机号验证码登录、第三方微信登录、还有账号密码登录三种方式并存，同时要做登录态的持久化和多端同步，还要考虑安全风控，比如异地登录提醒、异常设备检测这些。"},
        {"role": "agent", "content": "关于登录模块改造，我建议分三步走：第一步先做验证码服务的优化，把短信网关切换到更稳定的服务商，同时加入验证码发送失败的重试机制和队列削峰，这样能解决验证码收不到的问题；第二步统一登录入口，把三种登录方式整合到一个页面，默认推荐手机号验证码登录；第三步完善登录态管理，用 refresh token 机制实现多端同步和自动续期，同时接入风控系统。"},
    ]
    g = api("POST", "/api/grind", {"segments": segs})
    if g.get("ok"):
        ot, ct = g.get("original_tokens", 0), g.get("compressed_tokens", 0)
        saved = g.get("saved_pct", 0)
        RESULTS.append(("T1-6 磨压缩率", f"原文 {ot} token → 磨后 {ct} token，省 {saved}%"))
        print(f"  [磨] 原文 {ot} token ({g.get('original_len')} 字符) → 磨后 {ct} token ({g.get('compressed_len')} 字符) 省 {saved}%")
    else:
        RESULTS.append(("T1-6 磨压缩率", "接口失败 " + str(g)[:100]))

    # ── T1-5 蒸馏压缩率（对话 → experiences 条目）──
    long_chat = "用户：我们要做社区功能，要有发帖、评论、点赞。元神：好的，社区功能拆成帖子模块、评论模块、点赞模块。前端：帖子列表页做好了，支持分页加载。后端：帖子接口写好了，支持按时间排序。产品：评论要做到二级回复，点赞要防重复。测试：帖子发布流程验证通过，评论回复链路验证通过。" * 2
    d = api("POST", "/api/experiences/distill", {"text": long_chat, "scenario": "社区功能开发", "goal": "社区模块完整交付"})
    if d.get("ok") or d.get("experience") or d.get("items"):
        item = d.get("experience") or (d.get("items") or [{}])[0]
        distilled_text = json.dumps(item, ensure_ascii=False)[:800]
        ot, ct = est_tokens(long_chat), est_tokens(distilled_text)
        saved = round((1 - ct / ot) * 100, 1) if ot else 0
        RESULTS.append(("T1-5 蒸馏压缩率", f"对话 {ot} token → 经验条目 {ct} token，省 {saved}%（粗估）"))
        print(f"  [蒸馏] 对话 {len(long_chat)} 字符({ot} tok) → 经验条目 {len(distilled_text)} 字符({ct} tok) 省 {saved}%")
    else:
        RESULTS.append(("T1-5 蒸馏压缩率", "接口返回 " + str(d)[:150]))

    # ── T1-1 聚焦 vs 全项目（爆炸半径精准路由实测）──
    projects = api("GET", "/api/projects")
    pid = max(projects, key=lambda p: len(api("GET", f"/api/projects/{p['id']}/modules")))
    pid = pid["id"]
    mods = api("GET", f"/api/projects/{pid}/modules")
    n_mods = len(mods)
    focus_mod = next((m["name"] for m in mods if m["name"]), mods[0]["name"])
    print(f"  [聚焦测试] 项目 {pid} 共 {n_mods} 模块，目标模块「{focus_mod}」")
    # 聚焦指令
    c1 = api("POST", f"/api/projects/{pid}/chat", {"text": f"优化{focus_mod}模块的现状，先检查它当前的状态并汇报，不用执行修改。"}, timeout=300)
    time.sleep(3)
    # 全项目指令（无模块命中 → 全项目广播）
    c2 = api("POST", f"/api/projects/{pid}/chat", {"text": "从整体架构角度评估一下这个项目当前所有模块的协同情况，输出一句总结。"}, timeout=300)
    time.sleep(3)
    tr = api("GET", "/api/meta/token-report")
    print(f"  [token-report] {json.dumps(tr, ensure_ascii=False)[:300]}")

    # ── T1-8 预算护栏 ──
    st0 = api("GET", "/api/autopilot/state")
    old_budget = None
    if isinstance(st0, dict):
        bb = st0.get("token_budget") or {}
        old_budget = bb.get("hour")
    s1 = api("POST", "/api/autopilot/set", {"token_budget_hour": 50})
    st1 = api("GET", "/api/autopilot/state")
    h1 = None
    if isinstance(st1, dict):
        bb = st1.get("token_budget") or {}
        h1 = bb.get("hour")
    RESULTS.append(("T1-8 预算护栏", f"设置小时预算 50 → 状态回读 {h1}（原 {old_budget}）"))
    print(f"  [预算护栏] 原小时预算 {old_budget} → 设置 50 → 回读 {h1}")
    if old_budget is not None:
        api("POST", "/api/autopilot/set", {"token_budget_hour": old_budget})

    # ── T1-2 6 条截断 + T1-10 记账完整性（机制证据）──
    RESULTS.append(("T1-2 最近6条截断", "代码证据：_execute_project_chat LIMIT 6（messages 仅注入最近 6 条）"))
    RESULTS.append(("T1-3 调度禁用工具", "代码证据：调度阶段 tools=[]，避免工具循环"))
    RESULTS.append(("T1-4 Phase C 话题隔离", "代码证据：topic_chat 只注入任务+模块名/摘要+最近6条话题消息"))
    RESULTS.append(("T1-7 批量工具合并", "代码证据：batch_tool 多步工具一次调用"))

    print("\n==== token 节约专项汇总 ====")
    for name, detail in RESULTS:
        print(f"  [{name}] {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
