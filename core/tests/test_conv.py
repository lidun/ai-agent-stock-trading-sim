"""对话域测试（spec-02 §6.2 / spec-06 §6.1 数据契约）。

覆盖：Agent 种子、会话开/复用、发送-回执链-回复、未读/已读、历史分页、WS 事件。
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

from conftest import csrf_headers

MANAGER = "agent-manager"
DEMO = "agent-demo-001"


def _send(authed_client, conv_id: str, body: str, expect: int = 200):
    r = authed_client.post(f"/api/conversations/{conv_id}/messages",
                           json={"body": body}, headers=csrf_headers(authed_client))
    assert r.status_code == expect, r.text
    return r.json()["message"] if expect == 200 else None


def _wait_reply(authed_client, conv_id: str, user_msg_id: str, timeout: float = 8.0) -> dict:
    """轮询会话直到用户消息 delivered 且出现 agent 回复（回执链推进）。"""
    deadline = time.time() + timeout
    reply = None
    while time.time() < deadline:
        r = authed_client.get(f"/api/conversations/{conv_id}/messages")
        assert r.status_code == 200
        data = r.json()
        msgs = data["messages"]
        user = next((m for m in msgs if m["id"] == user_msg_id), None)
        if user and user["status"] == "delivered":
            reply = next((m for m in msgs
                          if m["direction"] == "agent" and m["ts"] >= user["ts"]), None)
            if reply:
                return reply
        time.sleep(0.05)
    raise AssertionError("回复未在超时内到达（回执链未走完）")


def test_agents_seeded(authed_client):
    r = authed_client.get("/api/agents")
    assert r.status_code == 200
    agents = r.json()["agents"]
    roles = {a["id"]: a for a in agents}
    assert MANAGER in roles and roles[MANAGER]["role"] == "manager"
    assert DEMO in roles and roles[DEMO]["role"] == "strategy"


def test_conversation_open_create_and_reuse(authed_client):
    r = authed_client.post("/api/conversations", json={"agent_id": MANAGER},
                           headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    conv_id = r.json()["id"]
    r2 = authed_client.post("/api/conversations", json={"agent_id": MANAGER},
                            headers=csrf_headers(authed_client))
    assert r2.json()["id"] == conv_id          # 联系人式单会话复用


def test_conversation_open_unknown_agent_404(authed_client):
    r = authed_client.post("/api/conversations", json={"agent_id": "nope"},
                           headers=csrf_headers(authed_client))
    assert r.status_code == 404


def test_send_reply_chain_and_read(authed_client):
    conv_id = authed_client.post("/api/conversations", json={"agent_id": DEMO},
                                 headers=csrf_headers(authed_client)).json()["id"]
    msg = _send(authed_client, conv_id, "今天请盯一下低波红利板块的候选池。")
    assert msg["status"] == "queued"

    reply = _wait_reply(authed_client, conv_id, msg["id"])
    assert reply["direction"] == "agent"
    assert reply["status"] == "delivered"
    assert reply["msg_type"] == "reply"
    assert reply["delivered_via"] == "web"

    # 回复未读 → 会话未读 1；标记已读后归零
    convs = authed_client.get("/api/conversations").json()["conversations"]
    target = next(c for c in convs if c["id"] == conv_id)
    assert target["unread"] == 1
    read = authed_client.post(f"/api/conversations/{conv_id}/read",
                              headers=csrf_headers(authed_client))
    assert read.json()["updated"] == 1
    convs = authed_client.get("/api/conversations").json()["conversations"]
    assert next(c for c in convs if c["id"] == conv_id)["unread"] == 0


def test_messages_history_pagination(authed_client):
    conv_id = authed_client.post("/api/conversations", json={"agent_id": MANAGER},
                                 headers=csrf_headers(authed_client)).json()["id"]
    for i in range(30):       # 30 条用户消息 → 另有约 30 条桩回复，总计 > 50
        _send(authed_client, conv_id, f"第 {i} 条上下文 {i}")
    deadline = time.time() + 10
    while time.time() < deadline:
        total = len(authed_client.get(f"/api/conversations/{conv_id}/messages").json()["messages"])
        r_all = authed_client.get(f"/api/conversations/{conv_id}/messages?limit=100")
        if r_all.json()["has_older"] is False and len(r_all.json()["messages"]) >= 58:
            break
        time.sleep(0.05)
    r = authed_client.get(f"/api/conversations/{conv_id}/messages")
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 50
    assert data["has_older"] is True and data["next_before_ts"]
    r_older = authed_client.get(
        f"/api/conversations/{conv_id}/messages?"
        + urlencode({"before_ts": data["next_before_ts"], "before_id": data["next_before_id"]}))
    older = r_older.json()
    assert older["has_older"] is False
    assert older["messages"], "更早的一页非空"
    boundary = (data["next_before_ts"], data["next_before_id"])
    assert all((m["ts"], m["id"]) <= boundary for m in older["messages"])


def test_send_requires_csrf(authed_client):
    conv_id = authed_client.post("/api/conversations", json={"agent_id": MANAGER},
                                 headers=csrf_headers(authed_client)).json()["id"]
    r = authed_client.post(f"/api/conversations/{conv_id}/messages", json={"body": "hi"})
    assert r.status_code == 403        # 写请求缺 CSRF 头被中间件拦截


def test_ws_receives_conv_events(authed_client):
    conv_id = authed_client.post("/api/conversations", json={"agent_id": MANAGER},
                                 headers=csrf_headers(authed_client)).json()["id"]
    import json as _json
    events = []
    with authed_client.websocket_connect("/ws") as ws:
        msg = _send(authed_client, conv_id, "ws 事件验证")
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                raw = ws.receive_text()
            except Exception:
                break
            ev = _json.loads(raw)
            if ev.get("type") == "conv":
                events.append(ev)
            if any(e["event"] == "message_new" for e in events):
                break
        assert any(e["event"] == "message_status" for e in events)
        new = next(e for e in events if e["event"] == "message_new")
        assert new["message"]["direction"] == "agent"
        assert new["conv_id"] == conv_id
