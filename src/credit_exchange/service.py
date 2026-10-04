"""交易领域服务：账户冻结、限价单、价格时间优先撮合与成交回报。

所有写操作都在 ``BEGIN IMMEDIATE`` 单事务内完成：成交写入、冻结扣减、
可用入账与清算状态翻转要么全部提交，要么全部回滚。

清算模式：

* ``auto_post=True``（默认）：撮合后在同一事务内过账清算分录，
  满足“撮合与扣减同一事务”；
* ``auto_post=False``：成交与 PENDING 分录先提交（模拟过账前宕机/批处理），
  冻结仍覆盖全部已成交义务，随后由管理命令重放过账，绝不产生第二笔成交。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .clearing import post_pending_clearing, reconcile_order_freeze
from .db import transaction
from .errors import ExchangeError
from .schema import BASE_ASSET, PAIR_CODE, QUOTE_ASSET

_BUY = "BUY"
_SELL = "SELL"
_NEW = "NEW"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Exchange:
    """无状态服务对象，连接由调用方持有（便于事务与测试）。"""

    def __init__(self, conn: sqlite3.Connection, auto_post: bool = True) -> None:
        self.conn = conn
        self.auto_post = auto_post

    # ---------------------------------------------------------------- 账户

    def open_account(
        self,
        account_id: str,
        holder: str,
        balances: dict[str, int] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        balances = balances or {}
        payload = {"account_id": account_id, "holder": holder, "balances": balances}

        def do() -> dict[str, Any]:
            if self.conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone():
                raise ExchangeError("ACCOUNT_EXISTS", f"账户已存在：{account_id}", 409)
            self.conn.execute(
                "INSERT INTO accounts(account_id, holder) VALUES(?, ?)",
                (account_id, holder),
            )
            for asset, amount in balances.items():
                if amount < 0:
                    raise ExchangeError("INVALID_AMOUNT", "初始余额不能为负")
                self._upsert_balance(account_id, asset, amount, 0)
                self._ledger(account_id, asset, amount, 0, "DEPOSIT", f"OPEN:{account_id}")
            return {"account_id": account_id, "holder": holder, "balances": balances}

        return self._idempotent("OPEN_ACCOUNT", idempotency_key, lambda: payload, do)

    def deposit(
        self,
        account_id: str,
        asset: str,
        amount: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise ExchangeError("INVALID_AMOUNT", "入账金额必须为正")
        payload = {"account_id": account_id, "asset": asset, "amount": amount}

        def do() -> dict[str, Any]:
            self._require_account(account_id)
            self._upsert_balance(account_id, asset, amount, 0)
            self._ledger(account_id, asset, amount, 0, "DEPOSIT", None)
            return {**payload, "balance": self.get_balance(account_id, asset)}

        return self._idempotent("DEPOSIT", idempotency_key, lambda: payload, do)

    def get_balance(self, account_id: str, asset: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT available, frozen, version FROM balances WHERE account_id=? AND asset=?",
            (account_id, asset),
        ).fetchone()
        if row is None:
            return {"available": 0, "frozen": 0, "version": 0}
        return {"available": row["available"], "frozen": row["frozen"], "version": row["version"]}

    def list_balances(self, account_id: str) -> list[dict[str, Any]]:
        self._require_account(account_id)
        rows = self.conn.execute(
            "SELECT asset, available, frozen, version FROM balances WHERE account_id=? ORDER BY asset",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 下单

    def place_order(
        self,
        client_order_id: str,
        account_id: str,
        side: str,
        price: int,
        qty: int,
        ttl_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if side not in (_BUY, _SELL):
            raise ExchangeError("INVALID_SIDE", "side 必须是 BUY 或 SELL")
        if not isinstance(price, int) or price <= 0:
            raise ExchangeError("INVALID_PRICE", "单价必须为正整数")
        if not isinstance(qty, int) or qty <= 0:
            raise ExchangeError("INVALID_QTY", "数量必须为正整数")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ExchangeError("INVALID_TTL", "ttl_seconds 必须为正整数")
        payload = {
            "client_order_id": client_order_id,
            "account_id": account_id,
            "side": side,
            "price": price,
            "qty": qty,
            "ttl_seconds": ttl_seconds,
        }

        def do() -> dict[str, Any]:
            pair = self._require_pair_normal()
            self._require_account(account_id)
            if self.conn.execute(
                "SELECT 1 FROM orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone():
                raise ExchangeError("ORDER_EXISTS", f"订单编号已存在：{client_order_id}", 409)

            freeze_asset = QUOTE_ASSET if side == _BUY else BASE_ASSET
            freeze_amount = price * qty if side == _BUY else qty
            self._freeze(account_id, freeze_asset, freeze_amount)

            expires_at: str | None = None
            if ttl_seconds is not None:
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
                ).strftime("%Y-%m-%d %H:%M:%S")
            cur = self.conn.execute(
                "INSERT INTO orders(client_order_id, pair_code, account_id, side, price, orig_qty, expires_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (client_order_id, pair["pair_code"], account_id, side, price, qty, expires_at),
            )
            order_id = int(cur.lastrowid)
            self._ledger(
                account_id, freeze_asset, -freeze_amount, freeze_amount, "FREEZE", client_order_id
            )

            fills = self._match(order_id)
            # 买单按更优价格成交时，立即释放价差对应的冻结（部分成交同样处理），
            # 无论清算是否延迟过账，该金额都可从 PENDING 分录精确推导。
            reconcile_order_freeze(self.conn, order_id)
            if self.auto_post:
                # 与撮合同一事务：扣冻结、入可用、翻转清算状态。
                post_pending_clearing(self.conn)
            return self._order_response(client_order_id, fills, idempotent_replay=False)

        return self._idempotent("PLACE_ORDER", idempotency_key, lambda: payload, do)

    def cancel_order(
        self,
        client_order_id: str,
        account_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"client_order_id": client_order_id, "account_id": account_id}

        def do() -> dict[str, Any]:
            order = self._require_order(client_order_id)
            if order["account_id"] != account_id:
                raise ExchangeError("FORBIDDEN", "只能撤销本账户订单", 403)
            released = 0
            released_asset: str | None = None
            if order["status"] == _NEW:
                released_asset = QUOTE_ASSET if order["side"] == _BUY else BASE_ASSET
                # 只释放未成交部分；已成交部分（含延迟过账的 PENDING）仍由冻结覆盖。
                remaining = order["orig_qty"] - order["filled_qty"]
                released = remaining * order["price"] if order["side"] == _BUY else remaining
                if released:
                    self._release(account_id, released_asset, released)
                    self._ledger(
                        account_id, released_asset, released, -released, "CANCEL", client_order_id
                    )
                self.conn.execute(
                    "UPDATE orders SET status='CANCELLED', updated_at=datetime('now') WHERE id=?",
                    (order["id"],),
                )
            else:
                raise ExchangeError(
                    "ORDER_NOT_ACTIVE",
                    f"订单 {client_order_id} 当前状态 {order['status']}，无法撤销",
                    409,
                )
            return {
                "order": self._order_dto(client_order_id),
                "released": {"asset": released_asset, "amount": released},
                "idempotent_replay": False,
            }

        return self._idempotent("CANCEL_ORDER", idempotency_key, lambda: payload, do)

    def expire_orders(self, now: str | None = None) -> dict[str, Any]:
        """失效所有已到期的 NEW 订单并释放剩余冻结（可被管理命令反复调用）。"""
        now = now or _now_iso()
        expired: list[dict[str, Any]] = []
        with transaction(self.conn):
            rows = self.conn.execute(
                "SELECT id, client_order_id, account_id, side, price, orig_qty, filled_qty"
                " FROM orders WHERE status='NEW' AND expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            ).fetchall()
            for row in rows:
                remaining = row["orig_qty"] - row["filled_qty"]
                asset = QUOTE_ASSET if row["side"] == _BUY else BASE_ASSET
                released = remaining * row["price"] if row["side"] == _BUY else remaining
                if released:
                    self._release(row["account_id"], asset, released)
                    self._ledger(
                        row["account_id"], asset, released, -released, "EXPIRE", row["client_order_id"]
                    )
                self.conn.execute(
                    "UPDATE orders SET status='EXPIRED', updated_at=datetime('now') WHERE id=?",
                    (row["id"],),
                )
                expired.append(
                    {"client_order_id": row["client_order_id"], "released": released, "asset": asset}
                )
        return {"expired": expired, "count": len(expired)}

    # ---------------------------------------------------------------- 监管

    def suspend_pair(self, reason: str, operator: str) -> dict[str, Any]:
        if not reason:
            raise ExchangeError("INVALID_REASON", "暂停原因必填")
        with transaction(self.conn):
            self._require_pair(PAIR_CODE)
            self.conn.execute(
                "UPDATE trading_pairs SET status='SUSPENDED', suspended_at=datetime('now') WHERE pair_code=?",
                (PAIR_CODE,),
            )
            self.conn.execute(
                "INSERT INTO regulatory_actions(pair_code, action, reason, operator) VALUES(?, 'SUSPEND', ?, ?)",
                (PAIR_CODE, reason, operator),
            )
        return {"pair_code": PAIR_CODE, "status": "SUSPENDED", "reason": reason, "operator": operator}

    def resume_pair(self, reason: str, operator: str) -> dict[str, Any]:
        if not reason:
            raise ExchangeError("INVALID_REASON", "恢复说明必填")
        with transaction(self.conn):
            self._require_pair(PAIR_CODE)
            self.conn.execute(
                "UPDATE trading_pairs SET status='NORMAL', suspended_at=NULL WHERE pair_code=?",
                (PAIR_CODE,),
            )
            self.conn.execute(
                "INSERT INTO regulatory_actions(pair_code, action, reason, operator) VALUES(?, 'RESUME', ?, ?)",
                (PAIR_CODE, reason, operator),
            )
        return {"pair_code": PAIR_CODE, "status": "NORMAL", "reason": reason, "operator": operator}

    def pair_status(self) -> dict[str, Any]:
        return dict(self._require_pair(PAIR_CODE))

    # ---------------------------------------------------------------- 查询

    def get_order(self, client_order_id: str) -> dict[str, Any]:
        """返回订单剩余量与全部成交依据。"""
        order = self._order_dto(client_order_id)
        return {"order": order, "fills": self.list_trades(client_order_id)}

    def order_book(self, depth: int = 50) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT side, price, SUM(orig_qty - filled_qty) AS qty, COUNT(*) AS orders"
            " FROM orders WHERE status='NEW' GROUP BY side, price"
        ).fetchall()
        bids = sorted(
            (dict(r) for r in rows if r["side"] == _BUY), key=lambda r: -r["price"]
        )[:depth]
        asks = sorted(
            (dict(r) for r in rows if r["side"] == _SELL), key=lambda r: r["price"]
        )[:depth]
        return {"pair_code": PAIR_CODE, "bids": bids, "asks": asks}

    def list_trades(self, client_order_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT t.id AS trade_id, t.pair_code, t.price, t.qty, t.executed_at,"
            " taker.client_order_id AS taker_order_id, maker.client_order_id AS maker_order_id,"
            " taker.account_id AS taker_account, maker.account_id AS maker_account,"
            " (SELECT MIN(status) FROM clearing_entries WHERE trade_id=t.id) AS clearing_status"
            " FROM trades t"
            " JOIN orders taker ON taker.id=t.taker_order_id"
            " JOIN orders maker ON maker.id=t.maker_order_id"
        )
        params: tuple[Any, ...] = ()
        if client_order_id:
            sql += " WHERE taker.client_order_id=? OR maker.client_order_id=?"
            params = (client_order_id, client_order_id)
        sql += " ORDER BY t.id"
        trades = []
        for r in self.conn.execute(sql, params).fetchall():
            item = dict(r)
            item["basis"] = (
                f"价格时间优先：对手盘 {item['maker_order_id']} 在价格 {item['price']}"
                " 为盘口最优档位且同档最早"
            )
            trades.append(item)
        return trades

    # ---------------------------------------------------------------- 撮合

    def _match(self, taker_id: int) -> list[dict[str, Any]]:
        taker = self.conn.execute("SELECT * FROM orders WHERE id=?", (taker_id,)).fetchone()
        fills: list[dict[str, Any]] = []
        while True:
            remaining = taker["orig_qty"] - taker["filled_qty"]
            if remaining <= 0:
                break
            if taker["side"] == _BUY:
                # 最低卖价、同价最早；价格不得高于买价。
                sql = (
                    "SELECT * FROM orders WHERE status='NEW' AND side='SELL' AND price<=?"
                    " AND account_id<>? AND id<>? ORDER BY price ASC, id ASC LIMIT 1"
                )
            else:
                # 最高买价、同价最早；价格不得低于卖价。
                sql = (
                    "SELECT * FROM orders WHERE status='NEW' AND side='BUY' AND price>=?"
                    " AND account_id<>? AND id<>? ORDER BY price DESC, id ASC LIMIT 1"
                )
            maker = self.conn.execute(
                sql, (taker["price"], taker["account_id"], taker["id"])
            ).fetchone()
            if maker is None:
                break
            match_qty = min(remaining, maker["orig_qty"] - maker["filled_qty"])
            trade_id = self._record_trade(taker, maker, match_qty)
            fills.append(
                {
                    "trade_id": trade_id,
                    "price": maker["price"],
                    "qty": match_qty,
                    "counterparty_order_id": maker["client_order_id"],
                    "basis": (
                        f"价格时间优先：{maker['side']} 单 {maker['client_order_id']}"
                        f" 在价格 {maker['price']} 为盘口最优档位（同价最早，序号 {maker['id']}）"
                    ),
                }
            )
            taker = self.conn.execute("SELECT * FROM orders WHERE id=?", (taker_id,)).fetchone()
        return fills

    def _record_trade(self, taker: sqlite3.Row, maker: sqlite3.Row, qty: int) -> int:
        price = maker["price"]
        cost = price * qty
        cur = self.conn.execute(
            "INSERT INTO trades(pair_code, taker_order_id, maker_order_id, price, qty)"
            " VALUES(?, ?, ?, ?, ?)",
            (PAIR_CODE, taker["id"], maker["id"], price, qty),
        )
        trade_id = int(cur.lastrowid)

        if taker["side"] == _BUY:
            buyer, seller = taker, maker
        else:
            buyer, seller = maker, taker

        # 四条清算分录（先 PENDING，过账时翻 POSTED）：
        # 买方从冻结扣 B、收 A；卖方从冻结扣 A、收 B。
        entries = [
            (buyer["account_id"], QUOTE_ASSET, -cost, "DEBIT"),
            (buyer["account_id"], BASE_ASSET, qty, "CREDIT"),
            (seller["account_id"], BASE_ASSET, -qty, "DEBIT"),
            (seller["account_id"], QUOTE_ASSET, cost, "CREDIT"),
        ]
        for account_id, asset, amount, direction in entries:
            self.conn.execute(
                "INSERT INTO clearing_entries(trade_id, account_id, asset, amount, direction)"
                " VALUES(?, ?, ?, ?, ?)",
                (trade_id, account_id, asset, amount, direction),
            )

        self._bump_filled(buyer["id"], qty)
        self._bump_filled(seller["id"], qty)
        return trade_id

    def _bump_filled(self, order_id: int, qty: int) -> None:
        self.conn.execute(
            "UPDATE orders SET filled_qty=filled_qty+?,"
            " status=CASE WHEN filled_qty+?>=orig_qty THEN 'FILLED' ELSE status END,"
            " updated_at=datetime('now') WHERE id=?",
            (qty, qty, order_id),
        )

    # ---------------------------------------------------------------- 余额原语

    def _freeze(self, account_id: str, asset: str, amount: int) -> None:
        cur = self.conn.execute(
            "UPDATE balances SET available=available-?, frozen=frozen+?, version=version+1"
            " WHERE account_id=? AND asset=? AND available>=?",
            (amount, amount, account_id, asset, amount),
        )
        if cur.rowcount == 0:
            raise ExchangeError(
                "INSUFFICIENT_AVAILABLE",
                f"可用额度不足：账户 {account_id} 积分 {asset} 需冻结 {amount}",
                422,
            )

    def _release(self, account_id: str, asset: str, amount: int) -> None:
        cur = self.conn.execute(
            "UPDATE balances SET frozen=frozen-?, available=available+?, version=version+1"
            " WHERE account_id=? AND asset=? AND frozen>=?",
            (amount, amount, account_id, asset, amount),
        )
        if cur.rowcount == 0:
            raise ExchangeError("INCONSISTENT_FROZEN", f"冻结额度异常，无法释放 {amount} {asset}")

    def _upsert_balance(
        self, account_id: str, asset: str, available_delta: int, frozen_delta: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO balances(account_id, asset, available, frozen, version)"
            " VALUES(?, ?, ?, ?, 1)"
            " ON CONFLICT(account_id, asset) DO UPDATE SET"
            " available=balances.available+excluded.available,"
            " frozen=balances.frozen+excluded.frozen, version=balances.version+1",
            (account_id, asset, available_delta, frozen_delta),
        )

    def _ledger(
        self,
        account_id: str,
        asset: str,
        available_delta: int,
        frozen_delta: int,
        ref_type: str,
        ref_id: str | None,
    ) -> None:
        row = self.conn.execute(
            "SELECT available, frozen FROM balances WHERE account_id=? AND asset=?",
            (account_id, asset),
        ).fetchone()
        if row is None:
            raise ExchangeError("BALANCE_NOT_FOUND", f"缺少余额行：{account_id}/{asset}")
        if row["available"] < 0 or row["frozen"] < 0:
            raise ExchangeError("NEGATIVE_BALANCE", f"余额被扣成负数：{account_id}/{asset}")
        self.conn.execute(
            "INSERT INTO ledger_entries(account_id, asset, amount, frozen_delta, ref_type, ref_id,"
            " balance_after, frozen_after) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                asset,
                available_delta,
                frozen_delta,
                ref_type,
                ref_id,
                row["available"],
                row["frozen"],
            ),
        )

    # ---------------------------------------------------------------- 幂等

    def _idempotent(
        self,
        request_type: str,
        key: str | None,
        build_payload: Callable[[], dict[str, Any]],
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if key is None:
            with transaction(self.conn):
                return action()
        request_hash = hashlib.sha256(
            json.dumps(build_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT request_hash, response_json FROM idempotent_requests"
                " WHERE request_type=? AND idempotency_key=?",
                (request_type, key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ExchangeError("IDEMPOTENCY_CONFLICT", "相同幂等键对应不同请求内容", 409)
                replay = json.loads(existing["response_json"])
                replay["idempotent_replay"] = True
                return replay
            result = action()
            result["idempotent_replay"] = False
            self.conn.execute(
                "INSERT INTO idempotent_requests(request_type, idempotency_key, request_hash, response_json)"
                " VALUES(?, ?, ?, ?)",
                (request_type, key, request_hash, json.dumps(result, ensure_ascii=False, sort_keys=True)),
            )
            return result

    # ---------------------------------------------------------------- 辅助

    def _require_account(self, account_id: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone() is None:
            raise ExchangeError("ACCOUNT_NOT_FOUND", f"账户不存在：{account_id}", 404)

    def _require_pair(self, pair_code: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM trading_pairs WHERE pair_code=?", (pair_code,)
        ).fetchone()
        if row is None:
            raise ExchangeError("PAIR_NOT_FOUND", f"交易对不存在：{pair_code}", 404)
        return row

    def _require_pair_normal(self) -> sqlite3.Row:
        pair = self._require_pair(PAIR_CODE)
        if pair["status"] != "NORMAL":
            raise ExchangeError(
                "PAIR_SUSPENDED",
                f"交易对 {PAIR_CODE} 已被监管暂停，拒绝新单与撮合（可撤单、可过账清算）",
                423,
            )
        return pair

    def _require_order(self, client_order_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        if row is None:
            raise ExchangeError("ORDER_NOT_FOUND", f"订单不存在：{client_order_id}", 404)
        return row

    def _order_dto(self, client_order_id: str) -> dict[str, Any]:
        row = self._require_order(client_order_id)
        return {
            "client_order_id": row["client_order_id"],
            "account_id": row["account_id"],
            "pair_code": row["pair_code"],
            "side": row["side"],
            "price": row["price"],
            "orig_qty": row["orig_qty"],
            "filled_qty": row["filled_qty"],
            "remaining_qty": row["orig_qty"] - row["filled_qty"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
        }

    def _order_response(
        self, client_order_id: str, fills: list[dict[str, Any]], idempotent_replay: bool
    ) -> dict[str, Any]:
        return {
            "order": self._order_dto(client_order_id),
            "fills": fills,
            "idempotent_replay": idempotent_replay,
        }
