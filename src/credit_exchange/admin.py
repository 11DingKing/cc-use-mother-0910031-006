"""管理命令行。

重点命令 ``replay-clearing``：重放所有未完成（PENDING）清算分录并过账。
分录状态与唯一约束保证重复执行不会产生第二笔交易或第二次扣账。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .clearing import audit_freezes, clearing_status, post_pending_clearing
from .db import connect
from .schema import init_db
from .service import Exchange


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="credit-exchange-admin", description="双积分交易撮合清算管理命令")
    parser.add_argument("--db", default="exchange.sqlite3", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库")

    p_open = sub.add_parser("open-account", help="开户")
    p_open.add_argument("account_id")
    p_open.add_argument("holder")
    p_open.add_argument("--credit-a", type=int, default=0)
    p_open.add_argument("--credit-b", type=int, default=0)
    p_open.add_argument("--idempotency-key")

    p_dep = sub.add_parser("deposit", help="积分入账")
    p_dep.add_argument("account_id")
    p_dep.add_argument("asset")
    p_dep.add_argument("amount", type=int)
    p_dep.add_argument("--idempotency-key")

    p_exp = sub.add_parser("expire", help="失效到期订单并释放冻结")
    p_exp.add_argument("--now", help="覆盖当前时间（YYYY-mm-dd HH:MM:SS，UTC）")

    p_sus = sub.add_parser("suspend", help="监管暂停交易对")
    p_sus.add_argument("reason")
    p_sus.add_argument("--operator", default="监管审计员")

    p_res = sub.add_parser("resume", help="恢复交易对")
    p_res.add_argument("reason")
    p_res.add_argument("--operator", default="监管审计员")

    p_order = sub.add_parser("order", help="查询订单（剩余量与成交依据）")
    p_order.add_argument("client_order_id")

    sub.add_parser("clearing-status", help="查看待过账清算")
    sub.add_parser("replay-clearing", help="重放并过账未完成清算（幂等，可反复执行）")
    sub.add_parser("audit", help="账实核对：冻结是否与订单及待清算一致")

    p_book = sub.add_parser("order-book", help="查看盘口")
    p_book.add_argument("--depth", type=int, default=50)

    p_serve = sub.add_parser("serve", help="启动 HTTP/JSON 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--deferred-clearing", action="store_true", help="成交后不立即过账，等待 replay-clearing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    conn = connect(args.db)
    init_db(conn)
    code = 0
    try:
        ex = Exchange(conn)
        if args.command == "init-db":
            _print({"status": "ok", "db": args.db})
        elif args.command == "open-account":
            balances = {}
            if args.credit_a:
                balances["CREDIT_A"] = args.credit_a
            if args.credit_b:
                balances["CREDIT_B"] = args.credit_b
            _print(ex.open_account(args.account_id, args.holder, balances, args.idempotency_key))
        elif args.command == "deposit":
            _print(ex.deposit(args.account_id, args.asset, args.amount, args.idempotency_key))
        elif args.command == "expire":
            _print(ex.expire_orders(args.now))
        elif args.command == "suspend":
            _print(ex.suspend_pair(args.reason, args.operator))
        elif args.command == "resume":
            _print(ex.resume_pair(args.reason, args.operator))
        elif args.command == "order":
            _print(ex.get_order(args.client_order_id))
        elif args.command == "order-book":
            _print(ex.order_book(args.depth))
        elif args.command == "clearing-status":
            _print(clearing_status(conn))
        elif args.command == "replay-clearing":
            result = post_pending_clearing(conn)
            audit = audit_freezes(conn)
            _print({**result, "audit": audit})
            code = 0 if audit["consistent"] else 2
        elif args.command == "audit":
            audit = audit_freezes(conn)
            _print(audit)
            code = 0 if audit["consistent"] else 2
        elif args.command == "serve":
            from .api import serve

            httpd = serve(args.db, args.host, args.port, auto_post=not args.deferred_clearing)
            print(f"listening on http://{args.host}:{args.port}", file=sys.stderr)
            httpd.serve_forever()
    finally:
        conn.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
