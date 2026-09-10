"""策略分析聚合测试（spec-06 §6.4 P2：资金曲线/指标摘要卡/演进账本）。"""
from __future__ import annotations

from core import analytics, reporting
from core.accountstore import create_trial_agent
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"


def _seed_curve(st, account_id: str = DEMO) -> None:
    """直写账户 nav 并用 store_engine_report 落 4 个交易日报 → 曲线点。"""
    c = state_conn(st)
    seq = [("2026-09-01", 1.0000), ("2026-09-02", 1.0200),
           ("2026-09-03", 0.9800), ("2026-09-04", 1.0100)]
    for d, nav in seq:
        with write_txn(c) as cw:
            cw.execute("UPDATE accounts SET cash=?, nav=?, total_pnl=?, today_pnl=?"
                       " WHERE id=?",
                       (nav * 100000, nav, (nav - 1) * 100000, 0.0, account_id))
        reporting.store_engine_report(st, account_id, d)


def _seed_exits(st, account_id: str = DEMO) -> None:
    c = state_conn(st)
    c.execute(
        "INSERT INTO exit_trackings (id, account_id, sell_trade_id, symbol, sell_date,"
        " sell_price, qty, status, fwd_return_pct, excess_pct, conclusion, period_high,"
        " period_low, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("et-1", account_id, "t-1", "600000", "2026-09-01", 10.0, 100, "done",
         3.2, 2.1, "卖对", 12.0, 9.0, "2026-09-01T15:00:00Z"))
    c.execute(
        "INSERT INTO exit_trackings (id, account_id, sell_trade_id, symbol, sell_date,"
        " sell_price, qty, status, fwd_return_pct, excess_pct, conclusion, period_high,"
        " period_low, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("et-2", account_id, "t-2", "000001", "2026-09-02", 9.0, 200, "done",
         -1.5, -2.0, "卖早", 11.0, 8.0, "2026-09-02T15:00:00Z"))


def _seed_signals(st, account_id: str = DEMO) -> None:
    """主账户 3 条已结清（+3.2/0/-1.5）+ 1 条在途 + 1 条 trial 样本（应排除）。"""
    c = state_conn(st)
    base = ("INSERT INTO signal_registry (id, account_id, sig_type, symbol, reg_date,"
            " concept_tag, env_bucket, exception, pitfall_id, fwd_return_pct,"
            " fwd_end_date, quality, strategy_version_no, trial_flag, ref_price,"
            " last_close, sessions_done, last_seen, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

    def ins(sid, fwd, end, trial=0):
        c.execute(base, (sid, account_id, "buy", "600000", "2026-08-20", "AI", "up",
                         0, "", fwd, end, "", "v1", trial, 10.0, 10.0, 10,
                         "2026-09-03", "2026-08-20T15:00:00Z"))

    ins("sg-win", 3.2, "2026-09-03")
    ins("sg-flat", 0.0, "2026-09-03")
    ins("sg-loss", -1.5, "2026-09-03")
    ins("sg-open", None, "")
    ins("sg-trial", 9.9, "2026-09-03", trial=1)


def test_metrics_from_reports_signals_and_exits(authed_client):
    st = authed_client.app.state
    _seed_curve(st)
    _seed_signals(st)
    _seed_exits(st)
    m = analytics.metrics(st, DEMO)
    assert m["as_of"] == "2026-09-04"
    assert m["settle_days"] == 4
    assert m["cum_return_pct"] == 1.0          # (1.01-1)*100
    assert m["max_drawdown_pct"] == 3.92       # 峰值 1.02 → 谷 0.98：0.04/1.02*100
    # 信号胜率：signal_registry 口径（trial 排除，在途不计入已结清）
    assert m["signal"]["n"] == 4
    assert m["signal"]["done"] == 3
    assert m["signal"]["win_n"] == 1 and m["signal"]["early_n"] == 1
    assert m["signal"]["tie_n"] == 1
    assert m["signal"]["win_rate_pct"] == 50.0
    assert m["signal"]["avg_fwd_return_pct"] == 0.57   # (3.2+0-1.5)/3 四舍五入
    # 卖出决策细分：exit_trackings 口径，字段独立
    assert m["exit"]["done"] == 2
    assert m["exit"]["win_n"] == 1 and m["exit"]["early_n"] == 1
    assert m["exit"]["win_rate_pct"] == 50.0
    assert abs(m["exit"]["avg_excess_pct"] - (2.1 + -2.0) / 2) < 1e-6


def test_equity_curve_series_and_bench(authed_client, monkeypatch):
    st = authed_client.app.state
    _seed_curve(st)
    create_trial_agent(st, agent_id="agent-ana-trial", name="分析试运行", window_days=5)

    def fake_bench(state, dates):
        return {"available": True, "reason": "",
                "points": [{"trade_date": "2026-09-01", "close": 3500.0,
                            "return_pct": 0.0},
                           {"trade_date": "2026-09-04", "close": 3535.0,
                            "return_pct": 1.0}]}
    monkeypatch.setattr(analytics, "_bench_series", fake_bench)

    curve = analytics.equity_curve(st, DEMO, span="1m")
    assert curve["range"] == "1m"
    roles = [s["role"] for s in curve["series"]]
    assert "main" in roles
    main = next(s for s in curve["series"] if s["role"] == "main")
    assert [p["trade_date"] for p in main["points"]] == \
        ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert main["last_return_pct"] == 1.0
    assert curve["benchmark"]["available"] is True

    # trial 子账本也出列（0 点，验证期尚未回放）
    t = analytics.equity_curve(st, "agent-ana-trial")
    assert {s["role"] for s in t["series"]} == {"main", "trial"}


def test_evolution_ledgers_and_archive(authed_client):
    st = authed_client.app.state
    _seed_curve(st)
    ev = analytics.evolution(st, DEMO)
    assert ev["ledgers"] and ev["ledgers"][0]["role"] == "main"
    main = ev["ledgers"][0]
    assert main["report_days"] == 4 and main["return_pct"] == 1.0
    assert main["last_report_date"] == "2026-09-04"
    assert ev["archive"] is None

    tr = create_trial_agent(st, agent_id="agent-ana-trial2", name="验收链", window_days=5)
    ev2 = analytics.evolution(st, "agent-ana-trial2")
    assert {l["role"] for l in ev2["ledgers"]} == {"main", "trial"}
    trial = next(l for l in ev2["ledgers"] if l["role"] == "trial")
    assert trial["trial"]["window_days"] == 5
    assert trial["trial"]["replay_status"] == "in_progress"
    assert tr["accounts"]  # 创建返回账户列表可读


def test_http_endpoints_roundtrip(authed_client):
    st = authed_client.app.state
    _seed_curve(st)
    r = authed_client.get(f"/api/agents/{DEMO}/metrics")
    assert r.status_code == 200, r.text
    assert r.json()["cum_return_pct"] == 1.0

    r2 = authed_client.get(f"/api/agents/{DEMO}/equity-curve?range=3m")
    assert r2.status_code == 200 and r2.json()["series"]

    r3 = authed_client.get(f"/api/agents/{DEMO}/evolution")
    assert r3.status_code == 200 and r3.json()["ledgers"]

    assert authed_client.get("/api/agents/nope/metrics").status_code == 404
    assert authed_client.get(
        f"/api/agents/{DEMO}/equity-curve?range=99").status_code == 400
    # 无会话 → 401
    client = authed_client  # noqa: F841 复用已登录；未登录 401 由全局鉴权测试覆盖
