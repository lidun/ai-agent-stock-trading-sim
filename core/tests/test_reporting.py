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


def test_daily_report_auto_stored_on_settle(authed_client):
    """结算成功 → daily_reports 首版同事务落盘，行内容与 build 直读完全一致；幂等不增行。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    reports = reporting.list_engine_reports(st, DEMO, "2026-09-04")
    assert len(reports) == 1
    rp = reports[0]
    assert rp["version"] == 1 and rp["status"] == "normal"
    assert rp["data_section"] == reporting.build_engine_data_section(st, DEMO, "2026-09-04")
    md = rp["merged_markdown"]
    assert "# 数据段日报 2026-09-04" in md
    assert "结算完成" in md and "cash=89994.9" in md
    assert "- 600000 ×1000 成本 10.0051 ｜ 收盘 10 ｜ 市值 10000" in md
    # 同一 settle_key 重复结算 → already_settled，不产生第二版本行
    from core import eodengine as eng
    r = eng.settle_account(
        st, DEMO, "2026-09-04",
        series_map={"600000": [("2026-09-04T09:31:00", 10.0)]},
        close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
    )
    assert r["already_settled"] is True
    assert len(reporting.list_engine_reports(st, DEMO, "2026-09-04")) == 1


def test_daily_report_absent_and_resend_keep_version_history(authed_client):
    """缺勤日报：未结算日数据段照常补齐（done=false/unsettled）、status=absent；
    修订新增版本行留痕，不覆盖既有版本（spec-04 §5.3）。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    store = reporting.store_engine_report(
        st, DEMO, "2026-09-08", status="absent", narrative="当日未运行（测试）")
    assert store["version"] == 1 and store["status"] == "absent"
    ds = reporting.list_engine_reports(st, DEMO, "2026-09-08")[0]["data_section"]
    assert ds["settlement"]["done"] is False and ds["annotations"]["unsettled"] is True
    assert ds["positions"]["items"] == [] and ds["positions"]["snapshot_available"] is False
    v2 = reporting.store_engine_report(st, DEMO, "2026-09-08", status="resend",
                                       narrative="修订补发")
    assert v2["version"] == 2
    rows = reporting.list_engine_reports(st, DEMO, "2026-09-08")
    assert [r["version"] for r in rows] == [2, 1]           # 新→旧
    assert rows[0]["status"] == "resend" and rows[0]["narrative"] == "修订补发"
    assert rows[1]["status"] == "absent" and rows[1]["narrative"] == "当日未运行（测试）"


def test_daily_report_status_guard(authed_client):
    """status 越界 → 抛 ValueError（不产生脏行）。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    try:
        reporting.store_engine_report(st, DEMO, "2026-09-04", status="bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("status 校验未生效")
    assert len(reporting.list_engine_reports(st, DEMO, "2026-09-04")) == 1


def test_fill_absent_report_backfills_data_and_guards(authed_client):
    """缺勤日报补生成（spec-04 §5.3）：未结算日可得部分数据段 + 叙述=原因；幂等护栏，
    结算成功后续版本照常补发。"""
    st = authed_client.app.state
    r = reporting.fill_absent_report(st, DEMO, "2026-09-08",
                                     reason="行情分钟档未就绪，结算任务未完成")
    assert r["skipped"] is False
    assert r["report"]["status"] == "absent" and r["report"]["version"] == 1
    row = reporting.list_engine_reports(st, DEMO, "2026-09-08")[0]
    assert row["status"] == "absent"
    assert row["narrative"] == "行情分钟档未就绪，结算任务未完成"
    ds = row["data_section"]
    assert ds["settlement"]["done"] is False and ds["annotations"]["unsettled"] is True
    assert ds["summary"]["cash"] == "100000"           # 账户现值 = 可得部分口径
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='report.absent_auto'"
    ).fetchone()[0] == 1
    again = reporting.fill_absent_report(st, DEMO, "2026-09-08", reason="仍缺口")
    assert again["skipped"] is True
    assert len(reporting.list_engine_reports(st, DEMO, "2026-09-08")) == 1
    _buy(st, day="2026-09-08", price=10.0)
    tl = reporting.list_report_dates(st, DEMO)
    assert tl[0]["trade_date"] == "2026-09-08"
    assert tl[0]["latest_version"] == 2 and tl[0]["status"] == "normal"
    late = reporting.fill_absent_report(st, DEMO, "2026-09-08", reason="X")
    assert late["skipped"] is True
