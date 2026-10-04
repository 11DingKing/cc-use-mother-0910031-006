"""清算过账与重放。

成交时只写入 PENDING 清算分录（与成交在同一事务）；过账时把分录应用到
余额并翻转为 POSTED。分录唯一约束与状态判断保证重放安全：

* 已 POSTED 的分录永不重复应用；
* 同一笔成交的四条分录要么全部过账、要么全部不过账。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .db import transaction
from .schema import BASE_ASSET, QUOTE_ASSET


def _maybe_tx(conn: sqlite3.Connection):
    """已在事务中则复用（与撮合同事务过账），否则自行开立即写事务。"""
    if conn.in_transaction:

        @contextmanager
        def _passthrough() -> Iterator[sqlite3.Connection]:
            yield conn

        return _passthrough()
    return transaction(conn)


def _pending_trade_ids(conn: sqlite3.Connection) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_id FROM clearing_entries WHERE status='PENDING' ORDER BY trade_id"
        ).fetchall()
    ]


def post_pending_clearing(conn: sqlite3.Connection) -> dict[str, Any]:
    """过账全部 PENDING 清算分录；重复调用不会产生第二笔扣账。

    无外层事务时按成交逐笔提交，单条失败不影响其它已完成成交。
    """
    posted_trades: list[int] = []
    while True:
        with _maybe_tx(conn):
            ids = _pending_trade_ids(conn)
            if not ids:
                break
            trade_id = ids[0]
            _post_one_trade(conn, trade_id)
            posted_trades.append(trade_id)
        if conn.in_transaction:
            # 外层事务（撮合事务）：一次处理完，由外层统一提交。
            rest = _pending_trade_ids(conn)
            for tid in rest:
                _post_one_trade(conn, tid)
                posted_trades.append(tid)
            break
    return {"posted_trade_ids": posted_trades, "posted_count": len(posted_trades)}


def _post_one_trade(conn: sqlite3.Connection, trade_id: int) -> None:
    entries = conn.execute(
        "SELECT account_id, asset, amount, direction FROM clearing_entries"
        " WHERE trade_id=? AND status='PENDING' ORDER BY id",
        (trade_id,),
    ).fetchall()
    if not entries:
        return
    if len(entries) != 4:
        raise RuntimeError(f"成交 {trade_id} 清算分录不完整：期望 4 条，实际 {len(entries)} 条")

    for e in entries:
        if e["direction"] == "DEBIT":
            # 从冻结中扣减（amount 为负）。
            cur = conn.execute(
                "UPDATE balances SET frozen=frozen+?, version=version+1"
                " WHERE account_id=? AND asset=? AND frozen+? >= 0",
                (e["amount"], e["account_id"], e["asset"], e["amount"]),
            )
            if cur.rowcount == 0:
                raise RuntimeError(
                    f"成交 {trade_id} 过账失败：账户 {e['account_id']} 冻结 {e['asset']} 不足"
                )
            _ledger(conn, e, trade_id, available_delta=0, frozen_delta=e["amount"])
        else:
            # 计入可用余额（amount 为正）。
            conn.execute(
                "UPDATE balances SET available=available+?, version=version+1"
                " WHERE account_id=? AND asset=?",
                (e["amount"], e["account_id"], e["asset"]),
            )
            _ledger(conn, e, trade_id, available_delta=e["amount"], frozen_delta=0)

    conn.execute(
        "UPDATE clearing_entries SET status='POSTED', posted_at=datetime('now')"
        " WHERE trade_id=? AND status='PENDING'",
        (trade_id,),
    )


def _ledger(
    conn: sqlite3.Connection,
    entry: sqlite3.Row,
    trade_id: int,
    available_delta: int,
    frozen_delta: int,
) -> None:
    row = conn.execute(
        "SELECT available, frozen FROM balances WHERE account_id=? AND asset=?",
        (entry["account_id"], entry["asset"]),
    ).fetchone()
    conn.execute(
        "INSERT INTO ledger_entries(account_id, asset, amount, frozen_delta, ref_type, ref_id,"
        " balance_after, frozen_after) VALUES(?, ?, ?, ?, 'CLEAR', ?, ?, ?)",
        (
            entry["account_id"],
            entry["asset"],
            available_delta,
            frozen_delta,
            str(trade_id),
            row["available"],
            row["frozen"],
        ),
    )


def reconcile_order_freeze(conn: sqlite3.Connection, order_id: int) -> int:
    """按订单实际成交情况校正买单冻结（释放价格改善部分）。

    买单应保留的冻结 = 剩余量×限价 + 已成交但尚未过账的成交金额；
    已过账部分在过账时已从冻结扣减。卖单冻结恒等于剩余量，由数量驱动，
    不存在价差，无需校正。
    """
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if order is None or order["side"] != "BUY":
        return 0
    remaining = order["orig_qty"] - order["filled_qty"]
    pending_cost = conn.execute(
        "SELECT COALESCE(SUM(-ce.amount), 0) FROM clearing_entries ce"
        " WHERE ce.account_id=? AND ce.asset=? AND ce.direction='DEBIT' AND ce.status='PENDING'"
        " AND ce.trade_id IN (SELECT id FROM trades WHERE taker_order_id=? OR maker_order_id=?)",
        (order["account_id"], QUOTE_ASSET, order_id, order_id),
    ).fetchone()[0]
    required_frozen = remaining * order["price"] + pending_cost
    row = conn.execute(
        "SELECT frozen FROM balances WHERE account_id=? AND asset=?",
        (order["account_id"], QUOTE_ASSET),
    ).fetchone()
    excess = (row["frozen"] if row else 0) - required_frozen
    if excess > 0:
        cur = conn.execute(
            "UPDATE balances SET frozen=frozen-?, available=available+?, version=version+1"
            " WHERE account_id=? AND asset=? AND frozen>=?",
            (excess, excess, order["account_id"], QUOTE_ASSET, excess),
        )
        if cur.rowcount == 0:
            raise RuntimeError("价格改善冻结释放失败")
        after = conn.execute(
            "SELECT available, frozen FROM balances WHERE account_id=? AND asset=?",
            (order["account_id"], QUOTE_ASSET),
        ).fetchone()
        conn.execute(
            "INSERT INTO ledger_entries(account_id, asset, amount, frozen_delta, ref_type, ref_id,"
            " balance_after, frozen_after) VALUES(?, ?, ?, ?, 'TRADE', ?, ?, ?)",
            (
                order["account_id"],
                QUOTE_ASSET,
                excess,
                -excess,
                order["client_order_id"],
                after["available"],
                after["frozen"],
            ),
        )
    return max(excess, 0)


def audit_freezes(conn: sqlite3.Connection) -> dict[str, Any]:
    """账实核对：余额 frozen 必须恰好覆盖 活动单冻结 + PENDING 过账义务。

    卖单：每张 NEW 单按剩余量冻结 BASE；
    买单：每张 NEW 单按 剩余量×限价 冻结 QUOTE；
    PENDING 成交义务按分录金额另计（尚未过账的扣冻结部分）。
    """
    rows = conn.execute(
        "SELECT o.account_id,"
        " SUM(CASE WHEN o.side='SELL' THEN o.orig_qty-o.filled_qty ELSE 0 END) AS base_orders,"
        " SUM(CASE WHEN o.side='BUY' THEN (o.orig_qty-o.filled_qty)*o.price ELSE 0 END) AS quote_orders"
        " FROM orders o WHERE o.status='NEW' GROUP BY o.account_id"
    ).fetchall()
    expected: dict[tuple[str, str], int] = {}
    for r in rows:
        if r["base_orders"]:
            expected[(r["account_id"], BASE_ASSET)] = r["base_orders"]
        if r["quote_orders"]:
            expected[(r["account_id"], QUOTE_ASSET)] = r["quote_orders"]
    for r in conn.execute(
        "SELECT account_id, asset, SUM(-amount) AS need FROM clearing_entries"
        " WHERE status='PENDING' AND direction='DEBIT' GROUP BY account_id, asset"
    ).fetchall():
        expected[(r["account_id"], r["asset"])] = expected.get((r["account_id"], r["asset"]), 0) + r["need"]

    mismatches: list[dict[str, Any]] = []
    balance_keys = {(r["account_id"], r["asset"]): r for r in conn.execute(
        "SELECT account_id, asset, available, frozen FROM balances"
    ).fetchall()}
    keys = set(expected) | set(balance_keys)
    for key in sorted(keys):
        want = expected.get(key, 0)
        got = balance_keys[key]["frozen"] if key in balance_keys else 0
        if want != got:
            mismatches.append(
                {
                    "account_id": key[0],
                    "asset": key[1],
                    "expected_frozen": want,
                    "actual_frozen": got,
                }
            )
    return {"consistent": not mismatches, "mismatches": mismatches}


def clearing_status(conn: sqlite3.Connection) -> dict[str, Any]:
    pending = conn.execute(
        "SELECT trade_id, COUNT(*) AS entries FROM clearing_entries"
        " WHERE status='PENDING' GROUP BY trade_id ORDER BY trade_id"
    ).fetchall()
    posted = conn.execute(
        "SELECT COUNT(*) FROM clearing_entries WHERE status='POSTED'"
    ).fetchone()[0]
    return {
        "pending_trade_count": len(pending),
        "pending_entry_count": sum(r["entries"] for r in pending),
        "posted_entry_count": posted,
        "pending_trades": [{"trade_id": r["trade_id"], "entries": r["entries"]} for r in pending],
    }
