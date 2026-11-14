"""统一的错误类型。message 一律是用户能看懂的中文说明。"""

from __future__ import annotations


class QuantLabError(Exception):
    code = "error"
    http_status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FormulaError(QuantLabError):
    """公式有问题。kind: syntax（写法有误）/ unsupported（含不支持的函数）/ lookahead（含未来函数）。"""

    code = "formula_error"

    def __init__(
        self, message: str, kind: str = "syntax", names: list[str] | None = None, line: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.names = names or []
        self.line = line


class DataError(QuantLabError):
    code = "data_error"


class ConfigError(QuantLabError):
    code = "config_error"


class NotFoundError(QuantLabError):
    code = "not_found"
    http_status = 404


class ConflictError(QuantLabError):
    code = "conflict"
    http_status = 409
