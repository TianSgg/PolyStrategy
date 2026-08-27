"""TickVerifier — HTTP 校验 tick_size=0.001，带超时/退避/重试。

WS 通知 tick 变为 0.001 仅是候选条件。SELL 前必须通过此模块主动校验。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from framework.strategy_runtime.market_data import MarketData

logger = logging.getLogger(__name__)

TARGET_TICK = Decimal("0.001")


@dataclass
class TickVerifyResult:
    confirmed: bool
    actual_tick: Optional[Decimal] = None
    error: Optional[str] = None


class TickVerifier:
    """对指定 token 执行 HTTP tick_size 校验。"""

    def __init__(
        self,
        market_data: Optional[MarketData] = None,
        backoff_ms: int = 1000,
        max_backoff_ms: int = 5000,
        max_duration_s: int = 1800,
    ) -> None:
        self._market_data = market_data or MarketData()
        self._backoff_ms = backoff_ms
        self._max_backoff_ms = max_backoff_ms
        self._max_duration_s = max_duration_s

    async def verify(self, token_id: str) -> TickVerifyResult:
        """持续校验直到 HTTP 确认 tick_size=0.001，最长 30 分钟。"""
        import time
        deadline = time.monotonic() + self._max_duration_s
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                tick_size = await self._market_data.get_tick_size(token_id)
                actual = Decimal(str(tick_size)) if tick_size else None

                if actual == TARGET_TICK:
                    return TickVerifyResult(confirmed=True, actual_tick=actual)

                logger.info(
                    "Tick check attempt %d: token=%s tick=%s (expected %s)",
                    attempt, token_id[:8], actual, TARGET_TICK,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Tick check error attempt %d: %s", attempt, e
                )

            backoff = min(
                self._backoff_ms * (2 ** (attempt - 1)),
                self._max_backoff_ms,
            ) / 1000.0
            await asyncio.sleep(backoff)

        logger.warning("Tick verify timeout after %ds: token=%s", self._max_duration_s, token_id[:8])
        return TickVerifyResult(confirmed=False, error="timeout")
