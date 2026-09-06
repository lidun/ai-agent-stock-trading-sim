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
    (
        2,
        """
        -- 会话/消息存储（spec-02 §6.2）+ Agent 注册表（生命周期状态机，总纲 §3.5）
        CREATE TABLE IF NOT EXISTS agents (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            role        TEXT NOT NULL CHECK (role IN ('manager', 'strategy')),
            status      TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('trial', 'running', 'paused', 'halted', 'archived')),
            created_ts  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT PRIMARY KEY,
            agent_id    TEXT NOT NULL REFERENCES agents(id),
            conv_type   TEXT NOT NULL CHECK (conv_type IN ('user_chat', 'report_direct', 'sync')),
            created_ts  TEXT NOT NULL,
            UNIQUE (agent_id, conv_type)
        );

        -- messages.status 语义（spec-02 §6.2）：delivered/pending_review/failed 为送达态；
        -- queued/processing 为 P1 回执链展示扩展（spec-06 §6.1 已发送→排队→处理中→已回复），非终态。
        CREATE TABLE IF NOT EXISTS messages (
            id                TEXT PRIMARY KEY,
            conv_id           TEXT NOT NULL REFERENCES conversations(id),
            agent_id          TEXT NOT NULL,
            direction         TEXT NOT NULL CHECK (direction IN ('user', 'agent')),
            msg_type          TEXT NOT NULL,
            body              TEXT NOT NULL DEFAULT '',
            payload_ref       TEXT NOT NULL DEFAULT '',
            delivered_via     TEXT NOT NULL DEFAULT '',
            sync_to_manager   INTEGER NOT NULL DEFAULT 0,
            status            TEXT NOT NULL DEFAULT 'delivered'
                              CHECK (status IN ('queued', 'processing', 'delivered',
                                                'pending_review', 'failed')),
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            last_error        TEXT NOT NULL DEFAULT '',
            read_ts           TEXT NOT NULL DEFAULT '',
            ts                TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conv_ts ON messages(conv_id, ts);
        CREATE INDEX IF NOT EXISTS idx_messages_conv_unread
            ON messages(conv_id, status, read_ts);

        -- 种子：管理 Agent + P1 单子 Agent（演示）。生命周期数据行由注册流程管理，这里仅确保存在。
        INSERT OR IGNORE INTO agents (id, name, role, status, created_ts)
            VALUES ('agent-manager', '管理 Agent', 'manager', 'running',
                    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
        INSERT OR IGNORE INTO agents (id, name, role, status, created_ts)
            VALUES ('agent-demo-001', '低波红利 · 演示子 Agent', 'strategy', 'running',
                    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
        """,
    ),
    (
        3,
        """
        -- 模拟账户（spec-01 §2.1 accounts，1:1 绑定子 Agent）
        -- 金额字段以 REAL 存储便于 SQL 聚合；接口层负责 Decimal 精度格式化。
        -- status 与 agents.status 同源映射（总纲 §3.5）：trial/normal/paused_buy/halted/archived
        CREATE TABLE IF NOT EXISTS accounts (
            id                  TEXT PRIMARY KEY REFERENCES agents(id),
            initial_capital     REAL NOT NULL DEFAULT 100000.0,
            cash                REAL NOT NULL DEFAULT 100000.0,
            nav                 REAL NOT NULL DEFAULT 1.0,
            shares              REAL NOT NULL DEFAULT 100000.0,
            total_pnl           REAL NOT NULL DEFAULT 0.0,
            today_pnl           REAL NOT NULL DEFAULT 0.0,
            granularity         TEXT NOT NULL DEFAULT 'eod_replay'
                                CHECK (granularity IN ('eod_replay', 'intraday_5m', 'intraday_1m')),
            granularity_history TEXT NOT NULL DEFAULT '[]',
            settle_key          TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'normal'
                                CHECK (status IN ('trial', 'normal', 'paused_buy', 'halted', 'archived')),
            active_version_no   TEXT NOT NULL DEFAULT '',
            created_ts          TEXT NOT NULL,
            updated_ts          TEXT NOT NULL
        );

        -- 为既有 strategy Agent 种子账户：初始 10 万现金、份额法口径（spec-01 §6.1：shares=注资额，nav=1）
        INSERT OR IGNORE INTO accounts
            (id, initial_capital, cash, nav, shares, total_pnl, today_pnl,
             granularity, granularity_history, settle_key, status, active_version_no,
             created_ts, updated_ts)
        SELECT id, 100000.0, 100000.0, 1.0, 100000.0, 0.0, 0.0,
               'eod_replay', '[]', '',
               CASE status
                   WHEN 'trial' THEN 'trial'
                   WHEN 'paused' THEN 'paused_buy'
                   WHEN 'halted' THEN 'halted'
                   WHEN 'archived' THEN 'archived'
                   ELSE 'normal' END,
               '', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
               strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
          FROM agents WHERE role = 'strategy';
        """,
    ),
    (
        4,
        """
        -- 撮合引擎存储层（spec-01 §2.2 holdings/lots、§2.3 condition_orders）
        -- 只建结构供引擎写入；本切片不含撮合逻辑，未提供写端点。

        CREATE TABLE IF NOT EXISTS holdings (
            id          TEXT PRIMARY KEY,
            account_id  TEXT NOT NULL REFERENCES accounts(id),
            symbol      TEXT NOT NULL,
            quantity    REAL NOT NULL DEFAULT 0,
            avg_cost    REAL NOT NULL DEFAULT 0,   -- 派生只读：真实价含费摊薄成本
            updated_ts  TEXT NOT NULL,
            UNIQUE (account_id, symbol)
        );

        -- lots = T+1 可卖唯一事实源（spec-01 §2.2：卖出可卖数= Σ lots(buy_date<今日).remaining）
        CREATE TABLE IF NOT EXISTS lots (
            id                   TEXT PRIMARY KEY,
            account_id           TEXT NOT NULL REFERENCES accounts(id),
            holding_id           TEXT NOT NULL REFERENCES holdings(id),
            buy_trade_id         TEXT NOT NULL DEFAULT '',
            buy_date             TEXT NOT NULL,
            buy_price            REAL NOT NULL,
            quantity             REAL NOT NULL,
            remaining            REAL NOT NULL,
            strategy_version_no  TEXT NOT NULL DEFAULT '',
            corp_action_flags    TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_lots_account_buydate ON lots(account_id, buy_date);

        -- condition_orders：交易意图唯一通道（spec-01 §2.3）
        CREATE TABLE IF NOT EXISTS condition_orders (
            id                   TEXT PRIMARY KEY,
            account_id           TEXT NOT NULL REFERENCES accounts(id),
            order_type           TEXT NOT NULL
                                 CHECK (order_type IN ('buy', 'sell_take_profit', 'sell_stop',
                                                       'sell_trail', 'sell_open_board',
                                                       'buy_seal_confirm', 'basket', 'time', 'combo')),
            direction            TEXT NOT NULL CHECK (direction IN ('buy', 'sell')),
            scope                TEXT NOT NULL DEFAULT 'single'
                                 CHECK (scope IN ('single', 'basket', 'holding')),
            symbol               TEXT NOT NULL DEFAULT '',
            symbols              TEXT NOT NULL DEFAULT '[]',
            trigger              TEXT NOT NULL DEFAULT '',
            basis                TEXT NOT NULL DEFAULT 'replay_l0'
                                 CHECK (basis IN ('replay_l0', 'approx_l2', 'intraday')),
            price_ref            TEXT NOT NULL DEFAULT 'absolute'
                                 CHECK (price_ref IN ('absolute', 'pct_change', 'vs_cost')),
            qty                  REAL,
            amount               REAL,
            budget               REAL,
            price_type           TEXT NOT NULL DEFAULT 'market'
                                 CHECK (price_type IN ('market', 'limit')),
            limit_price          REAL,
            validity             TEXT NOT NULL DEFAULT 'today'
                                 CHECK (validity IN ('today', 'until', 'long')),
            valid_until          TEXT NOT NULL DEFAULT '',
            priority             INTEGER NOT NULL DEFAULT 0,
            status               TEXT NOT NULL DEFAULT 'active'
                                 CHECK (status IN ('active', 'partial', 'filled', 'cancelled',
                                                   'expired', 'invalid')),
            insufficient_events  INTEGER NOT NULL DEFAULT 0,
            invalid_reason       TEXT NOT NULL DEFAULT '',
            strategy_version_no  TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL,
            creator              TEXT NOT NULL,
            reason               TEXT NOT NULL DEFAULT '',
            settled_on           TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_co_account_status ON condition_orders(account_id, status);
        CREATE INDEX IF NOT EXISTS idx_co_account_created ON condition_orders(account_id, created_at);
        """,
    ),
    (
        5,
        """
        -- 撮合成交与日终结算（spec-01 §2.5/§3.8）：EOD 结算引擎产出，单事务写入。

        CREATE TABLE IF NOT EXISTS trades (
            id                  TEXT PRIMARY KEY,
            account_id          TEXT NOT NULL REFERENCES accounts(id),
            order_id            TEXT NOT NULL REFERENCES condition_orders(id),
            symbol              TEXT NOT NULL,
            side                TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            qty                 REAL NOT NULL,
            price               REAL NOT NULL,        -- 真实成交价
            amount              REAL NOT NULL,        -- 成交额 = qty × price
            fee_total           REAL NOT NULL DEFAULT 0,
            commission          REAL NOT NULL DEFAULT 0,
            stamp_tax           REAL NOT NULL DEFAULT 0,
            transfer_fee        REAL NOT NULL DEFAULT 0,
            trade_time          TEXT NOT NULL,        -- 采样点时刻（本地墙钟，见引擎契约）
            basis_requested     TEXT NOT NULL,
            basis_used          TEXT NOT NULL,
            quality             TEXT NOT NULL DEFAULT '',
            settle_date         TEXT NOT NULL,
            reason              TEXT NOT NULL DEFAULT '',
            strategy_version_no TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_trades_account_settle ON trades(account_id, settle_date);
        CREATE INDEX IF NOT EXISTS idx_trades_order ON trades(order_id);

        -- 结算幂等键：settle_key UNIQUE（trade_date:account_id）→ 崩溃重跑不重复（spec-01 §3.1.5/§3.8）
        CREATE TABLE IF NOT EXISTS settlement_log (
            id               TEXT PRIMARY KEY,
            settle_key       TEXT NOT NULL UNIQUE,
            trade_date       TEXT NOT NULL,
            account_id       TEXT NOT NULL REFERENCES accounts(id),
            granularity_used TEXT NOT NULL,            -- JSON {symbol: 档位}
            status           TEXT NOT NULL DEFAULT 'done'
                             CHECK (status IN ('done', 'pending')),
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_settlelog_account ON settlement_log(account_id, trade_date);
        """,
    ),
    (
        6,
        """
        -- #63 独立试运行账户：accounts 与 agents 由 1:1（id 同键）放开为 1:N
        --（account.agent_id → agent；role main/trial/validation 区分用途，parent_agent_id 指父 Agent）。
        -- 既有主账户迁移：role=main、parent=自身；child 表按 accounts(id) 的 FK 语义不变。
        -- @disable_fk
        CREATE TABLE IF NOT EXISTS accounts_v6 (
            id                  TEXT PRIMARY KEY,
            agent_id            TEXT NOT NULL REFERENCES agents(id),
            role                TEXT NOT NULL DEFAULT 'main'
                                CHECK (role IN ('main', 'trial', 'validation')),
            parent_agent_id     TEXT NOT NULL DEFAULT '',
            initial_capital     REAL NOT NULL DEFAULT 100000.0,
            cash                REAL NOT NULL DEFAULT 100000.0,
            nav                 REAL NOT NULL DEFAULT 1.0,
            shares              REAL NOT NULL DEFAULT 100000.0,
            total_pnl           REAL NOT NULL DEFAULT 0.0,
            today_pnl           REAL NOT NULL DEFAULT 0.0,
            granularity         TEXT NOT NULL DEFAULT 'eod_replay'
                                CHECK (granularity IN ('eod_replay', 'intraday_5m', 'intraday_1m')),
            granularity_history TEXT NOT NULL DEFAULT '[]',
            settle_key          TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'normal'
                                CHECK (status IN ('trial', 'normal', 'paused_buy', 'halted', 'archived')),
            active_version_no   TEXT NOT NULL DEFAULT '',
            created_ts          TEXT NOT NULL,
            updated_ts          TEXT NOT NULL
        );

        INSERT INTO accounts_v6
            (id, agent_id, role, parent_agent_id, initial_capital, cash, nav, shares,
             total_pnl, today_pnl, granularity, granularity_history, settle_key, status,
             active_version_no, created_ts, updated_ts)
        SELECT id, id, 'main', id, initial_capital, cash, nav, shares,
               total_pnl, today_pnl, granularity, granularity_history, settle_key, status,
               active_version_no, created_ts, updated_ts
          FROM accounts;

        DROP TABLE accounts;
        ALTER TABLE accounts_v6 RENAME TO accounts;
        """,
    ),
    (
        7,
        """
        -- #spec-01 §3.6 新股/ST 买入拦截（引擎切片）：账户级豁免（JSON token 数组，需审批）
        -- token ∈ {"st","ipo"}；默认 [] = 全拦。引擎 settle 时按 restrict_map × 本列判定。
        ALTER TABLE accounts ADD COLUMN buy_exempt TEXT NOT NULL DEFAULT '[]';
        """,
    ),
    (
        8,
        """
        -- spec-01 §8.1 卖出跟踪（引擎切片）：卖出成交时引擎自动登记 exit_trackings，
        -- N（默认 10）个交易日后确定性结清（确定性、零 token，阈值引擎常量可调）。
        -- 列口径：sell_price/qty 为成交实值；fwd/bench/excess 为百分数；period_high/low
        -- 为跟踪窗口（卖出日次日起）逐结算日累计极值；bench_sell_close 为卖出日基准收盘。
        CREATE TABLE IF NOT EXISTS exit_trackings (
            id               TEXT PRIMARY KEY,
            account_id       TEXT NOT NULL REFERENCES accounts(id),
            sell_trade_id    TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            sell_date        TEXT NOT NULL,
            sell_price       REAL NOT NULL,
            qty              REAL NOT NULL,
            sell_reason      TEXT NOT NULL DEFAULT '主动',
            status           TEXT NOT NULL DEFAULT 'tracking'
                             CHECK (status IN ('tracking', 'done')),
            track_end_date   TEXT NOT NULL DEFAULT '',
            fwd_return_pct   REAL NOT NULL DEFAULT 0,
            bench_return_pct REAL NOT NULL DEFAULT 0,
            excess_pct       REAL NOT NULL DEFAULT 0,
            period_high      REAL NOT NULL,
            period_low       REAL NOT NULL,
            conclusion       TEXT NOT NULL DEFAULT ''
                             CHECK (conclusion IN ('', '卖对', '卖平', '卖早')),
            is_loss_case     INTEGER NOT NULL DEFAULT 0,
            bench_sell_close REAL NOT NULL DEFAULT 0,
            last_close       REAL NOT NULL DEFAULT 0,
            last_bench       REAL NOT NULL DEFAULT 0,
            quality          TEXT NOT NULL DEFAULT '',
            created_ts       TEXT NOT NULL,
            done_ts          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_exit_account_status
            ON exit_trackings(account_id, status, sell_date);
        """,
    ),
    (
        9,
        """
        -- 卖出跟踪按“行内会话日”推进（spec-01 §8.1 接线切片）：纯跟踪日可能没有账户结算
        -- 活动，settlement_log 日序无法代表跟踪日进度 → 改由 settle_exits 每推进一个
        -- 不同交易日自增 sessions_done（last_seen 防同日重复推进/重试幂等），不再依赖
        -- settlement_log 计数。
        ALTER TABLE exit_trackings ADD COLUMN sessions_done INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE exit_trackings ADD COLUMN last_seen TEXT NOT NULL DEFAULT '';
        """,
    ),
    (
        10,
        """
        -- spec-01 §7 D5 熔断日买入冻结：熔断触发的账户当日买入类单不成交、不推进（保持
        -- active），每冻结交易日标一次 circuit_break 事件（audit 留痕 + 本列计数供日报
        -- 数据段，语义同 insufficient_events）；卖出类单照常。解冻后挂单恢复参与。
        ALTER TABLE condition_orders ADD COLUMN circuit_break_events INTEGER NOT NULL DEFAULT 0;
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
            # @disable_fk 标记：需重建被引用的父表（drop+rename），必须临时关闭外键
            fk_off = "@disable_fk" in sql
            if fk_off:
                conn.execute("PRAGMA foreign_keys=OFF")
            try:
                conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
            finally:
                if fk_off:
                    conn.execute("PRAGMA foreign_keys=ON")
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
