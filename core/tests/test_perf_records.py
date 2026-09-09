"""spec-02 §11 性能与费用留痕测试：单价折算（缓存分价）、未配置单价 NULL、台账/日聚
合/归属聚合、narrative 记账回填 llm_perf_id、失败路径 failed 记账、pricing 路由。"""
from __future__ import annotations

from core import narrative, perf_records, reporting
from core.db import state_conn
from test_reporting import DEMO, _buy
from conftest import csrf_headers


def _rec(st, **kw):
    base = dict(agent_id=DEMO, task_id="t1", task_type="narrative",
                provider="deepseek", model="deepseek-chat", ok=True,
                usage={"prompt_tokens": 1000, "cached_tokens": 200,
                       "completion_tokens": 1000, "total_tokens": 2000})
    base.update(kw)
    return perf_records.record_chat_usage(st, **base)


def _rows(st, agent=DEMO):
    return state_conn(st).execute(
        "SELECT * FROM performance_records WHERE agent_id=? ORDER BY created_ts, id",
        (agent,)).fetchall()


def test_pricing_upsert_and_cost_split_cache_priced(authed_client):
    st = authed_client.app.state
    perf_records.pricing_upsert(
        st, actor="test", provider="deepseek", model="deepseek-chat",
        input_per_1k=1.0, output_per_1k=2.0, cache_read_per_1k=0.5)
    r = _rec(st)
    assert r["ok"] and r["priced"] is True
    assert abs(r["cost_yuan"] - 2.9) < 1e-6          # (800*1+200*0.5+1000*2)/1000
    row = _rows(st)[0]
    assert row["cached_tokens"] == 200 and row["tokens_in"] == 1000
    assert row["result"] == "ok" and row["task_type"] == "narrative"


def test_cache_null_falls_back_to_input_price(authed_client):
    st = authed_client.app.state
    perf_records.pricing_upsert(st, actor="test", provider="deepseek",
                                model="m2", input_per_1k=1.0, output_per_1k=1.0,
                                cache_read_per_1k=None)
    r = _rec(st, model="m2")
    assert abs(r["cost_yuan"] - 2.0) < 1e-6          # cached 200 也按 input 价 1.0 计


def test_unpriced_record_keeps_null_cost(authed_client):
    st = authed_client.app.state
    r = _rec(st)
    assert r["priced"] is False and r["cost_yuan"] is None
    assert _rows(st)[0]["cost_yuan"] is None


def test_daily_and_group_summary(authed_client):
    st = authed_client.app.state
    perf_records.pricing_upsert(st, actor="test", provider="deepseek",
                                model="deepseek-chat", input_per_1k=1.0,
                                output_per_1k=2.0, cache_read_per_1k=0.5)
    _rec(st, ok=True)
    _rec(st, agent_id=DEMO, ok=False)
    daily = perf_records.daily_summary(st, days=7)
    assert len(daily) == 1 and daily[0]["llm_calls"] == 2
    assert abs(daily[0]["cost_yuan"] - 5.8) < 1e-6
    grp = perf_records.agent_task_summary(st, days=7)
    assert len(grp) == 1 and grp[0]["agent_id"] == DEMO
    assert grp[0]["task_type"] == "narrative" and grp[0]["llm_calls"] == 2
    led = perf_records.ledger(st, limit=10)
    assert len(led) == 2 and led[0]["task_id"] == "t1"
    assert any(x["result"] == "failed" for x in led)


def test_narrative_success_links_perf_id(authed_client, monkeypatch):
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    from core import llm
    fake = {"content": "【四、市场观察（主观）】\n观察（主观）。\n"
                       "【五、判断与明日方向】\n维持。\n【六、申报叙述】\n本日无新增申报动议。",
            "model": "deepseek-chat", "provider": "deepseek",
            "usage": {"prompt_tokens": 300, "cached_tokens": 0,
                      "completion_tokens": 100, "total_tokens": 400}}
    monkeypatch.setattr(llm, "chat",
                        lambda state, messages, *, timeout_s: dict(fake))
    res = narrative.generate_narrative(st, DEMO, "2026-09-04")
    assert res["ok"] and res.get("perf_id") and res["cost_yuan"] is None
    row = state_conn(st).execute(
        "SELECT llm_perf_id FROM daily_reports WHERE agent_id=? AND trade_date=?",
        (DEMO, "2026-09-04")).fetchone()
    assert row["llm_perf_id"] == res["perf_id"]
    pr = _rows(st)[0]
    assert pr["result"] == "ok" and pr["tokens_in"] == 300


def test_narrative_provider_failure_records_failed(authed_client, monkeypatch):
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    from core import llm
    def boom(state, messages, *, timeout_s):
        raise llm.LLMProviderError("429 限流")
    monkeypatch.setattr(llm, "chat", boom)
    res = narrative.generate_narrative(st, DEMO, "2026-09-04")
    assert res["ok"] is False and res["code"] == "llm_failed"
    pr = _rows(st)[0]
    assert pr["result"] == "failed" and pr["detail"].startswith("429")


def test_pricing_routes(authed_client):
    st = authed_client.app.state
    r = authed_client.post(
        "/api/usage/pricing",
        json={"provider": "deepseek", "model": "deepseek-chat",
              "input_per_1k": 1.0, "output_per_1k": 2.0,
              "cache_read_per_1k": 0.5},
        headers=csrf_headers(authed_client))
    assert r.status_code == 200 and r.json()["price"]["provider"] == "deepseek"
    got = authed_client.get("/api/usage/pricing", params={"provider": "deepseek"})
    assert got.status_code == 200 and len(got.json()["rows"]) == 1
    daily = authed_client.get("/api/usage/daily", params={"days": 7})
    assert daily.status_code == 200 and daily.json()["days"] == []
    led = authed_client.get("/api/usage/ledger")
    assert led.status_code == 200 and led.json()["rows"] == []
    bad = authed_client.post(
        "/api/usage/pricing", json={"provider": "deepseek", "model": "",
                                    "input_per_1k": 1.0, "output_per_1k": 2.0},
        headers=csrf_headers(authed_client))
    assert bad.status_code == 400
