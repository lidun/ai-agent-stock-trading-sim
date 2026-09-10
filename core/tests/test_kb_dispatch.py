"""候选参考卡 UCB 下发测试（spec-05 §3.5/§3.9：排序、dispatch_n 计数、退出下发）。"""
from __future__ import annotations

import math

from core import kb
from core.db import state_conn
from core.tests.conftest import csrf_headers

SPEC = {
    "trigger_rule": "收盘前 5 分钟涨幅 < 0 且成交额 < 近 20 日均值 50%",
    "computation": "按日内 5m 序列末段相邻分钟判定触达",
    "data_sources": ["l1_minute", "eod_daily"],
}


def _positive(state, name, status="valid", *, bucket="cn_a_main", n=30,
              win_rate=0.5, expectancy=1.0, dispatch_n=0):
    e = kb.create_entry(state, type_="positive", name=name, description="x")
    if status != "observing":
        kb.transition_kb(state, e["id"], action="start_validation", note="闸2 评审通过")
    if status == "valid":
        kb.transition_kb(state, e["id"], action="approve_valid", note="早证据充分")
    elif status == "invalid":
        kb.transition_kb(state, e["id"], action="invalidate", note="滚动期望为负")
    elif status == "sealed":
        kb.transition_kb(state, e["id"], action="seal", note="样本不足封存")
    kb.upsert_stats(state, e["id"], env_bucket=bucket, sample_n=n, win_rate=win_rate,
                    avg_win=2.0, avg_loss=-1.0, expectancy=expectancy,
                    dispatch_n=dispatch_n)
    return e


def _dispatch_of(state, kb_id, bucket="cn_a_main"):
    row = state_conn(state).execute(
        "SELECT dispatch_n FROM kb_stats WHERE kb_id=? AND env_bucket=?",
        (kb_id, bucket)).fetchone()
    return int(row["dispatch_n"])


def test_ucb_score_is_explore_first_and_no_epsilon(authed_client):
    assert kb.ucb_score(1.0, 0, 5) is None          # 未下发 → 优先探索
    s = kb.ucb_score(1.0, 2, 10, c=1.0)
    assert abs(s - (1.0 + math.sqrt(math.log(10) / 2))) < 1e-9
    # c 越大探索项越强；同 n 下期望值高的分更高
    assert kb.ucb_score(2.0, 3, 20, c=1.0) > kb.ucb_score(1.0, 3, 20, c=1.0)


def test_dispatch_unexplored_first_and_increments(authed_client):
    st = authed_client.app.state
    played = _positive(st, "老概念", dispatch_n=10, expectancy=5.0)
    fresh = _positive(st, "新概念", status="validating", dispatch_n=0, expectancy=1.0)
    dead = _positive(st, "已失效", status="invalid")
    sealed = _positive(st, "已封存", status="sealed")
    soft = _positive(st, "已软删")
    kb.update_entry(st, soft["id"], actor="user", deleted=True, note="口径过时")

    out = kb.dispatch_cards(st, limit=10, record=True)
    ids = [c["kb_id"] for c in out["cards"]]
    assert ids[0] == fresh["id"]                     # 未下发优先
    assert dead["id"] not in ids and sealed["id"] not in ids
    assert soft["id"] not in ids                     # 软删退出下发
    assert out["cards"][0]["score"] is None
    assert all(c.get("note") for c in out["cards"])  # 参考非指令标注
    # dispatch_n 各 +1
    assert _dispatch_of(st, played["id"]) == 11
    assert _dispatch_of(st, fresh["id"]) == 1
    # 再次下发：两者均已探索，按 UCB 分排序（期望高的老概念领先）
    out2 = kb.dispatch_cards(st, limit=10)
    assert out2["cards"][0]["kb_id"] == played["id"]
    assert out2["cards"][0]["score"] > out2["cards"][1]["score"]


def test_dispatch_env_filter_and_pitfall_orientation(authed_client):
    st = authed_client.app.state
    pos = _positive(st, "主桶概念", bucket="cn_a_main")
    kb.upsert_stats(st, pos["id"], env_bucket="cn_a_hot", sample_n=30, win_rate=0.4,
                    expectancy=-0.5, dispatch_n=0)
    pit = kb.create_entry(st, name="缩量陷阱", type_="pitfall", description="x",
                          computable_spec=SPEC, severity="mid")
    kb.transition_kb(st, pit["id"], action="start_validation", note="闸2 评审通过")
    kb.transition_kb(st, pit["id"], action="approve_valid", note="证据充分")
    kb.upsert_stats(st, pit["id"], env_bucket="cn_a_main", sample_n=30, win_rate=0.3,
                    avg_win=1.0, avg_loss=-3.0, expectancy=-3.0, dispatch_n=0)

    out = kb.dispatch_cards(st, env_bucket="cn_a_main", limit=10)
    buckets = {c["kb_id"]: c for c in out["cards"]}
    assert pos["id"] in buckets and pit["id"] in buckets
    assert all(c["env_bucket"] == "cn_a_main" for c in out["cards"])
    assert buckets[pit["id"]]["expectancy"] == 3.0   # 避坑取向取负


def test_dispatch_http_roundtrip_and_audit(authed_client):
    st = authed_client.app.state
    e = _positive(st, "接口概念", dispatch_n=0)
    h = csrf_headers(authed_client)
    r = authed_client.post("/api/kb/reference-cards",
                           json={"env_bucket": "cn_a_main", "limit": 5}, headers=h)
    assert r.status_code == 200, r.text
    assert any(c["kb_id"] == e["id"] for c in r.json()["cards"])
    assert _dispatch_of(st, e["id"]) == 1
    row = state_conn(st).execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE action='kb.dispatch'").fetchone()
    assert row["c"] >= 1
