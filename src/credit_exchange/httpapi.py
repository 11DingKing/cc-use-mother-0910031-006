"""HTTP 接口（标准库实现，零第三方依赖）。

所有写接口通过请求头 ``X-Request-Id`` 或报文字段 ``request_id`` 实现幂等：
同一 request_id 重复提交返回首次结果，绝不重复成交/扣减。

路由：
  POST   /admin/accounts                 开户
  POST   /admin/issue                    授信/充值          （幂等）
  POST   /admin/markets                  建立双积分交易对
  POST   /admin/markets/{code}/halt      监管暂停           （幂等）
  POST   /admin/markets/{code}/resume    恢复交易           （幂等）
  POST   /admin/replay                   重放未完成清算     （?dry_run=1）
  POST   /orders                         限价委托并即时撮合 （幂等）
  POST   /orders/{id}/cancel             撤单（并发安全）    （幂等）
  POST   /orders/{id}/expire             订单失效           （幂等）
  GET    /orders/{id}                    剩余量与成交依据
  GET    /accounts/{id}/balance?asset=   总额/冻结/可用
  GET    /markets/{code}/book            限价簿
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import ExchangeError
from .service import ExchangeService


def create_handler(svc: ExchangeService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CreditExchange/1.0"

        def log_message(self, fmt: str, *args) -> None:  # 安静化
            return

        # ------------------------------------------------------------ 工具

        def _json_response(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ExchangeError("请求体必须是合法 JSON") from exc
            if not isinstance(body, dict):
                raise ExchangeError("请求体必须是 JSON 对象")
            return body

        def _request_id(self, body: dict) -> str:
            rid = self.headers.get("X-Request-Id") or body.get("request_id")
            if not rid:
                raise ExchangeError("写操作必须提供 X-Request-Id 以保证幂等")
            return str(rid)

        def _handle(self, fn) -> None:
            try:
                result, status = fn()
                self._json_response(status, {"ok": True, "data": result})
            except ExchangeError as exc:
                self._json_response(exc.http_status, {"ok": False, "error": exc.to_dict()})
            except Exception as exc:  # noqa: BLE001 - 边界统一处理
                self._json_response(500, {"ok": False, "error": {
                    "code": "INTERNAL_ERROR", "message": str(exc)}})

        # ------------------------------------------------------------ GET

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            qs = parse_qs(parts.query)

            def route():
                seg = path.strip("/").split("/")
                if len(seg) == 2 and seg[0] == "orders":
                    return svc.get_order(seg[1]), 200
                if len(seg) == 3 and seg[0] == "accounts" and seg[2] == "balance":
                    asset = qs.get("asset", [""])[0]
                    if not asset:
                        raise ExchangeError("缺少 asset 查询参数")
                    return svc.get_balance(seg[1], asset), 200
                if len(seg) == 3 and seg[0] == "markets" and seg[2] == "book":
                    return svc.order_book(seg[1]), 200
                raise ExchangeError(f"未知路径 {path}")

            self._handle(route)

        # ------------------------------------------------------------ POST

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            qs = parse_qs(parts.query)
            seg = path.strip("/").split("/")
            body = self._read_body()

            def route():
                if path == "/admin/accounts":
                    return svc.create_account(body["account_id"], body.get("name", "")), 201
                if path == "/admin/issue":
                    return svc.issue(
                        self._request_id(body), body["account_id"],
                        body["asset"], int(body["amount"])), 200
                if path == "/admin/markets":
                    return svc.create_market(
                        body["market"], body["base_asset"], body["quote_asset"]), 201
                if len(seg) == 4 and seg[:2] == ["admin", "markets"] and seg[3] == "halt":
                    return svc.halt_market(self._request_id(body), seg[2]), 200
                if len(seg) == 4 and seg[:2] == ["admin", "markets"] and seg[3] == "resume":
                    return svc.resume_market(self._request_id(body), seg[2]), 200
                if path == "/admin/replay":
                    dry = qs.get("dry_run", ["0"])[0] in ("1", "true", "yes")
                    return svc.replay_clearing(body.get("market"), dry_run=dry), 200
                if path == "/orders":
                    return svc.place_order(
                        self._request_id(body), body["market"], body["account_id"],
                        body["side"], int(body["price"]), int(body["qty"])), 201
                if len(seg) == 3 and seg[0] == "orders" and seg[2] == "cancel":
                    return svc.cancel_order(
                        self._request_id(body), seg[1], body["account_id"]), 200
                if len(seg) == 3 and seg[0] == "orders" and seg[2] == "expire":
                    return svc.expire_order(self._request_id(body), seg[1]), 200
                raise ExchangeError(f"未知路径 {path}")

            self._handle(route)

    return Handler


def serve(svc: ExchangeService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), create_handler(svc))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="http-server", daemon=True)
    thread.start()
    return httpd
