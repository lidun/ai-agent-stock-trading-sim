"""运行配置：以环境变量为主，可被 create_app(settings_override) 覆盖（用于测试）。

配置项全部取自有 CORE_ 前缀的环境变量；数值型配置支持 "0"/"false" 语义布尔转换。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _as_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8001
    data_dir: Path = field(default_factory=lambda: Path("core/data"))
    db_path: Path | None = None

    # 单用户登录（spec-06 §3/§6.13）
    auth_username: str = "admin"
    auth_password_env: str = ""            # 仅首次初始化时用于生成凭据
    credentials_file: str = "credentials.json"
    session_ttl_days: int = 7
    session_cookie_name: str = "aat_session"
    csrf_cookie_name: str = "aat_csrf"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    login_rate_max: int = 5                # 失败窗口内的最大尝试次数
    login_rate_window_s: int = 900         # 失败窗口（秒）
    login_lock_seconds: int = 900          # 超限后锁定秒数

    # 进程可靠（#41/#42）
    single_instance_lock: bool = True
    instance_lock_file: str = "core.lock"

    log_level: str = "INFO"
    env: str = "dev"                       # dev | prod（prod 强制 Secure Cookie）

    def resolved_db_path(self) -> Path:
        if self.db_path is not None:
            return Path(self.db_path)
        return Path(self.data_dir) / "aat.db"

    def resolved_credentials_path(self) -> Path:
        return Path(self.data_dir) / self.credentials_file

    def resolved_lock_path(self) -> Path:
        return Path(self.data_dir) / self.instance_lock_file


def load_settings() -> Settings:
    data_dir = Path(os.getenv("CORE_DATA_DIR", "core/data"))
    return Settings(
        host=os.getenv("CORE_HOST", "127.0.0.1"),
        port=_as_int("CORE_PORT", 8001),
        data_dir=data_dir,
        db_path=Path(os.getenv("CORE_DB_PATH", "")) if os.getenv("CORE_DB_PATH") else None,
        auth_username=os.getenv("CORE_AUTH_USERNAME", "admin"),
        auth_password_env=os.getenv("CORE_AUTH_PASSWORD", ""),
        credentials_file=os.getenv("CORE_CREDENTIALS_FILE", "credentials.json"),
        session_ttl_days=_as_int("CORE_SESSION_TTL_DAYS", 7),
        session_cookie_name=os.getenv("CORE_SESSION_COOKIE", "aat_session"),
        csrf_cookie_name=os.getenv("CORE_CSRF_COOKIE", "aat_csrf"),
        cookie_secure=_as_bool("CORE_COOKIE_SECURE", False),
        cookie_samesite=os.getenv("CORE_COOKIE_SAMESITE", "lax"),
        login_rate_max=_as_int("CORE_LOGIN_RATE_MAX", 5),
        login_rate_window_s=_as_int("CORE_LOGIN_RATE_WINDOW_S", 900),
        login_lock_seconds=_as_int("CORE_LOGIN_LOCK_S", 900),
        single_instance_lock=_as_bool("CORE_SINGLE_INSTANCE_LOCK", True),
        log_level=os.getenv("CORE_LOG_LEVEL", "INFO").upper(),
        env=os.getenv("CORE_ENV", "dev"),
    )


def settings_from_override(override: dict) -> Settings:
    """把环境配置与 override 合并（测试用）。"""
    s = load_settings()
    for k, v in override.items():
        if not hasattr(s, k):
            raise ValueError(f"未知配置项: {k}")
        setattr(s, k, v)
    return s
