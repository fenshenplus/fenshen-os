"""
分身 · 公网中继 · 共享协议常量与令牌派生

设计要点（无状态、可水平扩展）：
- 每个用户（每套桌面安装）拥有唯一 tunnel_id（随机 uuid4）与 tunnel_token。
- tunnel_token = HMAC-SHA256(relay_secret, tunnel_id) 的 hex。
  中继服务端只需持有 relay_secret 即可无状态校验，无需中心化注册表。
- 安全性边界 = 不可猜测的 tunnel_id（128-bit 随机）。即使 relay_secret 泄露，
  攻击者也必须知道某个有效 tunnel_id 才能连入对应隧道；token 防伪造。
- 鉴权首帧同时携带 tunnel_id，避免依赖各版本 websockets 的 request.path 取值差异。
"""
import hashlib
import hmac
import uuid

# ── 帧类型 ──
AUTH = "auth"            # 首帧：{"type":"auth","role":...,"token":...,"tunnel":...}
AUTH_OK = "auth_ok"      # {"type":"auth_ok","role":...}
AUTH_FAIL = "auth_fail"  # {"type":"auth_fail","reason":...} （随后关闭）
PEER_ONLINE = "peer_online"    # 对端已上线：{"type":"peer_online","role":...}
PEER_OFFLINE = "peer_offline"  # 对端已离线：{"type":"peer_offline","role":...}

# auth 之后的业务帧由业务层定义（list_rooms/message/new_message/...），
# 中继一律透明转发，不做解释。

ROLE_DESKTOP = "desktop"
ROLE_MOBILE = "mobile"


def generate_tunnel_id() -> str:
    """每套安装唯一、不可猜测的隧道 ID。"""
    return uuid.uuid4().hex


def derive_tunnel_token(secret: str, tunnel_id: str) -> str:
    """无状态令牌派生：HMAC-SHA256(secret, tunnel_id)。中继端同式校验。"""
    return hmac.new(
        secret.encode("utf-8"),
        tunnel_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
