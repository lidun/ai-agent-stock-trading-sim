"""HTTP API 装配与通用端点。

- 中间件：CSRF 校验（写请求须携带 X-CSRF-Token，与 CSRF Cookie 一致）；
  安全响应头。
- 通用端点：GET /api/health（systemd 健康检查/看门狗用）；GET /api/meta。
- WebSocket /ws：登录后连接，心跳 ping/pong（spec-06 §3 断线/重连由前端承担）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from core import __version__
from core.auth import get_session, hash_token
from core.db import state_conn
from core.config import Settings

log = logging.getLogger(__name__)

api = APIRouter()

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_SKIP_CSRF_PATHS = {"/api/auth/login", "/api/auth/csrf", "/api/health", "/ws"}


class CsrfMiddleware(BaseHTTPMiddleware):
    """双提交 Cookie CSRF：写请求必须携带与 CSRF Cookie 一致的 X-CSRF-Token。"""

    async def dispatch(self, request: Request, call_next):
        settings: Settings = request.app.state.settings
        method = request.method.upper()
        path = request.url.path

        if method in _UNSAFE and path not in _SKIP_CSRF_PATHS:
            session_cookie = request.cookies.get(settings.session_cookie_name)
            csrf_cookie = request.cookies.get(settings.csrf_cookie_name)
            if session_cookie:
                header = request.headers.get("x-csrf-token", "")
                if not csrf_cookie or header != csrf_cookie:
                    return JSONResponse(status_code=403, content={"detail": "CSRF 校验失败"})
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api") or request.url.path == "/ws":
            return response
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss:; script-src 'self'",
        )
        return response


@api.get("/api/health")
def health(request: Request):
    state = request.app.state
    settings: Settings = state.settings
    started = state.started_at
    return {
        "ok": True,
        "service": "ai-agent-trading-core",
        "version": __version__,
        "env": settings.env,
        "uptime_s": int(time.time() - started),
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(
            timespec="seconds"),
        "db": bool(state_conn(state).execute("SELECT 1").fetchone()[0] == 1),
        "single_instance": state.instance_acquired,
    }


@api.get("/api/meta")
def meta(request: Request):
    settings: Settings = request.app.state.settings
    return {"auth_configured": _credential_file_exists(settings)}


def _credential_file_exists(settings: Settings) -> bool:
    return settings.resolved_credentials_path().exists()


# ---------- WebSocket：登录连接 + 心跳 ----------

async def _ws_cookie(websocket: WebSocket, name: str) -> str | None:
    raw_cookie = websocket.headers.get("cookie", "")
    for part in raw_cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


@api.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    settings: Settings = websocket.app.state.settings
    raw = await _ws_cookie(websocket, settings.session_cookie_name)
    session = get_session(websocket.app.state, raw) if raw else None
    if session is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    log.info("ws connected: user=%s", session["username"])
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")
            else:
                await websocket.send_text(f"echo:{message}")
    except WebSocketDisconnect:
        log.info("ws disconnected")
    except Exception as e:  # noqa: BLE001
        log.warning("ws error: %s", e)
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
