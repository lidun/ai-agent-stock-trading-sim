"""站内通知（web）WebSocket 推送通道（spec-06 §8/§6.13 默认通道）。

core 服务事件（新消息、状态变更、任务态等）经 /ws 推给前端；前端负责断线重连，
服务端只做尽力投递（客户端掉线期间的事件由页面下次拉取补齐，见 spec-04 §6.4）。
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


def register(state, websocket) -> None:
    clients = getattr(state, "ws_clients", None)
    if clients is None:
        state.ws_clients = set()
        clients = state.ws_clients
    clients.add(websocket)


def unregister(state, websocket) -> None:
    clients = getattr(state, "ws_clients", None)
    if clients:
        clients.discard(websocket)


def client_count(state) -> int:
    return len(getattr(state, "ws_clients", set()) or set())


async def broadcast(state, event: dict) -> None:
    """向所有在线连接广播一个事件；发送失败即摘除该连接。"""
    clients = getattr(state, "ws_clients", None)
    if not clients:
        return
    text = json.dumps(event, ensure_ascii=False)
    dead = []
    for ws in list(clients):
        try:
            await ws.send_text(text)
        except Exception as e:  # noqa: BLE001
            log.debug("ws 推送失败（摘除连接）: %s", e)
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
