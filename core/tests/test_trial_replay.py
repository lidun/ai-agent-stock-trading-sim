"""试运行历史回放切片（spec-05 §6.2）：窗口台账 / 会话记账 / 结算隔离 / 最近 N 回放。"""
from __future__ import annotations

import json
from datetime import datetime

from core import accountstore, orderstore, quotes_tencent as q, settle_day
from core.db import state_conn, write_txn
from core.quotes_tencent import parse_day_rows
from core.settle_scheduler import EodSettleTrigger
from conftest import csrf_headers

from _feedkit import AxisFeed, FIX, MultiDayL2Feed

NOW = datetime(2026, 9, 5, 15, 40)
PICKS = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def _row(d: str) -> dict:
    rows = parse_day_rows((FIX / "tencent_day_sh600000.json").read_text("utf-8"))
    return next(r for r in rows if str(r["date"]) == d)


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
    # launch 验收后：trial 归档、主账户恢复 auto 参与（launch 需过 §6.1 硬门槛）
    orderstore.place_order(st, account_id=by_role["trial"]["id"], creator=agent_id,
                           symbol="600000", qty=100, price_type="market",
                           reason="冻结用例挂单")
    for d in ("2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31"):
        accountstore.add_trial_session(st, agent_id, d)
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
    st = authed_client.app.state
    agent_id, _ = _mk_agent(authed_client, "窗口下限用例")
    trigger = EodSettleTrigger(st, feed=AxisFeed())
    out = trigger.run_trial_backfill(
        now=datetime(2026, 9, 1, 15, 40))       # 轴 <09-01 仅 9 日，足够 → 满
    assert len(out["replayed"]) == 1
    entry = out["replayed"][0]
    assert len(entry["days"]) == 5
    assert entry["days"][-1]["date"] < "2026-09-01"


def _insert_hist_order(state, *, order_id, account_id, d, qty=100):
    low = float(_row(d)["low"])
    with write_txn(state_conn(state)) as c:
        c.execute(
            """
            INSERT INTO condition_orders(id, account_id, order_type, direction, scope, symbol,
                trigger, basis, price_ref, qty, price_type, validity, priority, status,
                created_at, creator, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order_id, account_id, "buy", "buy", "single", "600000",
                json.dumps({"op": "le", "price": low}), "replay_l0",
                "absolute", qty, "limit", "today", 0, "active",
                f"{d}T09:00:00", account_id, "试运行历史回放下单",
            ),
        )


def test_trial_replay_fills_historical_l2_e2e(authed_client):
    """5 日回放窗口逐日真实成交（历史日无分钟档 → L2 官方收盘），主账户零污染，验收归档含证据。"""
    st = authed_client.app.state
    created = accountstore.create_trial_agent(st, agent_id="agent-rp-e2e",
                                              name="回放成交闭环", window_days=5)
    trial_id = {a["role"]: a for a in created["accounts"]}["trial"]["id"]
    for i, d in enumerate(PICKS):
        _insert_hist_order(st, order_id=f"tr-{i}", account_id=trial_id, d=d)

    trigger = EodSettleTrigger(st, feed=MultiDayL2Feed())
    out = trigger.run_trial_backfill(now=NOW)
    assert len(out["replayed"]) == 1
    assert all(not d2["error"] for d2 in out["replayed"][0]["days"])
    replay = accountstore.trial_replay(st, agent_id="agent-rp-e2e")
    assert replay["status"] == "done"
    assert replay["sessions"] == PICKS

    conn = state_conn(st)
    debit = 0.0
    for i, d in enumerate(PICKS):
        tr = conn.execute("SELECT * FROM trades WHERE order_id=?", (f"tr-{i}",)).fetchone()
        assert tr is not None, f"{d} 应有成交"
        assert tr["settle_date"] == d and tr["basis_used"] == "l2"
        close = float(_row(d)["close"])
        assert abs(tr["price"] - close) < 1e-6
        assert tr["trade_time"] == f"{d}T15:00:00"     # L2 近似：官方收盘时点
        debit += tr["amount"] + tr["fee_total"]
    # settlement_log 5 个回放日；成交额=各日官方收盘×100，现金=初始−Σ(成交额+费用)
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM settlement_log WHERE account_id=? ORDER BY trade_date",
        (trial_id,)).fetchall()]
    assert days == PICKS
    holding = conn.execute(
        "SELECT symbol, quantity FROM holdings WHERE account_id=?", (trial_id,)).fetchone()
    assert holding["quantity"] == 500
    trial = accountstore.get_account(st, trial_id)
    assert abs(float(trial["cash"]) - round(100000.0 - debit, 2)) < 0.01
    # 幂等：重放不重复成交/记账
    assert trigger.run_trial_backfill(now=NOW)["replayed"] == []
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE account_id=?", (trial_id,)
                        ).fetchone()[0] == 5
    # 验收归档：快照完整携带回放证据（结算 5 日/成交 5/挂单 5/回放 5 会话）
    archived = accountstore.finish_trial(st, agent_id="agent-rp-e2e",
                                         decision="launch", verdict="L2 历史回放验收通过")
    snap = archived["snapshot"]
    assert snap["replay"]["sessions"] == 5 and snap["counts"]["settle_days"] == 5
    assert snap["counts"]["trades"] == 5 and snap["counts"]["orders"] == 5
    main = accountstore.get_account(st, "agent-rp-e2e")
    assert main["cash"] == "100000.00" and main["status"] == "normal"


def test_finish_launch_hard_gates_reject_immature(authed_client):
    """spec-05 §6.1 launch 硬门槛：回放窗口未满 / 无任何条件单尝试 → 拒绝（reject 无门槛）。"""
    st = authed_client.app.state

    def _expect(agent_id, msg, decision="launch"):
        try:
            accountstore.finish_trial(st, agent_id=agent_id, decision=decision)
        except LookupError as e:
            assert msg in str(e)
        else:
            raise AssertionError(f"{agent_id} {decision} 应当被拒绝")

    # ① 有订单但窗口只回放 3 日 → 不可 launch
    a1 = accountstore.create_trial_agent(st, agent_id="gate-under-window",
                                         name="窗口未满", window_days=5)
    t1 = {a["role"]: a for a in a1["accounts"]}["trial"]["id"]
    orderstore.place_order(st, account_id=t1, creator="gate-under-window",
                           symbol="600000", qty=100, price_type="market",
                           reason="窗口未满用例")
    for d in ("2026-08-26", "2026-08-27", "2026-08-28"):
        accountstore.add_trial_session(st, "gate-under-window", d)
    _expect("gate-under-window", "回放未满")

    # ② 窗口满但零条件单尝试 → 不可 launch
    a2 = accountstore.create_trial_agent(st, agent_id="gate-no-order",
                                         name="无下单尝试", window_days=5)
    for d in ("2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31"):
        accountstore.add_trial_session(st, "gate-no-order", d)
    _expect("gate-no-order", "无任何条件单尝试")

    # ③ 同上场景 reject 无门槛放行（否定留证）
    rejected = accountstore.finish_trial(st, agent_id="gate-no-order", decision="reject")
    assert rejected["agent"]["status"] == "archived"


class _GapFeed(MultiDayL2Feed):
    """单日缺口 feed：2026-09-02 日线整体供给失败 → 该日结算异常（轴请求不受影响）。"""

    def day_rows(self, symbol, start, end):
        if end == "2026-09-02":
            raise q.QuoteGapError("2026-09-02 分钟/日线双缺口（测试注入）")
        return super().day_rows(symbol, start, end)


def test_finish_launch_rejects_settlement_abnormal_day(authed_client):
    """spec-05 §6.1 规则级异常不允许上线：回放中存在“有订单却未落账”的异常日 → 拒 launch。

    数据缺口日不计入合格回放（窗口保持 in_progress、审计 replay_gap）；即使事后人工补记
    会话使窗口记满，缺结算日志的异常日仍拦截 launch。"""
    st = authed_client.app.state
    created = accountstore.create_trial_agent(st, agent_id="gate-abnormal",
                                              name="结算异常守卫", window_days=5)
    trial_id = {a["role"]: a for a in created["accounts"]}["trial"]["id"]
    for i, d in enumerate(PICKS):
        _insert_hist_order(st, order_id=f"ab-{i}", account_id=trial_id, d=d)

    trigger = EodSettleTrigger(st, feed=_GapFeed())
    out = trigger.run_trial_backfill(now=NOW)
    entry = out["replayed"][0]
    blocked = [d for d in entry["days"] if d.get("blocked")]
    assert [d["date"] for d in blocked] == ["2026-09-02"]
    # 缺口日不计数 → 窗口保持 in_progress（下轮重试）
    replay = accountstore.trial_replay(st, agent_id="gate-abnormal")
    assert replay["status"] == "in_progress" and replay["sessions_done"] == 4
    kinds = {r[0] for r in state_conn(st).execute(
        "SELECT DISTINCT action FROM audit_logs WHERE action='trade.trial_replay_gap'"
    ).fetchall()}
    assert "trade.trial_replay_gap" in kinds
    # 手动补记会话使窗口记满（模拟数据补齐重试后账目齐备的场景）
    accountstore.add_trial_session(st, "gate-abnormal", "2026-09-02")
    replay = accountstore.trial_replay(st, agent_id="gate-abnormal")
    assert replay["status"] == "done"
    try:
        accountstore.finish_trial(st, agent_id="gate-abnormal", decision="launch")
    except LookupError as e:
        assert "2026-09-02" in str(e) and "规则级异常" in str(e)
    else:
        raise AssertionError("存在结算异常日不应 launch")
