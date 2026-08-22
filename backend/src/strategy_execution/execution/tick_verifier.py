"""TickVerifier — HTTP 校验 tick_size=0.001，带超时/退避/重试。

WS 通知 tick 变为 0.001 仅是候选条件。SELL 前必须通过此模块主动校验。
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from market import get_market_service
from strategy_execution.enums import RunEventType
from strategy_execution.run_state import RunStateMachine

logger = logging.getLogger(__name__)

TARGET_TICK = Decimal("0.001")


class TickVerifyResult:
    """验证结果。"""
    def __init__(self, confirmed: bool, actual_tick: Optional[Decimal] = None, error: Optional[str] = None):
        self.confirmed = confirmed
        self.actual_tick = actual_tick
        self.error = error


class TickVerifier:
    """对指定 token 执行 HTTP tick_size 校验。"""

    def __init__(self, max_retries: int = 3, backoff_ms: int = 1000) -> None:
        self._max_retries = max_retries
        self._backoff_ms = backoff_ms

    async def verify(
        self,
        token_id: str,
        run_machine: Optional[RunStateMachine] = None,
    ) -> TickVerifyResult:
        """执行校验，失败时指数退避重试。

        Returns:
            TickVerifyResult: confirmed=True 表示 tick=0.001 已确认。
        """
        market_svc = get_market_service()

        for attempt in range(1, self._max_retries + 1):
            try:
                tick_size = await market_svc.get_tick_size(token_id)
                actual = Decimal(str(tick_size)) if tick_size else None

                if actual == TARGET_TICK:
                    if run_machine:
                        await run_machine.emit_event(RunEventType.TICK_CHECK_PASS, {
                            "token_id": token_id,
                            "attempt": attempt,
                            "tick_size": str(actual),
                        })
                    return TickVerifyResult(confirmed=True, actual_tick=actual)

                logger.info(
                    "Tick check attempt %d/%d: token=%s tick=%s (expected %s)",
                    attempt, self._max_retries, token_id, actual, TARGET_TICK,
                )

                if run_machine and attempt == self._max_retries:
                    await run_machine.emit_event(RunEventType.TICK_CHECK_FAIL, {
                        "token_id": token_id,
                        "attempt": attempt,
                        "actual_tick": str(actual),
                    })

            except Exception as e:
                logger.warning("Tick check error attempt %d/%d: %s", attempt, self._max_retries, e)
                if run_machine and attempt == self._max_retries:
                    await run_machine.emit_event(RunEventType.TICK_CHECK_FAIL, {
                        "token_id": token_id,
                        "attempt": attempt,
                        "error": str(e),
                    })
                if attempt == self._max_retries:
                    return TickVerifyResult(confirmed=False, error=str(e))

            # 指数退避
            if attempt < self._max_retries:
                backoff = self._backoff_ms * (2 ** (attempt - 1)) / 1000.0
                await asyncio.sleep(backoff)

        return TickVerifyResult(confirmed=False, actual_tick=None)
