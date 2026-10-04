"""领域错误类型。"""
from __future__ import annotations

from typing import Any


class ExchangeError(Exception):
    """带稳定错误码与 HTTP 状态码的业务异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 400,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.body = body if body is not None else {"error": {"code": code, "message": message}}
