"""知识库状态机数据结论候选测试（spec-05 §3.2/§3.3：单桶门槛/滚动失效/避坑取向）。"""
from __future__ import annotations

from core import kb, signalstore
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"
SPEC = {
    "trigger_rule": "收盘前 5 分钟涨幅 < 0 且成交额 < 近 20 日均值 50%",
    "computation": "按日内 5m 序列末段相邻分钟判定触达",
    "data_sources": ["l1_minute", "eod_daily"],
}


def _seed_settled(state, *, concept="", pitfall_id="", sig_type="buy", env="cn_a_main",
                  n=3, fwd=1.0, end="2026-09-03", trial=0, exception=0,
                  start="2026-08-20"):
    ids = []
    for i in range(n):
        ids.append(signalstore.register(
            state, DEMO, sig_type, f"60000{i}", start, concept_tag=concept,
            env_bucket=env, pitfall_id=pitfall_id, exception=exception,
            ref_price=10.0, trial_flag=trial))
    c = state_conn(state)
    with write_txn(c) as cc:
        for sid in ids:
            cc.execute("UPDATE signal_registry SET fwd_return_pct=?, fwd_end_date=?"
                       " WHERE id=?", (fwd, end, sid))
    return ids


def _to_valid(state, kb_id):
    kb.transition_kb(state, kb_id, action="start_validation", note="闸2 评审通过")
    kb.transition_kb(state, kb_id, action="approve_valid", note="证据充分转有效")


def test_candidates_promote_positive(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, type_="positive", name="动量突破",
                          description="放量突破前高的动量延续")
    _seed_settled(st, concept="动量突破", n=3, fwd=2.0)
    small = kb.create_entry(st, type_="positive", name="小样本概念",
                            description="样本不足不晋升")
    _seed_settled(st, concept="小样本概念", n=2, fwd=9.0)

    ev = kb.evaluate_candidates(st, min_n=3, rolling_days=60)
    got = [c for c in ev["candidates"] if c["kb_id"] == ent["id"]]
    assert len(got) == 1
    c = got[0]
    assert c["action"] == "promote_valid" and c["orientation"] == "forward"
    assert c["sample_n"] == 3 and c["win_rate"] == 1.0 and c["expectancy"] == 2.0
    assert all(c["kb_id"] != small["id"] for c in ev["candidates"])
    assert ev["insufficient_buckets"] >= 1


def test_candidates_invalidate_on_rolling_window(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, type_="positive", name="均值回归",
                          description="超跌反弹均值回归")
    _to_valid(st, ent["id"])
    _seed_settled(st, concept="均值回归", n=3, fwd=5.0, end="2026-06-01")
    _seed_settled(st, concept="均值回归", n=3, fwd=-3.0, end="2026-09-03")

    ev = kb.evaluate_candidates(st, min_n=3, rolling_days=1)
    c = next(c for c in ev["candidates"] if c["kb_id"] == ent["id"])
    assert c["action"] == "invalidate"
    assert c["rolling_n"] == 3 and c["rolling_expectancy"] == -3.0
    assert "滚动 1 交易日" in c["reason"]


def test_candidates_pitfall_avoid_orientation(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, name="尾盘缩量走弱陷阱", type_="pitfall",
                          description="尾盘无量阴跌不宜抄底", computable_spec=SPEC,
                          severity="mid", source="retrospective", origin_agent=DEMO)
    # 被拦截后下跌 = 真避坑（取向取负后期望为正）
    _seed_settled(st, pitfall_id=ent["id"], sig_type="pitfall_intercept",
                  concept="尾盘缩量走弱陷阱", n=3, fwd=-2.0)
    _seed_settled(st, pitfall_id=ent["id"], sig_type="pitfall_intercept",
                  concept="尾盘缩量走弱陷阱", n=1, fwd=-2.0, exception=1)

    ev = kb.evaluate_candidates(st, min_n=3, rolling_days=60)
    c = next(c for c in ev["candidates"] if c["kb_id"] == ent["id"])
    assert c["action"] == "promote_valid" and c["orientation"] == "avoid"
    assert c["sample_n"] == 3 and c["expectancy"] == 2.0
    assert c["intercept_n"] == 3 and c["exception_n"] == 1


def test_candidates_trial_and_http_roundtrip(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, type_="positive", name="高股息轮动",
                          description="红利防御轮动")
    _seed_settled(st, concept="高股息轮动", n=3, fwd=2.0)
    # trial 样本不参与晋升证据
    _seed_settled(st, concept="高股息轮动", n=5, fwd=9.0, trial=1)

    r = authed_client.get("/api/kb/candidates?min_n=3&rolling_days=60")
    assert r.status_code == 200, r.text
    cands = r.json()["candidates"]
    c = next(c for c in cands if c["kb_id"] == ent["id"])
    assert c["sample_n"] == 3 and c["expectancy"] == 2.0
