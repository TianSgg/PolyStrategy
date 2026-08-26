"""OrderExecutor — 对接 Polymarket CLOB 的下单/撤单执行器。

实现 strategy_runtime.interfaces.OrderExecutorProtocol。
策略可直接使用，也可继承重写特定方法。

余额管理：
  集成 BalancePoller（后台 1s 轮询），下单前检查缓存余额，
  失败后自动刷新并可重试。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from account.service import get_account_service
from framework.strategy_runtime.interfaces import OrderResult
from framework.strategy_runtime.toolkit.execution.balance_poller import BalancePoller, get_or_create_poller

logger = logging.getLogger(__name__)

DEFAULT_GTD_SEC = 1800


def generate_order_id() -> str:
    return str(uuid.uuid4())


class OrderExecutor:
    """Polymarket CLOB 下单执行器（集成余额轮询）。"""

    def __init__(self, proxy_wallet: str = "") -> None:
        self._account_service = get_account_service()
        self._proxy_wallet = proxy_wallet
        self._cached_params: Dict[str, Dict[str, Any]] = {}
        self._poller: Optional[BalancePoller] = None

    def cache_market_params(
        self, token_id: str, tick_size: str, neg_risk: bool
    ) -> None:
        self._cached_params[token_id] = {
            "tick_size": tick_size,
            "neg_risk": neg_risk,
        }

    async def ensure_poller(self, proxy_wallet: str = "") -> BalancePoller:
        """获取当前账户的 BalancePoller（懒加载）。"""
        wallet = (proxy_wallet or self._proxy_wallet).lower()
        if self._poller is None or self._poller._proxy_wallet != wallet:
            self._poller = await get_or_create_poller(wallet)
        return self._poller

    @property
    def available_cash(self) -> Decimal:
        """当前可用余额（instant read）。poller 未就绪时返回 0。"""
        if self._poller is None:
            return Decimal("0")
        return self._poller.available_cash

    def position_size(self, token_id: str) -> Decimal:
        """当前持仓数量（instant read）。"""
        if self._poller is None:
            return Decimal("0")
        return self._poller.position_size(token_id)

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: str,
        size: str,
        *,
        proxy_wallet: str = "",
        tick_size: str | None = None,
        neg_risk: bool | None = None,
        gtd_sec: int = DEFAULT_GTD_SEC,
        check_balance: bool = True,
    ) -> OrderResult:
        """下单。返回 OrderResult。

        check_balance=True 时，BUY 订单会先检查缓存余额，
        余额不足直接返回 insufficient_balance 而不发请求。
        """
        wallet = (proxy_wallet or self._proxy_wallet).lower()
        params = self._cached_params.get(token_id, {})
        ts = tick_size or params.get("tick_size", "0.01")
        nr = neg_risk if neg_risk is not None else params.get("neg_risk", False)

        order_id = generate_order_id()

        poller = await self.ensure_poller(wallet)

        if check_balance and side.upper() == "BUY":
            notional = Decimal(price) * Decimal(size)
            if poller.available_cash < notional:
                logger.warning(
                    "Insufficient balance: need=%s available=%s wallet=%s",
                    notional, poller.available_cash, wallet[:8],
                )
                return OrderResult(
                    order_id=order_id,
                    status="insufficient_balance",
                    filled_size="0",
                    filled_price=None,
                )

        send_ns = time.monotonic_ns()

        try:
            result = await asyncio.to_thread(
                self._account_service.place_limit_order,
                wallet,
                token_id,
                side.upper(),
                float(Decimal(size)),
                float(Decimal(price)),
                ts,
                nr,
                gtd_sec if side.upper() == "BUY" else None,
            )
        except Exception as e:
            logger.error("Order failed: token=%s side=%s err=%s", token_id, side, e)
            asyncio.create_task(poller.refresh())
            return OrderResult(
                order_id=order_id,
                status="failed",
                filled_size="0",
                filled_price=None,
            )

        parsed = self._parse_result(order_id, result, send_ns)
        if parsed.status == "failed":
            asyncio.create_task(poller.refresh())
        return parsed

    async def cancel_order(self, order_id: str, *, proxy_wallet: str = "") -> bool:
        """撤单。成功返回 True。"""
        wallet = (proxy_wallet or self._proxy_wallet).lower()
        try:
            await asyncio.to_thread(
                self._account_service.cancel_order, wallet, order_id
            )
            return True
        except Exception as e:
            logger.error("Cancel failed: order=%s err=%s", order_id, e)
            return False

    def _parse_result(
        self, order_id: str, result: Optional[Dict[str, Any]], send_ns: int
    ) -> OrderResult:
        latency_ms = (time.monotonic_ns() - send_ns) / 1_000_000
        logger.debug("Order response latency: %.1fms", latency_ms)

        if not result:
            return OrderResult(
                order_id=order_id, status="failed", filled_size="0", filled_price=None
            )

        raw_status = result.get("status", "")
        clob_order_id = result.get("orderID", order_id)

        if raw_status == "matched":
            taking = Decimal(str(result.get("takingAmount", 0)))
            making = Decimal(str(result.get("makingAmount", 0)))
            filled = taking if taking > 0 else making
            return OrderResult(
                order_id=clob_order_id,
                status="filled",
                filled_size=str(filled),
                filled_price=None,
            )
        elif raw_status == "live":
            return OrderResult(
                order_id=clob_order_id,
                status="live",
                filled_size="0",
                filled_price=None,
            )
        else:
            return OrderResult(
                order_id=clob_order_id,
                status="failed",
                filled_size="0",
                filled_price=None,
            )

    def balance_snapshot(self) -> Dict[str, Any]:
        """返回当前余额快照（用于日志/API 观察）。"""
        if self._poller is None:
            return {"status": "poller_not_initialized"}
        return self._poller.snapshot()
