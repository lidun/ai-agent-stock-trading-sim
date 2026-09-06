"""安全原语：口令哈希（Argon2）、会话/CSRF Token、登录速率限制、凭据存储、单实例锁。

spec-06 §3 / 总纲 §12.7：单用户账号，口令 Argon2 哈希存储（credentials.json，0600）；
登录失败速率限制与审计；core 进程级单实例锁（#41）。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

log = logging.getLogger(__name__)

_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, hash_len=32)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hash_str: str, password: str) -> bool:
    try:
        return _ph.verify(hash_str, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return _sha256(token)


class CredentialStore:
    """单用户凭据：data_dir/credentials.json（Argon2 哈希），文件 0600。"""

    def __init__(self, path: Path, username: str):
        self.path = path
        self.username = username
        self._lock = threading.Lock()

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            log.error("凭据文件损坏: %s（可删除后重启以重建）", self.path)
            return None
        if data.get("username") != self.username:
            log.error("凭据文件用户(%s)与配置用户(%s)不一致", data.get("username"), self.username)
            return None
        return data

    def save(self, password_hash: str) -> None:
        self._ensure_parent()
        payload = {"username": self.username, "password_hash": password_hash, "updated_at": _now_iso()}
        with self._lock:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def verify(self, password: str) -> bool:
        data = self.load()
        if not data:
            return False
        return verify_password(data["password_hash"], password)


def bootstrap_credentials(store: CredentialStore, env_password: str) -> str:
    """首次启动初始化凭据。返回：(密码哈希)。

    优先取 CORE_AUTH_PASSWORD；未提供则自动生成随机口令并打印一次（仅开发引导用，
    生产部署请通过 CORE_AUTH_PASSWORD 显式设置，见 deploy/README.md）。
    """
    if not store.exists():
        password = env_password or secrets.token_urlsafe(12)
        store.save(hash_password(password))
        if env_password:
            log.info("已依据 CORE_AUTH_PASSWORD 初始化凭据: %s", store.path)
        else:
            log.warning("未设置 CORE_AUTH_PASSWORD——自动生成初始口令。")
            log.warning(">>> 首次登录口令: %s（请登录后立即在 设置-账户与安全 中修改）", password)
    return password if not env_password else env_password


class LoginRateLimiter:
    """内存滑动窗口速率限制：按 ip:username 计数，超限锁定。"""

    def __init__(self, max_attempts: int = 5, window_s: int = 900, lock_s: int = 900):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self.lock_s = lock_s
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _key(self, ip: str, username: str) -> str:
        return f"{ip}:{username}"

    def check(self, ip: str, username: str) -> tuple[bool, float]:
        """返回 (是否放行, 剩余锁定秒数)。"""
        now = time.monotonic()
        key = self._key(ip, username)
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            if until > now:
                return False, int(until - now)
            self._prune(key, now)
            return len(self._fails.get(key, [])) < self.max_attempts, 0.0

    def record_failure(self, ip: str, username: str) -> bool:
        """记录一次失败；返回是否触发锁定。"""
        now = time.monotonic()
        key = self._key(ip, username)
        with self._lock:
            self._prune(key, now)
            fails = self._fails.setdefault(key, [])
            fails.append(now)
            if len(fails) >= self.max_attempts:
                self._locked_until[key] = now + self.lock_s
                self._fails.pop(key, None)
                return True
        return False

    def reset(self, ip: str, username: str) -> None:
        key = self._key(ip, username)
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)

    def _prune(self, key: str, now: float) -> None:
        fails = self._fails.get(key)
        if not fails:
            return
        cutoff = now - self.window_s
        self._fails[key] = [t for t in fails if t > cutoff]


class InstanceLock:
    """进程级单实例锁（#41）：lock 文件 + fcntl 排他锁。"""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fh = self.path.open("a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            log.error("core 已在运行（单实例锁被占用: %s）——拒绝启动（#41）", self.path)
            self.release()
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{time.time()}\n")
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
