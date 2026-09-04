#!/usr/bin/env python3
"""分身 · API Key 持久化回归测试（对应评估标准 D0 / §8 #1）

验证即便发生「重装」类数据丢失，API Key 也能从冗余副本恢复——
这是用户「每次重装就忘了」的根因闭环测试。

场景：
  S1  DB→store 回填：用户只配过一次 Key（冗余 store 从未写入）→ 启动同步后 store 必须被补齐。
      复现根因：旧代码仅 set_model 时写 store，配过一次就不再写，store 永远空。
  S2  store→DB 恢复：模拟「彻底重装删除 ~/.fenshen」（DB 丢失）但 Application Support store 幸存
      → 重新启动同步后，Key 必须从 store 恢复回 DB，get_model_config 可取到。

用法：python tests/test_model_key_persistence.py
退出码：0 = 全通过；1 = 有失败。
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 全部走临时目录，绝不污染真实 ~/.fenshen / Application Support
TMP = tempfile.mkdtemp(prefix="fenshen-persist-")
DB_PATH = os.path.join(TMP, "fenshen.db")
STORE_DIR = os.path.join(TMP, "app_support", "com.fenshen.app")
os.environ["FENSHEN_DB_PATH"] = DB_PATH
os.environ["FENSHEN_MODEL_STORE"] = STORE_DIR
os.environ["HOME"] = TMP  # 防御：即便代码回退到 ~/ 路径也不污染真实环境

PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name + ((" — " + info) if (info and not cond) else ""))


import backend.main as M  # noqa: E402  （导入即触发 init_db + sync_model_configs，使用上述临时路径）

AGENT = "__meta__"
TEST_KEY = "sk-S1-TEST-KEY-123"


def _db_key():
    conn = M.get_db()
    try:
        row = conn.execute("SELECT api_key FROM model_configs WHERE agent_id=?", (AGENT,)).fetchone()
        return row["api_key"] if row else None
    finally:
        conn.close()


# ── S1：DB 有 key、store 空 → 同步后 store 必须被补齐 ──
conn = M.get_db()
conn.execute(
    "INSERT OR REPLACE INTO model_configs (agent_id,provider,base_url,api_key,model_name) VALUES (?,?,?,?,?)",
    (AGENT, "deepseek", None, TEST_KEY, "deepseek-chat"))
conn.commit()
conn.close()

M.sync_model_configs()
store_path = M._model_store_path()
check("S1 冗余 store 文件已生成", os.path.exists(store_path), store_path)
store1 = json.load(open(store_path)) if os.path.exists(store_path) else {}
check("S1 store 已补齐 __meta__ key", store1.get(AGENT, {}).get("api_key") == TEST_KEY)


# ── S2：模拟「彻底重装删除 ~/.fenshen」（DB 丢失），store 幸存 → 从 store 恢复 ──
for suffix in ("", "-wal", "-shm"):
    p = DB_PATH + suffix
    if os.path.exists(p):
        os.remove(p)
M.init_db()  # 重建空表（等同重装后首次启动）

recovered = M.get_model_config(AGENT)  # DB 空、store 有 → 应触发 store→DB 恢复
check("S2 store→DB 恢复：get_model_config 取回 key",
      recovered is not None and recovered.get("api_key") == TEST_KEY, str(recovered))

M.sync_model_configs()  # 再同步一次，确认 DB 已落库且与 store 一致
check("S2 DB 已重新落库 key", _db_key() == TEST_KEY)
check("S2 恢复后 store 仍保留 key", json.load(open(store_path)).get(AGENT, {}).get("api_key") == TEST_KEY)


shutil.rmtree(TMP, ignore_errors=True)
print("\nPASS=%d  FAIL=%d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
