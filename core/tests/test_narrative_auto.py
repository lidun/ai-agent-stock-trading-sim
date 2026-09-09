"""18:31 叙述段自动任务（spec-04 §5.3 AutoNarrativeSweep）测试。

覆盖：窗口外零动作、只收主账户结算 done 的 normal 日报（trial/回放不入列）、
未配置模型服务聚合留痕且配置就绪自动续跑、瞬时失败指数退避重试耗尽后聚合留痕。
"""
from __future__ import annotations

import json
from datetime import datetime, time

from core import llm, narrative, reporting
from core.db import state_conn, write_txn
from test_reporting import DEMO, _buy
from test_settle_day import _seed_agent_account

_FAKE = {"content": "【四、市场观察（主观）】\n震荡（主观推演）。\n"
                    "【五、判断与明日方向】\n维持持仓。\n"
                    "【六、申报叙述】\n本日无新增申报动议。",
         "finish_reason": "stop", "model": "deepseek-chat",
         "usage": {"prompt_tokens": 10, "completion_tokens": 10,
                   "total_tokens": 20}}

_NOW = datetime(2026, 9, 6, 19, 0, 0)


def _st(authed_client):
    return authed_client.app.state


def _make(st, start_at=time(0, 0), **kw):
    kw.setdefault("lookback_days", 40)
    return narrative.AutoNarrativeSweep(st, start_at=start_at, **kw)


def _audit_rows(st):
    return [dict(r) for r in state_conn(st).execute(
        "SELECT action, result, detail FROM audit_logs"
        " WHERE object_type='daily_reports' ORDER BY id").fetchall()]


def _detail(row):
    try:
        return json.loads(row["detail"]).get("detail", row["detail"])
    except ValueError:
        return row["detail"]


def _insert_direct_report(st, account_id, trade_date):
    conn = state_conn(st)
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO daily_reports(id, agent_id, trade_date, version,"
            " data_section, narrative, merged_markdown, status, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"dr-{account_id}", account_id, trade_date, 1,
             json.dumps({"settlement": {"done": True}}), "", "", "normal", ""),
        )


def test_sweep_outside_window_is_noop(authed_client):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")
    calls = []
    sweep = _make(st, start_at=time(18, 31))
    res = sweep.sweep(datetime(2026, 9, 6, 12, 0, 0))
    assert res["status"] == "outside_window"
    assert res["candidates"] == 0
    assert calls == []
    assert reporting.list_engine_reports(
        st, DEMO, "2026-09-04")[0]["narrative"] == ""


def test_sweep_generates_only_main_settled_report(authed_client, monkeypatch):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")
    _seed_agent_account(st, "agent-b-nm")
    conn = state_conn(st)
    with write_txn(conn) as c:
        c.execute("UPDATE accounts SET role='trial' WHERE id='agent-b-nm'")
    _insert_direct_report(st, "agent-b-nm", "2026-09-04")
    calls = []
    monkeypatch.setattr(llm, "provider_configured", lambda state: True)
    monkeypatch.setattr(
        llm, "chat",
        lambda state, messages, *, timeout_s: (calls.append(1), dict(_FAKE))[1])
    res = _make(st).sweep(_NOW)
    assert res["status"] == "ok"
    assert res["candidates"] == 1
    assert res["generated"] == 1
    assert len(calls) == 1
    assert "【四、市场观察" in reporting.list_engine_reports(
        st, DEMO, "2026-09-04")[0]["narrative"]
    rows = state_conn(st).execute(
        "SELECT narrative FROM daily_reports WHERE agent_id='agent-b-nm'").fetchall()
    assert rows[0]["narrative"] == ""


def test_sweep_unconfigured_aggregates_then_resumes(authed_client, monkeypatch):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")
    sweep = _make(st)
    res = sweep.sweep(_NOW)
    assert res["status"] == "ok" and res["noprov"] == 1
    assert res["generated"] == 0 and res["candidates"] == 1
    auto = [r for r in _audit_rows(st) if r["action"] == "report.narrative_auto"]
    assert len(auto) == 1 and auto[0]["result"] == "skipped"
    assert "2026-09-04" in _detail(auto[0])
    assert reporting.list_engine_reports(
        st, DEMO, "2026-09-04")[0]["narrative"] == ""

    res2 = sweep.sweep(_NOW)
    assert res2["noprov"] == 1
    assert len([r for r in _audit_rows(st)
                if r["action"] == "report.narrative_auto"]) == 1

    calls = []
    monkeypatch.setattr(llm, "provider_configured", lambda state: True)
    monkeypatch.setattr(
        llm, "chat",
        lambda state, messages, *, timeout_s: (calls.append(1), dict(_FAKE))[1])
    res3 = sweep.sweep(_NOW)
    assert res3["generated"] == 1 and len(calls) == 1
    assert "【四、市场观察" in reporting.list_engine_reports(
        st, DEMO, "2026-09-04")[0]["narrative"]


def test_sweep_transient_failure_retries_then_final(authed_client, monkeypatch):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")
    clock = {"t": 0.0}
    calls = []
    monkeypatch.setattr(llm, "provider_configured", lambda state: True)

    def boom(state, messages, *, timeout_s):
        calls.append(1)
        raise llm.LLMProviderError("429 限流")

    monkeypatch.setattr(llm, "chat", boom)
    sweep = _make(st, max_attempts=2, base_backoff_s=1.0,
                  mono=lambda: clock["t"])
    res1 = sweep.sweep(_NOW)
    assert res1["retrying"] == 1 and res1["failed"] == 0 and len(calls) == 1
    clock["t"] = 100.0
    res2 = sweep.sweep(_NOW)
    assert res2["failed"] == 1 and res2["retrying"] == 0 and len(calls) == 2
    assert reporting.list_engine_reports(
        st, DEMO, "2026-09-04")[0]["narrative"] == ""
    finals = [r for r in _audit_rows(st)
              if r["action"] == "report.narrative_auto" and r["result"] == "failed"]
    assert len(finals) == 1 and "重试 2 次" in _detail(finals[0])
    clock["t"] = 200.0
    res3 = sweep.sweep(_NOW)
    assert res3["generated"] == 0 and res3["failed"] == 0
    assert len(calls) == 2
