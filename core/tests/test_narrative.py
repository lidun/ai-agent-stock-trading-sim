"""日报 LLM 叙述段生产者测试（spec-04 §5.2/§9.1 确定性降级）。

覆盖：结算 done 才生成、生成含四~六段并落 narrative、幂等跳过、force 重写、
未配置确定性码、调用失败留痕、无结算/无日报状态码、HTTP 路由。
"""
from __future__ import annotations

import json

from core import llm, narrative, reporting
from core.db import state_conn
from test_reporting import DEMO, _buy
from conftest import csrf_headers

_FAKE = {"content": "【四、市场观察（主观）】\n外部环境延续震荡（主观推演）。\n"
                    "【五、判断与明日方向】\n持仓 600000 维持，跌破止损线再减。\n"
                    "【六、申报叙述】\n本日无新增申报动议。",
         "finish_reason": "stop", "model": "deepseek-chat",
         "usage": {"prompt_tokens": 300, "completion_tokens": 120,
                   "total_tokens": 420}}


def _st(authed_client):
    return authed_client.app.state


def _audits(st):
    return [dict(r) for r in state_conn(st).execute(
        "SELECT action, detail FROM audit_logs"
        " WHERE object_type='daily_reports' ORDER BY id DESC").fetchall()]


def test_generate_requires_settled_report(authed_client):
    st = _st(authed_client)
    reporting.store_engine_report(st, DEMO, "2026-09-08", status="normal",
                                  narrative="")
    res = narrative.generate_narrative(st, DEMO, "2026-09-08")
    assert res["ok"] is False and res["code"] == "not_settled"


def test_generate_missing_report(authed_client):
    res = narrative.generate_narrative(_st(authed_client), DEMO, "2026-09-01")
    assert res["ok"] is False and res["code"] == "no_report"


def test_generate_unconfigured_is_deterministic(authed_client):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")
    res = narrative.generate_narrative(st, DEMO, "2026-09-04")
    assert res["ok"] is False and res["code"] == "not_configured"
    rows = reporting.list_engine_reports(st, DEMO, "2026-09-04")
    assert rows[0]["narrative"] == ""


def test_generate_writes_narrative(authed_client, monkeypatch):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")
    seen = {}

    def fake_chat(state, messages, *, timeout_s):
        seen.update(messages=messages, timeout=timeout_s)
        return dict(_FAKE)

    monkeypatch.setattr(llm, "chat", fake_chat)
    res = narrative.generate_narrative(st, DEMO, "2026-09-04")
    assert res["ok"] is True and res["code"] == "generated"
    assert res["narrative_len"] > 0 and res["usage"]["total_tokens"] == 420
    rows = reporting.list_engine_reports(st, DEMO, "2026-09-04")
    assert "【四、市场观察" in rows[0]["narrative"]
    payload = json.dumps(seen["messages"][1]["content"])
    assert DEMO in payload and "600000" in payload
    assert "【六、申报叙述" in rows[0]["narrative"]


def test_generate_skips_when_present_then_force(authed_client, monkeypatch):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")
    calls = []

    def fake_chat(state, messages, *, timeout_s):
        calls.append(1)
        return dict(_FAKE)

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert narrative.generate_narrative(st, DEMO, "2026-09-04")["code"] == "generated"
    skipped = narrative.generate_narrative(st, DEMO, "2026-09-04")
    assert skipped["ok"] is True and skipped["code"] == "skipped"
    assert len(calls) == 1
    rewritten = narrative.generate_narrative(st, DEMO, "2026-09-04", force=True)
    assert rewritten["code"] == "generated"
    assert len(calls) == 2


def test_generate_llm_failure_leaves_empty_and_audits(authed_client, monkeypatch):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")

    def boom(state, messages, *, timeout_s):
        raise llm.LLMProviderError("429 限流")

    monkeypatch.setattr(llm, "chat", boom)
    res = narrative.generate_narrative(st, DEMO, "2026-09-04")
    assert res["ok"] is False and res["code"] == "llm_failed"
    assert reporting.list_engine_reports(st, DEMO, "2026-09-04")[0]["narrative"] == ""
    gen = [a for a in _audits(st) if a["action"] == "report.narrative_generate"]
    assert len(gen) == 1 and "429" in gen[0]["detail"]


def test_route_generate(authed_client, monkeypatch):
    st = _st(authed_client)
    _buy(st, day="2026-09-04")
    monkeypatch.setattr(
        llm, "chat", lambda state, messages, *, timeout_s: dict(_FAKE))
    r = authed_client.post(
        f"/api/accounts/{DEMO}/reports/2026-09-04/narrative/generate",
        headers=csrf_headers(authed_client))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["code"] == "generated"
    assert "【四、市场观察" in reporting.list_engine_reports(
        st, DEMO, "2026-09-04")[0]["narrative"]
