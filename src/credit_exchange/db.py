"""SQLite 连接与建表。

仅使用标准库，开启 WAL 与外键；写事务统一使用 ``BEGIN IMMEDIATE`` 以
保证多线程/多进程并发下的串行化提交（SQLite 层面的可串行调度）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    code        TEXT PRIMARY KEY,            -- 交易对，如 CREDIT_A/CREDIT_B
    base_asset  TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    halted      INTEGER NOT NULL DEFAULT 0,  -- 监管暂停：1 时禁止新成交
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT ''
);

-- 每种积分每个账户一行；金额均为非负整数（最小计量单位）
CREATE TABLE IF NOT EXISTS balances (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    asset      TEXT NOT NULL,
    total_bal  INTEGER NOT NULL DEFAULT 0 CHECK (total_bal >= 0),
    frozen     INTEGER NOT NULL DEFAULT 0 CHECK (frozen >= 0 AND frozen <= total_bal),
    PRIMARY KEY (account_id, asset)
);

CREATE TABLE IF NOT EXISTS orders (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- 价格时间优先中的"时间"
    order_id   TEXT NOT NULL UNIQUE,
    market     TEXT NOT NULL REFERENCES markets(code),
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    side       TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price      INTEGER NOT NULL CHECK (price > 0),
    qty        INTEGER NOT NULL CHECK (qty > 0),       -- 原始委托量
    filled     INTEGER NOT NULL DEFAULT 0 CHECK (filled >= 0 AND filled <= qty),
    status     TEXT NOT NULL DEFAULT 'NEW'
               CHECK (status IN ('NEW','PARTIAL','FILLED','CANCELLED','EXPIRED','HALTED')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 撮合簿读取顺序：买盘价格从高到低、卖盘价格从低到高，同价按 seq
CREATE INDEX IF NOT EXISTS idx_book_buy
    ON orders(market, price, seq) WHERE status IN ('NEW','PARTIAL') AND side='BUY';
CREATE INDEX IF NOT EXISTS idx_book_sell
    ON orders(market, price, seq) WHERE status IN ('NEW','PARTIAL') AND side='SELL';

CREATE TABLE IF NOT EXISTS trades (
    trade_id   TEXT PRIMARY KEY,        -- 确定性生成：incoming_order_id:counter_order_id
    market     TEXT NOT NULL,
    taker_order TEXT NOT NULL,          -- 主动订单
    maker_order TEXT NOT NULL,          -- 被动挂单
    taker_account TEXT NOT NULL,
    maker_account TEXT NOT NULL,
    taker_side    TEXT NOT NULL CHECK (taker_side IN ('BUY','SELL')),
    price      INTEGER NOT NULL,
    qty        INTEGER NOT NULL CHECK (qty > 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 成交回报：每个成交每方一条（便于分别查询、审计）
CREATE TABLE IF NOT EXISTS fills (
    fill_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   TEXT NOT NULL REFERENCES trades(trade_id),
    order_id   TEXT NOT NULL,
    account_id TEXT NOT NULL,
    side       TEXT NOT NULL,
    price      INTEGER NOT NULL,
    qty        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clearing_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   TEXT NOT NULL,
    account_id TEXT NOT NULL,
    asset      TEXT NOT NULL,
    direction  TEXT NOT NULL CHECK (direction IN ('DEBIT','CREDIT')),
    amount     INTEGER NOT NULL CHECK (amount > 0),
    posted     INTEGER NOT NULL DEFAULT 0,   -- 0=待清算 1=已入账
    posted_at  TEXT,
    UNIQUE(trade_id, account_id, asset, direction)
);

-- 幂等请求记录：response 为首次调用结果的 JSON 文本
CREATE TABLE IF NOT EXISTS idempotent_requests (
    request_id TEXT PRIMARY KEY,
    scope      TEXT NOT NULL,
    req_json   TEXT NOT NULL,
    response   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,  # 事务由服务层显式管理
        check_same_thread=False,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
