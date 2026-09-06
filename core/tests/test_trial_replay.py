"""试运行历史回放切片（spec-05 §6.2）：窗口台账 / 会话记账 / 结算隔离 / 最近 N 回放。"""
from __future__ import annotations

from datetime import date, datetime

from core import accountstore, orderstore, settle_day
from core.db import state_conn
from core.settle_scheduler import EodSettleTrigger
from conftest import csrf_headers

from _feedkit import AxisFeed

NOW = datetime(2026, 9, 5, 15, 40)


def _mk_agent(authed_client, name, window=5):
    r = authed_client.post("/api/agents", json={"name": name},
                           headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    agent = r.json()["agent"]
    st = authed_client.app.state
    replay = accountstore.trial_replay(st, agent["id"])
    assert replay["window_days"] == window and replay["status"] == "in_progress"
    return agent["id"], r.json()["accounts"]


def test_create_trial_agent_generates_replay_ledger(authed_client):
    st = authed_client.app.state
    agent_id, _ = _mk_agent(authed_client, "回放台账用例")
    replay = accountstore.trial_replay(st, agent_id)
    assert replay is not None
    assert replay["trial_account_id"] == agent_id + ".trial"
    assert replay["window_days"] == 5 and replay["sessions"] == []
    # 非法窗口拒绝（5-20 约束）
    for bad in (3, 21):
        try:
            accountstore.create_trial_agent(st, agent_id=f"agent-w-{bad}",
                                            name=f"非法窗口{bad}", window_days=bad)
        except LookupError as e:
            assert "5-20" in str(e)
        else:
            raise AssertionError(f"窗口 {bad} 应当被拒绝")


def test_auto_settlement_frozen_during_trial_main_clean(authed_client):
    st = authed_client.app.state
    agent_id, accounts = _mk_agent(authed_client, "试运行冻结用例")
    by_role = {a["role"]: a for a in accounts}
    # 试运行期：auto 路径主/trial 均冻结（主账户零污染）
    auto = settle_day.eligible_accounts(st)
    assert by_role["main"]["id"] not in auto
    assert by_role["trial"]["id"] not in auto
    assert not settle_day.pending_any(st, "2026-09-04")
    # launch 验收后：trial 归档、主账户恢复 auto 参与
    r = authed_client.post(f"/api/agents/{agent_id}/trial/finish",
                           json={"decision": "launch"}, headers=csrf_headers(authed_client))
    assert r.status_code == 200
    auto = settle_day.eligible_accounts(st)
    assert by_role["main"]["id"] in auto
    assert by_role["trial"]["id"] not in auto


def test_replay_mode_only_in_progress_trial_account(authed_client):
    st = authed_client.app.state
    agent_id, accounts = _mk_agent(authed_client, "回放模式用例")
    by_role = {a["role"]: a for a in accounts}
    replay_only = settle_day.eligible_accounts(st, mode="replay")
    assert replay_only == [by_role["trial"]["id"]]
    assert by_role["main"]["id"] not in replay_only
    # 记满窗口 → 自动 done，不再进 replay/auto
    for d in ("2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"):
        accountstore.add_trial_session(st, agent_id, d)
    replay = accountstore.trial_replay(st, agent_id)
    assert replay["status"] == "done" and replay["sessions_done"] == 5
    assert settle_day.eligible_accounts(st, mode="replay") == []


def test_trial_session_idempotent_and_boundary(authed_client):
    st = authed_client.app.state
    agent_id, _ = _mk_agent(authed_client, "会话幂等用例")
    accountstore.add_trial_session(st, agent_id, "2026-09-01")
    accountstore.add_trial_session(st, agent_id, "2026-09-01")     # 同日幂等
    replay = accountstore.trial_replay(st, agent_id)
    assert replay["sessions_done"] == 1
    for d in ("2026-08-31", "2026-09-02", "2026-09-03", "2026-09-04"):
        accountstore.add_trial_session(st, agent_id, d)
    replay = accountstore.trial_replay(st, agent_id)
    assert replay["status"] == "done" and replay["sessions_done"] == 5
    # 满窗口后继续记 → 拒绝（等验收归档）
    try:
        accountstore.add_trial_session(st, agent_id, "2026-09-08")
    except LookupError as e:
        assert "已完成" in str(e)
    else:
        raise AssertionError("回放窗口完成后不应再记会话")


def test_run_trial_backfill_recent_sessions_then_done(authed_client):
    st = authed_client.app.state
    agent_id, accounts = _mk_agent(authed_client, "最近回放用例")
    trial_id = {a["role"]: a for a in accounts}["trial"]["id"]
    # 放一笔 orderstore 挂单验证 finish 前 trial 可下单（无结算必要性，仅冒烟）
    ok = orderstore.place_order(st, account_id=trial_id, creator=agent_id,
                                symbol="600000", qty=100, price_type="market",
                                reason="回放挂单冒烟")
    assert ok["status"] == "active"
    trigger = EodSettleTrigger(st, feed=AxisFeed())
    out = trigger.run_trial_backfill(now=NOW)
    assert len(out["replayed"]) == 1
    entry = out["replayed"][0]
    assert entry["agent_id"] == agent_id
    days = [d["date"] for d in entry["days"]]
    # 取最近 5 个交易日（严格早于 today，倒序取满窗口）
    assert days == ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert all(not d["error"] for d in entry["days"])
    replay = accountstore.trial_replay(st, agent_id)
    assert replay["status"] == "done" and replay["sessions"] == days
    # 主账户分毫未动（零污染）；trial 账户保持 trial 态等归档
    main = accountstore.get_account(st, agent_id)
    assert main["cash"] == "100000.00" and main["status"] == "normal"
    trial = accountstore.get_account(st, trial_id)
    assert trial["status"] == "trial"
    # 幂等：重复回放空跑（窗口已 done）
    again = trigger.run_trial_backfill(now=NOW)
    assert again["replayed"] == []
    # 完成审计留痕
    conn = state_conn(st)
    kinds = {r[0] for r in conn.execute(
        "SELECT DISTINCT action FROM audit_logs WHERE action LIKE 'trade.trial_replay%'"
    ).fetchall()}
    assert "trade.trial_replay_complete" in kinds


def test_run_trial_backfill_honors_window_lower_bound(authed_client):
    """最近优先选取且不越过 today：回放终点恒为 <today 的最近会话日。"""
    from datetime import timedelta
    st = authed_client.app.state
    agent_id, _ = _mk_agent(authed_client, "窗口下限用例")
    trigger = EodSettleTrigger(st, feed=AxisFeed())
    out = trigger.run_trial_backfill(
        now=datetime(2026, 9, 1, 15, 40))       # 轴 <09-01 仅 9 日，足够 → 满
    assert len(out["replayed"]) == 1
    entry = out["replayed"][0]
    assert len(entry["days"]) == 5
    assert entry["days"][-1]["date"] < "2026-09-01"
