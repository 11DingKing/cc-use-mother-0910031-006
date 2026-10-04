"""标准库 HTTP/JSON 接口。

所有写接口支持幂等：优先读取请求体 ``idempotency_key``，否则读取
``Idempotency-Key`` 请求头。重放返回首次响应并带 ``idempotency_replay=true``。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .clearing import audit_freezes, clearing_status, post_pending_clearing
from .db import connect
from .errors import ExchangeError
from .schema import init_db
from .service import Exchange


class _Store:
    """所有线程共享一个连接；BEGIN IMMEDIATE 把写请求串行化。"""

    def __init__(self, db_path: str, auto_post: bool = True) -> None:
        self.lock = threading.Lock()
        self.conn = connect(db_path, check_same_thread=False)
        init_db(self.conn)
        self.auto_post = auto_post


def create_handler(store: _Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CreditExchange/0.2"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静
            return

        # ------------------------------------------------------------ 基础

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ExchangeError("INVALID_JSON", f"请求体不是合法 JSON：{exc}", 400)
            if not isinstance(data, dict):
                raise ExchangeError("INVALID_BODY", "请求体必须是 JSON 对象")
            if "idempotency_key" not in data and self.headers.get("Idempotency-Key"):
                data["idempotency_key"] = self.headers["Idempotency-Key"]
            return data

        def _exchange(self) -> Exchange:
            return Exchange(store.conn, auto_post=store.auto_post)

        def _require(self, data: dict[str, Any], key: str) -> Any:
            if key not in data:
                raise ExchangeError("MISSING_FIELD", f"缺少必填字段：{key}")
            return data[key]

        # ------------------------------------------------------------ 路由

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(write=False)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch(write=True)

        def _dispatch(self, write: bool) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                data = self._read_json() if write else {}
                with store.lock:  # 串行化对共享连接的访问
                    self._route(path, query, data)
            except ExchangeError as exc:
                self._send(exc.http_status, exc.body)
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": {"code": "BAD_REQUEST", "message": str(exc)}})
            except Exception as exc:  # 防御：内部错误不泄露栈
                self._send(500, {"error": {"code": "INTERNAL", "message": str(exc)}})

        def _route(self, path: str, query: dict[str, list[str]], data: dict[str, Any]) -> None:
            ex = self._exchange()
            p = [seg for seg in path.split("/") if seg]

            # GET /healthz
            if path == "/healthz":
                return self._send(200, {"status": "ok"})

            # POST /accounts
            if path == "/accounts":
                return self._send(201, ex.open_account(
                    self._require(data, "account_id"),
                    self._require(data, "holder"),
                    data.get("balances") or {},
                    data.get("idempotency_key"),
                ))

            # /accounts/{id}/...
            if len(p) == 3 and p[0] == "accounts":
                account_id = p[1]
                if p[2] == "balances":
                    asset = query.get("asset", [None])[0]
                    if asset:
                        return self._send(200, {"asset": asset, **ex.get_balance(account_id, asset)})
                    return self._send(200, {"balances": ex.list_balances(account_id)})
                if p[2] == "deposits":
                    return self._send(200, ex.deposit(
                        account_id,
                        self._require(data, "asset"),
                        int(self._require(data, "amount")),
                        data.get("idempotency_key"),
                    ))

            # POST /orders
            if path == "/orders":
                return self._send(201, ex.place_order(
                    self._require(data, "client_order_id"),
                    self._require(data, "account_id"),
                    self._require(data, "side"),
                    int(self._require(data, "price")),
                    int(self._require(data, "qty")),
                    int(data["ttl_seconds"]) if data.get("ttl_seconds") is not None else None,
                    data.get("idempotency_key"),
                ))
            if path == "/order-book":
                return self._send(200, ex.order_book(int(query.get("depth", [50])[0])))
            if path == "/trades":
                return self._send(200, {"trades": ex.list_trades(query.get("order", [None])[0])})
            if path == "/pair-status":
                return self._send(200, ex.pair_status())

            # /orders/{coid} 与 /orders/{coid}/cancel
            if len(p) >= 2 and p[0] == "orders":
                coid = p[1]
                if len(p) == 2:
                    return self._send(200, ex.get_order(coid))
                if len(p) == 3 and p[2] == "cancel":
                    return self._send(200, ex.cancel_order(
                        coid,
                        self._require(data, "account_id"),
                        data.get("idempotency_key"),
                    ))

            # 管理类
            if path == "/admin/suspend":
                return self._send(200, ex.suspend_pair(
                    self._require(data, "reason"), data.get("operator", "监管审计员")
                ))
            if path == "/admin/resume":
                return self._send(200, ex.resume_pair(
                    self._require(data, "reason"), data.get("operator", "监管审计员")
                ))
            if path == "/admin/expire":
                return self._send(200, ex.expire_orders())
            if path == "/admin/clearing":
                return self._send(200, clearing_status(store.conn))
            if path == "/admin/clearing/replay":
                return self._send(200, post_pending_clearing(store.conn))
            if path == "/admin/audit":
                return self._send(200, audit_freezes(store.conn))

            self._send(404, {"error": {"code": "NOT_FOUND", "message": f"无此路由：{path}"}})

    return Handler


def serve(db_path: str = ":memory:", host: str = "127.0.0.1", port: int = 8080,
          auto_post: bool = True) -> ThreadingHTTPServer:
    store = _Store(db_path, auto_post=auto_post)
    httpd = ThreadingHTTPServer((host, port), create_handler(store))
    return httpd
