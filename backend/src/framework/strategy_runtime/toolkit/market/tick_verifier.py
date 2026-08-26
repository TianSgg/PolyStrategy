"""TickVerifier — HTTP 校验 tick_size=0.001，带超时/退避/重试。

WS 通知 tick 变为 0.001 仅是候选条件。SELL 前必须通过此模块主动校验。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from framework.strategy_runtime.toolkit.market.market_data import MarketData

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
        max_retries: int = 3,
        backoff_ms: int = 1000,
    ) -> None:
        self._market_data = market_data or MarketData()
        self._max_retries = max_retries
        self._backoff_ms = backoff_ms

    async def verify(self, token_id: str) -> TickVerifyResult:
        """执行校验，失败时指数退避重试。"""
        for attempt in range(1, self._max_retries + 1):
            try:
                tick_size = await self._market_data.get_tick_size(token_id)
                actual = Decimal(str(tick_size)) if tick_size else None

                if actual == TARGET_TICK:
                    return TickVerifyResult(confirmed=True, actual_tick=actual)

                logger.info(
                    "Tick check attempt %d/%d: token=%s tick=%s (expected %s)",
                    attempt, self._max_retries, token_id[:8], actual, TARGET_TICK,
                )
            except Exception as e:
                logger.warning(
                    "Tick check error attempt %d/%d: %s", attempt, self._max_retries, e
                )
                if attempt == self._max_retries:
                    return TickVerifyResult(confirmed=False, error=str(e))

            if attempt < self._max_retries:
                backoff = self._backoff_ms * (2 ** (attempt - 1)) / 1000.0
                await asyncio.sleep(backoff)

        return TickVerifyResult(confirmed=False, actual_tick=None)
