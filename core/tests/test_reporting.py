"""日报引擎数据段测试（spec-04 §5.2 零 token 数据段：结算产物直读，纯确定性）。"""
from __future__ import annotations

import json

from core import eodengine, reporting
from core.db import state_conn
from _feedkit import L2OnlyFeed, DATE

DEMO = "agent-demo-001"


def _buy(state, *, day, qty=1000, price=10.0):
    from test_settle_day import _insert_buy_order
    _insert_buy_order(state, order_id=f"b-{day}", qty=qty,
                      trigger={"op": "le", "price": price}, created=f"{day}T09:00:00")
    r = eodengine.settle_account(
        state, DEMO, day,
        series_map={"600000": [(f"{day}T09:31:00", price), (f"{day}T09:32:00", price)]},
        close_map={"600000": price}, prev_close_map={"600000": price},
    )
    assert not r["already_settled"]


def _cash(state):
    return state_conn(state).execute(
        "SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]


def test_data_section_settled_day_matches_archived_snapshot(authed_client):
    """结算日：数据段还原当日持仓估值快照（买 1000@10，费 5.1），交易明细/状态齐备。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    ds = reporting.build_engine_data_section(st, DEMO, "2026-09-04")
    assert ds["settlement"]["done"] is True
    assert ds["summary"]["cash"] == "89994.9"
    pos = ds["positions"]["items"]
    assert len(pos) == 1 and pos[0] == {
        "symbol": "600000", "quantity": "1000", "avg_cost": "10.0051",
        "close": "10", "market_value": "10000",
    }
    assert ds["positions"]["totals"] == {"cash": "89994.9", "market_value": "10000",
                                         "equity": "99994.9"}
    assert ds["positions"]["snapshot_available"] is True
    tr = ds["operations"]["trades"]
    assert len(tr) == 1 and tr[0]["side"] == "buy" and tr[0]["qty"] == "1000"
    assert tr[0]["fee_total"] == "5.1"
    assert ds["execution"]["trades_filled"] == 1
    assert ds["annotations"]["degraded"] == []        # series_map replay_l0 非降级


def test_data_section_l2_settle_flags_degraded(authed_client):
    """L2 日线近似档结算 → annotations.degraded 标注该票（spec-01 §7 降级口径）。"""
    st = authed_client.app.state
    from test_settle_day import _insert_buy_order
    _insert_buy_order(st, order_id="b-l2", qty=100,
                      trigger={"op": "le", "price": 9.5}, created=f"{DATE}T09:00:00")
    from core import settle_day
    settle_day.run_day(st, DATE, feed=L2OnlyFeed(), account_ids=[DEMO])
    ds = reporting.build_engine_data_section(st, DEMO, DATE)
    assert ds["annotations"]["degraded"] == ["600000"]
    assert ds["settlement"]["granularity_used"] == {"600000": "l2"}


def test_data_section_unsettled_day_reports_missing(authed_client):
    """无结算产物日：settlement.done=false、unsettled 标注、快照欠档不虚构。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")                       # 已结算 09-04
    ds = reporting.build_engine_data_section(st, DEMO, "2026-09-08")   # 未运行日
    assert ds["settlement"]["done"] is False and ds["settlement"]["status"] is None
    assert ds["annotations"]["unsettled"] is True
    assert ds["positions"]["items"] == [] and ds["positions"]["snapshot_available"] is False
    assert ds["operations"]["trades"] == []
    assert ds["execution"]["orders_today"] == {}


def test_data_section_corp_and_invalid_orders_aggregated(authed_client):
    """公司行动入 corp_actions、当日无效单按原因聚合进 execution（数据段五）。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04", qty=1000, price=10.0)
    ex = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, ex, close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "dividend", "cash_per_share": 0.5}},
    )
    assert not r["already_settled"]
    conn = state_conn(st)
    with conn:
        conn.execute(
            "UPDATE condition_orders SET status='invalid', invalid_reason='delisted'"
            " WHERE id='b-2026-09-04' AND account_id=?", (DEMO,))
    ds = reporting.build_engine_data_section(st, DEMO, ex)
    corp_acts = [c for c in ds["operations"]["corp_actions"]
                 if c["action"] == "dividend"]
    assert len(corp_acts) == 1
    assert corp_acts[0]["detail"]["net_credit"] == "500"
    # 无效单聚合入口：无当日新建单 → orders_today 为空（b-09-04 属 ex 日前的单）
    assert ds["execution"]["corp_events"] == 1
