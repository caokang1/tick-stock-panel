"""统一的错误类型。message 一律是用户能看懂的中文说明。"""

# ruff: noqa: N818 —— SourceRejected、SourceUnavailable 这两个名字在数据层里到处用到，保持不变

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
    requests = 0  # 出错前这次尝试实际发出的请求数；适配器知道时填写，供限流计数


class SourceRejected(DataError):
    """数据来源限制了访问（如被列入黑名单）。不得重试，不得绕过。"""

    code = "source_rejected"


class SourceUnavailable(DataError):
    """数据来源暂时不可用（超时、网络错误、返回异常）。"""

    code = "source_unavailable"


class ConfigError(QuantLabError):
    code = "config_error"


class NotFoundError(QuantLabError):
    code = "not_found"
    http_status = 404


class ConflictError(QuantLabError):
    code = "conflict"
    http_status = 409
