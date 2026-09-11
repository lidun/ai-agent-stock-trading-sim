"""分层摘要索引测试（spec-02 §4.1/§4.2）。"""
from __future__ import annotations

from datetime import date, datetime

from core import summaries, tasks
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"


def _add_entry(st, ts: str, body: str, *, agent_id: str = DEMO,
               mem_type: str = "market_note", key: str = "") -> str:
    rid = f"me-{key or body[:8]}-{ts}"
    with write_txn(state_conn(st)) as c:
        c.execute(
            "INSERT INTO memory_entries (id, agent_id, mem_type, version_no, ts,"
            " body, ref_ids, revision, source, dedup_key, quality)"
            " VALUES (?,?,?,'',?,?,'[]',0,'test',?, 'normal')",
            (rid, agent_id, mem_type, ts, body, key or rid))
    return rid


def test_day_summary_generates_and_records_ids(authed_client):
    st = authed_client.app.state
    a = _add_entry(st, "2026-09-04T02:00:00+00:00", "大盘缩量整理", key="d1")
    b = _add_entry(st, "2026-09-04T06:00:00+00:00", "关注券商异动", key="d2")
    r = summaries.generate(st, DEMO, "day", "2026-09-04", use_llm=False)
    assert r["skipped"] is False and r["source_count"] == 2
    s = summaries.get_summary(st, DEMO, "day", "2026-09-04")
    assert s is not None
    assert set(s["record_ids"]) == {a, b}
    assert "2026-09-04" in s["body"]


def test_recompute_upserts_in_place(authed_client):
    st = authed_client.app.state
    _add_entry(st, "2026-09-04T02:00:00+00:00", "初版", key="r1")
    first = summaries.generate(st, DEMO, "day", "2026-09-04", use_llm=False)
    _add_entry(st, "2026-09-04T07:00:00+00:00", "追加", key="r2")
    second = summaries.generate(st, DEMO, "day", "2026-09-04", use_llm=False)
    assert second["recomputed"] is True
    assert second["source_count"] == 2
    rows = state_conn(st).execute(
        "SELECT COUNT(*) n FROM memory_summaries WHERE agent_id=? AND period='day'"
        " AND period_key='2026-09-04'", (DEMO,)).fetchone()["n"]
    assert rows == 1
    assert summaries.get_summary(st, DEMO, "day", "2026-09-04")["id"] == first["id"]


def test_scan_pending_day_week_month(authed_client):
    st = authed_client.app.state
    # 8/10 周一（W33，2026-08 月）；9/1、9/2（W36）
    _add_entry(st, "2026-08-10T02:00:00+00:00", "八月记录", key="m1")
    _add_entry(st, "2026-09-01T02:00:00+00:00", "九月一日", key="w1")
    _add_entry(st, "2026-09-02T02:00:00+00:00", "九月二日", key="w2")
    today = date(2026, 9, 11)
    now = datetime(2026, 9, 11, 16, 0)
    pend = {(p["agent_id"], p["period"], p["period_key"])
            for p in summaries.scan_pending(st, today=today, now_bj=now)}
    assert (DEMO, "day", "2026-08-10") in pend
    assert (DEMO, "day", "2026-09-01") in pend
    assert (DEMO, "week", "2026-W33") in pend
    assert (DEMO, "week", "2026-W36") in pend
    # 月未齐备（缺周摘要）不出现
    assert (DEMO, "month", "2026-08") not in pend
    # 补周后 2026-08 月可合成
    summaries.generate(st, DEMO, "week", "2026-W33", use_llm=False)
    pend2 = {(p["period"], p["period_key"])
             for p in summaries.scan_pending(st, today=today, now_bj=now)
             if p["agent_id"] == DEMO}
    assert ("month", "2026-08") in pend2
    assert ("week", "2026-W33") not in pend2


def test_today_unclosed_not_pending(authed_client):
    st = authed_client.app.state
    _add_entry(st, "2026-09-11T02:00:00+00:00", "今日盘中", key="t1")
    pend = summaries.scan_pending(st, today=date(2026, 9, 11),
                                  now_bj=datetime(2026, 9, 11, 11, 0))
    assert not [p for p in pend if p["period_key"] == "2026-09-11"]
    pend2 = summaries.scan_pending(st, today=date(2026, 9, 11),
                                   now_bj=datetime(2026, 9, 11, 16, 0))
    assert [p for p in pend2 if p["period"] == "day"
            and p["period_key"] == "2026-09-11"]


def test_month_synthesized_from_week_record_ids(authed_client):
    st = authed_client.app.state
    a = _add_entry(st, "2026-08-10T02:00:00+00:00", "八月十日", key="mo1")
    b = _add_entry(st, "2026-08-13T02:00:00+00:00", "八月十三日", key="mo2")
    summaries.generate(st, DEMO, "week", "2026-W33", use_llm=False)
    r = summaries.generate(st, DEMO, "month", "2026-08", use_llm=False)
    assert r["source_count"] == 1  # 以周摘要为输入
    s = summaries.get_summary(st, DEMO, "month", "2026-08")
    assert set(s["record_ids"]) == {a, b}  # 继承周摘要 record_ids 全集
    assert set(s["record_ids"]) == set(
        summaries.get_summary(st, DEMO, "week", "2026-W33")["record_ids"])


def test_summary_task_handler(authed_client):
    st = authed_client.app.state
    _add_entry(st, "2026-09-04T02:00:00+00:00", "回补记录", key="h1")
    r = tasks.enqueue(st, task_type="摘要补跑", agent_id=DEMO,
                      trade_date="2026-09-04", dedup_key="day:2026-09-04",
                      payload={"agent_id": DEMO, "period": "day",
                               "period_key": "2026-09-04"})
    out = tasks.run_deferrable(st, limit=10)
    assert out["done"] == 1
    assert tasks.get_task(st, r["id"])["status"] == "done"
    assert summaries.get_summary(st, DEMO, "day", "2026-09-04") is not None


def test_enqueue_pending_idempotent(authed_client):
    st = authed_client.app.state
    _add_entry(st, "2026-09-04T02:00:00+00:00", "幂等", key="i1")
    first = summaries.enqueue_pending(st, today=date(2026, 9, 11),
                                      now_bj=datetime(2026, 9, 11, 16, 0))
    assert any(p["period"] == "day" and p["period_key"] == "2026-09-04"
               for p in first)
    second = summaries.enqueue_pending(st, today=date(2026, 9, 11),
                                       now_bj=datetime(2026, 9, 11, 16, 0))
    assert second == []
