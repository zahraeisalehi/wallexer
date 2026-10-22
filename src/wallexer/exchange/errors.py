"""Exchange error types."""

from __future__ import annotations

from typing import Any


class WallexError(Exception):
    """Base class for exchange client errors."""


class ApiError(WallexError):
    """Non-success response (HTTP error or success=false envelope)."""

    def __init__(
        self,
        status: int,
        message: str,
        code: int | None = None,
        fields: Any = None,
        body: Any = None,
    ) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.code = code
        self.fields = fields or {}
        self.body = body  # the complete error response, as received (for logs and diagnosis)

    def detail(self) -> dict[str, Any]:
        """Everything the exchange said: status, code, message, per-field result, raw body."""
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "result": self.fields,
            "body": self.body,
        }


class TransportError(WallexError):
    """Network failure or non-JSON body (e.g. an nginx 503 HTML page)."""


class ResponseShapeError(WallexError):
    """A response did not match the expected shape; `raw` holds what was received."""

    def __init__(self, what: str, error: str, raw: Any) -> None:
        super().__init__(f"unexpected {what} response shape: {error}")
        self.raw = raw


class OrdersDisabled(WallexError):
    """Raised when order placement/cancel is attempted without the live latch enabled."""


class OrderRejected(ApiError):
    """The exchange refused an order (400/422)."""


class OrderStatusUnknown(WallexError):
    """Placement timed out and the follow-up lookup did not find the order. Never re-send blindly."""

    def __init__(self, client_id: str) -> None:
        super().__init__(f"order {client_id}: status unknown after timeout")
        self.client_id = client_id
