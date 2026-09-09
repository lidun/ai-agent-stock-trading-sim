"""EVOQUANT 验证窗运行闭环回归（spec-05 §4.2/§4.3 + spec-01 §2.7 v0.6）。

链路真实引擎驱动：open_validation（checkpoint draft + 独立验证账户）→ 真实行情注入
结算成交（engine settle_account）→ advance_windows 按交易日推进 → 满窗判定
activate/rollback/sealed，全部统计取自真实 trades/condition_orders 记录。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from core import accountstore
from core import eodengine
from core import evoquant
from core import settle_scheduler
from core import strategy_memory
from core import strategy_versions as sv
from core.db import state_conn
from _feedkit import MultiDayL2Feed

DEMO = "agent-demo-001"

CFG = {"selection": {"filter": "dividend", "top": 30},
       "risk": {"single_stock_cap": 1.0}}


def _place(state, account_id: str, *, order_id: str, side: str, qty: int,
           price: float, trade_date: str, version_no: str):
    from core.db import write_txn
    otype = "buy" if side == "buy" else "sell_take_profit"
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            """
            INSERT INTO condition_orders(id, account_id, order_type, direction,
                scope, symbol, trigger, basis, price_ref, qty, price_type, validity,
                priority, status, strategy_version_no, created_at, creator, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order_id, account_id, otype, side, "single", "600000",
                json.dumps({"op": "le" if side == "buy" else "ge", "price": price}),
                "replay_l0", "absolute", qty, "limit", "today", 0, "active",
                version_no, f"{trade_date}T09:00:00", DEMO, "",
            ),
        )


def _settle(state, account_id: str, d: str, samples,
            prev_close: float | None = None) -> dict:
    kwargs = {}
    if prev_close is not None:
        kwargs["prev_close_map"] = {"600000": prev_close}
    return eodengine.settle_account(
        state, account_id, d,
        series_map={"600000": samples},
        close_map={"600000": samples[-1][1]},
        **kwargs,
    )


def _sell_ev(state, account_id: str, version_no: str, d0: str, d1: str) -> list[dict]:
    rows = state_conn(state).execute(
        "SELECT amount, fee_total, realized_pnl FROM trades"
        " WHERE account_id=? AND strategy_version_no=? AND side='sell'"
        " AND settle_date BETWEEN ? AND ?",
        (account_id, version_no, d0, d1),
    ).fetchall()
    return [dict(r) for r in rows]


def test_open_validation_accounts_and_guard(authed_client):
    st = authed_client.app.state
    opened = evoquant.open_validation(
        st, DEMO, version_no="v1", config=CFG, basis=["sig-1"],
        window_days=1, trade_target=20)
    acc = accountstore.get_account(st, f"{DEMO}.validation")
    assert acc["role"] == "validation" and acc["status"] == "trial"
    assert acc["id"] == f"{DEMO}.validation"
    assert acc["active_version_no"] == "v1"
    assert opened["draft"]["status"] == "draft"
    assert opened["window"]["status"] == "in_progress"
    # 演进记忆（checkpoint 事件逐条挂 version_no）
    from core import strategy_memory
    mem = strategy_memory.list_strategy_memory(st, DEMO)["items"]
    assert any(m["version_no"] == "v1" and m["source"].startswith("engine:checkpoint")
               for m in mem)

    # 同 Agent 同一时刻只允许一个在跑窗口
    with pytest.raises(LookupError, match="已有在跑验证窗"):
        evoquant.open_validation(st, DEMO, version_no="v2", config=CFG)

    # trial 态 Agent 拒绝开窗（验证面向在跑策略）
    accountstore.create_trial_agent(st, agent_id="kid-1", name="Kid1")
    with pytest.raises(LookupError, match="不在可验证运行态"):
        evoquant.open_validation(st, "kid-1", version_no="v1", config=CFG)

    # 关闭后（void 语义由判定收口）再开第二窗 → 每窗独立新账户
    evoquant.advance_windows(st, "2026-09-08")
    assert evoquant.advance_windows(st, "2026-09-08") == []  # 幂等
    opened2 = evoquant.open_validation(st, DEMO, version_no="v2", config=CFG,
                                       window_days=1)
    assert opened2["account"]["id"] == f"{DEMO}.validation.2"
    assert opened2["account"]["cash"] == "100000.00"


def test_window_activate_rollback_sealed_real_engine(authed_client):
    """四轮真实引擎数据：通过→晋升、通过→降级为候选、失败→回退、无样本→封存。"""
    st = authed_client.app.state

    # ---- 第 1 轮：v1 通过（买入@10、卖出@12 → 期望值为正）→ activate，主指针切 v1
    o1 = evoquant.open_validation(st, DEMO, version_no="v1", config=CFG,
                                  window_days=10, trade_target=2)
    a1 = o1["account"]["id"]
    _place(st, a1, order_id="w1-b1", side="buy", qty=300, price=10.0,
           trade_date="2026-09-07", version_no="v1")
    _settle(st, a1, "2026-09-07",
            [("2026-09-07T09:31:00", 10.00), ("2026-09-07T09:32:00", 10.02)])
    r1 = evoquant.advance_windows(st, "2026-09-07")[0]
    assert r1["reached"] is False and r1["sessions_done"] == 1

    _place(st, a1, order_id="w1-s1", side="sell", qty=200, price=12.0,
           trade_date="2026-09-08", version_no="v1")
    _settle(st, a1, "2026-09-08",
            [("2026-09-08T09:31:00", 12.00), ("2026-09-08T09:32:00", 12.10)],
            prev_close=10.02)
    sells = _sell_ev(st, a1, "v1", "2026-09-07", "2026-09-08")
    assert sells and all(s["realized_pnl"] > 0 for s in sells)
    r1 = evoquant.advance_windows(st, "2026-09-08")[0]
    assert r1["decision"] == "activate" and r1["reached"] is True
    assert sv.get_version(st, DEMO, "v1")["status"] == "active"
    assert accountstore.get_account(st, a1)["status"] == "archived"
    main = accountstore.get_account(st, DEMO)
    assert main["active_version_no"] == "v1"
    # activate 演进记忆正文由 EVOQUANT 写入窗口收口证据（而非通用占位文案）
    mem1 = strategy_memory.list_strategy_memory(st, DEMO)["items"]
    act1 = [m for m in mem1 if m["version_no"] == "v1"
            and m["source"].startswith("engine:activate")]
    assert act1 and "窗口验证收口(activate)" in act1[0]["body"]
    assert "期望值(含费)" in act1[0]["body"] and "成交样本 " in act1[0]["body"]
    assert "违规 0" in act1[0]["body"] and "熔断 0" in act1[0]["body"]
    # 收口日日报补写判定注解版：版本递增留痕 + annotations.window_decision 派生同源
    from core import reporting  # noqa: PLC0415
    rows = reporting.list_engine_reports(st, a1, "2026-09-08")
    ver2 = next((r for r in rows if r["version"] == 2), None)
    assert ver2 is not None
    wd = ver2["data_section"]["annotations"].get("window_decision")
    assert wd and wd["decision"] == "activate" and wd["version_no"] == "v1"
    assert wd["expectation_pct"] is not None and wd["trade_samples"] >= 1
    assert "EVOQUANT 验证窗收口" in ver2["merged_markdown"]

    # ---- 第 2 轮：v2 通过 → v1 自动降为 validated（回退候选）
    o2 = evoquant.open_validation(st, DEMO, version_no="v2", config=CFG,
                                  window_days=10, trade_target=2)
    a2 = o2["account"]["id"]
    _place(st, a2, order_id="w2-b1", side="buy", qty=300, price=10.0,
           trade_date="2026-09-09", version_no="v2")
    _settle(st, a2, "2026-09-09",
            [("2026-09-09T09:31:00", 10.00), ("2026-09-09T09:32:00", 10.02)])
    evoquant.advance_windows(st, "2026-09-09")
    _place(st, a2, order_id="w2-s1", side="sell", qty=200, price=12.5,
           trade_date="2026-09-10", version_no="v2")
    _settle(st, a2, "2026-09-10",
            [("2026-09-10T09:31:00", 12.50), ("2026-09-10T09:32:00", 12.55)],
            prev_close=10.02)
    r2 = evoquant.advance_windows(st, "2026-09-10")[0]
    assert r2["decision"] == "activate"
    assert sv.get_version(st, DEMO, "v2")["status"] == "active"
    assert sv.get_version(st, DEMO, "v1")["status"] == "validated"

    # ---- 第 3 轮：v3 失败（买入@10、卖出@8.5 → 期望值远低于 -2%）→ 自动回退到 v1
    o3 = evoquant.open_validation(st, DEMO, version_no="v3", config=CFG,
                                  window_days=10, trade_target=2)
    a3 = o3["account"]["id"]
    _place(st, a3, order_id="w3-b1", side="buy", qty=300, price=10.0,
           trade_date="2026-09-11", version_no="v3")
    _settle(st, a3, "2026-09-11",
            [("2026-09-11T09:31:00", 10.00), ("2026-09-11T09:32:00", 10.02)])
    evoquant.advance_windows(st, "2026-09-11")
    _place(st, a3, order_id="w3-s1", side="sell", qty=200, price=8.5,
           trade_date="2026-09-12", version_no="v3")
    _settle(st, a3, "2026-09-12",
            [("2026-09-12T09:31:00", 8.50), ("2026-09-12T09:32:00", 8.55)],
            prev_close=10.02)
    sells3 = _sell_ev(st, a3, "v3", "2026-09-11", "2026-09-12")
    assert sells3 and all(s["realized_pnl"] < 0 for s in sells3)
    r3 = evoquant.advance_windows(st, "2026-09-12")[0]
    assert r3["decision"] == "rollback", r3
    assert r3["sessions_done"] == 2
    # 独立账户语义（spec-05 v0.5）：否决候选本身，主账户保持现役 v2，不再回退 v1
    v3 = sv.get_version(st, DEMO, "v3")
    assert v3["status"] == "rolled_back" and v3["failure_reason"]
    assert v3["rolled_back_to"] == "v2"
    assert sv.get_version(st, DEMO, "v2")["status"] == "active"
    assert sv.get_version(st, DEMO, "v1")["status"] == "validated"
    assert accountstore.get_account(st, DEMO)["active_version_no"] == "v2"
    # 失败原因入记忆（防重复 #25）
    mem = strategy_memory.list_strategy_memory(st, DEMO)["items"]
    assert any(m["version_no"] == "v3"
               and m["source"].startswith("engine:rollback")
               and "主账户保持现役" in m["body"] for m in mem)

    # ---- 第 4 轮：窗口满但零成交 → sealed（证据不足，防假激活），draft 保持
    o4 = evoquant.open_validation(st, DEMO, version_no="v4", config=CFG,
                                  window_days=1, trade_target=20)
    a4 = o4["account"]["id"]
    r4 = evoquant.advance_windows(st, "2026-09-15")[0]
    assert r4["decision"] == "sealed"
    assert "无卖出样本" in r4["reason"]
    assert sv.get_version(st, DEMO, "v4")["status"] == "draft"
    assert accountstore.get_account(st, a4)["status"] == "archived"


def test_windows_route_via_scheduler_hook(authed_client):
    """advance 钩子只对 in_progress 生效；done 后重复推进零副作用。"""
    st = authed_client.app.state
    evoquant.open_validation(st, DEMO, version_no="v1", config=CFG,
                             window_days=1, trade_target=99)
    w = evoquant.get_window(st, DEMO, "v1")
    # 模拟 EodSettleTrigger 钩子：两次同日调用 → 会话只记 1
    evoquant.advance_windows(st, "2026-09-07")
    evoquant.advance_windows(st, "2026-09-07")
    w2 = evoquant.get_window(st, DEMO, "v1")
    assert w2["sessions_done"] == 1 and w2["status"] == "done"
    assert w2["decision"] == "sealed"
    assert w2["final_snapshot"]["final"]["decision"] == "sealed"


class _SnapDayFeed:
    """只应答快照日期的实时探测 feed（纯窗口日无行情拉取，供 settle_once 走交易日确认）。"""

    def __init__(self, day: str):
        self.day = day

    def realtime_batch(self, symbols):
        return {"600000": {"ts": self.day.replace("-", "") + "153500"}}


def _at_day(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T15:40:00")


def test_settle_once_ages_window_on_idle_days(authed_client):
    """实时路径：验证窗跨无成交交易日也按会话日推进（spec-05 §6.2 空日计数对齐）。

    窗口在跑但账户当日无订单无持仓 → 不得走空日快路径跳过；每个真实交易日推进一次，
    window_days 到期即 sealed（零成交样本，不晋升不执行）。
    """
    st = authed_client.app.state
    feed = _SnapDayFeed("2026-09-02")
    trig = settle_scheduler.EodSettleTrigger(
        st, feed=feed, corp_events={}, probe_ttl_s=0)
    evoquant.open_validation(st, DEMO, version_no="v1", config=CFG,
                             window_days=2, trade_target=500)

    r1 = trig.settle_once(now=_at_day("2026-09-02"))
    assert r1["status"] == "no_pending" and len(r1["windows"]) == 1
    w1 = r1["windows"][0]
    assert w1["reached"] is False and w1["sessions_done"] == 1

    feed.day = "2026-09-03"
    r2 = trig.settle_once(now=_at_day("2026-09-03"))
    assert r2["status"] == "no_pending" and len(r2["windows"]) == 1
    w2 = r2["windows"][0]
    assert w2["reached"] is True and w2["decision"] == "sealed"
    assert w2["sessions_done"] == 2 and w2["trade_samples"] == 0
    w = evoquant.get_window(st, DEMO, "v1")
    assert w["status"] == "done" and w["decision"] == "sealed"
    assert accountstore.get_account(st, f"{DEMO}.validation")["status"] == "archived"
    assert accountstore.get_account(st, DEMO)["active_version_no"] == ""

    # 已收口后：纯空日恢复快路径（不再探测/推进窗口）
    feed.day = "2026-09-04"
    r3 = trig.settle_once(now=_at_day("2026-09-04"))
    assert r3["status"] == "no_pending" and "windows" not in r3


def test_catchup_backfills_window_sessions(authed_client):
    """快进回放：重启补缺的会话日逐日计入验证窗（spec-04 §2.4/§2.6 对齐 spec-05 §6.2）。

    catchup 覆盖 N 个历史交易日时，期间在跑窗口按交易日逐日推进；达到 window_days
    即密封收口；重复补跑幂等零重复（done 窗口不再计会话）。
    """
    st = authed_client.app.state
    feed = MultiDayL2Feed()
    trig = settle_scheduler.EodSettleTrigger(st, feed=feed, corp_events={})
    evoquant.open_validation(st, DEMO, version_no="v1", config=CFG,
                             window_days=5, trade_target=500)

    out = trig.catchup_missed(now=_at_day("2026-09-05"),
                              start=datetime.fromisoformat("2026-08-20").date(),
                              lookback_days=60)
    progressed = [d for d in out["dates"] if d["windows"]]
    assert len(progressed) == 5            # 08-20/21/24/25/26 逐日推进后满窗
    assert [p["windows"][0]["sessions_done"] for p in progressed] == [1, 2, 3, 4, 5]
    sealed = progressed[-1]["windows"][0]
    assert sealed["decision"] == "sealed" and sealed["reached"] is True
    assert "无卖出样本" in sealed["reason"]

    # 重复补跑：done 窗口零副作用
    out2 = trig.catchup_missed(now=_at_day("2026-09-05"),
                               start=datetime.fromisoformat("2026-08-20").date(),
                               lookback_days=60)
    assert all(not d["windows"] for d in out2["dates"])
    w = evoquant.get_window(st, DEMO, "v1")
    assert w["status"] == "done" and w["decision"] == "sealed"
    assert w["sessions_done"] == 5
    assert accountstore.get_account(st, f"{DEMO}.validation")["status"] == "archived"


def test_validation_windows_http_readonly(authed_client):
    """验证窗台账只读 HTTP 面（spec-06：无写路由，判定由引擎推进）。"""
    st = authed_client.app.state
    evoquant.open_validation(st, DEMO, version_no="v1", config=CFG,
                             window_days=10, trade_target=20)
    c = authed_client

    r = c.get(f"/api/agents/{DEMO}/validation-windows")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == DEMO and body["total"] == 1
    w = body["items"][0]
    assert w["version_no"] == "v1" and w["status"] == "in_progress"
    assert w["window_days"] == 10 and w["trade_target"] == 20
    assert w["validation_account_id"] == f"{DEMO}.validation"

    r1 = c.get(f"/api/agents/{DEMO}/validation-windows/v1")
    assert r1.status_code == 200 and r1.json()["window"]["version_no"] == "v1"

    assert c.get(f"/api/agents/{DEMO}/validation-windows/v9").status_code == 404
    assert c.get("/api/agents/no-such-agent/validation-windows").status_code == 404
    # 只读护栏：无 HTTP 写路由（CSRF 拦截 403 / 未注册 404-405 均表明不存在写端点）
    assert c.post(f"/api/agents/{DEMO}/validation-windows").status_code in (403, 404, 405)
