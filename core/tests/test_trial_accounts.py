"""试运行双账户测试（spec-01 §2.8 #63）：创建/角色映射/下单资格/迁移升级。"""
from __future__ import annotations

from core import accountstore, db, orderstore
from core.db import state_conn
from conftest import csrf_headers


def test_create_trial_agent_creates_twin_accounts(authed_client):
    r = authed_client.post("/api/agents", json={"name": "红利低波 · 验证子 Agent"},
                           headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    body = r.json()
    agent = body["agent"]
    assert agent["role"] == "strategy" and agent["status"] == "trial"
    accounts = {a["role"]: a for a in body["accounts"]}
    assert set(accounts) == {"main", "trial"}
    main, trial = accounts["main"], accounts["trial"]
    assert main["id"] == agent["id"] and main["status"] == "normal"
    assert main["parent_agent_id"] == agent["id"]
    assert trial["id"] == agent["id"] + ".trial" and trial["status"] == "trial"
    assert trial["parent_agent_id"] == agent["id"]
    assert trial["agent_id"] == agent["id"] and trial["agent_name"] == agent["name"]
    # 看板账户列表同时可见（roles 各自独立，主账户零污染）
    st = authed_client.app.state
    ids = [a["id"] for a in accountstore.list_accounts(st)]
    assert agent["id"] in ids and trial["id"] in ids


def test_trial_account_orderable_only_by_trial_phase(authed_client):
    st = authed_client.app.state
    created = accountstore.create_trial_agent(st, agent_id="agent-trial-1",
                                              name="双账户下单测试")
    by_role = {a["role"]: a for a in created["accounts"]}
    trial_id = by_role["trial"]["id"]
    ok = orderstore.place_order(st, account_id=trial_id, creator="agent-trial-1",
                                symbol="600000", qty=100, price_type="market",
                                reason="试运行回放下单")
    assert ok["status"] == "active"
    # 试运行期 Agent 直下主账户 → 拒绝（主账户待 launch 后由 running Agent 交易）
    try:
        orderstore.place_order(st, account_id=by_role["main"]["id"],
                               creator="agent-trial-1", symbol="600000", qty=100,
                               price_type="market", reason="非法直下主账户")
    except orderstore.OrderError as e:
        assert "不允许下单" in str(e)
    else:
        raise AssertionError("试运行期 Agent 主账户下单应当被拒绝")


def test_main_account_blocked_when_agent_not_running(authed_client):
    st = authed_client.app.state
    created = accountstore.create_trial_agent(st, agent_id="agent-trial-2",
                                              name="主账户拦截测试")
    by_role = {a["role"]: a for a in created["accounts"]}
    conn = state_conn(st)
    conn.execute("UPDATE agents SET status='paused' WHERE id='agent-trial-2'")
    try:
        orderstore.place_order(st, account_id=by_role["main"]["id"],
                               creator="agent-trial-2", symbol="600000", qty=100,
                               price_type="market", reason="拦截用例")
    except orderstore.OrderError as e:
        assert "不允许下单" in str(e)
    else:
        raise AssertionError("暂停 Agent 下单应当被拒绝")


def test_upgrade_from_v5_db_backfills_roles_and_keeps_data(tmp_path):
    """v5 既有库升级 v6：accounts 重建、主账户 role=main/parent=自身、数据无损。"""
    _migrations = db._SCHEMA_MIGRATIONS
    saved = list(_migrations)
    try:
        db._SCHEMA_MIGRATIONS = saved[:5]          # 仅先应用到 v5
        path = tmp_path / "v5.db"
        db.migrate(path)
        conn = db.connect(path)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()]
        assert "role" not in cols and "agent_id" not in cols
        conn.close()
    finally:
        db._SCHEMA_MIGRATIONS = saved
    db.migrate(path)                                  # 升级到 v6
    conn = db.connect(path)
    rows = conn.execute(
        "SELECT id, agent_id, role, parent_agent_id, status FROM accounts"
    ).fetchall()
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["id"] == "agent-demo-001" and r["agent_id"] == "agent-demo-001"
    assert r["role"] == "main" and r["parent_agent_id"] == "agent-demo-001"
    assert r["status"] == "normal"
    cash = conn.execute(
        "SELECT cash FROM accounts WHERE id='agent-demo-001'").fetchone()["cash"]
    assert cash == 100000.0
    conn.close()
