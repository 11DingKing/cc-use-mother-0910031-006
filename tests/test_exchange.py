"""撮合清算核心回归测试。

覆盖契约四条不变量：
1. 订单冻结额度：下单冻结/成交释放/撤单释放/暂停释放，冻结恒等于活动订单持有；
2. 价格时间优先：最优价、同价按时间；
3. 成交清算原子性：撮合同事务扣减，总额守恒；
4. 幂等重放恢复：request_id 重试不产生第二笔，管理命令重放修复未完成清算。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_exchange import (  # noqa: E402
    ExchangeService,
    IdempotencyKeyReused,
    InsufficientBalance,
    InvalidRequest,
    MarketHalted,
    connect,
    init_schema,
)


def make_service(path: str = ":memory:") -> ExchangeService:
    conn = connect(path)
    init_schema(conn)
    svc = ExchangeService(conn)
    svc.create_account("A", "买方")
    svc.create_account("B", "卖方")
    svc.create_account("C", "旁观")
    svc.issue("i-a-base", "A", "BASE", 100_000)
    svc.issue("i-a-quote", "A", "QUOTE", 100_000)
    svc.issue("i-b-base", "B", "BASE", 100_000)
    svc.issue("i-b-quote", "B", "QUOTE", 100_000)
    svc.issue("i-c-base", "C", "BASE", 100_000)
    svc.issue("i-c-quote", "C", "QUOTE", 100_000)
    svc.create_market("BASE/QUOTE", "BASE", "QUOTE")
    return svc


class MatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_full_match_freezes_and_settles_in_one_go(self) -> None:
        sell = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 30)
        buy = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 10, 30)

        self.assertEqual(sell["status"], "NEW")  # 挂单时不成交
        self.assertEqual(buy["status"], "FILLED")
        self.assertEqual(buy["remaining_qty"], 0)
        self.assertEqual(len(buy["fills"]), 1)
        fill = buy["fills"][0]
        self.assertEqual(fill["trade_id"], f"T:{buy['order_id']}:{sell['order_id']}")
        self.assertEqual(fill["counterparty_account"], "B")
        self.assertEqual(fill["qty"], 30)

        # 卖方：货被收走、收到对价；冻结全部释放
        b_base = self.svc.get_balance("B", "BASE")
        b_quote = self.svc.get_balance("B", "QUOTE")
        self.assertEqual((b_base["total"], b_base["frozen"], b_base["available"]), (99_970, 0, 99_970))
        self.assertEqual((b_quote["total"], b_quote["frozen"], b_quote["available"]), (100_300, 0, 100_300))
        # 买方：收到货、付对价
        a_base = self.svc.get_balance("A", "BASE")
        a_quote = self.svc.get_balance("A", "QUOTE")
        self.assertEqual((a_base["total"], a_base["frozen"]), (100_030, 0))
        self.assertEqual((a_quote["total"], a_quote["frozen"]), (99_700, 0))

        sell_after = self.svc.get_order(sell["order_id"])
        self.assertEqual(sell_after["status"], "FILLED")
        self.assert_no_drift()

    def test_partial_fill_then_cancel_restores_available(self) -> None:
        sell = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 40)
        buy = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 10, 15)
        self.assertEqual(buy["status"], "FILLED")
        self.assertEqual(sell["order_id"], self.svc.get_order(sell["order_id"])["order_id"])
        sell_view = self.svc.get_order(sell["order_id"])
        self.assertEqual(sell_view["status"], "PARTIAL")
        self.assertEqual(sell_view["filled_qty"], 15)
        self.assertEqual(sell_view["remaining_qty"], 25)

        # 已成交 15 件释放冻结，剩余 25 件仍冻结
        bal = self.svc.get_balance("B", "BASE")
        self.assertEqual(bal["total"], 99_985)
        self.assertEqual(bal["frozen"], 25)
        self.assertEqual(bal["available"], 99_960)

        cancelled = self.svc.cancel_order("c1", sell["order_id"], "B")
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["remaining_qty"], 25)
        bal2 = self.svc.get_balance("B", "BASE")
        self.assertEqual((bal2["total"], bal2["frozen"], bal2["available"]), (99_985, 0, 99_985))
        self.assert_no_drift()

        # 已终态订单不能再撤
        with self.assertRaises(InvalidRequest):
            self.svc.cancel_order("c2", sell["order_id"], "B")

    def test_buy_order_freezes_notional_and_cancel_releases(self) -> None:
        buy = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 12, 10)
        bal = self.svc.get_balance("A", "QUOTE")
        self.assertEqual((bal["frozen"], bal["available"]), (120, 99_880))
        self.svc.cancel_order("c1", buy["order_id"], "A")
        bal2 = self.svc.get_balance("A", "QUOTE")
        self.assertEqual((bal2["frozen"], bal2["available"]), (0, 100_000))

    def test_insufficient_available_balance_rejects_without_residue(self) -> None:
        with self.assertRaises(InsufficientBalance):
            self.svc.place_order("x1", "BASE/QUOTE", "B", "BUY", 100, 1_001)
        # 失败后无订单、无冻结残留
        self.assertEqual(self.svc.order_book("BASE/QUOTE")["asks"], [])
        self.assertEqual(self.svc.order_book("BASE/QUOTE")["bids"], [])
        self.assert_no_drift()

    def test_price_time_priority(self) -> None:
        # 三笔卖单：11 元靠后、10 元两笔按时间排序
        s_late = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 11, 100)
        s_first = self.svc.place_order("s2", "BASE/QUOTE", "B", "SELL", 10, 20)
        s_second = self.svc.place_order("s3", "BASE/QUOTE", "B", "SELL", 10, 30)

        buy = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 11, 40)
        fills = buy["fills"]
        self.assertEqual([f["counter_order_id"] for f in fills],
                         [s_first["order_id"], s_second["order_id"]])
        self.assertEqual([f["price"] for f in fills], [10, 10])
        # 吃单限额 11，但更优的 10 元先成交；11 元卖单不动
        self.assertEqual(self.svc.get_order(s_late["order_id"])["filled_qty"], 0)

    def test_self_trade_is_blocked(self) -> None:
        self.svc.place_order("s1", "BASE/QUOTE", "A", "SELL", 10, 10)
        buy = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 10, 10)
        self.assertEqual(buy["status"], "NEW")
        self.assertEqual(buy["fills"], [])

    def assert_no_drift(self) -> None:
        drift = self.svc.replay_clearing(dry_run=True)["freeze_drift"]
        self.assertEqual(drift, [])


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_halt_releases_all_freezes_and_rejects_new_orders(self) -> None:
        sell = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 40)
        buy_resting = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 9, 20)
        result = self.svc.halt_market("h1", "BASE/QUOTE")
        self.assertGreaterEqual(len(result["released"]), 2)

        self.assertEqual(self.svc.get_order(sell["order_id"])["status"], "HALTED")
        self.assertEqual(self.svc.get_order(buy_resting["order_id"])["status"], "HALTED")
        self.assertEqual(self.svc.get_balance("B", "BASE")["frozen"], 0)
        self.assertEqual(self.svc.get_balance("A", "QUOTE")["frozen"], 0)

        with self.assertRaises(MarketHalted):
            self.svc.place_order("s9", "BASE/QUOTE", "B", "SELL", 10, 1)
        # 暂停期间撮合也不能发生（买卖单都被拒），恢复后正常
        self.svc.resume_market("r1", "BASE/QUOTE")
        again = self.svc.place_order("s2", "BASE/QUOTE", "B", "SELL", 10, 5)
        self.assertEqual(again["status"], "NEW")

    def test_expire_releases_remaining_freeze(self) -> None:
        sell = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 40)
        self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 10, 15)
        out = self.svc.expire_order("e1", sell["order_id"])
        self.assertEqual(out["status"], "EXPIRED")
        bal = self.svc.get_balance("B", "BASE")
        self.assertEqual((bal["total"], bal["frozen"]), (99_985, 0))

    def test_cancel_rejects_other_account(self) -> None:
        sell = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 10)
        with self.assertRaises(InvalidRequest):
            self.svc.cancel_order("c1", sell["order_id"], "C")


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_retry_same_request_returns_cached_order_and_no_second_trade(self) -> None:
        first = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 30)
        # 网络重试：同一 request_id 必须返回同一订单，不重复挂单
        retry = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 30)
        self.assertEqual(retry["order_id"], first["order_id"])

        buy = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 10, 30)
        buy_retry = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 10, 30)
        self.assertEqual(buy_retry["order_id"], buy["order_id"])
        self.assertEqual(len(buy_retry["fills"]), 1)

        trades = self.svc.conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
        orders = self.svc.conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        self.assertEqual((trades, orders), (1, 2))

    def test_same_request_id_with_different_payload_rejected(self) -> None:
        self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 30)
        with self.assertRaises(IdempotencyKeyReused):
            self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 11, 30)

    def test_cancel_retry_is_idempotent(self) -> None:
        sell = self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 30)
        r1 = self.svc.cancel_order("c1", sell["order_id"], "B")
        r2 = self.svc.cancel_order("c1", sell["order_id"], "B")
        self.assertEqual(r1["order_id"], r2["order_id"])
        self.assertEqual(self.svc.get_balance("B", "BASE")["frozen"], 0)

    def test_issue_retry_does_not_double_credit(self) -> None:
        self.svc.issue("dup-1", "C", "BASE", 500)
        self.svc.issue("dup-1", "C", "BASE", 500)
        self.assertEqual(self.svc.get_balance("C", "BASE")["total"], 100_500)


class ReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.svc.place_order("s1", "BASE/QUOTE", "B", "SELL", 10, 40)
        self.buy = self.svc.place_order("b1", "BASE/QUOTE", "A", "BUY", 10, 30)
        self.trade_id = self.buy["fills"][0]["trade_id"]

    def test_replay_clean_system_is_noop(self) -> None:
        r1 = self.svc.replay_clearing()
        self.assertEqual(r1["trades_scanned"], 1)
        self.assertEqual(r1["entries_repaired"], [])
        self.assertEqual(r1["new_trades_created"], 0)
        r2 = self.svc.replay_clearing()
        self.assertEqual(r2["entries_repaired"], [])
        trades = self.svc.conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
        self.assertEqual(trades, 1)

    def test_replay_repairs_unposted_entries_once(self) -> None:
        conn = self.svc.conn
        # 模拟崩溃：分录已生成但未过账，余额影响也未发生
        rows = conn.execute(
            "SELECT account_id, asset, direction, amount FROM clearing_entries WHERE trade_id=?",
            (self.trade_id,),
        ).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        for r in rows:  # 回滚已发生的余额变动
            if r["direction"] == "DEBIT":
                conn.execute("UPDATE balances SET total_bal=total_bal+? WHERE account_id=? AND asset=?",
                             (r["amount"], r["account_id"], r["asset"]))
            else:
                conn.execute("UPDATE balances SET total_bal=total_bal-? WHERE account_id=? AND asset=?",
                             (r["amount"], r["account_id"], r["asset"]))
        conn.execute("UPDATE clearing_entries SET posted=0, posted_at=NULL WHERE trade_id=?",
                     (self.trade_id,))
        conn.commit()

        broken = self.svc.get_balance("B", "QUOTE")
        self.assertEqual(broken["total"], 100_000)  # 对价未入账

        r1 = self.svc.replay_clearing()
        self.assertEqual(len(r1["entries_repaired"]), 4)
        self.assertEqual(
            self.svc.get_balance("B", "QUOTE")["total"], 100_300
        )
        # 再重放：全部已过账，绝不产生第二笔
        r2 = self.svc.replay_clearing()
        self.assertEqual(r2["entries_repaired"], [])
        self.assertEqual(self.svc.get_balance("B", "QUOTE")["total"], 100_300)
        self.assertEqual(
            self.svc.conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"], 1
        )

    def test_replay_recreates_missing_entries(self) -> None:
        conn = self.svc.conn
        rows = conn.execute(
            "SELECT account_id, asset, direction, amount FROM clearing_entries WHERE trade_id=?",
            (self.trade_id,),
        ).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM clearing_entries WHERE trade_id=?", (self.trade_id,))
        for r in rows:
            if r["direction"] == "DEBIT":
                conn.execute("UPDATE balances SET total_bal=total_bal+? WHERE account_id=? AND asset=?",
                             (r["amount"], r["account_id"], r["asset"]))
            else:
                conn.execute("UPDATE balances SET total_bal=total_bal-? WHERE account_id=? AND asset=?",
                             (r["amount"], r["account_id"], r["asset"]))
        conn.commit()

        r = self.svc.replay_clearing()
        reasons = {e["reason"] for e in r["entries_repaired"]}
        self.assertEqual(reasons, {"MISSING_ENTRY"})
        self.assertEqual(len(r["entries_repaired"]), 4)
        self.assertEqual(self.svc.get_balance("A", "BASE")["total"], 100_030)
        # 二次重放零修复
        self.assertEqual(self.svc.replay_clearing()["entries_repaired"], [])

    def test_dry_run_changes_nothing(self) -> None:
        conn = self.svc.conn
        conn.execute("UPDATE clearing_entries SET posted=0")
        before = conn.execute("SELECT COUNT(*) AS c FROM clearing_entries WHERE posted=1").fetchone()["c"]
        r = self.svc.replay_clearing(dry_run=True)
        after = conn.execute("SELECT COUNT(*) AS c FROM clearing_entries WHERE posted=1").fetchone()["c"]
        self.assertEqual(before, after)
        self.assertTrue(r["dry_run"])


class ConcurrencyTest(unittest.TestCase):
    """多连接并发：文件级数据库 + BEGIN IMMEDIATE 串行化。"""

    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = connect(self.path)
        init_schema(conn)
        svc = ExchangeService(conn)
        svc.create_market("BASE/QUOTE", "BASE", "QUOTE")
        conn.commit()
        conn.close()
        self.errors: list[BaseException] = []

    def tearDown(self) -> None:
        for ext in ("", "-wal", "-shm"):
            p = Path(self.path + ext)
            if p.exists():
                p.unlink()

    def _service(self) -> ExchangeService:
        return ExchangeService(connect(self.path))

    def test_parallel_orders_never_oversettle(self) -> None:
        # 20 个账户：一半挂卖单，一半并发吃单
        svc = self._service()
        for i in range(20):
            acc = f"P{i:02d}"
            svc.create_account(acc)
            svc.issue(f"iss-{acc}-base", acc, "BASE", 1_000)
            svc.issue(f"iss-{acc}-quote", acc, "QUOTE", 1_000_000)
        del svc

        barrier = threading.Barrier(20)

        def worker(i: int) -> None:
            s = self._service()
            try:
                acc = f"P{i:02d}"
                barrier.wait()
                if i % 2 == 0:
                    s.place_order(f"sell-{i}", "BASE/QUOTE", acc, "SELL", 10 + i % 3, 100)
                else:
                    s.place_order(f"buy-{i}", "BASE/QUOTE", acc, "BUY", 20, 50)
            except BaseException as exc:  # noqa: BLE001
                self.errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.errors, [])

        svc = self._service()
        # 守恒：每种积分总量 = 初始发行总量
        for asset, issued in (("BASE", 20_000), ("QUOTE", 20_000_000)):
            total = svc.conn.execute(
                "SELECT COALESCE(SUM(total_bal),0) AS s FROM balances WHERE asset=?", (asset,)
            ).fetchone()["s"]
            self.assertEqual(total, issued, f"{asset} 总额不守恒")
        # 无超卖：任何订单成交量 <= 委托量
        bad = svc.conn.execute("SELECT COUNT(*) AS c FROM orders WHERE filled > qty OR filled < 0").fetchone()["c"]
        self.assertEqual(bad, 0)
        # 冻结账实一致
        self.assertEqual(svc.replay_clearing()["freeze_drift"], [])

    def test_concurrent_cancel_vs_match_only_one_wins(self) -> None:
        svc = self._service()
        svc.create_account("S")
        svc.create_account("M")
        svc.issue("is1", "S", "BASE", 1_000)
        svc.issue("im1", "M", "QUOTE", 1_000_000)
        target = svc.place_order("sell-1", "BASE/QUOTE", "S", "SELL", 10, 100)["order_id"]
        del svc

        outcome: list[str] = []

        def cancel() -> None:
            s = self._service()
            try:
                s.cancel_order("cancel-1", target, "S")
                outcome.append("cancelled")
            except InvalidRequest:
                outcome.append("already-filled")

        def match() -> None:
            s = self._service()
            s.place_order("buy-1", "BASE/QUOTE", "M", "BUY", 10, 100)
            outcome.append("matched")

        t1 = threading.Thread(target=cancel)
        t2 = threading.Thread(target=match)
        t1.start(); t2.start()
        t1.join(); t2.join()

        svc = self._service()
        order = svc.get_order(target)
        self.assertIn(order["status"], ("CANCELLED", "FILLED"))
        if order["status"] == "FILLED":
            self.assertEqual(order["filled_qty"], 100)
            self.assertEqual(svc.get_balance("S", "BASE")["frozen"], 0)
        else:
            self.assertEqual(order["filled_qty"], 0)
            self.assertEqual(svc.get_balance("S", "BASE")["available"], 1_000)
        # 无论谁赢，不能出现"既成交又释放冻结"的双花
        self.assertEqual(svc.replay_clearing()["freeze_drift"], [])

    def test_concurrent_cancel_retries(self) -> None:
        svc = self._service()
        svc.create_account("S"); svc.issue("i1", "S", "BASE", 1_000)
        target = svc.place_order("sell-1", "BASE/QUOTE", "S", "SELL", 10, 100)["order_id"]
        del svc

        ok = {"n": 0}
        lock = threading.Lock()

        def cancel() -> None:
            s = self._service()
            s.cancel_order("cancel-same", target, "S")
            with lock:
                ok["n"] += 1

        threads = [threading.Thread(target=cancel) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(ok["n"], 8)  # 同一 request_id 人人拿到成功响应
        self.assertEqual(self._service().get_balance("S", "BASE")["frozen"], 0)


if __name__ == "__main__":
    unittest.main()
