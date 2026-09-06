"""SQLite 访问层：WAL + busy_timeout + 单写队列 + schema migration。

总纲 §12.5：#44 口径——业务数据 SQLite 单文件、写操作收敛到单写队列；
spec-02 §10 备份一致性（在线备份 API + 安全点）后续在备份模块实现。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from core import __version__

log = logging.getLogger(__name__)

_write_lock = threading.Lock()


class Connections:
    """每线程独立 SQLite 连接（sqlite3 连接不可跨线程使用）。

    写操作仍经全局 write_txn 锁串行化；读各自线程连接。
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.db_path)
            self._local.conn = c
        return c


def state_conn(state) -> sqlite3.Connection:
    """从 app.state 取当前线程连接。"""
    return state.db.conn()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """单写队列：所有写事务经全局锁串行化，避免多线程写竞争。"""
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def read_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("ROLLBACK")


_SCHEMA_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash    TEXT PRIMARY KEY,
            username      TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL,
            ip            TEXT NOT NULL DEFAULT '',
            user_agent    TEXT NOT NULL DEFAULT '',
            revoked       INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(username);

        CREATE TABLE IF NOT EXISTS audit_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            actor       TEXT NOT NULL,
            action      TEXT NOT NULL,
            object_type TEXT NOT NULL DEFAULT '',
            object_id   TEXT NOT NULL DEFAULT '',
            result      TEXT NOT NULL DEFAULT 'ok',
            detail      TEXT NOT NULL DEFAULT '',
            ip          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_audit_logs_ts ON audit_logs(ts);
        """,
    ),
]


def migrate(db_path: Path) -> None:
    """按序应用 schema migration；每次升级前对既有库做文件级快照（spec-02 §10）。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        current = row["v"] if row and row["v"] is not None else 0
        for version, sql in _SCHEMA_MIGRATIONS:
            if version <= current:
                continue
            if current > 0:
                _snapshot_before_upgrade(db_path, current)
            # 事务内执行 DDL（sqlite3 executescript 会隐式提交，故显式包 BEGIN/COMMIT）
            conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
            with write_txn(conn) as c:
                c.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, _now_iso()),
                )
            log.info("schema migration -> v%s", version)
        with write_txn(conn) as c:
            c.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('core_version', ?)",
                (__version__,),
            )
    finally:
        conn.close()


def _snapshot_before_upgrade(db_path: Path, current: int) -> None:
    backup = db_path.with_suffix(f".pre-v{current}.db")
    if backup.exists():
        return
    try:
        src = connect(db_path)
        try:
            dest = sqlite3.connect(str(backup))
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
    except sqlite3.Error as e:  # pragma: no cover - 备份失败不应阻断启动
        log.warning("升级前快照失败（继续启动）: %s", e)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
