"""kb_stats 日结算后确定性重算测试（spec-05 §3.2/§3.3 单桶/试运行隔离/stale 标注）。"""
from __future__ import annotations

from core import kb, signalstore
from core.db import state_conn, write_txn
from core.settle_scheduler import EodSettleTrigger

DEMO = "agent-demo-001"
SPEC = {
    "trigger_rule": "收盘前 5 分钟涨幅 < 0 且成交额 < 近 20 日均值 50%",
    "computation": "按日内 5m 序列末段相邻分钟判定触达",
    "data_sources": ["l1_minute", "eod_daily"],
}


def _settle(state, sid: str, fwd, end: str = "2026-09-03", quality: str = "") -> None:
    c = state_conn(state)
    with write_txn(c) as cc:
        cc.execute(
            "UPDATE signal_registry SET fwd_return_pct=?, fwd_end_date=?, quality=?"
            " WHERE id=?", (fwd, end, quality, sid))


def _positive_entry(state, name: str = "高股息防守轮动"):
    return kb.create_entry(state, type_="positive", name=name,
                           description="红利风格防御型轮动方法论")


def test_recompute_positive_bucket_stats(authed_client):
    st = authed_client.app.state
    ent = _positive_entry(st)
    sigs = [
        signalstore.register(st, DEMO, "buy", "600000", "2026-08-20",
                             concept_tag="高股息防守轮动", env_bucket="cn_a_main",
                             ref_price=10.0),
        signalstore.register(st, DEMO, "candidate", "600519", "2026-08-20",
                             concept_tag="高股息防守轮动", env_bucket="cn_a_main",
                             ref_price=20.0),
        signalstore.register(st, DEMO, "sell", "000001", "2026-08-20",
                             concept_tag="高股息防守轮动", env_bucket="cn_a_main",
                             ref_price=15.0),
        signalstore.register(st, DEMO, "buy", "600036", "2026-08-20",
                             concept_tag="高股息防守轮动", env_bucket="cn_a_main",
                             ref_price=30.0),
        # trial 样本应被排除；在途（未结清）不计入
        signalstore.register(st, DEMO, "buy", "601988", "2026-08-20",
                             concept_tag="高股息防守轮动", env_bucket="cn_a_main",
                             ref_price=5.0, trial_flag=1),
    ]
    _settle(st, sigs[0], 3.2)
    _settle(st, sigs[1], 0.0)
    _settle(st, sigs[2], -1.5)
    _settle(st, sigs[3], 2.0, quality="stale_close")
    _settle(st, sigs[4], 9.9)

    out = kb.recompute_stats(st, kb_id=ent["id"])
    assert out == {"entries": 1, "buckets": 1}
    s = kb.list_stats(st, kb_id=ent["id"])[0]
    assert s["env_bucket"] == "cn_a_main" and s["sample_n"] == 4
    assert s["win_rate"] == 0.5                     # +3.2 / +2.0 → 2/4
    assert s["avg_win"] == 2.6                      # (3.2+2.0)/2
    assert s["avg_loss"] == -1.5
    assert s["expectancy"] == 0.925                 # 0.5*2.6 + 0.25*(-1.5)
    assert s["stale_n"] == 1                        # stale 计入并单列
    assert s["window_days"] is None


def test_recompute_single_bucket_no_merge(authed_client):
    st = authed_client.app.state
    ent = _positive_entry(st)
    a = signalstore.register(st, DEMO, "buy", "600000", "2026-08-20",
                             concept_tag="高股息防守轮动", env_bucket="cn_a_main",
                             ref_price=10.0)
    b = signalstore.register(st, DEMO, "buy", "600000", "2026-08-20",
                             concept_tag="高股息防守轮动", env_bucket="cn_a_hot",
                             ref_price=10.0)
    _settle(st, a, 4.0)
    _settle(st, b, -2.0)
    kb.recompute_stats(st, kb_id=ent["id"])
    rows = {s["env_bucket"]: s for s in kb.list_stats(st, kb_id=ent["id"])}
    assert set(rows) == {"cn_a_main", "cn_a_hot"}
    assert rows["cn_a_main"]["sample_n"] == 1 and rows["cn_a_main"]["win_rate"] == 1.0
    assert rows["cn_a_hot"]["sample_n"] == 1 and rows["cn_a_hot"]["win_rate"] == 0.0


def test_recompute_pitfall_intercept_and_exception(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, name="尾盘缩量走弱陷阱", type_="pitfall",
                          description="尾盘无量阴跌不宜抄底", computable_spec=SPEC,
                          severity="mid", source="retrospective", origin_agent=DEMO)
    intercepted = signalstore.register_pitfall(
        st, DEMO, "600000", "2026-08-20", pitfall_id=ent["id"],
        concept_tag="尾盘缩量走弱陷阱", env_bucket="cn_a_main", ref_price=10.0)
    exception = signalstore.register_pitfall(
        st, DEMO, "600519", "2026-08-20", pitfall_id=ent["id"], exception=1,
        concept_tag="尾盘缩量走弱陷阱", env_bucket="cn_a_main", ref_price=10.0)
    # 被拦截后下跌=真避坑
    _settle(st, intercepted, -3.0)
    _settle(st, exception, 1.0)
    kb.recompute_stats(st, kb_id=ent["id"])
    s = kb.list_stats(st, kb_id=ent["id"])[0]
    assert s["sample_n"] == 2 and s["stale_n"] == 0
    assert s["intercept_n"] == 1 and s["exception_n"] == 1


def test_recompute_scoped_env_and_idempotent(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, type_="positive", name="行业轮动加速",
                          description="景气上行行业动量", env_scope="cn_a_hot")
    a = signalstore.register(st, DEMO, "buy", "600000", "2026-08-20",
                             concept_tag="行业轮动加速", env_bucket="cn_a_hot",
                             ref_price=10.0)
    # 其它桶的信号对 scoped 条目不进入统计
    signalstore.register(st, DEMO, "buy", "600519", "2026-08-20",
                         concept_tag="行业轮动加速", env_bucket="cn_a_main",
                         ref_price=10.0)
    _settle(st, a, 5.0)
    kb.recompute_stats(st, kb_id=ent["id"])
    first = kb.list_stats(st, kb_id=ent["id"])
    assert len(first) == 1 and first[0]["env_bucket"] == "cn_a_hot"
    assert first[0]["sample_n"] == 1
    # 幂等：重算不漂移，且保留人工 dispatch_n
    kb.upsert_stats(st, ent["id"], env_bucket="cn_a_hot", dispatch_n=7)
    kb.recompute_stats(st, kb_id=ent["id"])
    again = kb.list_stats(st, kb_id=ent["id"])[0]
    assert again["sample_n"] == 1 and again["win_rate"] == first[0]["win_rate"]
    assert again["dispatch_n"] == 7


def test_recompute_wired_after_settlement(authed_client):
    st = authed_client.app.state
    ent = _positive_entry(st)
    sid = signalstore.register(st, DEMO, "buy", "600000", "2026-08-20",
                               concept_tag="高股息防守轮动", env_bucket="cn_a_main",
                               ref_price=10.0)
    _settle(st, sid, 3.0)

    trig = EodSettleTrigger(st, feed=None)
    out = trig._recompute_kb()
    assert out["entries"] >= 1
    assert kb.list_stats(st, kb_id=ent["id"])[0]["sample_n"] == 1
    row = state_conn(st).execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE action='kb.stats.recompute_auto'"
    ).fetchone()
    assert row["c"] >= 1
