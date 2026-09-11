"""调用卡片测试（spec-02 §3.1/§3.2）。"""
from __future__ import annotations

import pytest

from core import memory_cards, tasks
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"


def _add_entry(st, key: str, *, quality: str = "normal",
               body: str = "券商板块放量，关注次日溢价",
               ts: str = "2026-09-04T02:00:00+00:00") -> str:
    rid = f"me-{key}"
    with write_txn(state_conn(st)) as c:
        c.execute(
            "INSERT INTO memory_entries (id, agent_id, mem_type, version_no, ts,"
            " body, ref_ids, revision, source, dedup_key, quality)"
            " VALUES (?,?,'market_note','',?,?,'[]',0,'test',?,?)",
            (rid, DEMO, ts, body, key, quality))
    return rid


def test_generate_card_fallback(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "c1")
    r = memory_cards.generate_card(st, mid, use_llm=False)
    assert r["skipped"] is False and r["card_version"] == 1
    assert r["status"] == "fallback"
    assert memory_cards.get_card(st, mid)["card_version"] == 1


def test_skip_when_exists(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "c2")
    memory_cards.generate_card(st, mid, use_llm=False)
    r = memory_cards.generate_card(st, mid, use_llm=False)
    assert r["skipped"] is True


def test_force_and_flag_regeneration(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "c3")
    first = memory_cards.generate_card(st, mid, use_llm=False)
    created_ts = memory_cards.get_card(st, mid)["created_ts"]
    second = memory_cards.generate_card(st, mid, use_llm=False, force=True)
    assert second["card_version"] == 2 and second["recomputed"] is True
    assert memory_cards.get_card(st, mid)["created_ts"] == created_ts
    # flagged 触发重生成并消费 flag
    with write_txn(state_conn(st)) as c:
        c.execute("UPDATE memory_entries SET quality='flagged' WHERE id=?", (mid,))
    pend = memory_cards.list_pending_cards(st, agent_id=DEMO)
    assert any(p["memory_id"] == mid and p["reason"] == "flagged" for p in pend)
    third = memory_cards.generate_card(st, mid, use_llm=False)
    assert third["card_version"] == 3
    assert memory_cards._entry(st, mid)["quality"] == "normal"
    assert memory_cards.list_pending_cards(st, agent_id=DEMO) == []
    assert first["memory_id"] == third["memory_id"]


def test_unknown_memory_raises(authed_client):
    with pytest.raises(LookupError):
        memory_cards.generate_card(authed_client.app.state, "nope", use_llm=False)


def test_card_task_handler(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "c4")
    r = tasks.enqueue(st, task_type="卡片生成", agent_id=DEMO, dedup_key=mid,
                      payload={"memory_id": mid})
    out = tasks.run_deferrable(st, limit=10)
    assert out["done"] == 1
    assert tasks.get_task(st, r["id"])["status"] == "done"
    assert memory_cards.get_card(st, mid) is not None


def test_enqueue_pending_idempotent(authed_client):
    st = authed_client.app.state
    _add_entry(st, "c5")
    first = memory_cards.enqueue_pending_cards(st, agent_id=DEMO)
    assert any(p["memory_id"] == "me-c5" for p in first)
    assert memory_cards.enqueue_pending_cards(st, agent_id=DEMO) == []
