"""
分身 · 桌面隧道客户端（桥）

把本机 127.0.0.1:8002/ws（移动端桥接，由 relay_ws.py 提供）通过公网中继暴露给手机。
- 连本机 /ws，首帧用 RELAY_TOKEN 鉴权（复用 relay_ws 协议，零改动）。
- 连公网中继，首帧用 tunnel_id/token 鉴权，role=desktop。
- 两路 WebSocket 双向透传业务帧，自动重连。

被 backend.main 的启动任务调用：relay_enabled 且隧道配置齐全时拉起。
"""
import asyncio
import json
import os
import ssl

try:
    from backend.relay.protocol import AUTH, AUTH_OK, derive_tunnel_token
except ImportError:  # pragma: no cover
    from relay.protocol import AUTH, AUTH_OK, derive_tunnel_token

import websockets

LOCAL_WS = os.environ.get("FENSHEN_LOCAL_WS", "ws://127.0.0.1:8002/ws")
RELAY_TOKEN = os.environ.get("FENSHEN_RELAY_TOKEN", "fenshen_mobile_v1")
RELAY_WS = os.environ.get("FENSHEN_RELAY_URL", "ws://127.0.0.1:8848")
SECRET = os.environ.get("FENSHEN_RELAY_SECRET", "")
INSECURE = os.environ.get("FENSHEN_RELAY_INSECURE", "0") == "1"


def _relay_uri(tunnel_id: str) -> str:
    sep = "&" if "?" in RELAY_WS else "?"
    return f"{RELAY_WS}{sep}tunnel={tunnel_id}"


def _ssl_ctx():
    if RELAY_WS.startswith("wss://") and INSECURE:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None  # 默认走系统证书校验


async def _bridge(local, relay):
    """双向透传：任一端关闭则整体结束。"""
    async def pipe(src, dst):
        try:
            async for frame in src:
                try:
                    await dst.send(frame)
                except Exception:
                    break
        except Exception:
            pass
        finally:
            try:
                await dst.close()
            except Exception:
                pass
    await asyncio.gather(pipe(local, relay), pipe(relay, local))


async def run_tunnel(tunnel_id: str, token: str = None, relay_url: str = None):
    """持久运行：连本机 + 连中继，双向透传，断线重连。

    relay_url 可显式传入（来自设置/环境变量）；缺省取 FENSHEN_RELAY_URL，再回退本机 8848。
    """
    if not token:
        token = derive_tunnel_token(SECRET, tunnel_id)
    if relay_url:
        global RELAY_WS
        RELAY_WS = relay_url
    uri = _relay_uri(tunnel_id)
    ctx = _ssl_ctx()
    while True:
        try:
            # 1) 本机 /ws 鉴权
            local = await websockets.connect(LOCAL_WS)
            await local.send(json.dumps({"type": "auth", "token": RELAY_TOKEN}))
            local_ok = json.loads(await local.recv())
            if local_ok.get("type") != "auth_ok":
                print("[tunnel] 本机 /ws 鉴权失败，重试…")
                await local.close()
                await asyncio.sleep(5)
                continue
            # 2) 中继鉴权
            relay = await websockets.connect(uri, ssl=ctx)
            await relay.send(json.dumps(
                {"type": AUTH, "role": "desktop", "token": token, "tunnel": tunnel_id}))
            relay_ok = json.loads(await relay.recv())
            if relay_ok.get("type") != AUTH_OK:
                print("[tunnel] 中继鉴权失败：", relay_ok)
                await relay.close()
                await local.close()
                await asyncio.sleep(10)
                continue
            print(f"[tunnel] 已连本机+中继 tunnel={tunnel_id[:8]}… 透传中")
            await _bridge(local, relay)
            print("[tunnel] 连接断开，重连中…")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa
            print("[tunnel] 异常：", repr(e))
        await asyncio.sleep(5)
