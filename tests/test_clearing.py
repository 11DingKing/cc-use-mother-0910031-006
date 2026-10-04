"""延迟清算、重放幂等与账实核对测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_exchange import Exchange, connect, init_db
from credit_exchange.clearing import audit_freezes, clearing_status, post_pending_clearing
from credit_exchange.errors import ExchangeError
from credit_exchange.schema import BASE_ASSET, QUOTE_ASSET


class DeferredClearingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # auto_post=False：成交与 PENDING 分录提交，但余额过账延迟，
        # 模拟过账前宕机，交由管理命令重放。
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.ex = Exchange(self.conn, auto_post=False)
        self.ex.open_account("acc-maker", "做市账户", {BASE_ASSET: 1000, QUOTE_ASSET: 1000})
        self.ex.open_account("acc-taker", "吃单账户", {BASE_ASSET: 1000, QUOTE_ASSET: 1000})

    def tearDown(self) -> None:
        self.conn.close()

    def test_pending_clearing_then_replay_posts_once(self) -> None:
        self.ex.place_order("ask-1", "acc-maker", "SELL", 10, 5)
        self.ex.place_order("bid-1", "acc-taker", "BUY", 10, 5)

        status = clearing_status(self.conn)
        self.assertEqual(status["pending_trade_count"], 1)
        self.assertEqual(status["pending_entry_count"], 4)
        # 过账前：冻结仍覆盖成交义务，可用未增加。
        self.assertEqual(self.ex.get_balance("acc-maker", BASE_ASSET)["frozen"], 5)
        self.assertEqual(self.ex.get_balance("acc-taker", QUOTE_ASSET)["frozen"], 50)
        self.assertEqual(self.ex.get_balance("acc-maker", QUOTE_ASSET)["available"], 1000)
        self.assertTrue(audit_freezes(self.conn)["consistent"])

        ledger_before = self.conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
        first = post_pending_clearing(self.conn)
        self.assertEqual(first["posted_trade_ids"], [1])
        ledger_after = self.conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
        self.assertEqual(ledger_after - ledger_before, 4)

        # 钱货两清。
        self.assertEqual(self.ex.get_balance("acc-maker", QUOTE_ASSET)["available"], 1050)
        self.assertEqual(self.ex.get_balance("acc-taker", BASE_ASSET)["available"], 1005)

        # 管理命令重放：不得产生第二笔交易/第二次扣账。
        replay = post_pending_clearing(self.conn)
        self.assertEqual(replay["posted_count"], 0)
        self.assertEqual(clearing_status(self.conn)["pending_trade_count"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM clearing_entries").fetchone()[0], 4
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0], ledger_after
        )
        self.assertEqual(self.ex.get_balance("acc-maker", QUOTE_ASSET)["available"], 1050)
        self.assertTrue(audit_freezes(self.conn)["consistent"])

    def test_partial_fill_cancel_then_replay(self) -> None:
        self.ex.place_order("ask-big", "acc-maker", "SELL", 10, 10)
        resp = self.ex.place_order("bid-part", "acc-taker", "BUY", 10, 4)
        # 买单 4 个全部成交，卖单剩余 6 个。
        self.assertEqual(resp["order"]["remaining_qty"], 0)
        self.assertEqual(self.ex.get_order("ask-big")["order"]["remaining_qty"], 6)
        # 撤掉卖方剩余量；已成交部分仍由 PENDING 冻结覆盖。
        self.ex.cancel_order("ask-big", "acc-maker")
        self.assertTrue(audit_freezes(self.conn)["consistent"])
        # 卖方冻结 4（待清算），买方冻结 40（全额成交待过账）。
        self.assertEqual(self.ex.get_balance("acc-maker", BASE_ASSET)["frozen"], 4)
        self.assertEqual(self.ex.get_balance("acc-taker", QUOTE_ASSET)["frozen"], 40)

        post_pending_clearing(self.conn)
        self.assertEqual(self.ex.get_balance("acc-maker", BASE_ASSET)["frozen"], 0)
        self.assertEqual(self.ex.get_balance("acc-taker", QUOTE_ASSET)["frozen"], 0)
        self.assertEqual(self.ex.get_balance("acc-maker", QUOTE_ASSET)["available"], 1040)
        self.assertEqual(self.ex.get_balance("acc-taker", BASE_ASSET)["available"], 1004)
        self.assertTrue(audit_freezes(self.conn)["consistent"])

    def test_price_improvement_under_deferred_clearing(self) -> None:
        self.ex.place_order("ask-low", "acc-maker", "SELL", 8, 10)
        self.ex.place_order("bid-high", "acc-taker", "BUY", 10, 10)
        # 价差 20 立即释放，成交金额 80 保持冻结等过账。
        bal = self.ex.get_balance("acc-taker", QUOTE_ASSET)
        self.assertEqual(bal["frozen"], 80)
        self.assertEqual(bal["available"], 920)
        self.assertTrue(audit_freezes(self.conn)["consistent"])
        post_pending_clearing(self.conn)
        bal = self.ex.get_balance("acc-taker", QUOTE_ASSET)
        self.assertEqual((bal["available"], bal["frozen"]), (920, 0))

    def test_suspend_then_replay_still_works(self) -> None:
        self.ex.place_order("ask-1", "acc-maker", "SELL", 10, 5)
        self.ex.place_order("bid-1", "acc-taker", "BUY", 10, 5)
        self.ex.suspend_pair("监管核查", "监管审计员")
        # 监管暂停不阻断未完成清算的重放过账。
        result = post_pending_clearing(self.conn)
        self.assertEqual(result["posted_count"], 1)
        self.assertTrue(audit_freezes(self.conn)["consistent"])
        with self.assertRaises(ExchangeError) as ctx:
            self.ex.place_order("bid-2", "acc-taker", "BUY", 10, 5)
        self.assertEqual(ctx.exception.code, "PAIR_SUSPENDED")


if __name__ == "__main__":
    unittest.main()
