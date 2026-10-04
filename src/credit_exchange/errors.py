"""领域错误类型与 HTTP 状态码映射。"""
from __future__ import annotations


class ExchangeError(Exception):
    """所有可预期业务错误的基类。"""

    code = "EXCHANGE_ERROR"
    http_status = 400

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


class InvalidRequest(ExchangeError):
    code = "INVALID_REQUEST"
    http_status = 400


class UnknownAccount(ExchangeError):
    code = "UNKNOWN_ACCOUNT"
    http_status = 404


class UnknownMarket(ExchangeError):
    code = "UNKNOWN_MARKET"
    http_status = 404


class UnknownOrder(ExchangeError):
    code = "UNKNOWN_ORDER"
    http_status = 404


class InsufficientBalance(ExchangeError):
    code = "INSUFFICIENT_BALANCE"
    http_status = 409

    def __init__(self, account: str, asset: str, required: int):
        super().__init__(
            f"账户 {account} 的 {asset} 可用余额不足，至少需要 {required}"
        )
        self.account = account
        self.asset = asset
        self.required = required


class MarketHalted(ExchangeError):
    code = "MARKET_HALTED"
    http_status = 409


class IdempotencyKeyReused(ExchangeError):
    """同一 request_id 携带了不同的请求参数。"""

    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409


class DuplicateMarket(ExchangeError):
    code = "DUPLICATE_MARKET"
    http_status = 409


class SettlementInvariant(ExchangeError):
    """扣减防护失败，说明账实一致性被破坏，整个事务回滚。"""

    code = "SETTLEMENT_INVARIANT"
    http_status = 500
