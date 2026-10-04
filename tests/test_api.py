"""HTTP/JSON 接口端到端测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_exchange.api import create_handler, _Store
from http.server import ThreadingHTTPServer


class ApiTestCase(unittest.TestCase):
    deferred = False

    def setUp(self) -> None:
        self.store = _Store(":memory:", auto_post=not self.deferred)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.store))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_with_idempotency_retry(self) -> None:
        self.assertEqual(201, self.request("POST", "/accounts", {
            "account_id": "acc-maker", "holder": "做市", "balances": {"CREDIT_A": 1000, "CREDIT_B": 1000}
        })[0])
        self.assertEqual(201, self.request("POST", "/accounts", {
            "account_id": "acc-taker", "holder": "吃单", "balances": {"CREDIT_A": 1000, "CREDIT_B": 1000}
        })[0])
        self.assertEqual(201, self.request("POST", "/orders", {
            "client_order_id": "ask-1", "account_id": "acc-maker",
            "side": "SELL", "price": 10, "qty": 5
        })[0])

        payload = {
            "client_order_id": "bid-1", "account_id": "acc-taker",
            "side": "BUY", "price": 10, "qty": 5, "idempotency_key": "net-retry-1",
        }
        s1, first = self.request("POST", "/orders", payload)
        s2, retry = self.request("POST", "/orders", payload)
        self.assertEqual(s1, 201)
        self.assertEqual(s2, 201)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(retry["idempotent_replay"])
        self.assertEqual(first["fills"], retry["fills"])

        s, trades = self.request("GET", "/trades")
        self.assertEqual(s, 200)
        self.assertEqual(len(trades["trades"]), 1)

        s, order = self.request("GET", "/orders/ask-1")
        self.assertEqual(s, 200)
        self.assertEqual(order["order"]["remaining_qty"], 0)
        self.assertEqual(order["order"]["status"], "FILLED")
        self.assertEqual(len(order["fills"]), 1)

        s, audit = self.request("GET", "/admin/audit")
        self.assertEqual(s, 200)
        self.assertTrue(audit["consistent"])

    def test_suspend_blocks_and_cancel_works(self) -> None:
        self.request("POST", "/accounts", {
            "account_id": "a1", "holder": "x", "balances": {"CREDIT_A": 100}})
        self.request("POST", "/orders", {
            "client_order_id": "o1", "account_id": "a1", "side": "SELL", "price": 3, "qty": 10})
        s, resp = self.request("POST", "/admin/suspend", {"reason": "例行监管暂停"})
        self.assertEqual(s, 200)
        self.assertEqual(resp["status"], "SUSPENDED")
        s, err = self.request("POST", "/orders", {
            "client_order_id": "o2", "account_id": "a1", "side": "SELL", "price": 3, "qty": 1})
        self.assertEqual(s, 423)
        self.assertEqual(err["error"]["code"], "PAIR_SUSPENDED")
        s, cancel = self.request("POST", "/orders/o1/cancel", {"account_id": "a1"})
        self.assertEqual(s, 200)
        self.assertEqual(cancel["released"]["amount"], 10)
        s, bal = self.request("GET", "/accounts/a1/balances?asset=CREDIT_A")
        self.assertEqual(s, 200)
        self.assertEqual(bal["frozen"], 0)
        self.assertEqual(bal["available"], 100)

    def test_insufficient_available_returns_422(self) -> None:
        self.request("POST", "/accounts", {
            "account_id": "a2", "holder": "x", "balances": {"CREDIT_B": 5}})
        s, err = self.request("POST", "/orders", {
            "client_order_id": "poor", "account_id": "a2", "side": "BUY", "price": 10, "qty": 10})
        self.assertEqual(s, 422)
        self.assertEqual(err["error"]["code"], "INSUFFICIENT_AVAILABLE")


class DeferredApiTestCase(ApiTestCase):
    deferred = True

    def test_replay_clearing_endpoint_is_idempotent(self) -> None:
        self.request("POST", "/accounts", {
            "account_id": "acc-maker", "holder": "做市", "balances": {"CREDIT_A": 1000, "CREDIT_B": 1000}
        })
        self.request("POST", "/accounts", {
            "account_id": "acc-taker", "holder": "吃单", "balances": {"CREDIT_A": 1000, "CREDIT_B": 1000}
        })
        self.request("POST", "/orders", {
            "client_order_id": "ask-1", "account_id": "acc-maker",
            "side": "SELL", "price": 10, "qty": 5})
        s, resp = self.request("POST", "/orders", {
            "client_order_id": "bid-1", "account_id": "acc-taker",
            "side": "BUY", "price": 10, "qty": 5})
        self.assertEqual(s, 201)

        s, st = self.request("GET", "/admin/clearing")
        self.assertEqual(st["pending_trade_count"], 1)
        s, first = self.request("POST", "/admin/clearing/replay")
        self.assertEqual(first["posted_count"], 1)
        s, second = self.request("POST", "/admin/clearing/replay")
        self.assertEqual(second["posted_count"], 0)
        s, trades = self.request("GET", "/trades")
        self.assertEqual(len(trades["trades"]), 1)
        s, bal = self.request("GET", "/accounts/acc-maker/balances?asset=CREDIT_B")
        self.assertEqual(bal["available"], 1050)


if __name__ == "__main__":
    unittest.main()
