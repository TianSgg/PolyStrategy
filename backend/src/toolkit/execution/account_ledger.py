"""AccountLedger — 内存资金/份额保留管理。

核心不变式:
  available_cash = real_cash - reserved_buy_notional - safety_buffer
  available_shares(token) = confirmed_position - reserved_sell_shares

策略可直接使用或继承重写。
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SAFETY_BUFFER = Decimal("5")


class AccountLedger:
    """单个执行账户的资金状态管理。"""

    def __init__(
        self,
        initial_cash: Decimal = Decimal("0"),
        safety_buffer: Decimal = SAFETY_BUFFER,
    ) -> None:
        self._lock = asyncio.Lock()
        self._real_cash = initial_cash
        self._safety_buffer = safety_buffer
        self._reserved_buy: Decimal = Decimal("0")
        self._positions: Dict[str, Decimal] = {}
        self._reserved_sell: Dict[str, Decimal] = {}

    @property
    def available_cash(self) -> Decimal:
        return max(Decimal("0"), self._real_cash - self._reserved_buy - self._safety_buffer)

    def available_shares(self, token_id: str) -> Decimal:
        pos = self._positions.get(token_id, Decimal("0"))
        reserved = self._reserved_sell.get(token_id, Decimal("0"))
        return max(Decimal("0"), pos - reserved)

    def max_buy_shares(self, price: Decimal) -> Decimal:
        if price <= 0:
            return Decimal("0")
        return (self.available_cash / price).quantize(Decimal("1"), rounding=ROUND_DOWN)

    async def reserve_buy(self, price: Decimal, shares: Decimal) -> bool:
        notional = price * shares
        async with self._lock:
            if self.available_cash < notional:
                return False
            self._reserved_buy += notional
            return True

    async def release_buy(self, price: Decimal, shares: Decimal) -> None:
        notional = price * shares
        async with self._lock:
            self._reserved_buy = max(Decimal("0"), self._reserved_buy - notional)

    async def confirm_buy_fill(
        self, token_id: str, price: Decimal, filled_shares: Decimal
    ) -> None:
        notional = price * filled_shares
        async with self._lock:
            self._reserved_buy = max(Decimal("0"), self._reserved_buy - notional)
            self._real_cash -= notional
            self._positions[token_id] = (
                self._positions.get(token_id, Decimal("0")) + filled_shares
            )

    async def reserve_sell(self, token_id: str, shares: Decimal) -> bool:
        async with self._lock:
            if self.available_shares(token_id) < shares:
                return False
            self._reserved_sell[token_id] = (
                self._reserved_sell.get(token_id, Decimal("0")) + shares
            )
            return True

    async def release_sell(self, token_id: str, shares: Decimal) -> None:
        async with self._lock:
            current = self._reserved_sell.get(token_id, Decimal("0"))
            self._reserved_sell[token_id] = max(Decimal("0"), current - shares)

    async def confirm_sell_fill(
        self, token_id: str, price: Decimal, filled_shares: Decimal
    ) -> None:
        async with self._lock:
            current_reserved = self._reserved_sell.get(token_id, Decimal("0"))
            self._reserved_sell[token_id] = max(Decimal("0"), current_reserved - filled_shares)
            self._positions[token_id] = max(
                Decimal("0"), self._positions.get(token_id, Decimal("0")) - filled_shares
            )
            self._real_cash += price * filled_shares

    async def calibrate(self, real_cash: Decimal, positions: Dict[str, Decimal]) -> None:
        async with self._lock:
            self._real_cash = real_cash
            for token_id, size in positions.items():
                self._positions[token_id] = size
            for token_id in list(self._positions.keys()):
                if token_id not in positions:
                    self._positions[token_id] = Decimal("0")

    def snapshot(self) -> Dict[str, Any]:
        return {
            "real_cash": str(self._real_cash),
            "reserved_buy": str(self._reserved_buy),
            "available_cash": str(self.available_cash),
            "positions": {k: str(v) for k, v in self._positions.items()},
            "reserved_sell": {k: str(v) for k, v in self._reserved_sell.items()},
        }
