"""
分身 · 公网中继服务端（部署于 S4 ECS 47.111.25.150）

职责：
- 监听公网端口（默认 8848 ws；可启用 TLS 走 8443 wss）。
- 按 tunnel_id 配对一个 desktop 与一个 mobile。
- auth 之后双向透明透传业务帧，不解析业务内容。
- 令牌校验：HMAC-SHA256(FENSHEN_RELAY_SECRET, tunnel_id) == token（无状态）。

运行（本机调测）：
    FENSHEN_RELAY_SECRET=xxx python -m relay.relay_server
    # 或指定端口 / TLS
    FENSHEN_RELAY_PORT=8848 FENSHEN_RELAY_TLS=1 \
    FENSHEN_RELAY_CERT=/path/fullchain.pem FENSHEN_RELAY_KEY=/path/privkey.pem \
    python -m relay.relay_server

说明：
- 同一 tunnel_id 下 mobile 先于 desktop 连入时，mobile 收到 peer_offline 并保持连接等待；
  desktop 连入后双方收到 peer_online，开始透传。
- 同角色重复连入会踢掉旧连接（防止僵尸连接占用配对位）。
"""
import asyncio
import json
import os
import sys

# 同时支持「包内导入」(from backend.relay...) 与「独立运行」(from relay...)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/
try:
    from relay.protocol import (
        AUTH, AUTH_OK, AUTH_FAIL, PEER_ONLINE, PEER_OFFLINE,
        ROLE_DESKTOP, ROLE_MOBILE, derive_tunnel_token,
    )
except ImportError:  # pragma: no cover
    from backend.relay.protocol import (
        AUTH, AUTH_OK, AUTH_FAIL, PEER_ONLINE, PEER_OFFLINE,
        ROLE_DESKTOP, ROLE_MOBILE, derive_tunnel_token,
    )

import websockets

SECRET = os.environ.get("FENSHEN_RELAY_SECRET", "")
PORT = int(os.environ.get("FENSHEN_RELAY_PORT", "8848"))
HOST = os.environ.get("FENSHEN_RELAY_HOST", "0.0.0.0")
USE_TLS = os.environ.get("FENSHEN_RELAY_TLS", "0") == "1"
CERT = os.environ.get("FENSHEN_RELAY_CERT", "")
KEY = os.environ.get("FENSHEN_RELAY_KEY", "")


class Relay:
    def __init__(self):
        # tunnel_id -> {"desktop": WebSocket|None, "mobile": WebSocket|None}
        self.tunnels: dict[str, dict] = {}

    async def _send(self, ws, obj):
        try:
            await ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    def _slot(self, tunnel_id: str) -> dict:
        return self.tunnels.setdefault(tunnel_id, {"desktop": None, "mobile": None})

    async def handler(self, websocket):
        # 1) 收首帧 auth（含 tunnel_id / role / token）
        try:
            raw = await websocket.recv()
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("type") != AUTH:
            await self._send(websocket, {"type": AUTH_FAIL, "reason": "expect auth"})
            await websocket.close()
            return
        tunnel_id = msg.get("tunnel")
        role = msg.get("role")
        token = msg.get("token", "")
        if not tunnel_id or role not in (ROLE_DESKTOP, ROLE_MOBILE):
            await self._send(websocket, {"type": AUTH_FAIL, "reason": "bad auth fields"})
            await websocket.close()
            return
        if not SECRET or derive_tunnel_token(SECRET, tunnel_id) != token:
            await self._send(websocket, {"type": AUTH_FAIL, "reason": "bad token"})
            await websocket.close()
            return

        # 2) 占位 + 踢掉同角色旧连接
        slot = self._slot(tunnel_id)
        other = "mobile" if role == ROLE_DESKTOP else "desktop"
        old = slot.get(role)
        if old is not None and old is not websocket:
            try:
                await old.close()
            except Exception:
                pass
        slot[role] = websocket

        await self._send(websocket, {"type": AUTH_OK, "role": role})
        peer = slot.get(other)
        if peer is not None:
            await self._send(websocket, {"type": PEER_ONLINE, "role": role})
            await self._send(peer, {"type": PEER_ONLINE, "role": other})
            print(f"[relay] paired tunnel={tunnel_id[:8]} ({role}<->{other})")
        else:
            if role == ROLE_MOBILE:
                await self._send(websocket, {"type": PEER_OFFLINE, "role": ROLE_DESKTOP})
            print(f"[relay] {role} joined tunnel={tunnel_id[:8]} (waiting peer)")

        # 3) 透传循环：收到的业务帧转发给对端
        try:
            async for frame in websocket:
                dst = slot.get(other)
                if dst is not None:
                    try:
                        await dst.send(frame)
                    except Exception:
                        break
        except Exception:
            pass
        finally:
            # 4) 清理
            if slot.get(role) is websocket:
                slot[role] = None
            peer = slot.get(other)
            if peer is not None:
                await self._send(peer, {"type": PEER_OFFLINE, "role": role})
            print(f"[relay] {role} left tunnel={tunnel_id[:8]}")


async def main():
    if not SECRET:
        print("[relay] FATAL: 必须设置环境变量 FENSHEN_RELAY_SECRET")
        return
    ssl_ctx = None
    if USE_TLS:
        if not (CERT and KEY):
            print("[relay] FATAL: 启用 TLS 需同时设置 FENSHEN_RELAY_CERT / FENSHEN_RELAY_KEY")
            return
        import ssl
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(CERT, KEY)
    print(f"[relay] 监听 {HOST}:{PORT}  tls={bool(ssl_ctx)}  secret={'set' if SECRET else 'MISSING'}")
    async with websockets.serve(Relay().handler, HOST, PORT, ssl=ssl_ctx):
        await asyncio.Future()  # 常驻


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[relay] 已停止")
