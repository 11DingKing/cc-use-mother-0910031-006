"""交易撮合与清算核心服务。

所有写操作都在单个 ``BEGIN IMMEDIATE`` 事务内完成：事务一旦提交，订单状态、
冻结额、余额、成交、回报、清算分录同时生效；一旦回滚则全部不留痕迹。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from typing import Any, Callable, Iterable

from .errors import (
    DuplicateMarket,
    IdempotencyKeyReused,
    InsufficientBalance,
    InvalidRequest,
    MarketHalted,
    SettlementInvariant,
    UnknownAccount,
    UnknownMarket,
    UnknownOrder,
)

ACTIVE = ("NEW", "PARTIAL")


class ExchangeService:
    """线程安全的撮合清算服务。

    同一进程内的多线程共享一个连接时，由 :data:`_writelock` 串行化写事务；
    多连接/多进程访问同一数据库文件时，由 SQLite 的 ``BEGIN IMMEDIATE``
    与 busy_timeout 串行化提交。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._writelock = threading.RLock()

    # ------------------------------------------------------------------ 基础

    def create_account(self, account_id: str, name: str = "") -> dict:
        with self._writelock:
            self.conn.execute(
                "INSERT OR IGNORE INTO accounts(account_id, name) VALUES (?, ?)",
                (account_id, name),
            )
            self.conn.commit()
        return {"account_id": account_id, "name": name}

    def issue(self, request_id: str, account_id: str, asset: str, amount: int) -> dict:
        """授信/充值：增加某积分的总额（幂等）。"""
        amount = _positive_int(amount, "amount")
        payload = {"account_id": account_id, "asset": asset, "amount": amount}

        def run(conn: sqlite3.Connection) -> dict:
            if not _account_exists(conn, account_id):
                raise UnknownAccount(account_id)
            conn.execute(
                """
                INSERT INTO balances(account_id, asset, total_bal, frozen)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(account_id, asset)
                DO UPDATE SET total_bal = total_bal + ?
                """,
                (account_id, asset, amount, amount),
            )
            result = self.get_balance(account_id, asset)
            return {"account_id": account_id, "asset": asset, "credited": amount, "balance": result}

        return self._idempotent("issue", request_id, payload, run)

    def create_market(self, code: str, base_asset: str, quote_asset: str) -> dict:
        if not code or base_asset == quote_asset:
            raise InvalidRequest("交易代码不能为空，且两种积分不能相同")
        with self._writelock:
            try:
                self.conn.execute(
                    "INSERT INTO markets(code, base_asset, quote_asset) VALUES (?, ?, ?)",
                    (code, base_asset, quote_asset),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                raise DuplicateMarket(f"交易对 {code} 已存在") from exc
        return {"market": code, "base_asset": base_asset, "quote_asset": quote_asset}

    def halt_market(self, request_id: str, code: str) -> dict:
        """监管暂停：禁止撮合，并将活动订单置为 HALTED、释放剩余冻结。"""
        payload = {"market": code}

        def run(conn: sqlite3.Connection) -> dict:
            market = _get_market(conn, code)
            released = self._deactivate_book(
                conn, code, "HALTED",
                "UPDATE markets SET halted=1 WHERE code=?",
                (code,),
            )
            return {"market": code, "halted": True, "released": released,
                    "base_asset": market["base_asset"], "quote_asset": market["quote_asset"]}

        return self._idempotent("halt", request_id, payload, run)

    def resume_market(self, request_id: str, code: str) -> dict:
        payload = {"market": code}

        def run(conn: sqlite3.Connection) -> dict:
            _get_market(conn, code)
            conn.execute("UPDATE markets SET halted=0 WHERE code=?", (code,))
            return {"market": code, "halted": False}

        return self._idempotent("resume", request_id, payload, run)

    # ------------------------------------------------------------------ 下单

    def place_order(
        self,
        request_id: str,
        market: str,
        account_id: str,
        side: str,
        price: int,
        qty: int,
    ) -> dict:
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise InvalidRequest("side 必须是 BUY 或 SELL")
        price = _positive_int(price, "price")
        qty = _positive_int(qty, "qty")
        order_id = "ORD-" + uuid.uuid4().hex[:20]
        payload = {
            "market": market, "account_id": account_id, "side": side,
            "price": price, "qty": qty,
        }

        def run(conn: sqlite3.Connection) -> dict:
            row = _get_market(conn, market)
            if row["halted"]:
                raise MarketHalted(f"交易对 {market} 已被监管暂停，不能委托")
            if not _account_exists(conn, account_id):
                raise UnknownAccount(account_id)

            freeze_asset = row["quote_asset"] if side == "BUY" else row["base_asset"]
            freeze_amount = qty * price if side == "BUY" else qty
            _freeze(conn, account_id, freeze_asset, freeze_amount)

            cur = conn.execute(
                """INSERT INTO orders(order_id, market, account_id, side, price, qty)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_id, market, account_id, side, price, qty),
            )
            taker_seq = cur.lastrowid
            self._match(conn, row, order_id, taker_seq, account_id, side, price, qty)
            return self._order_view(conn, order_id)

        return self._idempotent("place_order", request_id, payload, run)

    def _match(
        self, conn: sqlite3.Connection, market: sqlite3.Row,
        order_id: str, taker_seq: int, taker_account: str,
        side: str, price: int, qty: int,
    ) -> None:
        """在当前事务内扫簿成交，直到吃单耗尽或无对手盘。"""
        remaining = qty
        if side == "BUY":
            sql = (
                "SELECT * FROM orders WHERE market=? AND status IN ('NEW','PARTIAL') "
                "AND side='SELL' AND price<=? ORDER BY price ASC, seq ASC"
            )
        else:
            sql = (
                "SELECT * FROM orders WHERE market=? AND status IN ('NEW','PARTIAL') "
                "AND side='BUY' AND price>=? ORDER BY price DESC, seq ASC"
            )
        candidates: Iterable[sqlite3.Row] = conn.execute(sql, (market["code"], price)).fetchall()

        for maker in candidates:
            if remaining <= 0:
                break
            # 重新加锁读取，防止同事务快照外的竞争（状态以当前行为准）
            maker = conn.execute(
                "SELECT * FROM orders WHERE order_id=? AND status IN ('NEW','PARTIAL')",
                (maker["order_id"],),
            ).fetchone()
            if maker is None:
                continue
            if maker["account_id"] == taker_account:
                continue  # 禁止自成交（wash trade）
            if side == "BUY" and maker["price"] > price:
                continue
            if side == "SELL" and maker["price"] < price:
                continue

            fill_qty = min(remaining, maker["qty"] - maker["filled"])
            if fill_qty <= 0:
                continue
            self._execute_trade(
                conn, market,
                taker_id=order_id, taker_account=taker_account, taker_side=side,
                taker_price=price, maker=maker, fill_qty=fill_qty,
            )
            remaining -= fill_qty

        self._set_order_status(conn, order_id, qty - remaining)

    def _execute_trade(
        self, conn: sqlite3.Connection, market: sqlite3.Row, *,
        taker_id: str, taker_account: str, taker_side: str, taker_price: int,
        maker: sqlite3.Row, fill_qty: int,
    ) -> None:
        """单笔成交：订单更新、冻结释放、余额扣减、回报、分录——同一事务。"""
        maker_id = maker["order_id"]
        maker_account = maker["account_id"]
        px = maker["price"]  # 价格优先：以挂单方（maker）报价成交
        notional = px * fill_qty
        trade_id = f"T:{taker_id}:{maker_id}"

        # 1) 成交主记录与双方回报（成交依据）
        conn.execute(
            """INSERT INTO trades(trade_id, market, taker_order, maker_order,
                                  taker_account, maker_account, taker_side, price, qty)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, market["code"], taker_id, maker_id,
             taker_account, maker_account, taker_side, px, fill_qty),
        )
        conn.execute(
            """INSERT INTO fills(trade_id, order_id, account_id, side, price, qty)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (trade_id, taker_id, taker_account, taker_side, px, fill_qty),
        )
        conn.execute(
            """INSERT INTO fills(trade_id, order_id, account_id, side, price, qty)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (trade_id, maker_id, maker_account,
             "SELL" if taker_side == "BUY" else "BUY", px, fill_qty),
        )

        # 2) 订单累计成交量
        conn.execute(
            "UPDATE orders SET filled=filled+?, updated_at=datetime('now') WHERE order_id=?",
            (fill_qty, taker_id),
        )
        conn.execute(
            "UPDATE orders SET filled=filled+?, updated_at=datetime('now') WHERE order_id=?",
            (fill_qty, maker_id),
        )
        self._set_order_status(conn, maker_id)

        # 3) 释放双方本笔成交对应的冻结（成交前资产仍处于冻结状态）。
        #    买单按自己的限价冻结，故买方释放额 = 成交量 × 委托限价；
        #    成交价（maker 报价）与限价的差额自然回到可用额度。
        if taker_side == "BUY":
            taker_release = fill_qty * taker_price
            maker_release = fill_qty
            # 买方冻结 quote（现金侧），卖方冻结 base（货物侧）
            _release_freeze(conn, taker_account, market["quote_asset"], taker_release)
            _release_freeze(conn, maker_account, market["base_asset"], maker_release)
            entries = [
                (maker_account, market["base_asset"], "DEBIT", fill_qty),    # 卖方交付积分
                (taker_account, market["base_asset"], "CREDIT", fill_qty),   # 买方收到积分
                (taker_account, market["quote_asset"], "DEBIT", notional),   # 买方支付对价
                (maker_account, market["quote_asset"], "CREDIT", notional),  # 卖方收到对价
            ]
        else:
            taker_release = fill_qty
            maker_release = notional  # 买方挂单按其限价冻结，成交价即其报价
            _release_freeze(conn, taker_account, market["base_asset"], taker_release)
            _release_freeze(conn, maker_account, market["quote_asset"], maker_release)
            entries = [
                (taker_account, market["base_asset"], "DEBIT", fill_qty),
                (maker_account, market["base_asset"], "CREDIT", fill_qty),
                (maker_account, market["quote_asset"], "DEBIT", notional),
                (taker_account, market["quote_asset"], "CREDIT", notional),
            ]

        # 4) 清算分录并逐笔过账到余额；分录在 (trade,账户,资产,方向) 上唯一，
        #    posted 标志保证同一笔分录的余额影响至多发生一次。
        for account_id, asset, direction, amount in entries:
            cur = conn.execute(
                """INSERT INTO clearing_entries(trade_id, account_id, asset, direction, amount, posted)
                   VALUES (?, ?, ?, ?, ?, 0)
                   ON CONFLICT(trade_id, account_id, asset, direction) DO NOTHING""",
                (trade_id, account_id, asset, direction, amount),
            )
            self._post_entry(conn, trade_id, account_id, asset, direction)

    def _post_entry(
        self, conn: sqlite3.Connection, trade_id: str,
        account_id: str, asset: str, direction: str,
    ) -> bool:
        """把一条未过账清算分录入账到余额。返回本次是否实际入账。

        ``UPDATE ... WHERE posted=0`` 的行计数是闸门：并发/重放下只有一个
        调用者能把它从 0 改成 1，余额扣减至多发生一次。
        """
        gate = conn.execute(
            """UPDATE clearing_entries SET posted=1, posted_at=datetime('now')
               WHERE trade_id=? AND account_id=? AND asset=? AND direction=? AND posted=0""",
            (trade_id, account_id, asset, direction),
        )
        if gate.rowcount == 0:
            return False
        amount = conn.execute(
            """SELECT amount FROM clearing_entries
               WHERE trade_id=? AND account_id=? AND asset=? AND direction=?""",
            (trade_id, account_id, asset, direction),
        ).fetchone()["amount"]
        if direction == "DEBIT":
            cur = conn.execute(
                """UPDATE balances SET total_bal=total_bal-?
                   WHERE account_id=? AND asset=? AND total_bal-? >= frozen""",
                (amount, account_id, asset, amount),
            )
            if cur.rowcount != 1:
                raise SettlementInvariant(
                    f"分录过账失败：{account_id} {asset} 不足以借记 {amount}"
                )
        else:
            conn.execute(
                """INSERT INTO balances(account_id, asset, total_bal, frozen)
                   VALUES (?, ?, ?, 0)
                   ON CONFLICT(account_id, asset)
                   DO UPDATE SET total_bal = total_bal + ?""",
                (account_id, asset, amount, amount),
            )
        return True

    @staticmethod
    def _set_order_status(conn: sqlite3.Connection, order_id: str, filled: int | None = None) -> None:
        row = conn.execute("SELECT qty, filled, status FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None or row["status"] not in ACTIVE:
            return
        new_filled = row["filled"] if filled is None else filled
        if new_filled >= row["qty"]:
            status = "FILLED"
        elif new_filled > 0:
            status = "PARTIAL"
        else:
            status = "NEW"
        conn.execute(
            "UPDATE orders SET status=?, updated_at=datetime('now') WHERE order_id=?",
            (status, order_id),
        )

    # ------------------------------------------------------------------ 撤单

    def cancel_order(self, request_id: str, order_id: str, account_id: str) -> dict:
        payload = {"order_id": order_id, "account_id": account_id}

        def run(conn: sqlite3.Connection) -> dict:
            order = self._load_active_order(conn, order_id)
            if order["account_id"] != account_id:
                raise InvalidRequest("只能撤销本账户的订单")
            self._invalidate(conn, order, "CANCELLED")
            return self._order_view(conn, order_id)

        return self._idempotent("cancel_order", request_id, payload, run)

    def expire_order(self, request_id: str, order_id: str) -> dict:
        """订单失效（运营/风控）：释放剩余冻结，置 EXPIRED。"""
        payload = {"order_id": order_id}

        def run(conn: sqlite3.Connection) -> dict:
            order = self._load_active_order(conn, order_id)
            self._invalidate(conn, order, "EXPIRED")
            return self._order_view(conn, order_id)

        return self._idempotent("expire_order", request_id, payload, run)

    @staticmethod
    def _load_active_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row:
        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if order is None:
            raise UnknownOrder(order_id)
        if order["status"] not in ACTIVE:
            raise InvalidRequest(f"订单 {order_id} 当前状态 {order['status']}，不可撤销/失效")
        return order

    def _invalidate(self, conn: sqlite3.Connection, order: sqlite3.Row, status: str) -> dict:
        remaining = order["qty"] - order["filled"]
        market = _get_market(conn, order["market"])
        asset = market["quote_asset"] if order["side"] == "BUY" else market["base_asset"]
        amount = remaining * order["price"] if order["side"] == "BUY" else remaining
        if remaining > 0:
            _release_freeze(conn, order["account_id"], asset, amount)
        conn.execute(
            "UPDATE orders SET status=?, updated_at=datetime('now') WHERE order_id=?",
            (status, order["order_id"]),
        )
        return {"released_asset": asset, "released_amount": amount, "remaining_qty": remaining}

    def _deactivate_book(
        self, conn: sqlite3.Connection, market: str, status: str, pre_sql: str | None = None,
        pre_args: tuple = (),
    ) -> list[dict]:
        """监管暂停时释放市场上全部活动订单的剩余冻结。"""
        if pre_sql:
            conn.execute(pre_sql, pre_args)
        released: list[dict] = []
        rows = conn.execute(
            "SELECT * FROM orders WHERE market=? AND status IN ('NEW','PARTIAL')",
            (market,),
        ).fetchall()
        for row in rows:
            info = self._invalidate(conn, row, status)
            released.append({"order_id": row["order_id"], **info})
        return released

    # ------------------------------------------------------------------ 查询

    def get_balance(self, account_id: str, asset: str) -> dict:
        row = self.conn.execute(
            "SELECT total_bal, frozen FROM balances WHERE account_id=? AND asset=?",
            (account_id, asset),
        ).fetchone()
        total = row["total_bal"] if row else 0
        frozen = row["frozen"] if row else 0
        return {"account_id": account_id, "asset": asset,
                "total": total, "frozen": frozen, "available": total - frozen}

    def get_order(self, order_id: str) -> dict:
        return self._order_view(self.conn, order_id)

    def order_book(self, market: str) -> dict:
        _get_market(self.conn, market)
        asks = self.conn.execute(
            """SELECT price, SUM(qty-filled) AS qty FROM orders
               WHERE market=? AND status IN ('NEW','PARTIAL') AND side='SELL'
               GROUP BY price ORDER BY price ASC""",
            (market,),
        ).fetchall()
        bids = self.conn.execute(
            """SELECT price, SUM(qty-filled) AS qty FROM orders
               WHERE market=? AND status IN ('NEW','PARTIAL') AND side='BUY'
               GROUP BY price ORDER BY price DESC""",
            (market,),
        ).fetchall()
        return {"market": market,
                "asks": [{"price": r["price"], "qty": r["qty"]} for r in asks],
                "bids": [{"price": r["price"], "qty": r["qty"]} for r in bids]}

    def _order_view(self, conn: sqlite3.Connection, order_id: str) -> dict:
        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if order is None:
            raise UnknownOrder(order_id)
        fills = conn.execute(
            """SELECT f.trade_id, f.price, f.qty, f.side,
                      CASE WHEN f.order_id=t.taker_order THEN t.maker_account
                           ELSE t.taker_account END AS counterparty_account,
                      CASE WHEN f.order_id=t.taker_order THEN t.maker_order
                           ELSE t.taker_order END AS counter_order_id,
                      t.created_at AS executed_at
               FROM fills f JOIN trades t ON f.trade_id=t.trade_id
               WHERE f.order_id=? ORDER BY f.fill_id""",
            (order_id,),
        ).fetchall()
        filled = order["filled"]
        avg_price = (
            sum(r["price"] * r["qty"] for r in fills) // filled if filled else None
        )
        return {
            "order_id": order["order_id"],
            "market": order["market"],
            "account_id": order["account_id"],
            "side": order["side"],
            "price": order["price"],
            "qty": order["qty"],
            "filled_qty": filled,
            "remaining_qty": order["qty"] - filled,
            "avg_fill_price": avg_price,
            "status": order["status"],
            "created_at": order["created_at"],
            "fills": [
                {"trade_id": r["trade_id"], "price": r["price"], "qty": r["qty"],
                 "side": r["side"], "counter_order_id": r["counter_order_id"],
                 "counterparty_account": r["counterparty_account"],
                 "executed_at": r["executed_at"]}
                for r in fills
            ],
        }

    # ------------------------------------------------------ 清算重放与对账

    def replay_clearing(self, market: str | None = None, dry_run: bool = False) -> dict:
        """按成交顺序重放清算，不产生第二笔交易。

        对每笔成交：补齐缺失的清算分录（唯一键去重），把仍是 posted=0 的
        分录入账（行计数闸门保证幂等），并校验 ``冻结额 == 活动订单持有``。
        全程只向 clearing_entries / balances 写入，绝不写 trades/fills。
        """
        with self._writelock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                query = "SELECT * FROM trades"
                args: tuple = ()
                if market:
                    query += " WHERE market=?"
                    args = (market,)
                query += " ORDER BY rowid"
                trades = conn.execute(query, args).fetchall()
                markets = {r["code"]: r for r in conn.execute("SELECT * FROM markets")}

                repaired_entries: list[dict] = []
                trade_count_before = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]

                for tr in trades:
                    expected = _expected_entries(tr, markets[tr["market"]])
                    existing = {
                        (r["account_id"], r["asset"], r["direction"]): r
                        for r in conn.execute(
                            "SELECT * FROM clearing_entries WHERE trade_id=?",
                            (tr["trade_id"],),
                        )
                    }
                    for account_id, asset, direction, amount in expected:
                        key = (account_id, asset, direction)
                        if key not in existing:
                            conn.execute(
                                """INSERT INTO clearing_entries
                                   (trade_id, account_id, asset, direction, amount, posted)
                                   VALUES (?, ?, ?, ?, ?, 0)
                                   ON CONFLICT DO NOTHING""",
                                (tr["trade_id"], account_id, asset, direction, amount),
                            )
                            repaired_entries.append(
                                {"trade_id": tr["trade_id"], "account_id": account_id,
                                 "asset": asset, "direction": direction, "amount": amount,
                                 "reason": "MISSING_ENTRY"})
                            # 补建的分录在本次重放直接入账，不再重复报告
                            self._post_entry(conn, tr["trade_id"], account_id, asset, direction)
                        elif self._post_entry(conn, tr["trade_id"], account_id, asset, direction):
                            repaired_entries.append(
                                {"trade_id": tr["trade_id"], "account_id": account_id,
                                 "asset": asset, "direction": direction, "amount": amount,
                                 "reason": "UNPOSTED_ENTRY"})

                drift = self._freeze_drift(conn)
                trade_count_after = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
                assert trade_count_after == trade_count_before, "重放过程中不得新增成交"

                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()
                return {
                    "trades_scanned": len(trades),
                    "entries_repaired": repaired_entries,
                    "freeze_drift": drift,
                    "new_trades_created": 0,
                    "dry_run": dry_run,
                }
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _freeze_drift(conn: sqlite3.Connection) -> list[dict]:
        """校验账面冻结 == 活动订单冻结持有量之和。"""
        rows = conn.execute(
            """
            WITH held AS (
                SELECT o.account_id,
                       CASE o.side WHEN 'BUY' THEN m.quote_asset ELSE m.base_asset END AS asset,
                       SUM(CASE o.side WHEN 'BUY' THEN (o.qty-o.filled)*o.price
                                       ELSE o.qty-o.filled END) AS hold
                FROM orders o JOIN markets m ON o.market=m.code
                WHERE o.status IN ('NEW','PARTIAL')
                GROUP BY 1, 2
            )
            SELECT h.account_id AS account_id, h.asset AS asset,
                   h.hold AS held, COALESCE(b.frozen,0) AS frozen
            FROM held h LEFT JOIN balances b
              ON h.account_id=b.account_id AND h.asset=b.asset
            WHERE COALESCE(b.frozen,0) != h.hold
            UNION ALL
            SELECT b.account_id, b.asset, 0, b.frozen
            FROM balances b LEFT JOIN held h
              ON h.account_id=b.account_id AND h.asset=b.asset
            WHERE h.account_id IS NULL AND b.frozen != 0
            """,
        ).fetchall()
        return [{"account_id": r["account_id"], "asset": r["asset"],
                 "order_holdings": r["held"], "ledger_frozen": r["frozen"]} for r in rows]

    # ------------------------------------------------------------------ 幂等

    def _idempotent(
        self, scope: str, request_id: str, payload: dict,
        action: Callable[[sqlite3.Connection], dict],
    ) -> dict:
        if not request_id:
            raise InvalidRequest("request_id 不能为空")
        key = f"{scope}:{request_id}"
        req_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self._writelock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT req_json, response FROM idempotent_requests WHERE request_id=?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    if existing["req_json"] != req_json:
                        raise IdempotencyKeyReused(
                            f"request_id {request_id} 曾用于不同的请求参数"
                        )
                    conn.commit()
                    return json.loads(existing["response"])

                result = action(conn)
                conn.execute(
                    "INSERT INTO idempotent_requests(request_id, scope, req_json, response) VALUES (?, ?, ?, ?)",
                    (key, scope, req_json, json.dumps(result, ensure_ascii=False)),
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise


# ---------------------------------------------------------------- 辅助函数

def _expected_entries(tr: sqlite3.Row, market: sqlite3.Row) -> list[tuple[str, str, str, int]]:
    """根据不可变成交记录重建四笔清算分录（借交付/收款，贷收入）。"""
    base, quote = market["base_asset"], market["quote_asset"]
    qty, notional = tr["qty"], tr["price"] * tr["qty"]
    taker, maker = tr["taker_account"], tr["maker_account"]
    if tr["taker_side"] == "BUY":
        return [
            (maker, base, "DEBIT", qty),       # 卖方交付积分
            (taker, base, "CREDIT", qty),      # 买方收到积分
            (taker, quote, "DEBIT", notional), # 买方支付对价
            (maker, quote, "CREDIT", notional),# 卖方收到对价
        ]
    return [
        (taker, base, "DEBIT", qty),
        (maker, base, "CREDIT", qty),
        (maker, quote, "DEBIT", notional),
        (taker, quote, "CREDIT", notional),
    ]


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InvalidRequest(f"{name} 必须是正整数")
    return value


def _account_exists(conn: sqlite3.Connection, account_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone() is not None


def _get_market(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM markets WHERE code=?", (code,)).fetchone()
    if row is None:
        raise UnknownMarket(f"未知交易对 {code}")
    return row


def _freeze(conn: sqlite3.Connection, account_id: str, asset: str, amount: int) -> None:
    row = conn.execute(
        "SELECT total_bal, frozen FROM balances WHERE account_id=? AND asset=?",
        (account_id, asset),
    ).fetchone()
    if row is None or row["total_bal"] - row["frozen"] < amount:
        raise InsufficientBalance(account_id, asset, amount)
    cur = conn.execute(
        "UPDATE balances SET frozen=frozen+? WHERE account_id=? AND asset=?",
        (amount, account_id, asset),
    )
    if cur.rowcount != 1:
        raise InsufficientBalance(account_id, asset, amount)


def _release_freeze(conn: sqlite3.Connection, account_id: str, asset: str, amount: int) -> None:
    if amount <= 0:
        return
    cur = conn.execute(
        "UPDATE balances SET frozen=frozen-? WHERE account_id=? AND asset=? AND frozen>=?",
        (amount, account_id, asset, amount),
    )
    if cur.rowcount != 1:
        raise SettlementInvariant(
            f"释放冻结失败：{account_id} {asset} 冻结额不足 {amount}（账实不一致）"
        )
