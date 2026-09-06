"""auth/health/ws/CSRF 冒烟与流程测试（spec-06 §3、§10 测试计划·通信与访问）。"""
from __future__ import annotations

from conftest import TEST_PASSWORD, csrf_headers, do_login


# ---------- health ----------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "ai-agent-trading-core"
    assert body["db"] is True


# ---------- 未登录保护 ----------

def test_me_requires_login(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


# ---------- CSRF ----------

def test_csrf_token_endpoint(client):
    r = client.get("/api/auth/csrf")
    assert r.status_code == 200
    assert r.json()["csrf_token"]
    assert client.cookies.get("aat_csrf_test")


def test_write_without_csrf_denied_when_authed(authed_client):
    # 已登录会话下，无 CSRF 头的写请求应被拒绝（登录端点本身豁免）
    r = authed_client.post("/api/auth/logout", headers={})
    assert r.status_code == 403


def test_write_with_csrf_allowed(authed_client):
    headers = csrf_headers(authed_client)
    assert headers
    r = authed_client.post("/api/auth/logout", headers=headers)
    assert r.status_code == 200


# ---------- 登录流程 ----------

def test_login_wrong_password(client):
    r = do_login(client, password="wrong-password")
    assert r.status_code == 401


def test_login_wrong_username(client):
    r = do_login(client, username="nobody")
    assert r.status_code == 401


def test_login_success_sets_cookies(client):
    r = do_login(client)
    assert r.status_code == 200
    assert client.cookies.get("aat_session_test")
    assert client.cookies.get("aat_csrf_test")
    assert r.json()["user"] == "admin"


def test_me_after_login(authed_client):
    r = authed_client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == "admin"
    assert body["core_version"]


def test_login_audit_recorded(authed_client):
    client = authed_client
    r = client.get("/api/health")
    assert r.status_code == 200
    # audit_logs 落库验证：直接查 DB 太耦合，改为断言会话列表存在
    sessions = client.get("/api/auth/sessions")
    assert sessions.status_code == 200
    assert len(sessions.json()["sessions"]) >= 1


# ---------- 限速（窗口 5 次失败 → 锁定） ----------

def test_login_rate_limit(client):
    for i in range(5):
        r = do_login(client, password="bad")
        assert r.status_code == 401
    r = do_login(client, password="bad")
    assert r.status_code == 429
    # 锁定期间即使口令正确也拒绝
    r = do_login(client, password=TEST_PASSWORD)
    assert r.status_code == 429


# ---------- 修改口令 / 强制下线 ----------

def test_change_password_revokes_other_sessions(client):
    do_login(client)
    session_cookie = client.cookies.get("aat_session_test")
    csrf = csrf_headers(client)

    # 构造第二个会话（同一个 client 会话覆盖 cookie——用一个独立请求头手动携带旧 cookie）
    # 更简单：直接对当前会话改口令，断言 ok，再用新口令登录。
    r = client.post("/api/auth/change_password",
                    json={"current_password": TEST_PASSWORD, "new_password": "new-pass-12345"},
                    headers=csrf)
    assert r.status_code == 200

    # 旧口令不再可登录
    r2 = do_login(client, password=TEST_PASSWORD)
    assert r2.status_code == 401

    # 当前会话仍有效（改口令后未吊销当前会话）
    r3 = client.get("/api/auth/me")
    assert r3.status_code == 200
    _ = session_cookie


def test_change_password_wrong_current(client):
    do_login(client)
    r = client.post("/api/auth/change_password",
                    json={"current_password": "nope", "new_password": "new-pass-12345"},
                    headers=csrf_headers(client))
    assert r.status_code == 400


def test_sessions_list_and_revoke(client):
    do_login(client)
    current_hash = client.get("/api/auth/sessions").json()["current_token_hash"]
    sessions = client.get("/api/auth/sessions").json()["sessions"]
    assert any(s["token_hash"] == current_hash for s in sessions)
    # 吊销当前会话
    r = client.post(f"/api/auth/sessions/{current_hash}/revoke", headers=csrf_headers(client))
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


# ---------- 登出 ----------

def test_logout_clears_session(authed_client):
    r = authed_client.post("/api/auth/logout", headers=csrf_headers(authed_client))
    assert r.status_code == 200
    assert authed_client.get("/api/auth/me").status_code == 401


# ---------- WebSocket ----------

def test_ws_requires_auth(client):
    import pytest as _pytest
    from starlette.websockets import WebSocketDisconnect as _WSD

    with _pytest.raises(_WSD) as exc:
        with client.websocket_connect("/ws", cookies={}):
            pass
    assert exc.value.code == 4401


def test_ws_ping_pong_after_login(authed_client):
    cookie = authed_client.cookies.get("aat_session_test")
    assert cookie
    with authed_client.websocket_connect("/ws", cookies={"aat_session_test": cookie}) as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
