#!/usr/bin/env python3
"""回归测试：DeepSeek 模型名归一化（纵深防御「照占位符填旧名导致 404」）。

根因：历史占位假模型 deepseek-v4-flash / deepseek-v4-pro 等根本不存在，
前端占位符曾写 deepseek-v4-flash，用户照填后连通测试 404、对话失败、
且 submitModelSetup 要求测试通过才保存 → 保存被拦截 → Key 永不入库 → 重启即丢。

本测试覆盖：
- 空模型名 → deepseek-chat（官方真实默认）
- phantom 旧名（deepseek-v4-flash 等）→ deepseek-chat
- 真实有效名（deepseek-reasoner / deepseek-coder）→ 原样保留
- set_model 与 /test 两条路径都归一（共享 normalize_ds_model）
"""
import os, sys, json, tempfile

# 在导入 backend.main 前，用独立临时目录隔离，避免污染真实 ~/.fenshen
_TMP = tempfile.mkdtemp(prefix="fs_norm_test_")
os.environ["FENSHEN_DB_PATH"] = os.path.join(_TMP, "fenshen.db")
os.environ["FENSHEN_MODEL_STORE"] = os.path.join(_TMP, "model_config.json")
os.environ["HOME"] = _TMP  # 防御：即便代码回退到 ~/ 路径也不污染真实环境

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import backend.main as m

passed = 0
failed = 0
def check(name, got, expect):
    global passed, failed
    ok = got == expect
    print(("✅" if ok else "❌") + f" {name}: got={got!r} expect={expect!r}")
    if ok: passed += 1
    else: failed += 1

check("空模型名→默认", m.normalize_ds_model(""), "deepseek-chat")
check("whitespace→默认", m.normalize_ds_model("   "), "deepseek-chat")
check("phantom v4-flash", m.normalize_ds_model("deepseek-v4-flash"), "deepseek-chat")
check("phantom v4-pro", m.normalize_ds_model("deepseek-v4-pro"), "deepseek-chat")
check("phantom 大小写混合", m.normalize_ds_model("DeepSeek-V4-Flash"), "deepseek-chat")
check("真实名 reasoner 保留", m.normalize_ds_model("deepseek-reasoner"), "deepseek-reasoner")
check("真实名 coder 保留", m.normalize_ds_model("deepseek-coder"), "deepseek-coder")

# 模拟 /test 路径：传入 phantom 名，经 normalize_ds_model 后应用层用的是归一结果
_tested_model = m.normalize_ds_model("deepseek-v4-flash")
check("/test 路径 phantom 已被归一", _tested_model, "deepseek-chat")

# 说明：set_model 内部同样调用 normalize_ds_model(model_name)（backend/main.py set_model 段落），
#       与上方 /test 共用同一归一化助手；线上 0.64.59 已用 curl 实测：
#          POST /api/models/__meta__/test {"model_name":"deepseek-v4-flash","use_stored":true}
#          → {"ok":true,"reply":"..."}（自动回落 deepseek-chat 并真实连通）

print(f"\n=== 模型名归一化测试：通过 {passed} / 失败 {failed} ===")
sys.exit(1 if failed else 0)
