"""记忆检索协议测试（spec-02 §3.2）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import memory_cards, memory_retrieval
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"
OTHER = "agent-manager"
NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


def _add_entry(st, key: str, body: str, *, agent_id: str = DEMO,
               ts: str = "2026-09-11T02:00:00+00:00",
               mem_type: str = "market_note") -> str:
    rid = f"me-{key}"
    with write_txn(state_conn(st)) as c:
        c.execute(
            "INSERT INTO memory_entries (id, agent_id, mem_type, version_no, ts,"
            " body, ref_ids, revision, source, dedup_key, quality)"
            " VALUES (?,?,?,'',?,?,'[]',0,'test',?, 'normal')",
            (rid, agent_id, mem_type, ts, body, key))
    return rid


def test_retrieve_prefers_card(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "k1", "券商板块放量异动，关注龙头")
    memory_cards.generate_card(st, mid, use_llm=False)
    r = memory_retrieval.retrieve(st, DEMO, "券商 放量", now=NOW)
    assert r["count"] >= 1
    top = r["items"][0]
    assert top["memory_id"] == mid and top["from_card"] is True


def test_retrieve_raw_fallback_within_window(authed_client):
    st = authed_client.app.state
    recent = _add_entry(st, "k2", "近期无卡原文：军工异动",
                        ts=(NOW - timedelta(days=1)).isoformat(timespec="seconds"))
    old = _add_entry(st, "k3", "远期无卡原文：军工异动",
                     ts=(NOW - timedelta(days=30)).isoformat(timespec="seconds"))
    r = memory_retrieval.retrieve(st, DEMO, "军工", now=NOW)
    ids = {i["memory_id"] for i in r["items"]}
    assert recent in ids and old not in ids
    assert all(i["from_card"] is False for i in r["items"])


def test_isolated_by_agent(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "k4", "私有记录：半导体", agent_id=OTHER)
    memory_cards.generate_card(st, mid, use_llm=False)
    r = memory_retrieval.retrieve(st, DEMO, "半导体", now=NOW)
    assert r["count"] == 0
    with pytest.raises(LookupError):
        memory_retrieval.get_full(st, DEMO, mid)


def test_get_full_returns_body(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "k5", "完整原文数字 12345")
    out = memory_retrieval.get_full(st, DEMO, mid)
    assert out["body"] == "完整原文数字 12345"


def test_intent_bonus(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "k6", "策略：均线金叉", mem_type="strategy")
    memory_cards.generate_card(st, mid, use_llm=False)
    plain = memory_retrieval.retrieve(st, DEMO, "均线 金叉", now=NOW)
    boosted = memory_retrieval.retrieve(st, DEMO, "均线 金叉", intent="strategy",
                                         now=NOW)
    assert boosted["items"][0]["score"] >= plain["items"][0]["score"]


def test_retrieve_route(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "k7", "路由检索：有色金属")
    memory_cards.generate_card(st, mid, use_llm=False)
    resp = authed_client.get(f"/api/memory/retrieve?agent_id={DEMO}&query=有色")
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1
    full = authed_client.get(
        f"/api/memory/{mid}/full?agent_id={DEMO}")
    assert full.status_code == 200 and full.json()["memory_id"] == mid


def test_retrieve_requires_auth(client):
    assert client.get(f"/api/memory/retrieve?agent_id={DEMO}&query=x").status_code \
        in (401, 403)
