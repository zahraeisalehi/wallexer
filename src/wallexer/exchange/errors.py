"""Exchange error types."""

from __future__ import annotations

from typing import Any


class WallexError(Exception):
    """Base class for exchange client errors."""


class ApiError(WallexError):
    """Non-success response (HTTP error or success=false envelope)."""

    def __init__(
        self, status: int, message: str, code: int | None = None, fields: Any = None
    ) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.code = code
        self.fields = fields or {}


class TransportError(WallexError):
    """Network failure or non-JSON body (e.g. an nginx 503 HTML page)."""


class OrdersDisabled(WallexError):
    """Raised when order placement/cancel is attempted without the live latch enabled."""


class OrderRejected(ApiError):
    """The exchange refused an order (400/422)."""


class OrderStatusUnknown(WallexError):
    """Placement timed out and the follow-up lookup did not find the order. Never re-send blindly."""

    def __init__(self, client_id: str) -> None:
        super().__init__(f"order {client_id}: status unknown after timeout")
        self.client_id = client_id
