"""试运行双账户测试（spec-01 §2.8 #63）：创建/角色映射/下单资格/迁移升级/验收归档。"""
from __future__ import annotations

import json

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


def _mk_trial_with_order(authed_client, name, *, order=True):
    """经 API 建试运行 Agent + 选配一笔 trial 挂单，返回 (agent_id, trial_id, main_id)。"""
    r = authed_client.post("/api/agents", json={"name": name},
                           headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    body = r.json()
    by_role = {a["role"]: a for a in body["accounts"]}
    trial_id, main_id = by_role["trial"]["id"], by_role["main"]["id"]
    if order:
        ok = orderstore.place_order(authed_client.app.state, account_id=trial_id,
                                    creator=body["agent"]["id"], symbol="600000",
                                    qty=100, price_type="market", reason="归档用例挂单")
        assert ok["status"] == "active"
    return body["agent"]["id"], trial_id, main_id


def _archive_row(st, archive_id):
    conn = state_conn(st)
    row = conn.execute(
        "SELECT * FROM trial_archives WHERE id=?", (archive_id,)).fetchone()
    return dict(row) if row else None


def test_finish_trial_launch_archives_evidence_keeps_main_clean(authed_client):
    st = authed_client.app.state
    agent_id, trial_id, main_id = _mk_trial_with_order(authed_client, "归档留证用例")
    r = authed_client.post(
        f"/api/agents/{agent_id}/trial/finish",
        json={"decision": "launch", "verdict": "试运行回放通过，验收合格"},
        headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"]["status"] == "running"
    assert body["archive_id"].endswith(".ta")
    # trial 账户整体归档、主账户零污染
    trial = accountstore.get_account(st, trial_id)
    main = accountstore.get_account(st, main_id)
    assert trial["status"] == "archived"
    assert main["status"] == "normal" and main["cash"] == "100000.00"
    # 留证快照：挂单计数与决策入库、一次写入
    row = _archive_row(st, body["archive_id"])
    assert row["decision"] == "launch" and "验收合格" in row["verdict"]
    snap = json.loads(row["snapshot"])
    assert snap["account"]["id"] == trial_id and snap["decision"] == "launch"
    assert snap["counts"]["orders"] == 1
    conn = state_conn(st)
    now = conn.execute("SELECT COUNT(*) FROM trial_archives").fetchone()[0]
    assert now == 1


def test_finish_trial_reject_archives_agent(authed_client):
    st = authed_client.app.state
    agent_id, trial_id, _ = _mk_trial_with_order(authed_client, "否决用例", order=False)
    r = authed_client.post(
        f"/api/agents/{agent_id}/trial/finish",
        json={"decision": "reject", "verdict": "夏普不足，策略回炉"},
        headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"]["status"] == "archived"
    assert accountstore.get_account(st, trial_id)["status"] == "archived"
    row = _archive_row(st, body["archive_id"])
    assert row["decision"] == "reject"


def test_finish_trial_idempotency_conflict(authed_client):
    """重复归档/二次决策 → 409（留证不可变更改）。"""
    st = authed_client.app.state
    agent_id, _, _ = _mk_trial_with_order(authed_client, "幂等冲突用例", order=False)
    h = csrf_headers(authed_client)
    first = authed_client.post(f"/api/agents/{agent_id}/trial/finish",
                               json={"decision": "launch"}, headers=h)
    assert first.status_code == 200
    second = authed_client.post(f"/api/agents/{agent_id}/trial/finish",
                                json={"decision": "reject"}, headers=h)
    assert second.status_code == 409
    assert ("不在试运行期" in second.json()["detail"]
            or "已归档" in second.json()["detail"])
    conn = state_conn(st)
    assert conn.execute("SELECT COUNT(*) FROM trial_archives").fetchone()[0] == 1


def test_finish_trial_bad_decision_rejected(authed_client):
    agent_id, _, _ = _mk_trial_with_order(authed_client, "非法决策用例", order=False)
    r = authed_client.post(f"/api/agents/{agent_id}/trial/finish",
                           json={"decision": "maybe"}, headers=csrf_headers(authed_client))
    assert r.status_code == 422
