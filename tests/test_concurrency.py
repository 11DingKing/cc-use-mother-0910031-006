"""并发撤单 vs 撮合同事发生、幂等键并发的回归测试。

使用文件型数据库 + 多连接 + 线程，验证 BEGIN IMMEDIATE 下：
无论谁先拿到写锁，都不会出现重复成交或冻结重复释放。
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

from credit_exchange import Exchange, connect, init_db
from credit_exchange.clearing import audit_freezes, post_pending_clearing
from credit_exchange.errors import ExchangeError
from credit_exchange.schema import BASE_ASSET, QUOTE_ASSET


class ConcurrentCancelTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def _seed(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass
        conn = connect(self.db_path)
        init_db(conn)
        ex = Exchange(conn)
        ex.open_account("acc-maker", "做市账户", {BASE_ASSET: 1000, QUOTE_ASSET: 1000})
        ex.open_account("acc-taker", "吃单账户", {BASE_ASSET: 1000, QUOTE_ASSET: 1000})
        conn.close()

    def test_cancel_versus_match_race(self) -> None:
        filled_rounds = 0
        cancelled_rounds = 0
        for i in range(20):
            # 每轮使用全新数据库，避免上一轮挂单跨轮撮合。
            self._seed()
            setup = connect(self.db_path)
            Exchange(setup).place_order("ask-1", "acc-maker", "SELL", 10, 5)
            setup.close()

            round_outcomes: list[str] = []
            round_errors: list[str] = []

            def buy() -> None:
                c = connect(self.db_path)
                try:
                    resp = Exchange(c).place_order("bid-1", "acc-taker", "BUY", 10, 5)
                    round_outcomes.append(
                        "FILLED" if resp["order"]["status"] == "FILLED" else "BUY_RESTED"
                    )
                except ExchangeError as exc:
                    round_errors.append(f"buy:{exc.code}")
                finally:
                    c.close()

            def cancel() -> None:
                c = connect(self.db_path)
                try:
                    resp = Exchange(c).cancel_order("ask-1", "acc-maker")
                    if resp["order"]["status"] == "CANCELLED":
                        round_outcomes.append("CANCELLED")
                except ExchangeError as exc:
                    # 买单先成交：订单已 FILLED，撤单被拒，绝不能释放冻结。
                    round_errors.append(f"cancel:{exc.code}")
                finally:
                    c.close()

            t1 = threading.Thread(target=buy)
            t2 = threading.Thread(target=cancel)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            check = connect(self.db_path)
            try:
                # 互斥结局之一：买单赢 → FILLED + 撤单被拒；撤单赢 → CANCELLED + 买单挂起。
                if "FILLED" in round_outcomes:
                    filled_rounds += 1
                    self.assertEqual(round_outcomes, ["FILLED"])
                    self.assertTrue(any(e.startswith("cancel:") for e in round_errors))
                else:
                    cancelled_rounds += 1
                    self.assertIn("CANCELLED", round_outcomes)
                    self.assertIn("BUY_RESTED", round_outcomes)
                expected_trades = 1 if "FILLED" in round_outcomes else 0
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM trades WHERE taker_order_id IN"
                        " (SELECT id FROM orders WHERE client_order_id='bid-1')"
                    ).fetchone()[0],
                    expected_trades,
                )
                self.assertTrue(audit_freezes(check)["consistent"])
            finally:
                check.close()

        # 20 轮里两种交错都应该出现过。
        self.assertGreater(filled_rounds, 0)
        self.assertGreater(cancelled_rounds, 0)
        self.assertEqual(filled_rounds + cancelled_rounds, 20)

    def test_concurrent_same_idempotency_key_executes_once(self) -> None:
        self._seed()
        setup = connect(self.db_path)
        Exchange(setup).place_order("ask-seed", "acc-maker", "SELL", 10, 5)
        setup.close()
        results: list[str] = []

        def retry_place() -> None:
            c = connect(self.db_path)
            try:
                resp = Exchange(c).place_order(
                    "bid-once", "acc-taker", "BUY", 10, 5, idempotency_key="dup-key"
                )
                results.append("replay" if resp["idempotent_replay"] else "first")
            except ExchangeError as exc:
                # 两个写锁竞争中落败方不应报唯一约束错误；BEGIN IMMEDIATE 已串行化。
                results.append(f"error:{exc.code}")
            finally:
                c.close()

        threads = [threading.Thread(target=retry_place) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["first", "replay"])
        check = connect(self.db_path)
        try:
            self.assertEqual(check.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
            self.assertTrue(audit_freezes(check)["consistent"])
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
