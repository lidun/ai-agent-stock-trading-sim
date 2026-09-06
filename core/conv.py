"""对话域路由（spec-06 §6.1 对话面板数据契约；spec-02 §6.2 会话/消息）。

- GET  /api/agents                Agent 注册表（联系人列表来源）
- GET  /api/conversations         会话列表（含未读数与最近一条）
- POST /api/conversations         打开/创建用户会话（agent+user_chat 唯一）
- GET  /api/conversations/{id}/messages   消息历史（上翻分页）
- POST /api/conversations/{id}/read       标记已读（滚动锚定到首条未读）
- POST /api/conversations/{id}/messages   发送用户消息 → 桩引擎异步回执
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import accountstore, chatstore, engine
from core.auth import audit, get_request_context, require_session
from core.ws import broadcast

router = APIRouter(prefix="/api", tags=["conversations"])

SessionDep = Annotated[dict, Depends(require_session)]

MSG_LIMIT = 50


class ConversationCreateIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)


class AgentCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class MessageSendIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


# ---------- Agents ----------

@router.get("/agents")
def agents(request: Request, session: SessionDep):
    return {"agents": chatstore.list_agents(request.app.state)}


@router.post("/agents")
def create_trial_agent(payload: AgentCreateIn, request: Request, session: SessionDep):
    """开通试运行策略子 Agent：随建主 normal + trial 双账户（spec-01 §2.8 #63）。"""
    import secrets as _secrets

    agent_id = "agent-" + _secrets.token_hex(6)
    try:
        created = accountstore.create_trial_agent(
            request.app.state, agent_id=agent_id, name=payload.name.strip())
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit(request.app.state, session["session"]["username"],
          "account.trial_agent_create", result="ok",
          object_type="agent", object_id=agent_id,
          detail=f"开通试运行子 Agent {payload.name}（main+trial 双账户）",
          ctx=get_request_context(request))
    return created


# ---------- Conversations ----------

@router.get("/conversations")
def conversations(request: Request, session: SessionDep):
    return {"conversations": chatstore.list_conversations(request.app.state)}


@router.post("/conversations")
def open_conversation(payload: ConversationCreateIn, request: Request,
                      session: SessionDep):
    try:
        conv = chatstore.ensure_user_chat(request.app.state, payload.agent_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    audit(request.app.state, session["session"]["username"], "chat.conversation_open",
          object_type="conversation", object_id=conv["id"], result="ok",
          ctx=get_request_context(request))
    return conv


# ---------- Messages ----------

@router.get("/conversations/{conv_id}/messages")
def messages(conv_id: str, request: Request, session: SessionDep,
             before_ts: str | None = None, before_id: str | None = None,
             limit: int = MSG_LIMIT):
    conv = chatstore.get_conversation(request.app.state, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    limit = max(1, min(limit, 100))
    page, has_older = chatstore.list_messages(request.app.state, conv_id,
                                              before_ts=before_ts, before_id=before_id,
                                              limit=limit)
    first = page[0] if page else None
    return {
        "conversation": conv,
        "messages": page,
        "has_older": has_older,
        "next_before_ts": first["ts"] if (has_older and first) else None,
        "next_before_id": first["id"] if (has_older and first) else None,
    }


@router.post("/conversations/{conv_id}/read")
async def mark_read(conv_id: str, request: Request, session: SessionDep):
    conv = chatstore.get_conversation(request.app.state, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    updated = chatstore.mark_conversation_read(request.app.state, conv_id)
    if updated:
        await broadcast(request.app.state, {
            "type": "conv", "event": "conversation_read",
            "conv_id": conv_id, "updated": updated,
        })
    return {"updated": updated}


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, payload: MessageSendIn, request: Request,
                       session: SessionDep):
    conv = chatstore.get_conversation(request.app.state, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    msg = chatstore.insert_message(
        request.app.state,
        conv_id=conv_id,
        agent_id=conv["agent_id"],
        direction="user",
        msg_type="ask",
        body=payload.body,
        status="queued",
        delivered_via="web",
    )
    audit(request.app.state, session["session"]["username"], "chat.message_send",
          object_type="message", object_id=msg["id"], result="ok",
          ctx=get_request_context(request))
    # 异步处理（P1 桩引擎；后续替换为调度器按任务投递 LangGraph）
    engine.schedule(request.app.state, msg, conv)
    return {"message": msg}
