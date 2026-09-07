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
