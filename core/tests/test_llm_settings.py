"""模型服务设置与 LLM 适配层测试（spec-04 §8 / spec-06 §6.13）。

覆盖：密钥加密落库+掩码不回显、省略 key 保留原密钥、未配置确定性信号、
连通性测试成功/失败留痕、移除配置、chat 统一入口与 health 配置态。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import llm
from core.db import state_conn
from conftest import csrf_headers

TEST_KEY = "sk-test-1234567890abcd"


def _put(client, body):
    return client.patch("/api/settings/llm-provider", json=body,
                        headers=csrf_headers(client))


def _delete(client):
    return client.delete("/api/settings/llm-provider", headers=csrf_headers(client))


def _test(client):
    return client.post("/api/settings/llm-provider/test", json={},
                       headers=csrf_headers(client))


def _configure(client, base_url="https://api.deepseek.com/v1",
               model="deepseek-chat", key=TEST_KEY, preset="DeepSeek"):
    return _put(client, {"preset": preset, "base_url": base_url,
                         "model": model, "api_key": key})


def test_settings_requires_session(client):
    assert client.get("/api/settings/llm-provider").status_code == 401


def test_provider_defaults_unconfigured(authed_client):
    body = authed_client.get("/api/settings/llm-provider").json()
    assert body["configured"] is False
    assert body["api_key_set"] is False and body["api_key_hint"] == ""
    assert body["model"] == "" and body["base_url"] == ""


def test_provider_upsert_masked_and_encrypted(authed_client):
    r = _configure(authed_client)
    assert r.status_code == 200
    body = r.json()["provider"]
    assert body["configured"] is True
    assert body["base_url"] == "https://api.deepseek.com/v1"
    assert body["model"] == "deepseek-chat"
    assert body["api_key_set"] is True
    assert body["api_key_hint"] == "****abcd"
    assert TEST_KEY not in r.text and TEST_KEY not in authed_client.get(
        "/api/settings/llm-provider").text
    # 落库为密文
    st = authed_client.app.state
    row = state_conn(st).execute(
        "SELECT api_key_enc, api_key_hint FROM llm_provider WHERE id=1").fetchone()
    assert row["api_key_enc"] != TEST_KEY and TEST_KEY not in row["api_key_enc"]
    assert row["api_key_hint"] == "****abcd"
    # 密钥文件 0600 且可解密回原文
    key_file = Path(st.db.db_path).parent / "llm.key"
    assert key_file.exists()
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert llm._dec(st, row["api_key_enc"]) == TEST_KEY


def test_provider_upsert_without_key_keeps_existing(authed_client):
    _configure(authed_client)
    st = authed_client.app.state
    before = state_conn(st).execute(
        "SELECT api_key_enc FROM llm_provider WHERE id=1").fetchone()["api_key_enc"]
    r = _put(authed_client, {"base_url": "https://api.deepseek.com/v1",
                             "model": "deepseek-chat", "preset": "DeepSeek"})
    assert r.status_code == 200
    assert r.json()["provider"]["api_key_hint"] == "****abcd"
    after = state_conn(st).execute(
        "SELECT api_key_enc FROM llm_provider WHERE id=1").fetchone()["api_key_enc"]
    assert before == after


def test_provider_invalid_base_rejected(authed_client):
    r = _put(authed_client, {"base_url": "not-a-url", "model": "x",
                             "api_key": TEST_KEY})
    assert r.status_code == 400
    assert authed_client.get("/api/settings/llm-provider").json()["configured"] is False


def test_test_endpoint_unconfigured_is_deterministic(authed_client):
    r = _test(authed_client)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["configured"] is False
    assert "未配置" in body["error"]


def test_test_endpoint_success_records_health(authed_client, monkeypatch):
    _configure(authed_client)
    seen = {}

    def fake_post(cfg, messages, *, timeout_s):
        seen.update(cfg=cfg, messages=messages, timeout=timeout_s)
        return {"content": "连通", "finish_reason": "stop", "model": cfg["model"],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4,
                          "total_tokens": 13}}

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    r = _test(authed_client)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["model"] == "deepseek-chat"
    assert body["latency_ms"] >= 0 and body["usage"]["total_tokens"] == 13
    st = authed_client.app.state
    row = state_conn(st).execute(
        "SELECT last_test_ok, last_test_error FROM llm_provider WHERE id=1").fetchone()
    assert row["last_test_ok"] == 1 and row["last_test_error"] == ""
    health = authed_client.get("/api/health").json()
    assert health["llm"]["configured"] is True
    assert health["llm"]["model"] == "deepseek-chat"
    assert "sk-" not in authed_client.get("/api/health").text


def test_test_endpoint_failure_records_error(authed_client, monkeypatch):
    _configure(authed_client)

    def boom(cfg, messages, *, timeout_s):
        raise llm.LLMProviderError("401 鉴权失败")

    monkeypatch.setattr(llm, "_post_chat", boom)
    r = _test(authed_client)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["configured"] is True
    assert "401" in body["error"]
    row = state_conn(authed_client.app.state).execute(
        "SELECT last_test_ok, last_test_error FROM llm_provider WHERE id=1").fetchone()
    assert row["last_test_ok"] == 0 and "401" in row["last_test_error"]


def test_provider_clear(authed_client):
    _configure(authed_client)
    assert _delete(authed_client).json()["provider"]["configured"] is False
    body = authed_client.get("/api/settings/llm-provider").json()
    assert body["configured"] is False and body["api_key_set"] is False
    assert body["model"] == "" and body["base_url"] == ""
    row = state_conn(authed_client.app.state).execute(
        "SELECT COUNT(*) n FROM llm_provider").fetchone()
    assert row["n"] == 0
    # 审计留痕
    audits = state_conn(authed_client.app.state).execute(
        "SELECT action FROM audit_logs"
        " WHERE object_type='llm_provider' ORDER BY id").fetchall()
    actions = [a["action"] for a in audits]
    assert "settings.llm.update" in actions and "settings.llm.clear" in actions


def test_chat_unconfigured_raises_deterministic(authed_client):
    with pytest.raises(llm.LLMNotConfigured):
        llm.chat(authed_client.app.state, [{"role": "user", "content": "hi"}])


def test_chat_uses_configured_provider(authed_client, monkeypatch):
    _configure(authed_client)
    seen = {}

    def fake_post(cfg, messages, *, timeout_s):
        seen.update(cfg=cfg, messages=messages)
        return {"content": "你好", "finish_reason": "stop", "model": cfg["model"],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3,
                          "total_tokens": 5}}

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    out = llm.chat(authed_client.app.state, [{"role": "user", "content": "你好"}])
    assert out["content"] == "你好"
    assert seen["cfg"]["base_url"] == "https://api.deepseek.com/v1"
    assert seen["cfg"]["api_key"] == TEST_KEY
    assert out["usage"]["total_tokens"] == 5
