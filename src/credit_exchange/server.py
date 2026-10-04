"""HTTP 服务启动入口：python -m credit_exchange.server DB_PATH --port 8080"""
from __future__ import annotations

import argparse
import time

from .db import connect, init_schema
from .httpapi import serve
from .service import ExchangeService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="积分交易撮合清算 HTTP 服务")
    parser.add_argument("db", help="SQLite 数据库文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    init_schema(conn)
    svc = ExchangeService(conn)
    httpd = serve(svc, args.host, args.port)
    print(f"积分交易服务已启动：http://{args.host}:{args.port}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
