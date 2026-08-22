"""OrderProjection — 幂等投影订单状态变更到运行和账本。

接收来源：
  1. 下单响应（place_fast_buy / place_normal_order 返回）
  2. User WS 事件（PLACEMENT / UPDATE / TRADE CONFIRMED / CANCELLATION）
  3. 轮询校准

所有来源经此模块统一处理，保证：
  - 同一 fill 不会重复增加持仓或释放保留
  - 撤单只释放未成交部分
  - 状态只向前推进（不回退）
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, Optional, Set

from strategy_execution.contracts import OrderReport
from strategy_execution.enums import (
    OrderPurpose,
    OrderSide,
    OrderStatus,
    RunEventType,
)
from strategy_execution.execution.account_ledger import AccountExecutionLedger
from strategy_execution.repository import StrategyOrderRepository
from strategy_execution.run_state import RunStateMachine

logger = logging.getLogger(__name__)

# 订单终态
_TERMINAL_STATUSES: Set[OrderStatus] = {
    OrderStatus.MATCHED,
    OrderStatus.CANCELED,
    OrderStatus.FAILED,
}


class OrderProjection:
    """将订单回报投影到内存状态。"""

    def __init__(
        self,
        order_repo: Optional[StrategyOrderRepository] = None,
    ) -> None:
        self._order_repo = order_repo or StrategyOrderRepository()
        # {client_order_id: 已处理的 trade_id 集合}
        self._processed_fills: Dict[str, Set[str]] = {}
        # {client_order_id: 上次已知的 matched_size}
        self._last_matched: Dict[str, Decimal] = {}

    async def on_fill(
        self,
        *,
        client_order_id: str,
        trade_id: str,
        fill_price: Decimal,
        fill_shares: Decimal,
        total_matched: Decimal,
        run_machine: RunStateMachine,
        ledger: AccountExecutionLedger,
        order_meta: Dict[str, Any],
    ) -> bool:
        """处理一笔成交。返回 True 表示新增处理，False 表示重复。"""
        # 幂等检查
        fills = self._processed_fills.setdefault(client_order_id, set())
        if trade_id in fills:
            logger.debug("Duplicate fill ignored: order=%s trade=%s", client_order_id, trade_id)
            return False
        fills.add(trade_id)

        # 更新 DB
        self._order_repo.update_fill(
            client_order_id,
            matched_size=total_matched,
            avg_matched_price=fill_price,
            status=OrderStatus.MATCHED.value if total_matched >= order_meta.get("requested_size", Decimal("0"))
            else OrderStatus.PARTIALLY_MATCHED.value,
        )

        # 更新账本和运行聚合
        side = order_meta.get("side", "BUY")
        token_id = order_meta.get("token_id", "")
        run_id = order_meta.get("run_id", "")

        if side == "BUY":
            await ledger.confirm_buy_fill(
                run_id=run_id,
                order_id=client_order_id,
                token_id=token_id,
                price=fill_price,
                filled_shares=fill_shares,
                trade_id=trade_id,
            )
            await run_machine.record_fill(fill_price, fill_shares, is_buy=True)
        else:
            await ledger.confirm_sell_fill(
                run_id=run_id,
                order_id=client_order_id,
                token_id=token_id,
                price=fill_price,
                filled_shares=fill_shares,
                trade_id=trade_id,
            )
            await run_machine.record_fill(fill_price, fill_shares, is_buy=False)

        # 追加时间线事件
        await run_machine.emit_event(RunEventType.FILL, {
            "client_order_id": client_order_id,
            "trade_id": trade_id,
            "side": side,
            "price": str(fill_price),
            "shares": str(fill_shares),
            "total_matched": str(total_matched),
        })

        self._last_matched[client_order_id] = total_matched
        return True

    async def on_cancel(
        self,
        *,
        client_order_id: str,
        run_machine: RunStateMachine,
        ledger: AccountExecutionLedger,
        order_meta: Dict[str, Any],
    ) -> bool:
        """处理撤单确认。只释放未成交部分。"""
        requested = order_meta.get("requested_size", Decimal("0"))
        matched = self._last_matched.get(client_order_id, Decimal("0"))
        unrealized = max(Decimal("0"), requested - matched)

        if unrealized <= 0:
            return False

        side = order_meta.get("side", "BUY")
        token_id = order_meta.get("token_id", "")
        run_id = order_meta.get("run_id", "")
        price = order_meta.get("limit_price", Decimal("0.99"))

        if side == "BUY":
            await ledger.release_buy(
                run_id=run_id,
                order_id=client_order_id,
                token_id=token_id,
                price=price,
                unrealized_shares=unrealized,
            )
        else:
            await ledger.release_sell(
                run_id=run_id,
                order_id=client_order_id,
                token_id=token_id,
                unrealized_shares=unrealized,
            )

        self._order_repo.update_response(
            client_order_id,
            clob_order_id=order_meta.get("clob_order_id"),
            status=OrderStatus.CANCELED.value,
            responded_at=None,
        )

        await run_machine.emit_event(RunEventType.CANCEL_CONFIRMED, {
            "client_order_id": client_order_id,
            "unrealized_shares": str(unrealized),
        })

        return True

    async def on_order_failed(
        self,
        *,
        client_order_id: str,
        run_machine: RunStateMachine,
        ledger: AccountExecutionLedger,
        order_meta: Dict[str, Any],
        error: str,
    ) -> None:
        """下单失败 — 释放全部保留。"""
        side = order_meta.get("side", "BUY")
        token_id = order_meta.get("token_id", "")
        run_id = order_meta.get("run_id", "")
        price = order_meta.get("limit_price", Decimal("0.99"))
        size = order_meta.get("requested_size", Decimal("0"))

        if side == "BUY":
            await ledger.release_buy(
                run_id=run_id,
                order_id=client_order_id,
                token_id=token_id,
                price=price,
                unrealized_shares=size,
            )
        else:
            await ledger.release_sell(
                run_id=run_id,
                order_id=client_order_id,
                token_id=token_id,
                unrealized_shares=size,
            )

        await run_machine.emit_event(RunEventType.ORDER_RESPONSE, {
            "client_order_id": client_order_id,
            "status": "FAILED",
            "error": error,
        })

    def cleanup_order(self, client_order_id: str) -> None:
        """运行关闭后清理内存。"""
        self._processed_fills.pop(client_order_id, None)
        self._last_matched.pop(client_order_id, None)
