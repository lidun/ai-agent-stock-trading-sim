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
    db_path = Path(db_path)
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
    (
        11,
        """
        -- spec-01 §7 硬红线单票上限：默认 1.00（v0.6 满仓策略 #62，参数保留仅作事故性
        -- 防护，账户可配）——买入撮合前校验“成交后单票市值 ≤ 上限 × 账户权益”，越界即
        -- 拒绝该采样点（记 insufficient 事件保持 active，价格回落可复判），满仓单票在
        -- 上限 1.00 下恒可通过（单票市值 ≤ 权益恒真），默认值不误伤全现金买入。
        ALTER TABLE accounts ADD COLUMN single_stock_cap REAL NOT NULL DEFAULT 1.0;
        """,
    ),
    (
        12,
        """
        -- spec-05 §6.2/#63 试运行验收归档留证：launch/reject 决策后 trial 账户整体归档为
        -- 验收证据（快照 JSON 不可变留底：账户终态 + 结算/订单/成交/持仓量），账户转
        -- archived；主账户零污染不动。字段以 REAL/TEXT 存，snapshot 一次写入不再修改。
        CREATE TABLE IF NOT EXISTS trial_archives (
            id            TEXT PRIMARY KEY,
            agent_id      TEXT NOT NULL REFERENCES agents(id),
            account_id    TEXT NOT NULL,
            decision      TEXT NOT NULL CHECK (decision IN ('launch', 'reject')),
            verdict       TEXT NOT NULL DEFAULT '',
            snapshot      TEXT NOT NULL,
            archived_ts   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trial_archives_agent
            ON trial_archives(agent_id, archived_ts);
        """,
    ),
    (
        13,
        """
        -- spec-05 §6.2 试运行回放窗口台账（#63）：子 Agent 创建进入试运行时生成
        -- trial_replays（窗口 5-20 默认 5，与 #18 N≥5 硬门槛对齐）。trial 账户历史回放
        -- 会话逐日记 replay_sessions（agent+trade_date 唯一 → 幂等，空日也计数），满
        -- 窗口即 done 停止自动回放，等 finish_trial（launch/reject → 归档留证）。
        -- 试运行期（agent.status='trial'）主账户由 settle_day 结算门控冻结（零污染），
        -- trial 账户仅经 EodSettleTrigger.run_trial_backfill 专责回放，不走通用 catchup。
        CREATE TABLE IF NOT EXISTS trial_replays (
            agent_id          TEXT PRIMARY KEY REFERENCES agents(id),
            trial_account_id  TEXT NOT NULL REFERENCES accounts(id),
            window_days       INTEGER NOT NULL
                              CHECK (window_days BETWEEN 5 AND 20),
            status            TEXT NOT NULL DEFAULT 'in_progress'
                              CHECK (status IN ('in_progress', 'done')),
            created_ts        TEXT NOT NULL,
            updated_ts        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS replay_sessions (
            agent_id    TEXT NOT NULL REFERENCES agents(id),
            account_id  TEXT NOT NULL REFERENCES accounts(id),
            trade_date  TEXT NOT NULL,
            created_ts  TEXT NOT NULL,
            UNIQUE (agent_id, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_replay_sessions_agent
            ON replay_sessions(agent_id, trade_date);
        """,
    ),
    (
        14,
        """
        -- 结算持仓估值快照（spec-04 §5.2 日报引擎数据段数据源，spec-01 结算产物归档）：
        -- 闭市后最终持仓按当日收盘/停牌估值价归档为 JSON，供日报/UI 还原当日市值，
        -- 无需再拉行情、不虚构价格。既有行按 NULL 处理（旧库无快照 → 数据段标欠档）。
        ALTER TABLE settlement_log ADD COLUMN positions_snapshot TEXT;
        """,
    ),
    (
        15,
        """
        -- 日报表（spec-04 §5.2/§5.3：daily_reports schema 归本规格）：引擎结算产物直读
        -- 生成 data_section（零 token）+ merged_markdown（确定性渲染）落库，narrative 为
        -- LLM 叙述段（本切片留空，由日报任务后续写入）；version 递增留痕——补发/修订新增
        -- 版本行不覆盖已推送版（v0.3 A5/A6 UNIQUE(agent_id,trade_date,version)）。
        -- 本库以账户为日报数据域，agent_id 存账户口径标识（单 Agent 演示 1:1）；status
        -- normal|absent|resend 对齐 §5.3（缺勤日报同样补数据段，叙述=原因说明）。
        CREATE TABLE IF NOT EXISTS daily_reports (
            id              TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            version         INTEGER NOT NULL,
            data_section    TEXT NOT NULL,
            narrative       TEXT NOT NULL DEFAULT '',
            merged_markdown TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'normal'
                            CHECK (status IN ('normal', 'absent', 'resend')),
            llm_perf_id     TEXT,
            created_ts      TEXT NOT NULL,
            UNIQUE (agent_id, trade_date, version)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_reports_agent_date
            ON daily_reports(agent_id, trade_date);
        """,
    ),
    (
        16,
        """
        -- 日报直达推送开关（spec-04 §6.2 notify_rules P1 最小化：站内单通道，仅一布尔列）。
        -- 默认开启；试运行/退役 Agent 的全程不推为硬约束（见 reporting 过滤，不受本列影响），
        -- 本列供 main 策略账户关闭某 Agent 的日报直达推送（日报中心/版本留痕不受影响）。
        ALTER TABLE agents ADD COLUMN notify_daily INTEGER NOT NULL DEFAULT 1;
        """,
    ),
    (
        17,
        """
        -- 审批单（spec-04 §4.1 approval_requests，独立成表 + 确定性短路 §4.2）。
        -- type 覆盖五类 + launch（v0.4）；本阶段实现 risk/exemption 豁免类效果器（§4.1），
        -- 其余 type 先走通用待办/过期/审计流程（效果器按 type 注册，未注册 type 通过后仅留痕）。
        CREATE TABLE IF NOT EXISTS approval_requests (
            id             TEXT PRIMARY KEY,
            type           TEXT NOT NULL
                           CHECK (type IN ('task', 'granularity', 'capability',
                                           'risk', 'exemption', 'launch')),
            agent_id       TEXT NOT NULL REFERENCES agents(id),
            payload        TEXT NOT NULL DEFAULT '{}',
            content_hash   TEXT NOT NULL DEFAULT '',
            status         TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'approved', 'rejected',
                                             'expired', 'withdrawn')),
            decided_by     TEXT NOT NULL DEFAULT '',
            decided_ts     TEXT NOT NULL DEFAULT '',
            reason         TEXT NOT NULL DEFAULT '',
            expires_ts     TEXT NOT NULL,
            close_note     TEXT NOT NULL DEFAULT '',
            result_ref     TEXT NOT NULL DEFAULT '',
            created_ts     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_approval_status_expires
            ON approval_requests(status, expires_ts);
        CREATE INDEX IF NOT EXISTS idx_approval_hash
            ON approval_requests(content_hash);
        CREATE INDEX IF NOT EXISTS idx_approval_agent_type
            ON approval_requests(agent_id, type, status);
        """,
    ),
    (
        18,
        """
        -- 冻结证券（spec-01 直控接口 / spec-06 §6.3）：逐票冻结买入，保留卖出与风控。
        -- 冻结即时生效：冻结时该 Agent 主账户该票 active 买入单同事务取消；解除后恢复可买。
        CREATE TABLE IF NOT EXISTS frozen_securities (
            id         TEXT PRIMARY KEY,
            agent_id   TEXT NOT NULL REFERENCES agents(id),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            symbol     TEXT NOT NULL,
            reason     TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT 'user',
            created_ts TEXT NOT NULL,
            UNIQUE (agent_id, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_frozen_agent_symbol
            ON frozen_securities(agent_id, symbol);
        """,
    ),
    (
        19,
        """
        -- 知识与经验库（spec-05 §3 kb_entries / kb_stats）。
        -- 状态机（§3.2）：observing → validating → valid；validating → invalid/sealed；
        --   invalid/sealed 经复核可回 validating。晋升只由 signal_registry 客观统计驱动，
        --   管理 Agent/用户负责评审确认（评审记录 review_gate2_ref）。删除=软删（§3.7），
        --   历史统计与信号引用保留供审计。
        CREATE TABLE IF NOT EXISTS kb_entries (
            id              TEXT PRIMARY KEY,          -- KB-0001 递增
            type            TEXT NOT NULL
                            CHECK (type IN ('positive', 'pitfall')),
            name            TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            computable_spec TEXT NOT NULL DEFAULT '{}', -- JSON: trigger_rule/computation/data_sources
            severity        TEXT NOT NULL DEFAULT ''     -- pitfall: high|mid|low
                            CHECK (severity IN ('', 'high', 'mid', 'low')),
            env_scope       TEXT NOT NULL DEFAULT 'all',
            status          TEXT NOT NULL DEFAULT 'observing'
                            CHECK (status IN ('observing', 'validating', 'valid',
                                              'invalid', 'sealed')),
            invalid_reason  TEXT NOT NULL DEFAULT '',
            sealed_reason   TEXT NOT NULL DEFAULT '',
            source          TEXT NOT NULL DEFAULT 'user'
                            CHECK (source IN ('user', 'retrospective',
                                              'manager_observation', 'market_anomaly')),
            created_by      TEXT NOT NULL DEFAULT 'user',
            origin_agent    TEXT REFERENCES agents(id),
            review_gate1_ref TEXT NOT NULL DEFAULT '{}',
            review_gate2_ref TEXT NOT NULL DEFAULT '{}',
            deleted_ts      TEXT NOT NULL DEFAULT '',
            created_ts      TEXT NOT NULL,
            updated_ts      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kb_status ON kb_entries(status);
        CREATE INDEX IF NOT EXISTS idx_kb_type_status ON kb_entries(type, status);
        CREATE INDEX IF NOT EXISTS idx_kb_source ON kb_entries(source);

        CREATE TABLE IF NOT EXISTS kb_stats (
            id            TEXT PRIMARY KEY,
            kb_id         TEXT NOT NULL REFERENCES kb_entries(id),
            env_bucket    TEXT NOT NULL DEFAULT 'all',
            sample_n      INTEGER NOT NULL DEFAULT 0,
            win_rate      REAL,
            avg_win       REAL,
            avg_loss      REAL,
            expectancy    REAL,
            intercept_n   INTEGER NOT NULL DEFAULT 0,   -- #28 被拦截样本（真避坑）
            exception_n   INTEGER NOT NULL DEFAULT 0,   -- #28 破例样本
            stale_n       INTEGER NOT NULL DEFAULT 0,   -- #34 stale-price 样本
            dispatch_n    INTEGER NOT NULL DEFAULT 0,   -- §3.9 UCB n_i
            window_days   INTEGER,
            note          TEXT NOT NULL DEFAULT '',
            updated_ts    TEXT NOT NULL,
            UNIQUE (kb_id, env_bucket)
        );
        CREATE INDEX IF NOT EXISTS idx_kb_stats_kb ON kb_stats(kb_id);
        """,
    ),
    (
        20,
        """
        -- 策略章程版本链（spec-05 §4.1 语义受控：#21 双层结构；spec-02 §9 版本不变量）
        -- charter 语义：核心理念不落 config、按版本快照存储；locked=1 表示核心理念已锁定，
        -- 变更需用户授权（写入方=策略发布/管理 Agent；Web 详情只读，写入口后置）。
        -- checkpoint 提交校验 charter_hash 未变（spec-05 §4.1 语义受控执行机制）。
        CREATE TABLE IF NOT EXISTS strategy_charter_versions (
            id           TEXT PRIMARY KEY,
            agent_id     TEXT NOT NULL REFERENCES agents(id),
            version_no   TEXT NOT NULL,
            active       INTEGER NOT NULL DEFAULT 0,
            core_belief  TEXT NOT NULL DEFAULT '',
            layers       TEXT NOT NULL DEFAULT '{}',
            locked       INTEGER NOT NULL DEFAULT 1,
            charter_hash TEXT NOT NULL DEFAULT '',
            note         TEXT NOT NULL DEFAULT '',
            created_ts   TEXT NOT NULL,
            UNIQUE (agent_id, version_no)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_charter_active
            ON strategy_charter_versions(agent_id) WHERE active = 1;
        CREATE INDEX IF NOT EXISTS idx_charter_agent_ts
            ON strategy_charter_versions(agent_id, created_ts);
        """,
    ),
    (
        21,
        """
        -- spec-02 §3.1 memory_entries（原文，append-only）——本片落地 type=strategy
        -- 演进记忆子集（spec-05 §4.1/§4.2：每次优化记录 前/后/依据/预期/结果 全量入记忆，
        -- 逐条挂 version_no，spec-02 §9 不变量）。
        -- 幂等：UNIQUE(agent_id, dedup_key)；写入方=引擎 EVOQUANT 优化流（spec-05 §4.1，
        -- 本片只读+内部受控写测试）。卡片/分层摘要/向量属 spec-02 §3.2/§4 后续切片，不在此建表。
        CREATE TABLE IF NOT EXISTS memory_entries (
            id          TEXT PRIMARY KEY,
            agent_id    TEXT NOT NULL REFERENCES agents(id),
            mem_type    TEXT NOT NULL DEFAULT 'strategy'
                        CHECK (mem_type IN ('user_requirement', 'strategy',
                                            'trade_decision', 'market_note')),
            version_no  TEXT NOT NULL DEFAULT '',
            ts          TEXT NOT NULL,
            body        TEXT NOT NULL,
            ref_ids     TEXT NOT NULL DEFAULT '[]',
            revision    INTEGER NOT NULL DEFAULT 0,
            source      TEXT NOT NULL DEFAULT '',
            dedup_key   TEXT NOT NULL DEFAULT '',
            quality     TEXT NOT NULL DEFAULT 'normal'
                        CHECK (quality IN ('normal', 'flagged')),
            UNIQUE (agent_id, dedup_key)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_agent_type_ts
            ON memory_entries(agent_id, mem_type, ts);
        """,
    ),
    (
        22,
        """
        -- spec-05 §2 能力配置中心统一域（能力模型 + 绑定清单）。
        -- 只读域切片：注册/绑定/解绑的写入口语义属管理 Agent + spec-04 审批流
        -- （spec-05 §2.3 申请-下发闭环），本片仅建 schema + 受控 seed（测试用），
        -- 公开写路由不开放。绑定=能力行(capability_id,version)×agent 一条，
        -- unbound_ts 留痕解绑/回滚（spec-05 §2.4）；同 (capability_id,version,agent)
        -- 只允许一条在绑。
        CREATE TABLE IF NOT EXISTS capabilities (
            id                 TEXT PRIMARY KEY,
            name               TEXT NOT NULL,
            capability_type    TEXT NOT NULL
                               CHECK (capability_type IN ('skill', 'tool', 'mcp', 'datasource')),
            version            TEXT NOT NULL,
            description        TEXT NOT NULL DEFAULT '',
            maintainer         TEXT NOT NULL DEFAULT '',
            source_type        TEXT NOT NULL
                               CHECK (source_type IN ('opensource', 'api', 'selfmade')),
            source_ref         TEXT NOT NULL DEFAULT '',
            sandbox_status     TEXT NOT NULL DEFAULT 'pending'
                               CHECK (sandbox_status IN ('pending', 'passed', 'failed')),
            sandbox_report_ref TEXT NOT NULL DEFAULT '',
            metadata           TEXT NOT NULL DEFAULT '{}',
            status             TEXT NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'deprecated')),
            created_ts         TEXT NOT NULL,
            updated_ts         TEXT NOT NULL,
            UNIQUE (name, version)
        );
        CREATE INDEX IF NOT EXISTS idx_cap_type_status ON capabilities(capability_type, status);

        CREATE TABLE IF NOT EXISTS capability_bindings (
            id            TEXT PRIMARY KEY,
            capability_id TEXT NOT NULL REFERENCES capabilities(id),
            version       TEXT NOT NULL,
            agent_id      TEXT NOT NULL REFERENCES agents(id),
            bound_by      TEXT NOT NULL DEFAULT '',
            bound_ts      TEXT NOT NULL,
            unbound_ts    TEXT NOT NULL DEFAULT '',
            created_ts    TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_cap_binding_active
            ON capability_bindings(capability_id, version, agent_id) WHERE unbound_ts = '';
        CREATE INDEX IF NOT EXISTS idx_cap_binding_agent
            ON capability_bindings(agent_id, unbound_ts);
        """,
    ),
    (
        23,
        """
        -- spec-02 §9 策略版本化存储（引擎 config 全量快照 + 演进状态机）。
        -- EVOQUANT 消费方：checkpoint 建 draft → 验证通过 activate 晋升（validated_on），
        -- 旧 active 降为 validated 候选；失败 rollback 标 rolled_back 并回最近 validated。
        CREATE TABLE IF NOT EXISTS strategy_versions (
            id             TEXT PRIMARY KEY,
            agent_id       TEXT NOT NULL,
            version_no     TEXT NOT NULL,
            parent_version TEXT NOT NULL DEFAULT '',
            status         TEXT NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft','validated','active','rolled_back')),
            config         TEXT NOT NULL,
            config_diff    TEXT NOT NULL DEFAULT '{}',
            basis          TEXT NOT NULL DEFAULT '[]',
            created_by     TEXT NOT NULL DEFAULT 'strategy_agent',
            created_ts     TEXT NOT NULL,
            trial_window   TEXT NOT NULL DEFAULT '{}',
            validated_on   TEXT NOT NULL DEFAULT '',
            failure_reason TEXT NOT NULL DEFAULT '',
            rolled_back_to TEXT NOT NULL DEFAULT '',
            UNIQUE (agent_id, version_no)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_versions_active
            ON strategy_versions(agent_id) WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS idx_strategy_versions_status
            ON strategy_versions(agent_id, status, created_ts);
        """,
    ),
    (
        24,
        """
        -- spec-05 §4.2/#63 独立验证账户运行台账（写方=EVOQUANT 调度环）。
        -- checkpoint 建验证版本时随建 role=validation 独立账户 + 本窗口行；窗口结束
        -- 判定（10 交易日或 ≥trade_target 笔成交，先到为准，§4.2 参数可调）由调度器按
        -- 真实结算日记入 sessions_done/trade_samples，满窗由 evoquant.maybe_adjudicate
        -- 依据窗口内真实引擎记录（realized_pnl/规则级违规/熔断）出结论：
        --   activate=通过（零违规 ∧ 期望 ≥ 现役同期 或 >0）→ spec-02 activate + 归档；
        --   rollback=失败（违规 ∨ 期望 < -2%）→ spec-02 rollback + 归档；
        --   sealed=窗口无成交样本，证据不足不出结论（防假激活）。
        -- final_snapshot 为判定时账户/版本真实终态 JSON 留证（同 trial_archives 语义）。
        CREATE TABLE IF NOT EXISTS strategy_validation_windows (
            id                      TEXT PRIMARY KEY,
            agent_id                TEXT NOT NULL REFERENCES agents(id),
            version_no              TEXT NOT NULL,
            status                  TEXT NOT NULL DEFAULT 'in_progress'
                                    CHECK (status IN ('in_progress', 'done', 'void')),
            validation_account_id   TEXT NOT NULL REFERENCES accounts(id),
            main_account_id         TEXT NOT NULL REFERENCES accounts(id),
            window_days             INTEGER NOT NULL DEFAULT 10
                                    CHECK (window_days BETWEEN 1 AND 60),
            trade_target            INTEGER NOT NULL DEFAULT 20
                                    CHECK (trade_target BETWEEN 1 AND 500),
            sessions_done           INTEGER NOT NULL DEFAULT 0,
            trade_samples           INTEGER NOT NULL DEFAULT 0,
            rule_violations         INTEGER NOT NULL DEFAULT 0,
            fuse_events             INTEGER NOT NULL DEFAULT 0,
            expectation             REAL,
            baseline_expectation    REAL,
            decision                TEXT NOT NULL DEFAULT ''
                                    CHECK (decision IN ('', 'activate', 'rollback', 'sealed')),
            decision_reason         TEXT NOT NULL DEFAULT '',
            window_start_trade_date TEXT NOT NULL DEFAULT '',
            final_snapshot          TEXT NOT NULL DEFAULT '{}',
            decided_ts              TEXT NOT NULL DEFAULT '',
            created_ts              TEXT NOT NULL,
            updated_ts              TEXT NOT NULL,
            UNIQUE (agent_id, version_no)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_validation_window_active
            ON strategy_validation_windows(agent_id) WHERE status = 'in_progress';
        CREATE INDEX IF NOT EXISTS idx_validation_window_status
            ON strategy_validation_windows(status, updated_ts);

        -- spec-05 §4.2 期望值（含费）取数：卖出成交记录每笔已实现净盈亏
        -- realized_pnl = 卖出净额(amount-fee) − 摊薄含费成本(avg_cost×qty)，
        -- 由引擎在真实卖出成交时写入（spec-01 记账产物），供版本窗口统计与
        -- 现役同期比较（真实事件，不虚构）。买入行为 0。
        ALTER TABLE trades ADD COLUMN realized_pnl REAL NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_trades_version_settle
            ON trades(strategy_version_no, account_id, settle_date);

        -- 验证窗会话台账：调度器在每个真实推进交易日（引擎确认过的结算日）登记一行，
        -- 空成交日也计数（同 trial replay_sessions 语义，spec-05 §6.2 对齐），
        -- (window_id, trade_date) 唯一 → 幂等重放不重复。窗口起点=首行交易日。
        CREATE TABLE IF NOT EXISTS validation_sessions (
            window_id  TEXT NOT NULL
                       REFERENCES strategy_validation_windows(id),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            trade_date TEXT NOT NULL,
            created_ts TEXT NOT NULL,
            UNIQUE (window_id, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_validation_sessions_win
            ON validation_sessions(window_id, trade_date);
        """,
    ),
    (
        25,
        """
        CREATE TABLE IF NOT EXISTS llm_provider (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            kind          TEXT NOT NULL DEFAULT 'openai_compat',
            preset        TEXT NOT NULL DEFAULT '',
            base_url      TEXT NOT NULL DEFAULT '',
            model         TEXT NOT NULL DEFAULT '',
            api_key_enc   TEXT NOT NULL DEFAULT '',
            api_key_hint  TEXT NOT NULL DEFAULT '',
            updated_ts    TEXT NOT NULL,
            updated_by    TEXT NOT NULL DEFAULT '',
            last_test_ts  TEXT,
            last_test_ok  INTEGER,
            last_test_error TEXT NOT NULL DEFAULT ''
        );
        """,
    ),
    (
        26,
        """
        -- spec-02 §11 单价配置化不写死：价格变动仅改表；cache_read_per_1k 为缓存命中
        -- 单价（无缓存机制的 provider 置 NULL，按 input 价计）。PK(provider, model)。
        CREATE TABLE IF NOT EXISTS provider_pricing (
            provider           TEXT NOT NULL,
            model              TEXT NOT NULL,
            input_per_1k       REAL NOT NULL DEFAULT 0,
            output_per_1k      REAL NOT NULL DEFAULT 0,
            cache_read_per_1k  REAL,
            updated_ts         TEXT NOT NULL,
            updated_by         TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (provider, model)
        );

        -- spec-02 §11 任务粒度性能与费用留痕（与账单可对）；daily_reports.llm_perf_id 追溯。
        CREATE TABLE IF NOT EXISTS performance_records (
            id              TEXT PRIMARY KEY,
            task_id         TEXT NOT NULL,
            agent_id        TEXT NOT NULL,
            task_type       TEXT NOT NULL,
            started_ts      TEXT,
            ended_ts        TEXT,
            duration_ms     INTEGER,
            mem_peak_mb     REAL,
            llm_calls       INTEGER NOT NULL DEFAULT 0,
            tool_calls      INTEGER NOT NULL DEFAULT 0,
            tokens_in       INTEGER NOT NULL DEFAULT 0,
            cached_tokens   INTEGER NOT NULL DEFAULT 0,
            tokens_out      INTEGER NOT NULL DEFAULT 0,
            cost_yuan       REAL,
            data_fetch_bytes INTEGER NOT NULL DEFAULT 0,
            result          TEXT NOT NULL DEFAULT 'ok'
                            CHECK (result IN ('ok', 'failed', 'high_cost')),
            high_cost_flag  INTEGER NOT NULL DEFAULT 0,
            provider        TEXT NOT NULL DEFAULT '',
            model           TEXT NOT NULL DEFAULT '',
            detail          TEXT NOT NULL DEFAULT '',
            created_ts      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_perf_records_task
            ON performance_records(task_id);
        CREATE INDEX IF NOT EXISTS idx_perf_records_agent_time
            ON performance_records(agent_id, created_ts);
        """,
    ),
    (
        27,
        """
        -- spec-03 §5.1 L0 3 秒采集序列：PK(symbol, trade_date, ts) 唯一 → 重采幂等；
        -- price/prev_close/cum_turnover 以 TEXT 存 Decimal；status=suspended 样本仅审计
        -- 不进判定序列；is_extended=1（15:00 后延续采样）仅作 close_candidates。
        CREATE TABLE IF NOT EXISTS l0_ticks (
            symbol        TEXT NOT NULL,
            trade_date    TEXT NOT NULL,
            ts            TEXT NOT NULL,
            price         TEXT NOT NULL,
            prev_close    TEXT NOT NULL,
            pct_chg       TEXT NOT NULL DEFAULT '0',
            cum_turnover  TEXT NOT NULL DEFAULT '0',
            status        TEXT NOT NULL DEFAULT 'normal'
                          CHECK (status IN ('normal', 'suspended')),
            is_extended   INTEGER NOT NULL DEFAULT 0,
            source        TEXT NOT NULL DEFAULT '',
            quality       TEXT NOT NULL DEFAULT 'ok',
            PRIMARY KEY (symbol, trade_date, ts)
        );

        -- spec-03 §4.1/§5.1 票级覆盖：分母=该票当日实际可交易秒数/3（盘中停牌秒数剔除），
        -- coverage_rate TEXT(Decimal)；quality/degraded_reason 供日报降级判定共用。
        CREATE TABLE IF NOT EXISTS l0_coverage (
            symbol           TEXT NOT NULL,
            trade_date       TEXT NOT NULL,
            expected_ticks   INTEGER NOT NULL DEFAULT 0,
            actual_ticks     INTEGER NOT NULL DEFAULT 0,
            started_ts       TEXT NOT NULL DEFAULT '',
            ended_ts         TEXT NOT NULL DEFAULT '',
            suspended_secs   INTEGER NOT NULL DEFAULT 0,
            coverage_rate    TEXT NOT NULL DEFAULT '',
            quality          TEXT NOT NULL DEFAULT 'ok',
            degraded_reason  TEXT NOT NULL DEFAULT '',
            notes            TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (symbol, trade_date)
        );

        -- spec-03 §3.3 单源同日判定不变式：当日绑定源记录，换源只对次日起生效。
        CREATE TABLE IF NOT EXISTS source_binding (
            symbol     TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            source     TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date)
        );

        -- spec-03 §4.1 官方收盘价源当日绑定（防跨源收盘价基准漂移，换源次日生效）。
        CREATE TABLE IF NOT EXISTS official_close_source (
            trade_date TEXT PRIMARY KEY,
            source     TEXT NOT NULL
        );

        -- spec-03 §3.1 观察集合（管理 Agent 配置、常驻，v0.1 支持；集合变更次日生效）。
        CREATE TABLE IF NOT EXISTS market_observation (
            symbol   TEXT PRIMARY KEY,
            added_ts TEXT NOT NULL,
            reason   TEXT NOT NULL DEFAULT '',
            active   INTEGER NOT NULL DEFAULT 1
        );

        -- spec-03 §3.1 每日采集清单快照（当日有效条件单 ∪ 持仓 ∪ 观察集合；审计口径）。
        CREATE TABLE IF NOT EXISTS collector_watchlist (
            trade_date TEXT NOT NULL,
            symbol     TEXT NOT NULL,
            reason     TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (trade_date, symbol)
        );

        -- spec-03 §6 本地 SQLite 缓存：L1 分钟线按票按日落盘 + 更新时间戳（增量更新）。
        CREATE TABLE IF NOT EXISTS minute_cache (
            symbol     TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            minute     TEXT NOT NULL,
            close      TEXT NOT NULL,
            source     TEXT NOT NULL DEFAULT '',
            fetched_ts TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date, minute)
        );

        -- spec-03 §6 本地 SQLite 缓存：日线（官方收盘价权威列增量缓存）。
        CREATE TABLE IF NOT EXISTS daily_cache (
            symbol     TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open       TEXT NOT NULL,
            close      TEXT NOT NULL,
            high       TEXT NOT NULL,
            low        TEXT NOT NULL,
            volume     TEXT NOT NULL DEFAULT '0',
            source     TEXT NOT NULL DEFAULT '',
            fetched_ts TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date)
        );
        """,
    ),
    (
        28,
        """
        -- spec-03 §7 quality 传播：结算日志记录票级数据质量标记（JSON {symbol:
        -- {quality, degraded_reason, notes}}，仅 quality != ok 的票），供日报数据段
        -- annotations.quality_marks 呈现；旧行 NULL 视为无标记。数据源 = 数据服务
        -- 消费接口（settle_input 返回，settle_day 汇总结算时注入），本列与日报同事务
        -- 落盘（结算与首版日报一致）。既有行无需迁移。
        ALTER TABLE settlement_log ADD COLUMN quality_marks TEXT;
        """,
    ),
    (
        29,
        """
        -- spec-01 §2.5/§8 信号注册表（引擎登记切片）：候选入选/买入成交/卖出成交/
        -- 避坑拦截登记 sig_type/symbol/reg_date/concept_tag/env_bucket/quality 与
        -- strategy_version_no/trial_flag；N（默认 10）个交易日后由引擎结算前瞻收益。
        -- 列口径：fwd_return_pct 为登记日基准（ref_price，官方收盘）到结清日收盘的涨跌
        -- 百分数；last_close 为最近可得价（停牌/退市 stale 结清用，不虚构）；exception/
        -- pitfall_id 服务避坑拦截（#54）；sessions_done/last_seen 为无日历表下的交易日
        -- 推进幂等（与 exit_trackings 同法）；fwd_end_date 非空即已结清。
        CREATE TABLE IF NOT EXISTS signal_registry (
            id                  TEXT PRIMARY KEY,
            account_id          TEXT NOT NULL REFERENCES accounts(id),
            sig_type            TEXT NOT NULL
                                CHECK (sig_type IN ('candidate', 'buy', 'sell',
                                                    'pitfall_intercept')),
            symbol              TEXT NOT NULL,
            reg_date            TEXT NOT NULL,
            concept_tag         TEXT NOT NULL DEFAULT '',
            env_bucket          TEXT NOT NULL DEFAULT '',
            exception           INTEGER NOT NULL DEFAULT 0,
            pitfall_id          TEXT NOT NULL DEFAULT '',
            fwd_return_pct      REAL,
            fwd_end_date        TEXT NOT NULL DEFAULT '',
            quality             TEXT NOT NULL DEFAULT '',
            strategy_version_no TEXT NOT NULL DEFAULT '',
            trial_flag          INTEGER NOT NULL DEFAULT 0,
            ref_price           REAL NOT NULL DEFAULT 0,
            last_close          REAL NOT NULL DEFAULT 0,
            sessions_done       INTEGER NOT NULL DEFAULT 0,
            last_seen           TEXT NOT NULL DEFAULT '',
            created_ts          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signal_registry_due
            ON signal_registry(sig_type, fwd_end_date, reg_date);
        CREATE INDEX IF NOT EXISTS idx_signal_registry_acct
            ON signal_registry(account_id, reg_date);
        """,
    ),
    (
        30,
        """
        -- spec-05 §3.11 概念标签治理：月度归并映射（append-only）。自由标签 alias →
        -- 规范条目 canonical_kb_id，统计按 canonical 归并、不回写历史信号行（保
        -- append-only 与可追溯）。每个 alias 只映射一个 canonical（PK 约束）；同
        -- canonical 可多 alias。merged_by/merged_ts/reason 留痕管理 Agent 确认。
        CREATE TABLE IF NOT EXISTS kb_tag_aliases (
            alias           TEXT PRIMARY KEY,
            canonical_kb_id TEXT NOT NULL REFERENCES kb_entries(id),
            merged_by       TEXT NOT NULL DEFAULT '',
            merged_ts       TEXT NOT NULL,
            reason          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_kb_tag_aliases_canonical
            ON kb_tag_aliases(canonical_kb_id);
        """,
    ),
    (
        31,
        """
        -- spec-05 §5 归档经验提取：Agent 归档/退休时的终局归因报告（终局统计确定性 +
        -- LLM 终局归因 + 管理 Agent 评审 + 回填知识库）。stats=按 (concept_tag×env_bucket)
        -- 归并统计 JSON；attribution=LLM 归因文本（status 标注生成态，未配置/失败降级）；
        -- review_ref=管理评审与回填决策留痕 JSON。
        CREATE TABLE IF NOT EXISTS retro_reports (
            id                 TEXT PRIMARY KEY,
            agent_id           TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'pending_review'
                               CHECK (status IN ('pending_review', 'confirmed')),
            stats              TEXT NOT NULL DEFAULT '{}',
            attribution        TEXT NOT NULL DEFAULT '',
            attribution_status TEXT NOT NULL DEFAULT '',
            review_ref         TEXT NOT NULL DEFAULT '{}',
            created_ts         TEXT NOT NULL,
            updated_ts         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_retro_reports_agent
            ON retro_reports(agent_id, created_ts DESC);
        """,
    ),
    (
        32,
        """
        -- spec-04 §2.1 调度任务表（P1 最小实现）+ §2.5 可延迟无时效任务组。
        -- 幂等键 UNIQUE(agent_id, task_type, trade_date, task_slot, dedup_key)：
        -- 补跑/重跑不重复生成；v0.6 事件触发任务以业务子键（归档事件 id 等）区分同日多事件。
        -- 本切片实现「入队 + 到期认领 + 处理器执行」最小闭环（tick 循环/资源闸门后续补）。
        CREATE TABLE IF NOT EXISTS task_schedule (
            id               TEXT PRIMARY KEY,
            agent_id         TEXT NOT NULL DEFAULT '',
            task_type        TEXT NOT NULL,
            task_slot        TEXT NOT NULL DEFAULT '',
            trade_date       TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'running', 'done',
                                               'failed', 'skipped', 'expired', 'partial')),
            scheduled_ts     TEXT NOT NULL DEFAULT '',
            started_ts       TEXT NOT NULL DEFAULT '',
            ended_ts         TEXT NOT NULL DEFAULT '',
            attempt_count    INTEGER NOT NULL DEFAULT 0,
            last_error       TEXT NOT NULL DEFAULT '',
            resource_class   TEXT NOT NULL DEFAULT 'light'
                             CHECK (resource_class IN ('scan', 'llm-heavy', 'light')),
            is_deferrable    INTEGER NOT NULL DEFAULT 0,
            priority         INTEGER NOT NULL DEFAULT 5,
            dedup_key        TEXT NOT NULL DEFAULT '',
            payload          TEXT NOT NULL DEFAULT '{}',
            created_ts       TEXT NOT NULL,
            UNIQUE (agent_id, task_type, trade_date, task_slot, dedup_key)
        );
        CREATE INDEX IF NOT EXISTS idx_task_schedule_status
            ON task_schedule(status, is_deferrable, priority);
        CREATE INDEX IF NOT EXISTS idx_task_schedule_agent
            ON task_schedule(agent_id, created_ts DESC);
        """,
    ),
    (
        33,
        """
        -- spec-04 §2.1/§3.3 interrupt 审批节点：任务行记录挂起时刻与来源审批单。
        ALTER TABLE task_schedule ADD COLUMN interrupt_entered_ts TEXT NOT NULL DEFAULT '';
        ALTER TABLE task_schedule ADD COLUMN approval_id TEXT NOT NULL DEFAULT '';
        ALTER TABLE task_schedule ADD COLUMN interrupt_note TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_task_schedule_interrupt
            ON task_schedule(status, interrupt_entered_ts);
        """,
    ),
    (
        34,
        """
        -- spec-04 §9.1 管理 Agent 安全自治模式：全局状态落库（单行，无进程内存依赖）。
        CREATE TABLE IF NOT EXISTS manager_state (
            id                          TEXT PRIMARY KEY,
            mode                        TEXT NOT NULL DEFAULT 'active'
                                        CHECK (mode IN ('active', 'autonomous')),
            consecutive_health_failures  INTEGER NOT NULL DEFAULT 0,
            consecutive_health_successes INTEGER NOT NULL DEFAULT 0,
            consecutive_resume_failures  INTEGER NOT NULL DEFAULT 0,
            last_health_ts              TEXT NOT NULL DEFAULT '',
            last_health_ok              INTEGER NOT NULL DEFAULT 1,
            last_reason                 TEXT NOT NULL DEFAULT '',
            degraded_since              TEXT NOT NULL DEFAULT '',
            recovered_ts                TEXT NOT NULL DEFAULT '',
            updated_ts                  TEXT NOT NULL
        );
        INSERT OR IGNORE INTO manager_state (id, updated_ts) VALUES ('manager', '');
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
