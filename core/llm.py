"""LLM 统一适配层与 provider 配置（spec-04 §8 / spec-06 §6.13）。

- provider 配置单行落库 llm_provider；API Key 以 Fernet 加密存储，密钥文件随
  数据目录（0600）。所有读回/路由/健康接口只暴露掩码（api_key_hint），明文仅
  在 chat/test 的进程内解密使用，绝不外溢到响应与日志。
- P1 固定单模型：OpenAI 兼容 /chat/completions 单端点（spec-04 §8 路由排期
  P1 固定单模型 → v1 启发式 P2 → v2 效果评分 P4）。Anthropic 等薄适配后续补。
- 未配置 provider 显式抛 LLMNotConfigured：上层按 spec-04 §9.1 确定性降级，
  不静默产出伪叙述/伪评审。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

_KEY_FILENAME = "llm.key"


class LLMNotConfigured(RuntimeError):
    """模型服务未配置/密钥缺失时的确定性信号（上层降级，不猜内容）。"""


class LLMProviderError(RuntimeError):
    """远端调用失败（网络/鉴权/响应解析）。message 面向 UI 展示。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _data_dir(state) -> Path:
    path = getattr(state.db, "db_path", None)
    if path is None:
        raise RuntimeError("db 未初始化，无法解析数据目录")
    return Path(path).parent


def _fernet(state) -> Fernet:
    """加载/生成随数据目录的 0600 密钥文件（首次生成，之后复用）。"""
    key_file = _data_dir(state) / _KEY_FILENAME
    if not key_file.exists():
        try:
            fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(Fernet.generate_key().decode("ascii"))
    if key_file.stat().st_mode & 0o777 != 0o600:
        key_file.chmod(0o600)
    try:
        return Fernet(key_file.read_text(encoding="ascii").strip())
    except (ValueError, InvalidToken) as exc:
        raise RuntimeError(f"LLM 密钥文件损坏：{key_file}") from exc


def _enc(state, plain: str) -> str:
    return _fernet(state).encrypt(plain.encode("utf-8")).decode("ascii")


def _dec(state, cipher: str) -> str | None:
    try:
        return _fernet(state).decrypt(cipher.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        log.warning("llm api_key 解密失败（可能密钥文件被更换），按未配置处理")
        return None


def _hint(key: str) -> str:
    return f"****{key[-4:]}" if len(key) >= 4 else "****"


def _row(state, conn=None) -> dict | None:
    c = conn or state_conn(state)
    return c.execute(
        "SELECT * FROM llm_provider WHERE id=1",
    ).fetchone()


def provider_public(row: dict | None) -> dict:
    if row is None:
        return {
            "configured": False,
            "kind": "openai_compat",
            "preset": "",
            "base_url": "",
            "model": "",
            "api_key_set": False,
            "api_key_hint": "",
            "updated_ts": "",
            "updated_by": "",
            "last_test": None,
        }
    ok = bool(row["last_test_ok"])
    return {
        "configured": bool(row["api_key_enc"] and row["base_url"] and row["model"]),
        "kind": row["kind"],
        "preset": row["preset"],
        "base_url": row["base_url"],
        "model": row["model"],
        "api_key_set": bool(row["api_key_enc"]),
        "api_key_hint": row["api_key_hint"],
        "updated_ts": row["updated_ts"],
        "updated_by": row["updated_by"],
        "last_test": {
            "ts": row["last_test_ts"],
            "ok": ok,
            "error": row["last_test_error"] if row["last_test_error"] else None,
        } if row["last_test_ts"] else None,
    }


def provider_get(state) -> dict:
    return provider_public(_row(state))


def provider_configured(state) -> bool:
    """模型服务是否已配置（密钥+端点+模型齐备）——供调度器廉价门，避免空转 chat。"""
    row = _row(state)
    return bool(row and row["api_key_enc"] and row["base_url"] and row["model"])


def provider_health(state) -> dict:
    """健康自检视图：不含 base_url（避免把端点到处带），仅状态与模型。"""
    row = _row(state)
    cfg = provider_public(row)
    return {
        "configured": cfg["configured"],
        "api_key_set": cfg["api_key_set"],
        "preset": cfg["preset"],
        "model": cfg["model"] if cfg["configured"] else "",
        "last_test": cfg["last_test"],
    }


def _validate(preset: str, base_url: str, model: str) -> None:
    parts = urlsplit(base_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base_url 须为 http(s) 端点（如 https://api.deepseek.com/v1）")
    if not model.strip():
        raise ValueError("模型名不能为空")
    if preset and len(preset) > 40:
        raise ValueError("preset 过长")


def provider_upsert(state, *, actor: str, preset: str = "", base_url: str = "",
                    model: str = "", api_key: str | None = None) -> dict:
    """保存 provider 配置；api_key 传 None 表示保留原密钥（掩码场景不回传明文）。"""
    base_url = base_url.strip().rstrip("/")
    model = model.strip()
    preset = preset.strip()
    if not base_url and not model and not api_key:
        raise ValueError("base_url/model/API Key 至少提供其一")
    if api_key is not None and not api_key.strip():
        raise ValueError("API Key 不能为空串；保留原密钥请省略该字段")
    if base_url or model:
        _validate(preset, base_url, model)
    conn = state_conn(state)
    now = _now_iso()
    with write_txn(conn) as c:
        existing = _row(state, conn=c)
        enc = existing["api_key_enc"] if existing else ""
        hint = existing["api_key_hint"] if existing else ""
        if api_key is not None:
            enc = _enc(state, api_key)
            hint = _hint(api_key)
        base_url = base_url or (existing["base_url"] if existing else "")
        model = model or (existing["model"] if existing else "")
        preset = preset or (existing["preset"] if existing else "")
        _validate(preset, base_url, model)
        if existing:
            c.execute(
                "UPDATE llm_provider SET preset=?, base_url=?, model=?,"
                " api_key_enc=?, api_key_hint=?, updated_ts=?, updated_by=? WHERE id=1",
                (preset, base_url, model, enc, hint, now, actor),
            )
        else:
            c.execute(
                "INSERT INTO llm_provider (id, preset, base_url, model, api_key_enc,"
                " api_key_hint, updated_ts, updated_by)"
                " VALUES (1,?,?,?,?,?,?,?)",
                (preset, base_url, model, enc, hint, now, actor),
            )
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (now, actor, "settings.llm.update", "llm_provider", "1", "updated",
             json.dumps({"preset": preset, "base_url": base_url, "model": model,
                         "key_changed": api_key is not None}, ensure_ascii=False), ""),
        )
    return provider_get(state)


def provider_clear(state, *, actor: str) -> dict:
    """移除模型服务配置（spec-06 §6.13 清除密钥语义，同时清 base/model）。"""
    conn = state_conn(state)
    with write_txn(conn) as c:
        row = _row(state, conn=c)
        if row:
            c.execute("DELETE FROM llm_provider WHERE id=1")
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, "settings.llm.clear", "llm_provider", "1",
             "cleared", json.dumps({"had_key": bool(row and row["api_key_enc"])},
                                   ensure_ascii=False), ""),
        )
    return provider_get(state)


def _config_secret(state) -> dict:
    row = _row(state)
    if row is None or not (row["api_key_enc"] and row["base_url"] and row["model"]):
        raise LLMNotConfigured("模型服务未配置（设置页 → 模型服务）")
    key = _dec(state, row["api_key_enc"])
    if not key:
        raise LLMNotConfigured("模型服务密钥不可解密，请重新在设置页填入")
    return {"base_url": row["base_url"], "model": row["model"], "api_key": key}


def _post_chat(cfg: dict, messages: list[dict], *, timeout_s: float) -> dict:
    url = f"{cfg['base_url']}/chat/completions"
    payload = {"model": cfg["model"], "messages": messages, "stream": False}
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        message = choice.get("message", {})
        usage = data.get("usage") or {}
        return {
            "content": message.get("content"),
            "finish_reason": choice.get("finish_reason"),
            "model": data.get("model") or cfg["model"],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
        }
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise LLMProviderError(f"模型服务调用失败：{exc}") from exc


def chat(state, messages: list[dict], *, timeout_s: float = 60.0) -> dict:
    """统一 chat 入口（spec-04 §8 适配层，P1 单模型）。

    返回 {content, finish_reason, model, usage}；未配置抛 LLMNotConfigured。
    tools/stream 等能力门槛后续在 provider 行上扩展（v1 路由排期）。
    """
    if not messages:
        raise ValueError("messages 不能为空")
    return _post_chat(_config_secret(state), messages, timeout_s=timeout_s)


def provider_test(state, *, timeout_s: float = 10.0, actor: str = "ui") -> dict:
    """连通性测试：发起 1 条极短 chat，结果写回 last_test_* 并留审计。"""
    try:
        cfg = _config_secret(state)
    except LLMNotConfigured as exc:
        return {"ok": False, "configured": False, "error": str(exc)}
    started = time.monotonic()
    try:
        resp = _post_chat(
            cfg, [{"role": "user", "content": "连通性自检：回复「连通」即可"}],
            timeout_s=timeout_s)
        latency_ms = int((time.monotonic() - started) * 1000)
        ok = True
        error = ""
        out = {"ok": True, "configured": True, "model": resp["model"],
               "latency_ms": latency_ms, "usage": resp["usage"]}
    except LLMProviderError as exc:
        ok = False
        error = str(exc)
        out = {"ok": False, "configured": True, "error": error}
    conn = state_conn(state)
    now = _now_iso()
    with write_txn(conn) as c:
        c.execute(
            "UPDATE llm_provider SET last_test_ts=?, last_test_ok=?,"
            " last_test_error=? WHERE id=1",
            (now, 1 if ok else 0, error, ),
        )
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (now, actor, "settings.llm.test", "llm_provider", "1",
             "ok" if ok else "failed",
             json.dumps({k: v for k, v in out.items() if k != "usage"},
                        ensure_ascii=False), ""),
        )
    return out
