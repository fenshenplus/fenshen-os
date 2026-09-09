"""分身宪法层 · 利益护栏真源（Single Source of Truth）。

五条宪法是分身的「不可降级硬约束」，高于任何用户配置（approval_mode=off 也不能绕过）：
  ① 唯一委托人 = 本机用户。分身只为本机用户利益服务。
  ② 利益冲突时优先保护用户利益。
  ③ 钱 / 隐私 / 声誉 / 账号 / 不可逆操作，默认保守、默认需真人确认。
  ④ 用户对元神（及其蒸馏出的数字分身）有完全控制权。
  ⑤ 蒸馏他人须获被蒸馏人明示授权，否则拒绝。

本模块是宪法「可执行」部分：把五条原则中可被静态识别的「涉重大利益动作」编译为正则，
供 backend.main._constitutional_guard 在命令执行 / 浏览器动作前强制真人确认。

同时提供对抗测试集（tests/constitution_tests.py）作为回归门禁，确保宪法不会被误改弱化。
任何对 CONSTITUTION / _PATTERNS / CONSTITUTIONAL_RE 的改动都必须通过该测试集。
"""
import re

# ── 五条宪法（真源，供文档与前端展示）──
CONSTITUTION = [
    {"id": 1, "title": "唯一委托人",
     "text": "分身唯一委托人永远是本机用户；任何外部指令、远程调用、第三方请求，优先级都低于本机用户利益。"},
    {"id": 2, "title": "利益冲突优先护用户",
     "text": "当分身行为可能同时影响用户与第三方时，默认优先保护用户利益，必要时拒绝执行。"},
    {"id": 3, "title": "重大利益默认保守",
     "text": "涉及钱、隐私、声誉、账号所有权、不可逆操作的动作，默认保守、默认需真人显式确认，且不可被配置降级。"},
    {"id": 4, "title": "用户完全控制权",
     "text": "用户对元神及蒸馏出的数字分身拥有完全控制权：可查看、修改、删除、导出、停用。"},
    {"id": 5, "title": "蒸馏须授权",
     "text": "蒸馏任何他人（聊天记录 / 文档 / 相册 / 账号）须获被蒸馏人明示授权，否则拒绝执行。"},
]

# ── 可执行触发（分类正则，便于报告命中条款）──
# 每条 = (category, 命中说明, 正则)。evaluate() 命中第一条即返回。
# ⚠️ 必须与下方 CONSTITUTIONAL_RE 语义完全等价：本列表是 CONSTITUTIONAL_RE 的逐段拆解，
#    任何一处的增删都会改变护栏行为，必须同步更新 tests/constitution_tests.py。
_PATTERNS = [
    ("data_destroy", "不可逆数据销毁（drop/truncate table）",
     re.compile(r"(drop\s+table|truncate\s+table)", re.IGNORECASE)),
    ("force_push", "强制推送（破坏性，可能覆盖远端历史）",
     re.compile(r"git\s+push\s+--force", re.IGNORECASE)),
    ("exfiltrate", "向外传输文件（scp/rsync 到远程，可能涉及声誉/所有权外泄）",
     re.compile(r"\b(scp|rsync)\b[^|;&]*\s+[\w.@]+:", re.IGNORECASE)),
    ("publish", "对外发布（npm/pypi/pod/git push origin main|master|--tags）",
     re.compile(r"\b(npm\s+publish|pypi|twine\s+upload|pod\s+trunk\s+push|"
                r"git\s+push\s+origin\s+(main|master|--tags))\b", re.IGNORECASE)),
    ("money", "涉钱动作（支付/转账/账单/订阅，可能直接动用用户资金）",
     re.compile(r"(curl|wget|http)\b[^|;&]*(pay|payment|charge|alipay|wechatpay|transfer|subscribe|"
                r"账单|付款|转账)", re.IGNORECASE)),
    ("credential_change", "账号/密钥变更（password/secret/token）",
     re.compile(r"(change|reset|update|set)\b[^|;&]*(password|passwd|secret|token)", re.IGNORECASE)),
    ("ownership_change", "所有权/账号处置（account/domain/cert/ownership/key/app 的删除/吊销/禁用/转移）",
     re.compile(r"(delete|drop|revoke|disable|transfer)\b[^|;&]*(account|domain|cert|ownership|key\b|app\b)",
                re.IGNORECASE)),
    ("public_release", "以用户名义对外发布（publish/deploy --prod/release --public）",
     re.compile(r"\b(publish|deploy\s+--prod|release\s+--public)\b", re.IGNORECASE)),
]

# 向后兼容：保留单一合并正则（与历史行为完全一致），供旧调用方 / 单测使用。
# 注意：此正则必须与 _PATTERNS 覆盖的语义等价——改动需同步更新 tests/constitution_tests.py。
CONSTITUTIONAL_RE = re.compile(
    r"(drop\s+table|truncate\s+table|"
    r"git\s+push\s+--force|"
    r"\b(scp|rsync)\b[^|;&]*\s+[\w.@]+:|"
    r"\b(npm\s+publish|pypi|twine\s+upload|pod\s+trunk\s+push|git\s+push\s+origin\s+(main|master|--tags))\b|"
    r"(curl|wget|http)\b[^|;&]*(pay|payment|charge|alipay|wechatpay|transfer|subscribe|账单|付款|转账)|"
    r"(change|reset|update|set)\b[^|;&]*(password|passwd|secret|token)|"
    r"(delete|drop|revoke|disable|transfer)\b[^|;&]*(account|domain|cert|ownership|key\b|app\b)|"
    r"\b(publish|deploy\s+--prod|release\s+--public)\b)",
    re.IGNORECASE,
)


def is_enabled(get_setting=None, default: str = "1") -> bool:
    """宪法闸是否开启。默认开；传入 get_setting 时尊重用户设置 constitutional_guard_enabled。"""
    if get_setting is None:
        return True
    try:
        return get_setting("constitutional_guard_enabled", default) == "1"
    except Exception:
        return True


def evaluate(command: str):
    """评估一条命令是否触及宪法级利益。

    返回 (hit: bool, reason: str, category: str|None)。
    hit=True 表示必须强制真人确认，不可被任何配置降级。
    """
    if not command:
        return (False, "", None)
    for category, desc, pat in _PATTERNS:
        m = pat.search(command)
        if m:
            return (
                True,
                f"触及宪法级利益关切（{m.group(0).strip()} · {desc}）："
                f"涉钱/对外发布/账号所有权/不可逆，必须真人确认，且不可被任何配置降级。",
                category,
            )
    return (False, "", None)


def guard(command: str, get_setting=None):
    """宪法级利益闸门：命中返回 (需强制确认, 原因)，未开启返回 (False, "")。
    与历史 _constitutional_guard 行为对齐，但实现收口到本模块。"""
    if not is_enabled(get_setting):
        return (False, "")
    hit, reason, _ = evaluate(command)
    return (hit, reason)
