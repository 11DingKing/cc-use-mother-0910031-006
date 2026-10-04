"""HTTP 接口端到端测试（标准库 http.client，不引入依赖）。"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_exchange import ExchangeService, connect, init_schema  # noqa: E402
from credit_exchange.httpapi import serve  # noqa: E402


class HttpApiTest(unittest.TestCase):
    def test_http_round_trip_and_idempotent_retry(self) -> None:
        import socket
        from contextlib import closing

        # 找一个空闲端口
        with closing(socket.socket()) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        conn = connect(":memory:")
        init_schema(conn)
        svc = ExchangeService(conn)
        httpd = serve(svc, "127.0.0.1", port)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{port}"

        def call(method: str, path: str, body: dict | None = None,
                 request_id: str | None = None, expect_error: bool = False):
            data = json.dumps(body or {}).encode()
            req = urllib.request.Request(base + path, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            if request_id:
                req.add_header("X-Request-Id", request_id)
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                payload = json.loads(exc.read())
                if expect_error:
                    return exc.code, payload
                self.fail(f"请求失败 {method} {path}: {payload}")

        call("POST", "/admin/accounts", {"account_id": "A", "name": "甲"})
        call("POST", "/admin/accounts", {"account_id": "B", "name": "乙"})
        call("POST", "/admin/issue", {"account_id": "A", "asset": "Q", "amount": 100000}, "iss-a")
        call("POST", "/admin/issue", {"account_id": "B", "asset": "P", "amount": 100000}, "iss-b")
        call("POST", "/admin/markets", {"market": "P/Q", "base_asset": "P", "quote_asset": "Q"})

        status, sell = call("POST", "/orders",
                            {"market": "P/Q", "account_id": "B", "side": "SELL",
                             "price": 10, "qty": 30}, "sell-1")
        self.assertEqual(status, 201)
        sell_id = sell["data"]["order_id"]

        # 网络重试：相同 request_id 返回同一订单
        _, retry = call("POST", "/orders",
                        {"market": "P/Q", "account_id": "B", "side": "SELL",
                         "price": 10, "qty": 30}, "sell-1")
        self.assertEqual(retry["data"]["order_id"], sell_id)

        _, buy = call("POST", "/orders",
                      {"market": "P/Q", "account_id": "A", "side": "BUY",
                       "price": 10, "qty": 30}, "buy-1")
        self.assertEqual(buy["data"]["remaining_qty"], 0)
        self.assertEqual(buy["data"]["fills"][0]["counterparty_account"], "B")

        # 查询订单与余额
        _, order = call("GET", f"/orders/{sell_id}")
        self.assertEqual(order["data"]["status"], "FILLED")
        _, bal = call("GET", "/accounts/B/balance?asset=Q")
        self.assertEqual(bal["data"]["available"], 300)  # 卖出 30P @10 收到的对价

        # 撤已成交单：幂等 request_id 下返回 400 业务错误
        code, err = call("POST", f"/orders/{sell_id}/cancel",
                         {"account_id": "B"}, "cancel-1", expect_error=True)
        self.assertEqual(code, 400)
        self.assertEqual(err["error"]["code"], "INVALID_REQUEST")

        # 重放：无修复、无新增成交
        _, rep = call("POST", "/admin/replay", {})
        self.assertEqual(rep["data"]["new_trades_created"], 0)
        self.assertEqual(rep["data"]["entries_repaired"], [])

        # 缺少 request_id 被拒
        code, err = call("POST", "/orders",
                         {"market": "P/Q", "account_id": "A", "side": "BUY",
                          "price": 9, "qty": 1}, expect_error=True)
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
