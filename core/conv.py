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

from core import accountstore, chatstore, engine, orderstore
from core.auth import audit, get_request_context, require_session
from core.ws import broadcast

router = APIRouter(prefix="/api", tags=["conversations"])

SessionDep = Annotated[dict, Depends(require_session)]

MSG_LIMIT = 50


def _notify_control(state, agent_id: str, body: str) -> dict | None:
    """直控干预自动通知子 Agent 纳入复盘（spec-06 §6.3：消息出现在对话面板）。"""
    try:
        conv = chatstore.ensure_user_chat(state, agent_id)
    except LookupError:
        return None
    return chatstore.insert_message(
        state, conv_id=conv["id"], agent_id=agent_id,
        direction="agent", msg_type="control", body=body,
        status="delivered", delivered_via="system",
    )


class ConversationCreateIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)


class AgentCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class TrialFinishIn(BaseModel):
    decision: str = Field(pattern="^(launch|reject)$")
    verdict: str = Field(default="", max_length=500)


class ControlIn(BaseModel):
    op: str = Field(pattern="^(pause_buy|halt|resume)$")


class FrozenIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    reason: str = Field(default="", max_length=200)


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


@router.get("/agents/{agent_id}/trial/progress")
def trial_progress(agent_id: str, request: Request, session: SessionDep):
    """试运行验收进度（spec-05 §6.1 门槛预览，spec-06 §6.4 试运行态展示）。"""
    progress = accountstore.trial_progress(request.app.state, agent_id)
    if progress is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")
    return progress


@router.get("/frozen")
def frozen_list(request: Request, session: SessionDep,
                agent_id: str | None = None):
    """冻结证券清单（常驻展示，spec-06 §6.3）。"""
    return {"frozen": accountstore.list_frozen(request.app.state, agent_id=agent_id)}


@router.post("/agents/{agent_id}/frozen")
def freeze_security(agent_id: str, payload: FrozenIn,
                    request: Request, session: SessionDep):
    """冻结证券：单票买入即时冻结（同事务取消该票 active 买入单）。"""
    try:
        result = accountstore.freeze_security(
            request.app.state, agent_id=agent_id, symbol=payload.symbol.strip(),
            reason=payload.reason, operator=session["session"]["username"])
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit(request.app.state, session["session"]["username"],
          "account.freeze_security", result="ok",
          object_type="account", object_id=result["account_id"],
          detail=f"冻结证券 {payload.symbol}：取消该票买入单 {result['cancelled_buy_orders']} 张",
          ctx=get_request_context(request))
    _notify_control(request.app.state, agent_id,
                    f"## 直控干预 · 冻结证券 {payload.symbol}\n"
                    f"- 取消该票买入条件单 {result['cancelled_buy_orders']} 张（保留卖出与风控）\n"
                    f"- 生效：即时；请将本次干预纳入复盘")
    return result


@router.delete("/agents/{agent_id}/frozen/{symbol}")
def unfreeze_security(agent_id: str, symbol: str,
                      request: Request, session: SessionDep):
    """解除冻结证券：恢复可买。"""
    try:
        result = accountstore.unfreeze_security(
            request.app.state, agent_id=agent_id, symbol=symbol,
            operator=session["session"]["username"])
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit(request.app.state, session["session"]["username"],
          "account.unfreeze_security", result="ok",
          object_type="account", object_id=result["agent_id"],
          detail=f"解除冻结证券 {symbol}",
          ctx=get_request_context(request))
    _notify_control(request.app.state, agent_id,
                    f"## 直控干预 · 解除冻结 {symbol}\n"
                    f"- 已恢复买入（此前被取消的单据不自动重建）\n"
                    f"- 生效：即时；请将本次干预纳入复盘")
    return result


@router.patch("/agents/{agent_id}/control")
def control_agent(agent_id: str, payload: ControlIn,
                  request: Request, session: SessionDep):
    """用户直控（spec-06 §6.3）：冻结买入/熔断冻结/解除恢复，秒级生效、审计留痕。"""
    label = {"pause_buy": "冻结买入（保留卖出与风控）",
             "halt": "熔断冻结（买卖全停）",
             "resume": "解除冻结恢复"}[payload.op]
    try:
        result = accountstore.control_agent(
            request.app.state, agent_id=agent_id, op=payload.op)
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit(request.app.state, session["session"]["username"],
          f"account.control_{payload.op}", result="ok",
          object_type="account", object_id=result["account"]["id"],
          detail=f"直控 {label}：账户状态 {result['from']} → {result['to']}",
          ctx=get_request_context(request))
    _notify_control(request.app.state, agent_id,
                    f"## 直控干预 · {label}\n"
                    f"- 账户状态：{result['from']} → {result['to']}\n"
                    f"- 生效：即时（引擎闸门秒级拦截）\n"
                    f"- 请将本次干预纳入复盘（审计 account.control_{payload.op} 已留痕）")
    return result


@router.post("/agents/{agent_id}/control/sell-all")
def emergency_sell_all(agent_id: str, request: Request, session: SessionDep):
    """紧急清仓（spec-06 §6.3）：逐票市价卖出条件单即时生成，不经 LLM。"""
    try:
        result = orderstore.emergency_sell_all(
            request.app.state, agent_id=agent_id)
    except orderstore.OrderError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit(request.app.state, session["session"]["username"],
          "account.emergency_sell", result="ok",
          object_type="account", object_id=result["account_id"],
          detail=f"紧急清仓：生成卖出条件单 {len(result['orders'])} 张"
                 f"（持仓 {result['holdings']} 只）",
          ctx=get_request_context(request))
    if result["blocked_halted"]:
        note = "账户处于熔断冻结态，卖出被闸门拦截，本次清仓未生成任何条件单"
    else:
        note = f"已为 {result['holdings']} 只持仓生成 {len(result['orders'])} 张市价卖出条件单"
    _notify_control(request.app.state, agent_id,
                    f"## 直控干预 · 紧急清仓\n"
                    f"- {note}\n"
                    f"- 生效：即时（不经 LLM）\n"
                    f"- 请将本次干预纳入复盘（审计 account.emergency_sell 已留痕）")
    return result


@router.post("/agents/{agent_id}/trial/finish")
def finish_trial(agent_id: str, payload: TrialFinishIn,
                 request: Request, session: SessionDep):
    """试运行验收（spec-05 §6.2）：launch 通过/reject 否决 → trial 归档留证、主账户零污染。"""
    try:
        archived = accountstore.finish_trial(
            request.app.state, agent_id=agent_id, decision=payload.decision,
            verdict=payload.verdict)
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit(request.app.state, session["session"]["username"],
          "account.trial_finish", result="ok",
          object_type="trial_archive", object_id=archived["archive_id"],
          detail=f"试运行验收 {payload.decision}：trial 账户归档留证",
          ctx=get_request_context(request))
    return archived


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
