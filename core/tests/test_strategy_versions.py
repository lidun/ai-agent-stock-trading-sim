"""spec-02 §9 策略版本化：checkpoint/activate/rollback 状态机 + 演进记忆事件。"""
from __future__ import annotations

import pytest

from core.db import state_conn
from core import strategy_versions as sv

CFG_V1 = {"selection": {"filter": "dividend", "top": 30},
          "risk": {"single_stock_cap": 0.1}}
CFG_V2 = {**CFG_V1, "exit": {"window_days": 3}}
DIFF_V2 = {"exit": {"window_days": 3}}


def test_checkpoint_activate_rollback_chain(authed_client):
    st = authed_client.app.state
    v1 = sv.checkpoint(st, "agent-demo-001", version_no="v1", config=CFG_V1,
                       basis=["sig-1", "mem-opt-001"],
                       trial_window={"window_days": 10, "cap_ceiling": 0.3})
    assert v1["status"] == "draft" and v1["parent_version"] == ""
    assert v1["basis"] == ["sig-1", "mem-opt-001"]

    from core import strategy_memory
    mem = strategy_memory.list_strategy_memory(st, "agent-demo-001")["items"]
    assert any(m["version_no"] == "v1"
               and m["source"].startswith("engine:checkpoint")
               and m["ref_ids"] == ["sig-1", "mem-opt-001"] for m in mem)

    sv.activate(st, "agent-demo-001", "v1")
    assert sv.get_version(st, "agent-demo-001", "v1")["status"] == "active"
    assert sv.get_version(st, "agent-demo-001", "v1")["validated_on"]

    v2 = sv.checkpoint(st, "agent-demo-001", version_no="v2", config=CFG_V2,
                       config_diff=DIFF_V2, basis=["mem-opt-002"])
    assert v2["parent_version"] == "v1"

    sv.activate(st, "agent-demo-001", "v2")
    assert sv.get_version(st, "agent-demo-001", "v2")["status"] == "active"
    assert sv.get_version(st, "agent-demo-001", "v1")["status"] == "validated"

    rb = sv.rollback(st, "agent-demo-001", reason="验证失败：期望值 <-2%")
    assert rb["ok"] is True
    assert rb["rolled_back"] == "v2" and rb["active_version"] == "v1"
    v2r = sv.get_version(st, "agent-demo-001", "v2")
    assert v2r["status"] == "rolled_back"
    assert v2r["failure_reason"] == "验证失败：期望值 <-2%"
    assert v2r["rolled_back_to"] == "v1"

    hist = sv.list_versions(st, "agent-demo-001")
    assert hist["active_version"] == "v1"
    # v1 回退后重新 active，v2 标 rolled_back；无残留 validated（其前身已回退）
    assert {v["status"] for v in hist["items"]} == {"active", "rolled_back"}


def test_duplicate_version_rejected(authed_client):
    st = authed_client.app.state
    sv.checkpoint(st, "agent-demo-001", version_no="v1", config=CFG_V1)
    with pytest.raises(ValueError):
        sv.checkpoint(st, "agent-demo-001", version_no="v1", config=CFG_V1)


def test_rollback_no_validated_target_rejects_and_alerts(authed_client):
    st = authed_client.app.state
    sv.checkpoint(st, "agent-demo-001", version_no="v1", config=CFG_V1)
    sv.activate(st, "agent-demo-001", "v1")
    rb = sv.rollback(st, "agent-demo-001", reason="首版本运行中出事")
    assert rb["ok"] is False and rb["reason"] == "no_validated_target"
    assert sv.get_version(st, "agent-demo-001", "v1")["status"] == "active"

    n = state_conn(st).execute(
        "SELECT COUNT(*) n FROM messages WHERE msg_type='strategy_alert'"
    ).fetchone()["n"]
    assert n == 1
    sv.rollback(st, "agent-demo-001", reason="首版本运行中出事")
    assert state_conn(st).execute(
        "SELECT COUNT(*) n FROM messages WHERE msg_type='strategy_alert'"
    ).fetchone()["n"] == 1  # 幂等，不重复告警


def test_guards(authed_client):
    st = authed_client.app.state
    with pytest.raises(ValueError):
        sv.checkpoint(st, "agent-demo-001", version_no="v9", config={},
                      created_by="strategy_agent")
    with pytest.raises(ValueError):
        sv.checkpoint(st, "agent-demo-001", version_no="v9", config=CFG_V1,
                      created_by="nobody")
    with pytest.raises(LookupError):
        sv.activate(st, "agent-demo-001", "no-such")


def test_read_routes(authed_client):
    st = authed_client.app.state
    sv.checkpoint(st, "agent-demo-001", version_no="v1", config=CFG_V1)
    sv.activate(st, "agent-demo-001", "v1")
    r = authed_client.get("/api/agents/agent-demo-001/strategy-versions")
    assert r.status_code == 200
    body = r.json()
    assert body["active_version"] == "v1" and body["total"] == 1
    one = authed_client.get(
        "/api/agents/agent-demo-001/strategy-versions/v1").json()["version"]
    assert one["config"]["risk"]["single_stock_cap"] == 0.1
    assert authed_client.get(
        "/api/agents/agent-demo-001/strategy-versions/nope").status_code == 404
