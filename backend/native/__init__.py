"""分身原生标准库（Native Stdlib）。

所有对「正确性 / 速度 / 确定性」要求高的通用流程，优先用原生代码实现并登记于此，
而非让 LLM 现场生成。元神在调度编码角色时，应先查本注册表：命中则走原生快路径
（直接、高效、快速、准确），仅在原生库覆盖不到时才允许生成新代码。

设计原则（与「极简优先 / Ponytail 法则」同源）：
- 标准流程 = 代码库（基本库），不靠推理堆出来；
- 安装包可更大——把标准库及依赖一并打进分身，离线即可确定性执行；
- 即使不蒸馏个性化人格，元神 + 分身原生能力对比其它 agent 也应有结构性优势。
"""
from . import auth
from . import verify

# 原生能力注册表：供元神系统提示与运行时工具调度枚举。
# 每项是「一个所有产品都通用的标准流程模块」。
NATIVE_CAPABILITIES = [
    {
        "module": "auth",
        "name": "账号与验证码",
        "desc": "注册 / 登录 / 登出 / 找回密码 / 短信验证码（阿里云 DysmsAPI 纯 HMAC-SHA1 自签，零第三方 SDK 依赖）",
        "tools": [
            "register_user", "verify_login", "reset_password",
            "issue_code", "send_sms_code", "check_rate_limit",
        ],
        "pure_stdlib": True,
    },
    {
        "module": "verify",
        "name": "验收与质量门",
        "desc": "Goal-Mode 验收原语：文件存在 / 测试通过 / 页面可达 / 构建无误 / 数据行数，确定性判定、不依赖 LLM",
        "tools": [
            "file_exists", "test_passed", "page_reachable",
            "build_ok", "db_row_count",
        ],
        "pure_stdlib": True,
    },
]

__all__ = ["auth", "verify", "NATIVE_CAPABILITIES"]
