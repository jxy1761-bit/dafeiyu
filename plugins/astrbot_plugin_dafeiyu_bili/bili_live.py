"""B 站直播弹幕：WebSocket 收弹幕（protover=2，zlib），不依赖 brotli。"""

from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib

import aiohttp

from . import bili_api as api

HEADER = struct.Struct(">IHHII")


def pack(op: int, body: bytes = b"", ver: int = 1) -> bytes:
    head = HEADER.pack(HEADER.size + len(body), HEADER.size, ver, op, 1)
    return head + body


def unpack(data: bytes):
    """递归拆包，产出 (operation, payload)。"""
    offset = 0
    while offset + HEADER.size <= len(data):
        total, hlen, ver, op, _seq = HEADER.unpack_from(data, offset)
        if total < HEADER.size or hlen < HEADER.size:
            break
        payload = data[offset + hlen:offset + total]
        if ver == 2:
            try:
                yield from unpack(zlib.decompress(payload))
            except Exception:
                pass
        elif ver == 3:
            try:
                import brotli  # type: ignore
                yield from unpack(brotli.decompress(payload))
            except Exception:
                pass
        else:
            yield op, payload
        offset += total


def _danmaku_text(info: list) -> tuple[str, str]:
    text = str(info[1] or "") if len(info) > 1 else ""
    user = ""
    try:
        user = str(info[2][1] or "")
    except Exception:
        pass
    return user, text


async def collect(room_id: str, cred: dict | None, seconds: int = 60,
                  on_batch=None, batch_seconds: int = 60) -> list[dict]:
    """进直播间收弹幕，最多 seconds 秒。on_batch(list) 每 batch_seconds 回调一次。"""
    room = await api.live_room_info(room_id, cred)
    real = str(room.get("room_id") or room_id)
    info = await api.live_danmu_info(real, cred)
    token = str(info.get("token") or "")
    hosts = info.get("host_list") or []
    if not hosts:
        raise RuntimeError("没有拿到弹幕服务器地址")
    uid = int(cred.get("mid") or 0) if cred else 0
    auth = json.dumps({
        "uid": uid, "roomid": int(real), "protover": 2,
        "platform": "web", "type": 2, "key": token,
        "buvid": str((cred or {}).get("buvid3") or ""),
    }, ensure_ascii=False).encode()

    last_err = None
    ws = None
    sess = aiohttp.ClientSession()
    for host in hosts:
        h = str(host.get("host") or "")
        if not h:
            continue
        port = int(host.get("wss_port") or 443)
        url = f"wss://{h}/sub" if port == 443 else f"wss://{h}:{port}/sub"
        try:
            ws = await sess.ws_connect(url, timeout=10, heartbeat=None)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
    if ws is None:
        await sess.close()
        raise RuntimeError(f"连不上弹幕服务器：{last_err}")

    out: list[dict] = []
    batch: list[dict] = []
    deadline = time.time() + max(5, int(seconds))
    next_batch = time.time() + max(10, int(batch_seconds))
    last_hb = 0.0

    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            await ws.send_bytes(pack(2))

    hb = asyncio.create_task(heartbeat())
    try:
        await ws.send_bytes(pack(7, auth))
        while time.time() < deadline:
            timeout = max(0.5, min(5.0, deadline - time.time()))
            try:
                msg = await ws.receive(timeout=timeout)
            except asyncio.TimeoutError:
                continue
            if msg.type == aiohttp.WSMsgType.BINARY:
                for op, payload in unpack(msg.data):
                    if op != 5:
                        continue
                    try:
                        d = json.loads(payload.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    cmd = str(d.get("cmd") or "")
                    if not cmd.startswith("DANMU_MSG"):
                        continue
                    info2 = d.get("info") or []
                    user, text = _danmaku_text(info2)
                    if not text:
                        continue
                    item = {"t": int(time.time()), "user": user, "text": text[:120]}
                    out.append(item)
                    batch.append(item)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
            if on_batch and time.time() >= next_batch and batch:
                try:
                    await on_batch(batch[:])
                except Exception:
                    pass
                batch = []
                next_batch = time.time() + max(10, int(batch_seconds))
    except asyncio.CancelledError:
        raise
    finally:
        hb.cancel()
        try:
            await ws.close()
        except Exception:
            pass
        await sess.close()
    if on_batch and batch:
        try:
            await on_batch(batch)
        except Exception:
            pass
    return out
