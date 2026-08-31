"""TickVerifier — 三源校验 tick_size=0.001，带超时/退避/重试。

WS 通知 tick 变为 0.001 仅是候选条件。SELL 前必须同时通过 WS、
HTTP /tick-size 和 HTTP /book 三个来源校验。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
import time

from framework.strategy_runtime.tick_size_service import (
    TickSizeConsensusError,
    TickSizeService,
)

logger = logging.getLogger(__name__)

TARGET_TICK = Decimal("0.001")


@dataclass
class TickVerifyResult:
    confirmed: bool
    actual_tick: Optional[Decimal] = None
    ws_tick_size: Optional[Decimal] = None
    tick_api_size: Optional[Decimal] = None
    book_tick_size: Optional[Decimal] = None
    error: Optional[str] = None


class TickVerifier:
    """对指定 token 执行三源 tick_size 校验。"""

    def __init__(
        self,
        tick_size_service: Optional[TickSizeService] = None,
        backoff_ms: int = 1000,
        max_backoff_ms: int = 5000,
        max_duration_s: int = 1800,
    ) -> None:
        self._tick_size_service = tick_size_service or TickSizeService()
        self._backoff_ms = backoff_ms
        self._max_backoff_ms = max_backoff_ms
        self._max_duration_s = max_duration_s

    async def verify(
        self,
        token_id: str,
        ws_tick_size: Decimal,
    ) -> TickVerifyResult:
        """持续校验直到三个来源都确认 tick_size=0.001，最长 30 分钟。"""
        deadline = time.monotonic() + self._max_duration_s
        attempt = 0
        last_result = TickVerifyResult(
            confirmed=False,
            ws_tick_size=ws_tick_size,
            error="not_started",
        )

        while time.monotonic() < deadline:
            attempt += 1
            try:
                actual = await self._tick_size_service.refresh_consensus(
                    token_id, ws_tick_size
                )

                if actual == TARGET_TICK:
                    return TickVerifyResult(
                        confirmed=True,
                        actual_tick=actual,
                        ws_tick_size=ws_tick_size,
                        tick_api_size=actual,
                        book_tick_size=actual,
                    )

                logger.info(
                    "Tick check attempt %d: token=%s consensus=%s (expected %s)",
                    attempt, token_id[:8], actual, TARGET_TICK,
                )
                last_result = TickVerifyResult(
                    confirmed=False,
                    actual_tick=actual,
                    ws_tick_size=ws_tick_size,
                    tick_api_size=actual,
                    book_tick_size=actual,
                    error="unexpected_tick_size",
                )
            except TickSizeConsensusError as exc:
                logger.warning(
                    "Tick source mismatch attempt %d: token=%s ws=%s tick_api=%s book=%s",
                    attempt,
                    token_id[:8],
                    exc.ws_tick_size,
                    exc.tick_api_size,
                    exc.book_tick_size,
                )
                last_result = TickVerifyResult(
                    confirmed=False,
                    ws_tick_size=exc.ws_tick_size,
                    tick_api_size=exc.tick_api_size,
                    book_tick_size=exc.book_tick_size,
                    error="tick_source_mismatch",
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Tick check error attempt %d: %s", attempt, e
                )
                last_result = TickVerifyResult(
                    confirmed=False,
                    ws_tick_size=ws_tick_size,
                    error=str(e),
                )

            backoff = min(
                self._backoff_ms * (2 ** (attempt - 1)),
                self._max_backoff_ms,
            ) / 1000.0
            await asyncio.sleep(backoff)

        logger.warning("Tick verify timeout after %ds: token=%s", self._max_duration_s, token_id[:8])
        last_result.error = last_result.error or "timeout"
        return last_result
