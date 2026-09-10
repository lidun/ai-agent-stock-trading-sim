"""概念验证进度与预计可验证时间外推测试（spec-05 §3.2 B3）。"""
from __future__ import annotations

from core import kb, signalstore
from core.db import state_conn

DEMO = "agent-demo-001"
DATES = [f"2026-07-{d:02d}" for d in range(1, 31)]


def _sig(state, concept, date, *, bucket="cn_a_main", trial=0, sig_type="buy"):
    return signalstore.register(state, DEMO, sig_type, "600000", date,
                               concept_tag=concept, env_bucket=bucket,
                               ref_price=10.0, trial_flag=trial)


def _positive(state, name, *, n=0, bucket="cn_a_main"):
    e = kb.create_entry(state, type_="positive", name=name, description="x")
    if n:
        kb.upsert_stats(state, e["id"], env_bucket=bucket, sample_n=n, win_rate=0.5,
                        expectancy=1.0)
    return e


def _bucket(ev, kb_id):
    return next(b for b in ev["buckets"] if b["kb_id"] == kb_id)


def test_evidence_eta_extrapolates_from_recent_frequency(authed_client):
    st = authed_client.app.state
    for d in DATES:                                   # 建立 30 交易日轴
        _sig(st, "轴概念", d)
    ent = _positive(st, "节奏概念", n=20)
    for d in DATES[-5:]:                              # 近 5 日 5 条入信号
        _sig(st, "节奏概念", d)

    ev = kb.evidence_eta(st, min_n=30, window_days=30)
    assert ev["window_len"] == 30 and ev["window_cutoff"] == DATES[0]
    b = _bucket(ev, ent["id"])
    assert b["sample_n"] == 20 and b["recent_n"] == 5
    assert b["freq"] == round(5 / 30, 4)
    assert 60 <= b["eta_days"] <= 61                   # (30-20)/(5/30)=60


def test_evidence_eta_no_recent_signals(authed_client):
    st = authed_client.app.state
    _sig(st, "轴概念", DATES[-1])
    ent = _positive(st, "冷门概念", n=10)
    ev = kb.evidence_eta(st, min_n=30, window_days=30)
    b = _bucket(ev, ent["id"])
    assert b["recent_n"] == 0 and b["freq"] == 0.0
    assert b["eta_days"] is None and "暂无入信号" in b["reason"]


def test_evidence_eta_sufficient_and_trial_excluded(authed_client):
    st = authed_client.app.state
    for d in DATES[-3:]:
        _sig(st, "轴概念", d)
    full = _positive(st, "样本充足概念", n=30)
    trial = _positive(st, "试运行概念", n=5)
    for d in DATES[-3:]:                              # trial 信号不计入频率
        _sig(st, "试运行概念", d, trial=1)

    ev = kb.evidence_eta(st, min_n=30, window_days=30)
    bf = _bucket(ev, full["id"])
    assert bf["eta_days"] == 0 and "样本充足" in bf["reason"]
    bt = _bucket(ev, trial["id"])
    assert bt["recent_n"] == 0 and bt["eta_days"] is None


def test_evidence_eta_http_roundtrip(authed_client):
    st = authed_client.app.state
    _sig(st, "轴概念", DATES[-1])
    ent = _positive(st, "接口概念", n=12)
    r = authed_client.get("/api/kb/evidence-eta?min_n=30&window_days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_len"] == 1
    assert any(b["kb_id"] == ent["id"] for b in body["buckets"])
