"""数据库结构与常量。

双积分设定：
* BASE（基础积分，代码 CREDIT_A）：挂单数量对应的标的积分；
* QUOTE（计价积分，代码 CREDIT_B）：报价与计价使用的积分。

交易对 ``A/B``：买单冻结 B（按 数量×单价），卖单冻结 A。
"""
from __future__ import annotations

import sqlite3

PAIR_CODE = "CREDIT_A/CREDIT_B"
BASE_ASSET = "CREDIT_A"
QUOTE_ASSET = "CREDIT_B"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id   TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 单一真值余额：available 与 frozen 之和即为账户该积分的总账余额。
CREATE TABLE IF NOT EXISTS balances (
    account_id   TEXT NOT NULL REFERENCES accounts(account_id),
    asset        TEXT NOT NULL,
    available    INTEGER NOT NULL DEFAULT 0 CHECK (available >= 0),
    frozen       INTEGER NOT NULL DEFAULT 0 CHECK (frozen >= 0),
    version      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, asset)
);

CREATE TABLE IF NOT EXISTS trading_pairs (
    pair_code    TEXT PRIMARY KEY,
    base_asset   TEXT NOT NULL,
    quote_asset  TEXT NOT NULL,
    -- NORMAL=正常；SUSPENDED=监管暂停（拒绝新单与撮合，允许撤单与清算）
    status       TEXT NOT NULL DEFAULT 'NORMAL'
        CHECK (status IN ('NORMAL', 'SUSPENDED')),
    suspended_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    pair_code     TEXT NOT NULL REFERENCES trading_pairs(pair_code),
    account_id    TEXT NOT NULL REFERENCES accounts(account_id),
    side          TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price         INTEGER NOT NULL CHECK (price > 0),
    orig_qty      INTEGER NOT NULL CHECK (orig_qty > 0),
    filled_qty    INTEGER NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
    -- NEW=活动单；CANCELLED=撤单；EXPIRED=订单失效；FILLED=全部成交
    status        TEXT NOT NULL DEFAULT 'NEW'
        CHECK (status IN ('NEW', 'CANCELLED', 'EXPIRED', 'FILLED')),
    -- 订单失效时间（可选）：到期未成交的剩余量由失效命令释放冻结。
    expires_at    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (filled_qty <= orig_qty)
);

-- 撮合遍历索引：价格时间优先。
-- 买单取允许的最高价：price <= 对手卖价，从高到低、时间从早到晚。
CREATE INDEX IF NOT EXISTS idx_orders_buy_book
    ON orders(side, price DESC, id ASC)
    WHERE status = 'NEW';
-- 卖单：价格从低到高、时间从早到晚。
CREATE INDEX IF NOT EXISTS idx_orders_sell_book
    ON orders(side, price ASC, id ASC)
    WHERE status = 'NEW';

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_code       TEXT NOT NULL,
    taker_order_id  INTEGER NOT NULL REFERENCES orders(id),
    maker_order_id  INTEGER NOT NULL REFERENCES orders(id),
    price           INTEGER NOT NULL CHECK (price > 0),
    qty             INTEGER NOT NULL CHECK (qty > 0),
    executed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (taker_order_id <> maker_order_id)
);

CREATE TABLE IF NOT EXISTS clearing_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id),
    account_id      TEXT NOT NULL REFERENCES accounts(account_id),
    asset           TEXT NOT NULL,
    amount          INTEGER NOT NULL,           -- 正=入账，负=扣减（取自冻结）
    direction       TEXT NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
    -- PENDING=已成交未过账；POSTED=已过账到可用余额
    status          TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'POSTED')),
    posted_at       TEXT,
    UNIQUE (trade_id, account_id, asset)
);

CREATE INDEX IF NOT EXISTS idx_clearing_pending
    ON clearing_entries(status, id) WHERE status = 'PENDING';

-- 总账流水：余额每一次变动都留痕，便于监管审计账实核对。
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT NOT NULL REFERENCES accounts(account_id),
    asset           TEXT NOT NULL,
    amount          INTEGER NOT NULL,           -- 正=可用增加，负=可用减少
    frozen_delta    INTEGER NOT NULL,           -- 正=冻结增加，负=冻结释放
    ref_type        TEXT NOT NULL,              -- DEPOSIT/FREEZE/TRADE/CANCEL/EXPIRE/CLEAR
    ref_id          TEXT,
    balance_after   INTEGER NOT NULL,           -- 变动后 available
    frozen_after    INTEGER NOT NULL,           -- 变动后 frozen
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 监管动作留痕（暂停/恢复交易对）。
CREATE TABLE IF NOT EXISTS regulatory_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_code       TEXT NOT NULL REFERENCES trading_pairs(pair_code),
    action          TEXT NOT NULL CHECK (action IN ('SUSPEND', 'RESUME')),
    reason          TEXT NOT NULL,
    operator        TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 请求幂等：同一 (request_type, idempotency_key) 只处理一次，
-- 重放时返回首次处理的响应快照。
CREATE TABLE IF NOT EXISTS idempotent_requests (
    request_type    TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (request_type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    trade_id        INTEGER REFERENCES trades(id),
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """建表并写入默认账户、余额与交易对。"""
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO trading_pairs(pair_code, base_asset, quote_asset, status) "
        "VALUES (?, ?, ?, 'NORMAL')",
        (PAIR_CODE, BASE_ASSET, QUOTE_ASSET),
    )
