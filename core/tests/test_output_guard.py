"""结构化输出校验-重写测试（spec-04 §3.5）。"""
from __future__ import annotations

from core import output_guard
from core.db import state_conn
from core.tests.conftest import csrf_headers

VALID_ORDER = {"symbol": "600000", "order_type": "buy", "direction": "buy",
               "qty": 100, "price_type": "market", "trigger": None}


def test_valid_condition_order():
    r = output_guard.validate("条件单", VALID_ORDER)
    assert r["ok"] is True and r["value"]["qty"] == 100


def test_json_string_and_code_fence():
    raw = "```json\n{\"decision\": \"approved\", \"approval_id\": \"ap-1\"}\n```"
    r = output_guard.validate("审批决定", raw)
    assert r["ok"] is True and r["value"]["decision"] == "approved"


def test_invalid_then_regenerated():
    calls = []

    def regen(previous, error):  # noqa: ANN001
        calls.append(error)
        return VALID_ORDER

    r = output_guard.validate("条件单", {"symbol": "600000"}, regenerate=regen)
    assert r["ok"] is True and r["attempts"] == 2 and calls


def test_fail_after_max_rewrites_records():
    seen = {}

    def regen(previous, error):  # noqa: ANN001
        return {"decision": "nope"}

    r = output_guard.validate(
        "审批决定", {"decision": "maybe"}, regenerate=regen, max_rewrites=2,
        on_failure=lambda v, e: seen.update({"errors": e}))
    assert r["ok"] is False and r["attempts"] == 3
    assert len(seen["errors"]) == 3


def test_unknown_kind_raises():
    try:
        output_guard.validate("不存在", {})
    except ValueError as exc:
        assert "未注册" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应抛 ValueError")


def test_http_validate_failure_records_audit(authed_client):
    st = authed_client.app.state
    r = authed_client.post(
        "/api/output-guard/validate",
        json={"kind": "审批决定", "raw": {"decision": "maybe"},
              "max_rewrites": 0, "agent_id": "agent-demo-001"},
        headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    row = state_conn(st).execute(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE action='llm.output_failed'"
    ).fetchone()
    assert row["n"] == 1

    kinds = authed_client.get("/api/output-guard/kinds")
    assert kinds.status_code == 200
    assert "条件单" in kinds.json()["kinds"]
