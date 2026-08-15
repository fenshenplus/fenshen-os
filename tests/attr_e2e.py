#!/usr/bin/env python3
"""疗效归因第⑤环 e2e：创建→信号演化→召回→聚合→淘汰"""
import json, os, urllib.request, urllib.error, urllib.parse, sys

BASE = "http://127.0.0.1:8011"
TOKEN = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", ".auth_token")).read().strip() \
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", ".auth_token")) \
    else open("data/.auth_token").read().strip()

PASS, FAIL = [], []
def call(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("x-fenshen-token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())
    except Exception as e:
        return -1, {"error": str(e)}

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))

# 1. 创建成功经验
st, j = call("POST", "/api/experiences", {"category":"success","scenario":"部署静态站到nginx","goal":"上线","attempts":"scp+nginx","outcome":"部署成功，访问达标","lesson":"先配server块再reload"})
check("创建经验", st==200 and j.get("ok"), j)
# 拿到最新一条 id
st, lst = call("GET", "/api/experiences")
eid = max((e["id"] for e in lst if e["scenario"]=="部署静态站到nginx"), default=None)
check("经验已落库且含权重字段", eid is not None and "weight" in lst[0] and "trust_score" in lst[0], f"eid={eid}")

# 2. 打 use 信号 → frequency 应 +1
call("POST", f"/api/experiences/{eid}/signal", {"kind":"use"})
st, lst = call("GET", "/api/experiences")
e = next(x for x in lst if x["id"]==eid)
check("use 信号累计 frequency", e["frequency"]>=1, f"freq={e['frequency']}")

# 3. 打 positive 信号 → trust 应上升（初始 0.5 → >0.5）
before = e["trust_score"]
call("POST", f"/api/experiences/{eid}/signal", {"kind":"positive"})
st, lst = call("GET", "/api/experiences")
e = next(x for x in lst if x["id"]==eid)
check("positive 信号提升 trust", e["trust_score"]>before, f"trust {before}→{e['trust_score']}")

# 4. outcome 更新重算权重（正反馈文本 → relevance 高、weight 上升）
call("POST", f"/api/experiences/{eid}/signal", {"kind":"outcome","outcome":"上线成功达标✅"})
st, lst = call("GET", "/api/experiences")
e = next(x for x in lst if x["id"]==eid)
check("outcome 正反馈 → relevance 高", e["relevance"]>=0.8, f"rel={e['relevance']}")
check("weight 在合理区间(0,1]", 0 < e["weight"] <= 1, f"w={e['weight']}")

# 5. 召回：按权重排序且能命中该经验
st, rj = call("GET", "/api/experiences/recall?q=" + urllib.parse.quote("部署静态站到nginx"))
check("召回接口返回", st==200 and rj.get("ok"), rj)
hit = any(x["id"]==eid for x in rj.get("items", []))
check("召回命中目标经验", hit, f"count={rj.get('count')}")

# 6. 聚合：/api/meta/attribution 含类别聚合且权重在(0,1]
st, aj = call("GET", "/api/meta/attribution")
aggs = aj.get("attribution", [])
ok_agg = any(a["key"].startswith("cat:") and 0 < a["weight"] <= 1 for a in aggs)
check("attribution 聚合产出类别权重", ok_agg, f"keys={[a['key'] for a in aggs]}")

# 7. 淘汰规则：连续 2 次负反馈 → eliminated=1，且召回不再返回
st, j = call("POST", "/api/experiences", {"category":"failure","scenario":"wrong-test-elim","goal":"x","attempts":"y","outcome":"失败","lesson":"z"})
st, lst = call("GET", "/api/experiences")
eid2 = max((e["id"] for e in lst if e["scenario"]=="wrong-test-elim"), default=None)
call("POST", f"/api/experiences/{eid2}/signal", {"kind":"negative"})
call("POST", f"/api/experiences/{eid2}/signal", {"kind":"negative"})
st, lst = call("GET", "/api/experiences")
e2 = next(x for x in lst if x["id"]==eid2)
check("连续负反馈 → eliminated", e2["eliminated"]==1, f"neg_streak={e2['neg_streak']}, elim={e2['eliminated']}")
st, rj = call("GET", f"/api/experiences/recall?q=" + urllib.parse.quote("wrong-test-elim"))
not_recalled = not any(x["id"]==eid2 for x in rj.get("items", []))
check("已淘汰经验不进召回", not_recalled, f"count={rj.get('count')}")

# 清理这两条测试经验
call("DELETE", f"/api/experiences/{eid}")
call("DELETE", f"/api/experiences/{eid2}")

print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
