"""单用户登录/会话/CSRF 路由（spec-06 §3、§6.13 账户与安全组）。

- POST /api/auth/login          登录：签发 HttpOnly 会话 Cookie + CSRF Cookie
- POST /api/auth/logout         登出：吊销会话并清 Cookie
- GET  /api/auth/me             当前会话信息
- POST /api/auth/change_password 修改口令（当前口令校验后更新凭据并吊销其他会话）
- GET  /api/auth/sessions       会话列表
- POST /api/auth/sessions/{id}/revoke  强制下线指定会话
- GET  /api/auth/csrf           刷新并返回 CSRF Token
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core import __version__
from core.db import read_txn, state_conn, write_txn
from core.security import (
    CredentialStore,
    LoginRateLimiter,
    bootstrap_credentials,
    hash_password,
    hash_token,
    new_token,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_COOKIE_MAX_AGE = None  # 与 expires 一致由服务端控制


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _client_ip(request: Request) -> str:
    # 生产经 Nginx 反代，取 X-Forwarded-For 首段（spec-06 §8：Nginx 负责设置真实 IP）
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def get_request_context(request: Request) -> dict:
    return {
        "ip": _client_ip(request),
        "user_agent": request.headers.get("user-agent", "")[:512],
    }


def _cookie_kwargs(settings) -> dict:
    return {
        "secure": settings.cookie_secure or settings.env == "prod",
        "httponly": True,
        "samesite": settings.cookie_samesite,
        "path": "/",
        "max_age": settings.session_ttl_days * 86400,
    }


# ---------- 会话读写 ----------

def create_session(state, username: str, ctx: dict) -> tuple[str, datetime]:
    token = new_token()
    expires = _utcnow() + timedelta(days=state.settings.session_ttl_days)
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO auth_sessions (token_hash, username, created_at, expires_at,"
            " last_seen_at, ip, user_agent, revoked) VALUES (?,?,?,?,?,?,?,0)",
            (hash_token(token), username, _iso(_utcnow()), _iso(expires), _iso(_utcnow()),
             ctx["ip"], ctx["user_agent"]),
        )
    return token, expires


def get_session(state, raw_token: str | None) -> dict | None:
    if not raw_token:
        return None
    conn = state_conn(state)
    with read_txn(conn) as c:
        row = c.execute(
            "SELECT token_hash, username, created_at, expires_at, last_seen_at, revoked FROM auth_sessions"
            " WHERE token_hash = ?",
            (hash_token(raw_token),),
        ).fetchone()
    if row is None or row["revoked"]:
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if expires <= _utcnow():
        return None
    return dict(row)


def touch_session(state, token_hash: str) -> None:
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?",
                  (_iso(_utcnow()), token_hash))


def revoke_session(state, token_hash: str, username: str | None = None) -> None:
    conn = state_conn(state)
    with write_txn(conn) as c:
        if username:
            c.execute("UPDATE auth_sessions SET revoked = 1 WHERE token_hash = ? AND username = ?",
                      (token_hash, username))
        else:
            c.execute("UPDATE auth_sessions SET revoked = 1 WHERE token_hash = ?", (token_hash,))


def audit(state, actor: str, action: str, *, object_type: str = "", object_id: str = "",
          result: str = "ok", detail: str = "", ctx: dict | None = None) -> None:
    conn = state_conn(state)
    ctx = ctx or {}
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id, result, detail, ip)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (_iso(_utcnow()), actor, action, object_type, object_id, result, detail[:2000],
             ctx.get("ip", "")),
        )


# ---------- API 模型 ----------

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class SessionIn(BaseModel):
    token_hash: str = Field(min_length=1)


# ---------- 依赖 ----------

def require_session(request: Request):
    settings = request.app.state.settings
    raw = request.cookies.get(settings.session_cookie_name)
    sess = get_session(request.app.state, raw)
    if sess is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return {"raw_token": raw, "session": sess}


SessionDep = Annotated[dict, Depends(require_session)]


def _credentials(request: Request) -> CredentialStore:
    s = request.app.state.settings
    return CredentialStore(s.resolved_credentials_path(), s.auth_username)


def _rate_limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.login_limiter


# ---------- 端点 ----------

@router.get("/csrf")
def csrf_token(request: Request, response: Response):
    """返回并下发 CSRF Token（前端写请求须携带 X-CSRF-Token 头）。"""
    token = new_token()
    response.set_cookie(
        request.app.state.settings.csrf_cookie_name,
        token,
        secure=request.app.state.settings.cookie_secure or request.app.state.settings.env == "prod",
        httponly=False,
        samesite=request.app.state.settings.cookie_samesite,
        path="/",
        max_age=request.app.state.settings.session_ttl_days * 86400,
    )
    return {"csrf_token": token}


@router.post("/login")
def login(payload: LoginIn, request: Request, response: Response):
    settings = request.app.state.settings
    store = _credentials(request)
    limiter = _rate_limiter(request)
    ctx = get_request_context(request)

    if payload.username != settings.auth_username:
        _failed_login(request, limiter, ctx, store, payload)
        raise HTTPException(status_code=401, detail="用户名或口令错误")

    allowed, wait = limiter.check(ctx["ip"], payload.username)
    if not allowed:
        audit(request.app.state, payload.username, "auth.login", result="locked",
              detail=f"失败次数过多，锁定 {wait}s", ctx=ctx)
        raise HTTPException(status_code=429, detail=f"尝试次数过多，请 {int(wait)} 秒后重试")

    if not store.exists():
        # 首次运行兜底：用环境变量初始化凭据
        bootstrap_credentials(store, settings.auth_password_env)
    if not store.verify(payload.password):
        _failed_login(request, limiter, ctx, store, payload)
        raise HTTPException(status_code=401, detail="用户名或口令错误")

    limiter.reset(ctx["ip"], payload.username)
    token, _expires = create_session(request.app.state, payload.username, ctx)
    audit(request.app.state, payload.username, "auth.login", result="ok", ctx=ctx)

    csrf = new_token()
    settings = request.app.state.settings
    secure = settings.cookie_secure or settings.env == "prod"
    response.set_cookie(settings.session_cookie_name, token, **_cookie_kwargs(settings))
    response.set_cookie(settings.csrf_cookie_name, csrf, secure=secure, httponly=False,
                        samesite=settings.cookie_samesite, path="/",
                        max_age=settings.session_ttl_days * 86400)
    return {"ok": True, "user": payload.username, "csrf_token": csrf}


def _failed_login(request, limiter, ctx, store, payload) -> None:
    locked = limiter.record_failure(ctx["ip"], payload.username)
    detail = "锁定触发" if locked else "口令错误"
    audit(request.app.state, payload.username or "unknown", "auth.login", result="fail",
          detail=detail, ctx=ctx)


@router.post("/logout")
def logout(request: Request, response: Response, session: SessionDep):
    settings = request.app.state.settings
    revoke_session(request.app.state, hash_token(session["raw_token"]))
    audit(request.app.state, session["session"]["username"], "auth.logout", result="ok",
          ctx=get_request_context(request))
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(session: SessionDep):
    s = session["session"]
    return {
        "user": s["username"],
        "login_at": s["created_at"],
        "core_version": __version__,
    }


@router.post("/change_password")
def change_password(payload: ChangePasswordIn, request: Request, response: Response,
                    session: SessionDep):
    store = _credentials(request)
    ctx = get_request_context(request)
    username = session["session"]["username"]
    if not store.verify(payload.current_password):
        audit(request.app.state, username, "auth.change_password", result="fail",
              detail="当前口令错误", ctx=ctx)
        raise HTTPException(status_code=400, detail="当前口令错误")
    store.save(hash_password(payload.new_password))
    # 吊销除当前会话外的全部会话（强制下线）
    conn = state_conn(request.app.state)
    with write_txn(conn) as c:
        c.execute("UPDATE auth_sessions SET revoked = 1 WHERE username = ? AND token_hash != ?",
                  (username, hash_token(session["raw_token"])))
    audit(request.app.state, username, "auth.change_password", result="ok",
          detail="口令已修改，其他会话已下线", ctx=ctx)
    return {"ok": True}


@router.get("/sessions")
def list_sessions(request: Request, session: SessionDep):
    conn = state_conn(request.app.state)
    with read_txn(conn) as c:
        rows = c.execute(
            "SELECT token_hash, created_at, last_seen_at, ip, user_agent, revoked, expires_at"
            " FROM auth_sessions WHERE username = ? ORDER BY created_at DESC",
            (session["session"]["username"],),
        ).fetchall()
    return {
        "current_token_hash": hash_token(session["raw_token"]),
        "sessions": [dict(r) for r in rows],
    }


@router.post("/sessions/{token_hash}/revoke")
def revoke(request: Request, token_hash: str, session: SessionDep):
    conn = state_conn(request.app.state)
    with write_txn(conn) as c:
        cur = c.execute(
            "UPDATE auth_sessions SET revoked = 1 WHERE token_hash = ? AND username = ?",
            (token_hash, session["session"]["username"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="会话不存在")
    audit(request.app.state, session["session"]["username"], "auth.session_revoke",
          object_type="auth_session", object_id=token_hash[:12], result="ok",
          ctx=get_request_context(request))
    return {"ok": True}
