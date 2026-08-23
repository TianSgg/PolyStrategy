"""OrderTracker — 订单生命周期跟踪，幂等投影成交/撤单到账本。

策略可用此组件统一处理来自下单响应和 WS 事件的状态更新。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, Set

from toolkit.execution.account_ledger import AccountLedger

logger = logging.getLogger(__name__)


class OrderTracker:
    """跟踪订单状态，将 fill/cancel 幂等投影到账本。"""

    def __init__(self, ledger: AccountLedger) -> None:
        self._ledger = ledger
        self._processed_fills: Dict[str, Set[str]] = {}
        self._matched_sizes: Dict[str, Decimal] = {}

    async def on_fill(
        self,
        *,
        order_id: str,
        trade_id: str,
        token_id: str,
        side: str,
        fill_price: Decimal,
        fill_shares: Decimal,
    ) -> bool:
        """处理一笔成交。返回 True 表示新增，False 表示重复。"""
        fills = self._processed_fills.setdefault(order_id, set())
        if trade_id in fills:
            return False
        fills.add(trade_id)

        if side.upper() == "BUY":
            await self._ledger.confirm_buy_fill(token_id, fill_price, fill_shares)
        else:
            await self._ledger.confirm_sell_fill(token_id, fill_price, fill_shares)

        self._matched_sizes[order_id] = (
            self._matched_sizes.get(order_id, Decimal("0")) + fill_shares
        )
        return True

    async def on_cancel(
        self,
        *,
        order_id: str,
        token_id: str,
        side: str,
        requested_size: Decimal,
        price: Decimal,
    ) -> None:
        """处理撤单 — 释放未成交部分。"""
        matched = self._matched_sizes.get(order_id, Decimal("0"))
        unrealized = max(Decimal("0"), requested_size - matched)
        if unrealized <= 0:
            return

        if side.upper() == "BUY":
            await self._ledger.release_buy(price, unrealized)
        else:
            await self._ledger.release_sell(token_id, unrealized)

    def cleanup(self, order_id: str) -> None:
        self._processed_fills.pop(order_id, None)
        self._matched_sizes.pop(order_id, None)
