"""审批流测试（spec-04 §4.1/§4.2/§4.4 最小确定性版）。"""
from __future__ import annotations

import json

from core import approval
from core.db import state_conn

DEMO = "agent-demo-001"


def _exemption_payload(tokens):
    return {"tokens": tokens}


def test_submit_approve_exemption_grants_buy_exempt(authed_client):
    """exemption 效果器：通过后 tokens 并入 accounts.buy_exempt，result_ref 留生效值。"""
    st = authed_client.app.state
    r = approval.submit_approval(
        st, type_="exemption", agent_id=DEMO,
        payload=_exemption_payload(["st", "new"]), reason="拟布局 ST 摘帽博取超额收益",
        requested_by="agent-demo-001")
    assert r["ok"] is True
    ap = r["approval"]
    assert ap["type"] == "exemption" and ap["status"] == "pending"
    assert ap["payload"]["tokens"] == ["st", "new"]
    decided = approval.decide_approval(st, ap["id"], decision="approved",
                                       reason="符合风控口径", decided_by="user")
    assert decided["ok"] is True and decided["approval"]["status"] == "approved"
    got = json.loads(decided["approval"]["result_ref"])
    assert got == ["st", "new"]
    acc = state_conn(st).execute(
        "SELECT buy_exempt FROM accounts WHERE id=?", (DEMO,)).fetchone()
    assert json.loads(acc["buy_exempt"]) == ["st", "new"]
    # 同内容再次申请 → 确定性短路命中已决，附上次结果，零新增行
    r2 = approval.submit_approval(
        st, type_="exemption", agent_id=DEMO,
        payload=_exemption_payload(["st", "new"]), reason="拟布局 ST 摘帽博取超额收益",
        requested_by="agent-demo-001")
    assert r2["ok"] is False and r2["reason"] == "hash_hit_approved"
    assert r2["approval"]["status"] == "approved"
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 1
    # 审计留痕
    log = state_conn(st).execute(
        "SELECT action, result FROM audit_logs"
        " WHERE action IN ('approval.submit','approval.decide') ORDER BY ts").fetchall()
    assert [tuple(x) for x in log] == [("approval.submit", "pending"),
                                       ("approval.decide", "approved")]


def test_submit_reject_hash_hit_and_cooldown(authed_client):
    """确定性短路：同哈希命中已决→附上次结果；驳回同类 24h 冷却可被 UI 豁免。"""
    st = authed_client.app.state
    r = approval.submit_approval(
        st, type_="risk", agent_id=DEMO,
        payload={"scope": "suspension_override", "symbol": "600000"},
        reason="停牌票复牌前先挂单")
    assert r["ok"] is True
    approval.decide_approval(st, r["approval"]["id"], decision="rejected",
                             reason="与复牌语义冲突", decided_by="user")
    # 同内容再次申请 → 直接附上次驳回结果，不进评审
    again = approval.submit_approval(
        st, type_="risk", agent_id=DEMO,
        payload={"scope": "suspension_override", "symbol": "600000"},
        reason="停牌票复牌前先挂单")
    assert again["ok"] is False and again["reason"] == "hash_hit_rejected"
    # 不同内容同 type → 冷却期退回；用户人工放行可提交
    other = approval.submit_approval(
        st, type_="risk", agent_id=DEMO,
        payload={"scope": "other"}, reason="新场景申请")
    assert other["ok"] is False and other["reason"] == "cooldown"
    other2 = approval.submit_approval(
        st, type_="risk", agent_id=DEMO,
        payload={"scope": "other"}, reason="新场景申请", cooldown_exempt=True)
    assert other2["ok"] is True


def test_pending_quota_limit_and_freed(authed_client):
    """§4.2 配额：每 Agent 每类型 pending ≤3，超限退回并提示待决单；决毕释放。"""
    st = authed_client.app.state
    ids = []
    for i in range(3):
        r = approval.submit_approval(
            st, type_="granularity", agent_id=DEMO,
            payload={"proposal": f"intraday_{i}"}, reason=f"第 {i} 档方案申请")
        assert r["ok"] is True
        ids.append(r["approval"]["id"])
    over = approval.submit_approval(
        st, type_="granularity", agent_id=DEMO,
        payload={"proposal": "intraday_3"}, reason="第 3 档方案申请")
    assert over["ok"] is False and over["reason"] == "pending_full"
    assert set(over["pending_ids"]) == set(ids)
    # 通过一件释放额度 → 新申请可进（无驳回，不触发冷却）
    approval.decide_approval(st, ids[0], decision="approved", reason="方案可行",
                             decided_by="user")
    r = approval.submit_approval(
        st, type_="granularity", agent_id=DEMO,
        payload={"proposal": "intraday_3"}, reason="第 3 档方案申请")
    assert r["ok"] is True


def test_decide_guards_and_expiry_sweep(authed_client):
    """§4.4 过期：pending 过期不可决（迟到仅留痕）、清扫标 expired；未知/二次决退回。"""
    st = authed_client.app.state
    assert approval.decide_approval(st, "no-such", decision="approved")["reason"] == "not_found"
    r = approval.submit_approval(
        st, type_="task", agent_id=DEMO,
        payload={"task": "rebalance"}, reason="月度调仓申请",
        expires_seconds=-1)                      # 立即过期
    assert r["ok"] is True
    # 先清扫（调度器路径）→ expired
    assert approval.expire_overdue(st) == 1
    ap = approval.get_approval(st, r["approval"]["id"])
    assert ap["status"] == "expired" and ap["close_note"].startswith("24h")
    # 过期后再决 → 仅留痕不生效（reason=already，审计 result=expired）
    late = approval.decide_approval(st, r["approval"]["id"], decision="approved",
                                    reason="迟到通过", decided_by="user")
    assert late["ok"] is False and late["reason"] == "already"
    late = approval.get_approval(st, r["approval"]["id"])
    assert late["status"] == "expired"
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='approval.decide'"
        " AND result='expired'").fetchone()[0] == 1
    # 二次决：非 pending 不可再决
    r2 = approval.submit_approval(
        st, type_="task", agent_id=DEMO,
        payload={"task": "rebalance2"}, reason="调仓申请 2")
    assert r2["ok"] is True
    assert approval.decide_approval(st, r2["approval"]["id"],
                                    decision="approved")["ok"] is True
    again = approval.decide_approval(st, r2["approval"]["id"],
                                     decision="rejected")
    assert again["ok"] is False and again["reason"] == "already"
    # 非法值守卫
    try:
        approval.submit_approval(st, type_="nope", agent_id=DEMO,
                                 payload={}, reason="x")
        raise AssertionError("应拒绝未知 type")
    except ValueError:
        pass


def test_list_approvals_filter_and_default_status(authed_client):
    st = authed_client.app.state
    approval.submit_approval(
        st, type_="exemption", agent_id=DEMO, payload=_exemption_payload(["st"]),
        reason="ST 豁免申请")
    assert len(approval.list_approvals(st, status="pending")) == 1
    assert len(approval.list_approvals(st, status="approved")) == 0
    assert len(approval.list_approvals(st, agent_id=DEMO, status="pending")) == 1
    assert approval.list_approvals(st, agent_id="no-such") == []
