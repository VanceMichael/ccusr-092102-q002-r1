"""归因账本的错误类型。HTTP 层据此映射状态码。"""

from __future__ import annotations

from typing import Any


class LedgerError(Exception):
    """所有领域错误的基类。"""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"错误": self.code, "说明": self.message, **({"细节": self.details} if self.details else {})}


class ValidationError(LedgerError):
    status = 422
    code = "unprocessable"


class NotFound(LedgerError):
    status = 404
    code = "not_found"


class PermissionDenied(LedgerError):
    status = 403
    code = "forbidden"


class ConflictError(LedgerError):
    """状态冲突：已冻结、乐观锁不匹配等。details 携带当前版本供合并。"""

    status = 409
    code = "conflict"


class TradingInstructionError(LedgerError):
    """文本中出现交易指令性表述——账本只承载研究结论，不产出交易指令。"""

    status = 422
    code = "trading_instruction_blocked"
