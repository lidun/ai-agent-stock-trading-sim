"""消息白名单与审阅测试（spec-02 §6.2）。"""
from __future__ import annotations

import pytest

from core import chatstore
from core.db import state_conn
from core.tests.conftest import csrf_headers

DEMO = "agent-demo-001"


def _conv(st):
    return chatstore.ensure_user_chat(st, DEMO)


def test_whitelisted_delivers(authed_client):
    st = authed_client.app.state
    conv = _conv(st)
    r = chatstore.deliver_agent_message(
        st, conv_id=conv["id"], agent_id=DEMO, msg_type="日报", body="今日日报")
    assert r["delivered"] is True and r["pending_review"] is False
    assert r["message"]["status"] == "delivered"


def test_non_whitelist_pending_review(authed_client):
    st = authed_client.app.state
    conv = _conv(st)
    r = chatstore.deliver_agent_message(
        st, conv_id=conv["id"], agent_id=DEMO, msg_type="闲聊", body="你好呀")
    assert r["delivered"] is False and r["pending_review"] is True
    assert r["message"]["status"] == "pending_review"
    pend = chatstore.list_pending_reviews(st)
    assert any(p["id"] == r["message"]["id"] for p in pend)
    row = state_conn(st).execute(
        "SELECT 1 FROM audit_logs WHERE action='message.pending_review'"
        " AND object_id=?", (r["message"]["id"],)).fetchone()
    assert row is not None


def test_review_approve_release(authed_client):
    st = authed_client.app.state
    conv = _conv(st)
    r = chatstore.deliver_agent_message(
        st, conv_id=conv["id"], agent_id=DEMO, msg_type="闲聊", body="x")
    mid = r["message"]["id"]
    out = chatstore.review_message(st, mid, approve=True, reviewer="admin")
    assert out["changed"] is True and out["approved"] is True
    assert out["message"]["status"] == "delivered"
    assert chatstore.list_pending_reviews(st) == []


def test_review_reject(authed_client):
    st = authed_client.app.state
    conv = _conv(st)
    r = chatstore.deliver_agent_message(
        st, conv_id=conv["id"], agent_id=DEMO, msg_type="闲聊", body="x")
    mid = r["message"]["id"]
    out = chatstore.review_message(st, mid, approve=False, reason="不允许闲聊")
    assert out["message"]["status"] == "failed"
    row = state_conn(st).execute(
        "SELECT last_error FROM messages WHERE id=?", (mid,)).fetchone()
    assert row["last_error"] == "不允许闲聊"


def test_review_non_pending_noop(authed_client):
    st = authed_client.app.state
    conv = _conv(st)
    r = chatstore.deliver_agent_message(
        st, conv_id=conv["id"], agent_id=DEMO, msg_type="日报", body="ok")
    out = chatstore.review_message(st, r["message"]["id"], approve=True)
    assert out["changed"] is False


def test_review_unknown_raises(authed_client):
    with pytest.raises(LookupError):
        chatstore.review_message(authed_client.app.state, "nope", approve=True)


def test_review_routes(authed_client):
    st = authed_client.app.state
    conv = _conv(st)
    r = chatstore.deliver_agent_message(
        st, conv_id=conv["id"], agent_id=DEMO, msg_type="闲聊", body="x")
    mid = r["message"]["id"]
    pend = authed_client.get("/api/messages/pending-review")
    assert pend.status_code == 200 and any(
        i["id"] == mid for i in pend.json()["items"])
    out = authed_client.post(f"/api/messages/{mid}/review",
                             json={"approve": True},
                             headers=csrf_headers(authed_client))
    assert out.status_code == 200 and out.json()["message"]["status"] == "delivered"
