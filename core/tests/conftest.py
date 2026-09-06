"""pytest 根：在导入 core.app 前先布置测试环境（单实例锁关闭、临时数据目录、测试口令）。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.mkdtemp(prefix="aat-core-test-"))

os.environ["CORE_SINGLE_INSTANCE_LOCK"] = "0"
os.environ["CORE_DATA_DIR"] = str(_TMP)
os.environ["CORE_LOG_LEVEL"] = "ERROR"
os.environ["CORE_AUTH_PASSWORD"] = "test-password-1"
os.environ["CORE_AUTH_USERNAME"] = "admin"
os.environ["CORE_SESSION_COOKIE"] = "aat_session_test"
os.environ["CORE_CSRF_COOKIE"] = "aat_csrf_test"
os.environ["CORE_ENV"] = "dev"

from core.app import create_app  # noqa: E402

TEST_PASSWORD = "test-password-1"


@pytest.fixture()
def client():
    """每测试独立数据目录 + 独立 app 实例（互相隔离）。"""
    data_dir = Path(tempfile.mkdtemp(prefix="aat-core-case-"))
    app = create_app({
        "data_dir": data_dir,
        "single_instance_lock": False,
        "engine_stub_delay_ms": 0,      # 对话测试即时推进回执链
    })
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def csrf_headers(client: TestClient) -> dict:
    """读取当前 CSRF Cookie 并组装写请求头。"""
    token = client.cookies.get("aat_csrf_test")
    headers = {"X-CSRF-Token": token} if token else {}
    return headers


def do_login(client: TestClient, password: str = TEST_PASSWORD, username: str = "admin"):
    headers = csrf_headers(client)
    r = client.post("/api/auth/login", json={"username": username, "password": password},
                    headers=headers)
    return r


@pytest.fixture()
def authed_client(client):
    r = do_login(client)
    assert r.status_code == 200, r.text
    return client
