"""结算可复算性回归（spec-01 §2.2/§6.6 + spec-02"同输入重跑同输出"）。

确定性：账户种子、订单、行情、公司行动全部离线注入，零外部网络。引擎在 id 生成
（secrets token）与审计/更新时间（墙钟）上是故意的非确定点——复算校验把这两类
volatile 内容归一化为 <TOKEN>/<TS> 后，断言两次全新实例重放同一场景的**业务终态
归一化文本逐字节一致**，并与仓库内 gold 文件一致（防引擎行为漂移）。

gold 文件更新： UPDATE_GOLDEN=1 python3 -m pytest core/tests/test_reproducibility.py -q
（先人工审阅 gold 内容后再 commit）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from core import eodengine
from core.app import create_app
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"
GOLDEN_DIR = Path(__file__).parent / "golden"
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z)?")
_TOKEN_RE = re.compile(r"\b(?:sl|dr|l|t|h|x)[0-9a-f]{20}\b")

_ACCOUNT_COLS = ("id", "agent_id", "cash", "nav", "total_pnl", "today_pnl", "shares",
                 "initial_capital", "settle_key", "status", "active_version_no",
                 "buy_exempt", "single_stock_cap")


def _insert_order(state, *, order_id, order_type, direction, qty, trigger, day,
                  validity="today"):
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            """
            INSERT INTO condition_orders(id, account_id, order_type, direction, scope,
                symbol, trigger, basis, price_ref, qty, price_type, validity, priority,
                status, created_at, creator, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (order_id, DEMO, order_type, direction, "single", "600000",
             json.dumps(trigger), "replay_l0", "absolute", qty, "limit", validity,
             0, "active", f"{day}T09:00:00", DEMO, "复算回归场景"),
        )


def _buy(state, *, day, qty=1000, price=10.0):
    _insert_order(state, order_id=f"b-{day}", order_type="buy", direction="buy",
                  qty=qty, trigger={"op": "le", "price": price}, day=day)
    r = eodengine.settle_account(
        state, DEMO, day,
        series_map={"600000": [(f"{day}T09:31:00", price), (f"{day}T09:32:00", price)]},
        close_map={"600000": price}, prev_close_map={"600000": price},
    )
    assert not r["already_settled"]


def _scenario_full(state):
    """确定性多日场景：买 → 红利(税) → 配股参与 → 止盈卖 → 送转，覆盖 corp/撮合随机锚点。"""
    d = "2026-09-07"
    _buy(state, day=d)                                   # 买 1000@10，费 5.1
    d = "2026-09-08"
    r = eodengine.settle_account(
        state, DEMO, d, close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "dividend", "cash_per_share": 0.5, "tax": True}},
    )
    assert not r["already_settled"]                       # 红利 500、持 1 日税 20% → 净 400
    d = "2026-09-09"
    r = eodengine.settle_account(
        state, DEMO, d, close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "rights", "ratio": 0.5, "price": 8.0,
                                "participate": True}},
    )
    assert not r["already_settled"]                       # 配股缴款 4000，新增 lot 500@8
    d = "2026-09-10"
    _insert_order(state, order_id="s-2026-09-10", order_type="sell_take_profit",
                  direction="sell", qty=600, trigger={"op": "ge", "price": 9.9}, day=d)
    r = eodengine.settle_account(
        state, DEMO, d,
        series_map={"600000": [(f"{d}T09:31:00", 9.95), (f"{d}T09:32:00", 10.0)]},
        close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
    )
    assert not r["already_settled"]                       # 卖 600@10，净 5991.94（FIFO 先旧 lot）
    d = "2026-09-14"
    r = eodengine.settle_account(
        state, DEMO, d, close_map={"600000": 5.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "split", "ratio": 0.5}},
    )
    assert not r["already_settled"]                       # 送转 ×1.5（余 400→600、500→750）
    return state


def _normalize(text: str) -> str:
    return _TS_RE.sub("<TS>", _TOKEN_RE.sub("<TOKEN>", text))


def _dump(state) -> str:
    """抓取业务终态为排序后文本：volatile 的 id/ts 交由归一化消除，行序用业务键固定。"""
    c = state_conn(state)
    parts = []
    rows = c.execute(
        "SELECT {cols} FROM accounts WHERE id=? ORDER BY id".format(
            cols=", ".join(_ACCOUNT_COLS)), (DEMO,)).fetchall()
    parts.append("accounts=" + json.dumps([dict(r) for r in rows], ensure_ascii=False,
                                           sort_keys=True, default=str))
    for name, order_by, where in (
        ("holdings", "symbol", "account_id=?"),
        ("lots", "buy_date, buy_price", "account_id=?"),
        ("condition_orders", "id", "account_id=?"),
        ("trades", "settle_date, qty, price", "account_id=?"),
        ("settlement_log", "settle_key", "account_id=?"),
        ("daily_reports", "trade_date, version", "agent_id=?"),
        ("audit_logs", "id", "actor=?"),
    ):
        rows = c.execute(
            "SELECT * FROM {t} WHERE {w} ORDER BY {o}".format(
                t=name, w=where, o=order_by), (DEMO,)).fetchall()
        parts.append(f"{name}=" + json.dumps([dict(r) for r in rows],
                                             ensure_ascii=False, sort_keys=True,
                                             default=str))
    return _normalize("\n".join(parts))


def _fresh_state():
    data_dir = Path(tempfile.mkdtemp(prefix="aat-repro-"))
    app = create_app({"data_dir": data_dir, "single_instance_lock": False})
    return app.state


def _new_state_mid():
    """全新实例重放至场景中间态（用于"同日单次结算 already_settled 幂等"断言）。"""
    st = _fresh_state()
    _buy(st, day="2026-09-07")
    eodengine.settle_account(
        st, DEMO, "2026-09-08", close_map={"600000": 10.0},
        prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "dividend", "cash_per_share": 0.5, "tax": True}},
    )
    return st


def test_reproducibility_fresh_replay_matches_golden():
    """全新实例重放同一场景 → 归一化业务终态与 gold 文件逐字节一致（不漂移）。"""
    gold = GOLDEN_DIR / "repro_full_normalized.txt"
    if "UPDATE_GOLDEN" in os.environ:
        gold.write_text(_dump(_scenario_full(_fresh_state())), encoding="utf-8")
        pytest.skip("UPDATE_GOLDEN=1：已重写 gold 文件，人工审阅后 commit")
    if not gold.exists():
        pytest.fail("gold 缺失——先跑 UPDATE_GOLDEN=1 生成并人工审阅后提交")
    assert _dump(_scenario_full(_fresh_state())) == gold.read_text(encoding="utf-8")


def test_reproducibility_two_fresh_instances_identical():
    """两次独立全新实例重放（换用不同随机锚）输出业务终态完全一致。"""
    a = _dump(_scenario_full(_fresh_state()))
    b = _dump(_scenario_full(_fresh_state()))
    assert a == b


def test_reproducibility_rewind_day_settle_idempotent():
    """同事务幂等：同日结算重跑 → already_settled，且不产生第二次入账（金标准前置）。"""
    st = _new_state_mid()
    r2 = eodengine.settle_account(
        st, DEMO, "2026-09-08", close_map={"600000": 10.0},
        prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "dividend", "cash_per_share": 0.5, "tax": True}},
    )
    assert r2["already_settled"]
    c = state_conn(st)
    assert c.execute("SELECT COUNT(*) n FROM audit_logs WHERE action='corp_action.dividend'",
                     ).fetchone()["n"] == 1
    au = c.execute(
        "SELECT result FROM audit_logs WHERE action='corp_action.dividend'").fetchone()
    assert au["result"] == "credited:400"
