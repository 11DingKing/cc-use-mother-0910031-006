"""数据库连接与事务管理。

所有写操作都通过 ``BEGIN IMMEDIATE`` 立即获取 SQLite 写锁，
从而把并发下单/撤单/撮合串行化，配合 ``busy_timeout`` 等待写锁，
避免“database is locked”与读后写竞争。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


def connect(db_path: str = ":memory:", check_same_thread: bool = True) -> sqlite3.Connection:
    """打开一个适合手动事务管理的连接。"""
    conn = sqlite3.connect(
        db_path, isolation_level=None, timeout=30, check_same_thread=check_same_thread
    )
    if db_path != ":memory:":
        # WAL 允许管理命令重放清算时与读接口并发。
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即取写锁的事务上下文，异常回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
