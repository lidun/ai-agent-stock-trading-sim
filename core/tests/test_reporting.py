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


def test_data_section_propagates_quality_marks(authed_client):
    """L1 分钟档缺根（≤容差）→ 仍走 L1 但票级 degraded 随结算落库并进日报数据段。"""
    st = authed_client.app.state
    from test_settle_day import _insert_buy_order
    from core import settle_day
    from _feedkit import L1SparseFeed
    _insert_buy_order(st, order_id="b-qm", qty=100,
                      trigger={"op": "le", "price": 999},
                      created=f"{DATE}T09:00:00")
    rep = settle_day.run_day(st, DATE, feed=L1SparseFeed(drop=5), account_ids=[DEMO])
    acct = rep["accounts"][0]
    assert not acct.get("error") and not acct.get("skipped"), acct
    ds = reporting.build_engine_data_section(st, DEMO, DATE)
    qm = ds["annotations"]["quality_marks"]
    assert qm["600000"]["quality"] == "degraded"
    assert qm["600000"]["degraded_reason"] == "coverage_gap"
    assert "缺 3 根" in qm["600000"]["notes"]
    assert ds["settlement"]["granularity_used"] == {"600000": "l1"}
    assert ds["annotations"]["degraded"] == []          # 档位降级与数据质量两轴独立
    md = reporting.render_engine_data_markdown(ds)
    assert "数据质量：600000 coverage_gap" in md


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


def test_report_direct_push_normal_first_version(authed_client):
    """spec-04 §6.1/§6.2：结算日首版日报直达用户会话（联系人式，非中转），幂等不重推。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    pushed = reporting.push_pending_report_deliveries(st, "2026-09-04")
    assert len(pushed) == 1
    m = pushed[0]
    assert m["direction"] == "agent" and m["msg_type"] == "report"
    assert m["status"] == "delivered" and m["delivered_via"] == "web"
    assert "【2026-09-04 · v1】结算日报" in m["body"]
    assert "现金 89994.9" in m["body"]
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='report.push'"
    ).fetchone()[0] == 1
    # 会话存在（report_direct 消息落在该 Agent 的用户会话）且未读角标=1
    conv = state_conn(st).execute(
        "SELECT id FROM conversations WHERE agent_id=? AND conv_type='user_chat'",
        (DEMO,)).fetchone()
    assert conv is not None
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM messages WHERE conv_id=? AND direction='agent'"
        " AND status='delivered' AND read_ts=''", (conv["id"],)).fetchone()[0] == 1
    # 幂等：重扫（重启/重试）不重复推送
    assert reporting.push_pending_report_deliveries(st, "2026-09-04") == []
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM messages WHERE msg_type='report'"
    ).fetchone()[0] == 1


def test_report_direct_absent_pushed_until_later_settle(authed_client):
    """缺勤日报首版直达；同日后到结算补发 v2 → 不再重复推送（修订由日报中心留痕承载）。"""
    st = authed_client.app.state
    reporting.fill_absent_report(st, DEMO, "2026-09-08", reason="行情缺口未结算")
    assert len(reporting.push_pending_report_deliveries(st, "2026-09-08")) == 1
    assert "缺勤日报（当日结算缺口" in state_conn(st).execute(
        "SELECT body FROM messages WHERE msg_type='report'").fetchone()[0]
    _buy(st, day="2026-09-08", price=10.0)          # 结算补发 → v2 normal
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM messages WHERE msg_type='report'").fetchone()[0] == 1


def test_report_direct_skips_trial_agent(authed_client):
    """试运行/退役 Agent 全程不推（spec-04 §6.2 v0.6，防历史回放日报轰炸用户会话）。"""
    from test_settle_day import _seed_agent_account
    st = authed_client.app.state
    _seed_agent_account(st, "agent-trial-xyz")       # 默认 running；改 trial 以复现试运行
    conn = state_conn(st)
    with conn:
        conn.execute("UPDATE agents SET status='trial' WHERE id='agent-trial-xyz'")
    reporting.fill_absent_report(st, "agent-trial-xyz", "2026-09-08",
                                 reason="试运行回放缺勤（不推）")
    assert reporting.push_pending_report_deliveries(st, "2026-09-08") == []
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM conversations WHERE agent_id='agent-trial-xyz'"
    ).fetchone()[0] == 0                              # 连会话都不建（零轰炸）


def test_daily_summary_deterministic_pushed_to_manager(authed_client):
    """§5.4 每日总汇报（确定性拼接版）→ 管理 Agent 会话；幂等；无日报日不生成。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    body = reporting.build_daily_summary_body(st, "2026-09-04")
    assert body is not None and "# 每日总汇报 · 2026-09-04" in body
    assert "确定性拼接版" in body
    assert "| 低波红利" in body and "| 正常 | 89994.9" in body
    assert "今日无结算/推送异常" in body
    msg = reporting.push_daily_summary(st, "2026-09-04")
    assert msg is not None
    assert msg["msg_type"] == "daily_summary" and msg["direction"] == "agent"
    assert msg["status"] == "delivered"
    conv = state_conn(st).execute(
        "SELECT id FROM conversations WHERE agent_id=? AND conv_type='user_chat'",
        ("agent-manager",)).fetchone()
    assert conv is not None and conv["id"] == msg["conv_id"]
    # 幂等：同日重试不重复推送
    assert reporting.push_daily_summary(st, "2026-09-04") is None
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM messages WHERE msg_type='daily_summary'"
    ).fetchone()[0] == 1
    # 无日报日期（周末/空日）→ 不生成
    assert reporting.push_daily_summary(st, "2026-09-05") is None


def test_monthly_report_deterministic_and_guard(authed_client):
    """§5.5 月度《策略体检报告》确定性版：净值/回撤与健康度字段；空月不生成；幂等。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04", qty=1000, price=10.0)
    body = reporting.build_monthly_report_body(st, 2026, 9)
    assert body is not None and "# 月度《策略体检报告》· 2026-09" in body
    assert "①净值（份额法口径）" in body and "回撤" in body
    assert "③健康度" in body
    msg = reporting.push_monthly_report(st, 2026, 9)
    assert msg is not None and msg["msg_type"] == "monthly_report"
    assert reporting.push_monthly_report(st, 2026, 9) is None     # 幂等
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM messages WHERE msg_type='monthly_report'"
    ).fetchone()[0] == 1
    # 无数据月（2026-08 无 main 日报）→ 不生成不推送
    assert reporting.build_monthly_report_body(st, 2026, 8) is None
    assert reporting.push_monthly_report(st, 2026, 8) is None


def test_push_settings_switch_gates_report_direct(authed_client):
    """spec-04 §6.2 notify_rules P1：notify_daily=off → 日报直达不推（含缺勤版）不建会话；
    同值重写幂等（仅一次审计）；重新开启恢复推送。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    assert reporting.get_push_settings(st, DEMO) is True      # 默认开启
    reporting.set_push_settings(st, DEMO, False, actor="tester")   # off（审计 1）
    reporting.set_push_settings(st, DEMO, False, actor="tester")   # 同值 → 幂等不再审计
    assert reporting.push_pending_report_deliveries(st, "2026-09-04") == []
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM conversations WHERE agent_id=? AND conv_type='user_chat'",
        (DEMO,)).fetchone()[0] == 0                             # 连会话都不建（零轰炸）
    assert reporting.set_push_settings(st, DEMO, True, actor="tester") is True  # on（审计 2）
    assert len(reporting.push_pending_report_deliveries(st, "2026-09-04")) == 1
    # 关闭后缺勤首版同样不推（同一开关统一约束）
    reporting.set_push_settings(st, DEMO, False, actor="tester")   # off（审计 3）
    reporting.fill_absent_report(st, DEMO, "2026-09-08", reason="行情缺口未结算（已关推送）")
    assert reporting.push_pending_report_deliveries(st, "2026-09-08") == []
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM messages WHERE msg_type='report'").fetchone()[0] == 1
    n = state_conn(st).execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='report.push_setting'"
    ).fetchone()[0]
    assert n == 3
