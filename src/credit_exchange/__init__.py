"""双积分挂牌交易撮合清算后端。

对应领域契约（domain/contract.json）的四条不变量：

- 订单冻结额度：下单即冻结，``可用 = 总额 - 冻结``；冻结余额始终等于全部
  活动订单（NEW/PARTIAL）的冻结持有量之和。
- 价格时间优先：限价簿按最优价格、同价按订单序号（时间）撮合。
- 成交清算原子性：订单撮合与双方四个科目余额扣减在同一个
  ``BEGIN IMMEDIATE`` 事务内完成；清算分录按成交编号唯一，可独立补登。
- 幂等重放恢复：全部写接口凭 request_id 幂等；未完成清算可重复重放，
  不会产生第二笔成交。
"""
from __future__ import annotations

from .db import connect, init_schema
from .errors import (
    DuplicateMarket,
    ExchangeError,
    IdempotencyKeyReused,
    InsufficientBalance,
    InvalidRequest,
    MarketHalted,
    UnknownAccount,
    UnknownMarket,
    UnknownOrder,
)
from .service import ExchangeService

__all__ = [
    "ExchangeService",
    "connect",
    "init_schema",
    "ExchangeError",
    "InvalidRequest",
    "InsufficientBalance",
    "MarketHalted",
    "IdempotencyKeyReused",
    "UnknownAccount",
    "UnknownMarket",
    "UnknownOrder",
    "DuplicateMarket",
]
