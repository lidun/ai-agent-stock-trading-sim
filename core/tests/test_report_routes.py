"""日报 API 出口测试（spec-06 §6.6 日报中心数据源：时间线 + 单日版本切换）。"""
from __future__ import annotations

from core import eodengine, reporting
from test_reporting import DEMO, _buy


def _absent_resend(st):
    reporting.store_engine_report(st, DEMO, "2026-09-08", status="absent",
                                  narrative="当日未运行（测试）")
    reporting.store_engine_report(st, DEMO, "2026-09-08", status="resend",
                                  narrative="修订补发")


def test_report_timeline_shows_latest_per_date(authed_client):
    """时间线：多日多版本 → 每交易日仅最新版，absent/resend 状态正确。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    _absent_resend(st)
    r = authed_client.get(f"/api/accounts/{DEMO}/reports")
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == DEMO
    entries = {e["trade_date"]: e for e in body["reports"]}
    assert sorted(entries) == ["2026-09-04", "2026-09-08"]
    assert entries["2026-09-04"]["latest_version"] == 1
    assert entries["2026-09-04"]["status"] == "normal"
    assert entries["2026-09-08"]["latest_version"] == 2
    assert entries["2026-09-08"]["status"] == "resend"


def test_report_detail_returns_version_history(authed_client):
    """单日详情：全版本新→旧，含 data_section/merged_markdown/narrative（spec-06 版本切换）。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    _absent_resend(st)
    r = authed_client.get(f"/api/accounts/{DEMO}/reports/2026-09-08")
    assert r.status_code == 200
    versions = r.json()["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["status"] == "resend" and versions[0]["narrative"] == "修订补发"
    assert versions[0]["data_section"]["settlement"]["done"] is False
    assert versions[0]["merged_markdown"].startswith("# 数据段日报 2026-09-08")
    assert "未结算" in versions[0]["merged_markdown"]
    # 未落库日期 → 空版本列表（UI 空态，非错误）
    assert authed_client.get(
        f"/api/accounts/{DEMO}/reports/2026-09-11").json()["versions"] == []


def test_report_api_requires_session(client):
    """未登录 401（client 夹具仅本测试使用，未执行过登录）。"""
    assert client.get(f"/api/accounts/{DEMO}/reports").status_code == 401
    assert client.get(f"/api/accounts/{DEMO}/reports/2026-09-04").status_code == 401


def test_report_api_unknown_account_404(authed_client):
    """未知账户 → 404（时间线与单日均校验属主账户存在）。"""
    assert authed_client.get("/api/accounts/no-such-agent/reports").status_code == 404
    assert authed_client.get(
        "/api/accounts/no-such-agent/reports/2026-09-04").status_code == 404


def test_report_detail_matches_direct_build(authed_client):
    """API 单日单版 data_section 与 build 直读一致（出口即数据段，无二次加工）。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    ds = reporting.build_engine_data_section(st, DEMO, "2026-09-04")
    got = authed_client.get(
        f"/api/accounts/{DEMO}/reports/2026-09-04").json()["versions"][0]["data_section"]
    assert got == ds

from core.db import state_conn


def _patch(client, url, body):
    client.get("/api/auth/csrf")
    from conftest import csrf_headers
    return client.patch(url, json=body, headers=csrf_headers(client))


def test_narrative_patch_persists_with_audit(authed_client):
    """叙述段写入：PATCH 落库可读、留审计；空叙述可用于清除。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    url = f"/api/accounts/{DEMO}/reports/2026-09-04/versions/1/narrative"
    r = _patch(authed_client, url, {"narrative": "四段观察：指数平开；五段：维持持仓。"})
    assert r.status_code == 200 and r.json()["report"]["unchanged"] is False
    detail = authed_client.get(
        f"/api/accounts/{DEMO}/reports/2026-09-04").json()["versions"][0]
    assert detail["narrative"] == "四段观察：指数平开；五段：维持持仓。"
    n = state_conn(st).execute(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE action='report.narrative_update'"
    ).fetchone()["n"]
    assert n == 1
    # 幂等同文重写 → unchanged=True 不新增审计
    r2 = _patch(authed_client, url, {"narrative": "四段观察：指数平开；五段：维持持仓。"})
    assert r2.json()["report"]["unchanged"] is True
    n2 = state_conn(st).execute(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE action='report.narrative_update'"
    ).fetchone()["n"]
    assert n2 == 1


def test_narrative_patch_guards(authed_client):
    """叙述段守卫：越限 400、未知版本 404。"""
    _buy(authed_client.app.state, day="2026-09-04")
    url = f"/api/accounts/{DEMO}/reports/2026-09-04/versions/1/narrative"
    assert _patch(authed_client, url, {"narrative": "长" * 12001}).status_code == 400
    assert _patch(authed_client, url, {"narrative": ""}).status_code == 200   # 清除合法
    assert _patch(authed_client, url.replace("/versions/1/", "/versions/99/"),
                  {"narrative": "x"}).status_code == 404


def test_report_export_downloads_markdown(authed_client):
    """导出：text/markdown + Content-Disposition，含数据段与叙述段尾部。"""
    st = authed_client.app.state
    _buy(st, day="2026-09-04")
    url = f"/api/accounts/{DEMO}/reports/2026-09-04/versions/1/narrative"
    assert _patch(authed_client, url, {"narrative": "叙述段正文"}).status_code == 200
    r = authed_client.get(f"/api/accounts/{DEMO}/reports/2026-09-04/export?version=1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith("report_agent-demo-001_2026-09-04_v1.md\"")
    assert r.text.startswith("# 数据段日报 2026-09-04")
    assert "## 叙述段\n\n叙述段正文" in r.text
    # 无版本日报 → 404
    assert authed_client.get(
        f"/api/accounts/{DEMO}/reports/2026-09-11/export").status_code == 404
