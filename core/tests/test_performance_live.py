"""性能监控现状快照（spec-04 §9）：回执链滞留 / 1h 流转 / 审批倒计时 / 进程态。"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from core import performance_live, seed_demo
from core.chatstore import ensure_user_chat
from core.db import state_conn, write_txn


def _iso(delta_s: float) -> str:
    return datetime.fromtimestamp(time.time() + delta_s, timezone.utc).isoformat(
        timespec="seconds")


def _seed_conversation(state):
    conv = ensure_user_chat(state, "agent-demo-001")
    return conv["id"]


def _fixture_live(state):
    """队列滞留（600s 前排队）+ 已送达（60s 前）+ 待决审批（8h 后过期）。"""
    seed_demo.seed(state)
    conv_id = _seed_conversation(state)
    c = state_conn(state)
    with write_txn(c) as cw:
        cw.execute(
            "INSERT INTO messages (id, conv_id, agent_id, direction, msg_type, status, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            ("m-queue", conv_id, "agent-demo-001", "user", "chat", "queued",
             _iso(-600)),
        )
        cw.execute(
            "INSERT INTO messages (id, conv_id, agent_id, direction, msg_type, status, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            ("m-done", conv_id, "agent-demo-001", "agent", "chat", "delivered",
             _iso(-60)),
        )
        cw.execute(
            "INSERT INTO approval_requests (id, type, agent_id, expires_ts, created_ts)"
            " VALUES (?,?,?,?,?)",
            ("ap-pending", "capability", "agent-demo-001", _iso(8 * 3600), _iso(-60)),
        )


def test_snapshot_live_tasks_and_msg_volume(authed_client):
    state = authed_client.app.state
    _fixture_live(state)
    out = performance_live.snapshot(state, started_ts=time.time() - 100)

    assert out["uptime_s"] == 100
    assert out["tasks"]["total"] >= 2
    assert out["tasks"]["status"].get("queued") == 1
    assert out["tasks"]["live"] == 1
    stale = out["tasks"]["stale_active"][0]
    assert stale["status"] == "queued" and 580 <= stale["age_s"] <= 620
    assert stale["agent_id"] == "agent-demo-001"

    assert out["msg_1h"]["delivered"] == 1
    assert out["msg_1h"]["user_requests"] == 1
    assert out["approvals"]["pending"] == 1
    assert 0 < out["approvals"]["next_expires_in_s"] <= 8 * 3600
    assert out["last_settle"] is None


def test_snapshot_empty_db_zero_aggregation(authed_client):
    state = authed_client.app.state
    seed_demo.seed(state)
    out = performance_live.snapshot(state)
    assert out["tasks"]["total"] == 0 and out["approvals"]["pending"] == 0
    assert out["msg_1h"]["delivered"] == 0
    assert out["tasks"]["stale_active"] == []


def test_performance_route_requires_session_and_returns_process(client):
    seed_demo.seed(client.app.state)
    r = client.get("/api/performance/live")
    assert r.status_code == 401


def test_performance_route_authed_merges_process_state(authed_client):
    state = authed_client.app.state
    _fixture_live(state)
    r = authed_client.get("/api/performance/live")
    assert r.status_code == 200
    body = r.json()
    assert body["approvals"]["pending"] == 1
    assert body["process"]["ws_clients"] == 0
    assert body["process"]["engine_stub_delay_ms"] == 0
