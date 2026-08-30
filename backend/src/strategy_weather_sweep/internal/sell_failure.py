"""SELL failure classification and event-level circuit breaking."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional


def classify_sell_error(status: str, error: Optional[str]) -> str:
    """Normalize CLOB/executor errors into a stable retry signature."""
    text = (error or "").lower()
    if status == "insufficient_balance" or "insufficient balance" in text:
        return "insufficient_balance"
    if "invalid tick size" in text or "tick size mismatch" in text:
        return "invalid_tick_size"
    if (
        "timeout" in text
        or "timed out" in text
        or "connection" in text
        or "network" in text
        or "tick size refresh failed" in text
    ):
        return "network_timeout"
    if (
        "auth" in text
        or "401" in text
        or "403" in text
        or "api key" in text
        or "signature" in text
    ):
        return "authentication_error"
    if (
        "invalid order" in text
        or "invalid amount" in text
        or "invalid price" in text
        or "invalid size" in text
        or "invalid param" in text
    ):
        return "invalid_order_params"
    return "unknown_api_error"


@dataclass
class SellFailureSnapshot:
    attempt_count: int
    consecutive_same_error: int
    error_signature: str
    last_error: Optional[str]
    elapsed_ms: int
    stop_reason: Optional[str]


class SellFailureTracker:
    """Track failures for one event's SELL phase."""

    def __init__(
        self,
        *,
        max_consecutive_same_error: int = 3,
        max_total_failures: int = 5,
        max_elapsed_ms: int = 180_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_consecutive_same_error = max_consecutive_same_error
        self._max_total_failures = max_total_failures
        self._max_elapsed_ms = max_elapsed_ms
        self._clock = clock
        self._started_at = clock()
        self._attempt_count = 0
        self._consecutive_same_error = 0
        self._error_signature = "unknown_api_error"
        self._last_error: Optional[str] = None

    def record(self, error_signature: str, error: Optional[str]) -> SellFailureSnapshot:
        self._attempt_count += 1
        if error_signature == self._error_signature:
            self._consecutive_same_error += 1
        else:
            self._error_signature = error_signature
            self._consecutive_same_error = 1
        self._last_error = error
        return self.snapshot()

    def snapshot(self) -> SellFailureSnapshot:
        stop_reason: Optional[str] = None
        if self._consecutive_same_error >= self._max_consecutive_same_error:
            stop_reason = "same_error_repeated"
        elif self._attempt_count >= self._max_total_failures:
            stop_reason = "total_failures_exceeded"
        elif self.elapsed_ms >= self._max_elapsed_ms:
            stop_reason = "deadline_exceeded"
        return SellFailureSnapshot(
            attempt_count=self._attempt_count,
            consecutive_same_error=self._consecutive_same_error,
            error_signature=self._error_signature,
            last_error=self._last_error,
            elapsed_ms=self.elapsed_ms,
            stop_reason=stop_reason,
        )

    @property
    def elapsed_ms(self) -> int:
        return int((self._clock() - self._started_at) * 1000)
