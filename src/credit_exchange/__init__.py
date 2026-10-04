"""双积分交易撮合清算后端。

核心不变量（对应 domain/contract.json）：

* 订单冻结额度：下单即冻结，成交/撤单/失效按剩余量精确释放；
* 价格时间优先：买单降价、卖单升价，同价按时间序列撮合；
* 成交清算原子性：成交、账户扣减与清算分录在同一事务提交；
* 幂等重放恢复：请求幂等键去重，清算过账可安全重放。
"""
from __future__ import annotations

from .clearing import clearing_status, post_pending_clearing
from .db import connect, transaction
from .errors import ExchangeError
from .schema import BASE_ASSET, PAIR_CODE, QUOTE_ASSET, init_db
from .service import Exchange

__all__ = [
    "BASE_ASSET",
    "Exchange",
    "ExchangeError",
    "PAIR_CODE",
    "QUOTE_ASSET",
    "clearing_status",
    "connect",
    "init_db",
    "post_pending_clearing",
    "transaction",
]

__version__ = "0.2.0"
