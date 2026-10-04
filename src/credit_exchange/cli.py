"""管理命令：重放未完成清算。

用法：
    python -m credit_exchange.cli replay DB_PATH [--market CODE] [--dry-run]
    python -m credit_exchange.cli seed   DB_PATH   # 演示用双积分环境

重放按成交顺序扫描 trades：补齐缺失分录、把 posted=0 的分录入账，并输出
冻结漂移对账结果。幂等设计保证重复执行不会产生第二笔成交或第二次扣减。
"""
from __future__ import annotations

import argparse
import json
import sys

from .db import connect, init_schema
from .errors import ExchangeError
from .service import ExchangeService


def cmd_replay(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    init_schema(conn)
    svc = ExchangeService(conn)
    result = svc.replay_clearing(args.market, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["freeze_drift"]:
        print("发现冻结额与活动订单持有不一致，请人工核查：", file=sys.stderr)
        print(json.dumps(result["freeze_drift"], ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(
        f"扫描成交 {result['trades_scanned']} 笔，"
        f"修复分录 {len(result['entries_repaired'])} 条，"
        f"新增成交 {result['new_trades_created']} 笔"
        f"{'（演练，未落库）' if args.dry_run else ''}"
    )
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """建立演示环境：两个账户、A/B 双积分、A/B 交易对。"""
    conn = connect(args.db)
    init_schema(conn)
    svc = ExchangeService(conn)
    svc.create_account("ACC-A", "申报企业甲")
    svc.create_account("ACC-B", "申报企业乙")
    svc.issue("seed-issue-a-A", "ACC-A", "CREDIT_A", 10_000)
    svc.issue("seed-issue-a-B", "ACC-A", "CREDIT_B", 5_000)
    svc.issue("seed-issue-b-A", "ACC-B", "CREDIT_A", 5_000)
    svc.issue("seed-issue-b-B", "ACC-B", "CREDIT_B", 10_000)
    svc.create_market("A/B", "CREDIT_A", "CREDIT_B")
    print("演示环境就绪：ACC-A、ACC-B 与交易对 A/B（CREDIT_A 计价 CREDIT_B）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="credit_exchange.cli", description="积分交易管理命令")
    sub = parser.add_subparsers(dest="command", required=True)

    p_replay = sub.add_parser("replay", help="重放未完成清算（幂等）")
    p_replay.add_argument("db", help="SQLite 数据库文件路径")
    p_replay.add_argument("--market", help="只处理指定交易对")
    p_replay.add_argument("--dry-run", action="store_true", help="只扫描不落库")
    p_replay.set_defaults(func=cmd_replay)

    p_seed = sub.add_parser("seed", help="初始化演示环境")
    p_seed.add_argument("db", help="SQLite 数据库文件路径")
    p_seed.set_defaults(func=cmd_seed)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ExchangeError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
