"""消息处理引擎（P1 桩）——异步推进回执链，产出演示回复。

spec-04 §3（LangGraph 执行层）与 LLM 接入（§8 固定单模型）后续里程碑替换本模块；
当前桩实现用于打通并验证 spec-06 §6.1 要求的：
  - 回执链：已发送(queued) → 处理中(processing) → 已回复(delivered)
  - 站内通知（web）经 WS 实时推送消息与状态事件

每个发往 Agent 的用户消息由 schedule() 编排：入队 → 处理 → 回复（写库 + 广播）。
状态持久在 messages.status；推送事件给在线前端（客户端掉线由下次拉取补齐）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import textwrap

from core import chatstore, ws as ws_channel
from core.auth import audit

log = logging.getLogger(__name__)

# 桩回复的角色感知正文（演示；真实回复由执行层产出）
def _stub_reply_body(agent: dict, user_text: str) -> str:
    preview = textwrap.shorten(user_text.strip().replace("\n", " "), width=120)
    if agent["role"] == "manager":
        lines = [
            f"> 已收到你的输入：**“{preview}”**\n",
            "**回执已送达（P1 桩引擎演示回复）**",
            "",
            "管理 Agent 与子 Agent 的会话链路已经打通，这条消息验证了完整链路：",
            "发送 → 排队 → 处理中 → 已回复，且经 WebSocket 实时推送到本会话。",
            "",
            "- 正式的需求澄清与《需求确认单》生成，由后续里程碑的 LangGraph 执行层接管（spec-04 §3）；",
            "- 你可以先围绕策略想法继续对话，或前往 **Agent 看板** 查看现有子 Agent。",
        ]
    else:
        lines = [
            f"> 已收到你的输入：**“{preview}”**\n",
            "**回执已送达（P1 桩引擎演示回复）**",
            "",
            f"{agent['name']} 的会话链路已连通。作为 P1 演示子 Agent，当前回复由 core 桩引擎产出，",
            "用于验证「联系人式会话 + 回执链 + 实时通知」闭环。",
            "",
            "- 策略执行的每日闭环（选股 → 条件单 → 结算 → 日报）在 **手动跑通一天** 切片中接入；",
            "- 本会话的历史将永久保存，任务收尾沉淀入记忆（spec-02 §6.2）。",
        ]
    return "\n".join(lines)


def _stub_payload(agent: dict) -> str:
    return json.dumps(
        {"kind": "stub_receipt", "engine": "stub", "role": agent["role"]},
        ensure_ascii=False,
    )


async def _step(state, delay_ms: float) -> None:
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)


async def process_message(state, user_message: dict, conversation: dict) -> None:
    """处理一条用户消息：推进状态机并产出一条 Agent 回复（失败置 failed 留痕）。"""
    settings = state.settings
    if not settings.engine_stub_enabled:
        chatstore.set_message_status(state, user_message["id"], "delivered")
        await _notify(state, conversation, "message_status", user_message["id"])
        return

    agent = chatstore.get_agent(state, conversation["agent_id"])
    try:
        # 1) queued 已落库（发送时写入）；广播后推进 processing
        await _notify(state, conversation, "message_status", user_message["id"])

        await _step(state, settings.engine_stub_delay_ms)
        chatstore.set_message_status(state, user_message["id"], "processing")
        await _notify(state, conversation, "message_status", user_message["id"])

        await _step(state, settings.engine_stub_delay_ms * 2)

        # 2) 产出 Agent 回复（本桩为确定性文本；正式引擎按 spec-02 §6.2 白名单 msg_type）
        reply = chatstore.insert_message(
            state,
            conv_id=conversation["id"],
            agent_id=conversation["agent_id"],
            direction="agent",
            msg_type="reply",
            body=_stub_reply_body(agent or {}, user_message["body"]),
            payload_ref=_stub_payload(agent or {}),
            status="delivered",
            delivered_via="web",
        )
        chatstore.set_message_status(state, user_message["id"], "delivered")
        audit(state, conversation["agent_id"], "chat.message_reply", result="ok",
              object_type="message", object_id=reply["id"],
              detail="stub engine 回复（P1 桩）")
        await _notify(state, conversation, "message_new", reply["id"])
        await _notify(state, conversation, "message_status", user_message["id"])
    except Exception as e:  # noqa: BLE001
        log.exception("消息处理失败 conv=%s msg=%s", conversation["id"], user_message["id"])
        chatstore.set_message_status(state, user_message["id"], "failed",
                                           last_error=str(e)[:500])


async def _notify(state, conversation: dict, event: str, message_id: str) -> None:
    if not getattr(state.settings, "notifications_via_ws", True):
        return
    message = chatstore.get_message(state, message_id)
    payload = {"type": "conv", "event": event, "conv_id": conversation["id"], "message": message}
    await ws_channel.broadcast(state, payload)


def schedule(state, user_message: dict, conversation: dict) -> None:
    """把一条已落库的用户消息交给桩引擎异步处理（P1 桩；后续替换为调度器投递）。"""
    asyncio.get_running_loop().create_task(
        process_message(state, user_message, conversation)
    )
