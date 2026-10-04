"""撮合、冻结、部分成交与幂等的核心回归测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_exchange import Exchange, connect, init_db
from credit_exchange.clearing import audit_freezes, clearing_status
from credit_exchange.errors import ExchangeError
from credit_exchange.schema import BASE_ASSET, QUOTE_ASSET


class ExchangeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.ex = Exchange(self.conn)
        self.ex.open_account("acc-maker", "做市账户", {BASE_ASSET: 1000, QUOTE_ASSET: 1000})
        self.ex.open_account("acc-taker", "吃单账户", {BASE_ASSET: 1000, QUOTE_ASSET: 1000})

    def tearDown(self) -> None:
        self.conn.close()

    def test_freeze_on_place_and_full_fill(self) -> None:
        self.ex.place_order("ask-1", "acc-maker", "SELL", 10, 5)
        # 卖单冻结 A，可用减少、冻结增加，总量不变。
        self.assertEqual(
            self.ex.get_balance("acc-maker", BASE_ASSET),
            {"available": 995, "frozen": 5, "version": 2},
        )
        resp = self.ex.place_order("bid-1", "acc-taker", "BUY", 10, 5)
        self.assertEqual(resp["order"]["status"], "FILLED")
        self.assertEqual(resp["order"]["remaining_qty"], 0)
        self.assertEqual(len(resp["fills"]), 1)
        self.assertEqual(resp["fills"][0]["qty"], 5)
        self.assertIn("价格时间优先", resp["fills"][0]["basis"])
        # 钱货两清：买方出 B 收 A，卖方出 A 收 B，冻结清零。
        self.assertEqual(self.ex.get_balance("acc-taker", BASE_ASSET)["available"], 1005)
        self.assertEqual(self.ex.get_balance("acc-taker", QUOTE_ASSET)["available"], 950)
        self.assertEqual(self.ex.get_balance("acc-maker", BASE_ASSET)["available"], 995)
        self.assertEqual(self.ex.get_balance("acc-maker", QUOTE_ASSET)["available"], 1050)
        for acc in ("acc-maker", "acc-taker"):
            for asset in (BASE_ASSET, QUOTE_ASSET):
                self.assertEqual(self.ex.get_balance(acc, asset)["frozen"], 0)
        self.assertTrue(audit_freezes(self.conn)["consistent"])

    def test_partial_fill_then_cancel_releases_remaining(self) -> None:
        # 挂单卖 4 个，吃单买 10 个：吃单部分成交后剩 6 个挂在盘口。
        self.ex.place_order("ask-small", "acc-maker", "SELL", 10, 4)
        resp = self.ex.place_order("bid-big", "acc-taker", "BUY", 10, 10)
        self.assertEqual(resp["order"]["status"], "NEW")
        self.assertEqual(resp["order"]["filled_qty"], 4)
        self.assertEqual(resp["order"]["remaining_qty"], 6)
        # 卖方全部成交冻结清零；买方剩余 6 个 B 冻结（6*10）。
        self.assertEqual(self.ex.get_balance("acc-maker", BASE_ASSET)["frozen"], 0)
        self.assertEqual(self.ex.get_balance("acc-taker", QUOTE_ASSET)["frozen"], 60)

        cancelled = self.ex.cancel_order("bid-big", "acc-taker")
        self.assertEqual(cancelled["released"], {"asset": QUOTE_ASSET, "amount": 60})
        self.assertEqual(cancelled["order"]["status"], "CANCELLED")
        self.assertEqual(self.ex.get_balance("acc-taker", QUOTE_ASSET)["frozen"], 0)
        self.assertEqual(self.ex.get_balance("acc-taker", QUOTE_ASSET)["available"], 1000 - 40)
        # 重复撤单必须报错，不能再释放一次。
        with self.assertRaises(ExchangeError) as ctx:
            self.ex.cancel_order("bid-big", "acc-taker")
        self.assertEqual(ctx.exception.code, "ORDER_NOT_ACTIVE")
        self.assertEqual(self.ex.get_balance("acc-taker", QUOTE_ASSET)["available"], 960)
        self.assertTrue(audit_freezes(self.conn)["consistent"])

    def test_insufficient_available_rejects_without_side_effect(self) -> None:
        with self.assertRaises(ExchangeError) as ctx:
            self.ex.place_order("bid-poor", "acc-taker", "BUY", 100, 1000)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_AVAILABLE")
        self.assertEqual(clearing_status(self.conn)["posted_entry_count"], 0)
        self.assertEqual(self.ex.order_book()["asks"], [])
        self.assertEqual(self.ex.order_book()["bids"], [])

    def test_price_time_priority(self) -> None:
        # 三个卖单：同价 10 的按时间优先，另有更优价 9。
        self.ex.place_order("ask-a", "acc-maker", "SELL", 10, 3)
        self.ex.place_order("ask-b", "acc-maker", "SELL", 10, 2)
        self.ex.place_order("ask-c", "acc-maker", "SELL", 9, 2)
        resp = self.ex.place_order("bid-x", "acc-taker", "BUY", 10, 5)
        counterparties = [f["counterparty_order_id"] for f in resp["fills"]]
        # 先吃价格 9 的 ask-c，再吃同价 10 中最早的 ask-a。
        self.assertEqual(counterparties, ["ask-c", "ask-a"])
        self.assertEqual([f["price"] for f in resp["fills"]], [9, 10])

    def test_price_improvement_releases_extra_freeze(self) -> None:
        self.ex.place_order("ask-low", "acc-maker", "SELL", 8, 10)
        resp = self.ex.place_order("bid-high", "acc-taker", "BUY", 10, 10)
        self.assertEqual(resp["fills"][0]["price"], 8)
        # 按更优价成交，多冻结的 20 立即回到可用。
        bal = self.ex.get_balance("acc-taker", QUOTE_ASSET)
        self.assertEqual(bal["frozen"], 0)
        self.assertEqual(bal["available"], 920)
        self.assertTrue(audit_freezes(self.conn)["consistent"])

    def test_idempotent_place_order_network_retry(self) -> None:
        self.ex.place_order("ask-1", "acc-maker", "SELL", 10, 5)
        first = self.ex.place_order("bid-1", "acc-taker", "BUY", 10, 5, idempotency_key="req-1")
        # 网络重试：相同幂等键返回首次结果，不产生第二笔成交。
        retry = self.ex.place_order("bid-1", "acc-taker", "BUY", 10, 5, idempotency_key="req-1")
        self.assertTrue(retry["idempotent_replay"])
        self.assertFalse(first["idempotent_replay"])
        self.assertEqual(retry["fills"], first["fills"])
        self.assertEqual(len(self.ex.list_trades()), 1)
        # 相同键不同内容必须拒绝。
        with self.assertRaises(ExchangeError) as ctx:
            self.ex.place_order("bid-1", "acc-taker", "BUY", 11, 5, idempotency_key="req-1")
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.ex.list_trades()), 1)

    def test_idempotent_cancel_retry(self) -> None:
        self.ex.place_order("ask-1", "acc-maker", "SELL", 10, 5)
        first = self.ex.cancel_order("ask-1", "acc-maker", idempotency_key="cancel-1")
        retry = self.ex.cancel_order("ask-1", "acc-maker", idempotency_key="cancel-1")
        self.assertTrue(retry["idempotent_replay"])
        self.assertEqual(first["released"], retry["released"])
        self.assertEqual(self.ex.get_balance("acc-maker", BASE_ASSET)["available"], 1000)

    def test_self_cross_is_blocked(self) -> None:
        self.ex.place_order("ask-self", "acc-maker", "SELL", 10, 5)
        resp = self.ex.place_order("bid-self", "acc-maker", "BUY", 10, 5)
        self.assertEqual(resp["fills"], [])
        self.assertEqual(resp["order"]["status"], "NEW")

    def test_expire_releases_remaining_freeze(self) -> None:
        self.ex.place_order("ask-ttl", "acc-maker", "SELL", 10, 8, ttl_seconds=60)
        self.ex.place_order("bid-now", "acc-taker", "BUY", 10, 3)
        bal_during = self.ex.get_balance("acc-maker", BASE_ASSET)
        self.assertEqual(bal_during["frozen"], 5)
        # 未到期：什么都不做。
        self.assertEqual(self.ex.expire_orders("2000-01-01 00:00:00")["count"], 0)
        result = self.ex.expire_orders("2099-01-01 00:00:00")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["expired"][0]["released"], 5)
        order = self.ex.get_order("ask-ttl")["order"]
        self.assertEqual(order["status"], "EXPIRED")
        self.assertEqual(order["remaining_qty"], 5)
        # 重复失效不产生第二次释放。
        self.assertEqual(self.ex.expire_orders("2099-01-01 00:00:00")["count"], 0)
        self.assertEqual(self.ex.get_balance("acc-maker", BASE_ASSET)["frozen"], 0)
        self.assertTrue(audit_freezes(self.conn)["consistent"])

    def test_suspension_blocks_new_orders_allows_cancel_and_clearing(self) -> None:
        self.ex.place_order("ask-1", "acc-maker", "SELL", 10, 5)
        self.ex.suspend_pair("监管核查", "监管审计员-张")
        with self.assertRaises(ExchangeError) as ctx:
            self.ex.place_order("bid-1", "acc-taker", "BUY", 10, 5)
        self.assertEqual(ctx.exception.code, "PAIR_SUSPENDED")
        # 暂停期间仍可撤单，冻结精确释放。
        cancelled = self.ex.cancel_order("ask-1", "acc-maker")
        self.assertEqual(cancelled["released"]["amount"], 5)
        self.assertEqual(self.ex.get_balance("acc-maker", BASE_ASSET)["frozen"], 0)
        self.ex.resume_pair("核查结束", "监管审计员-张")
        self.ex.place_order("ask-2", "acc-maker", "SELL", 10, 5)
        resp = self.ex.place_order("bid-2", "acc-taker", "BUY", 10, 5)
        self.assertEqual(resp["order"]["status"], "FILLED")


if __name__ == "__main__":
    unittest.main()
