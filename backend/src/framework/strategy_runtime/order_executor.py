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

from framework.trading import place_limit_order, cancel_order as _cancel_order
from framework.trading.provider import get_client
from framework.strategy_runtime.interfaces import CancelResult, OrderResult
from framework.strategy_runtime.balance_poller import BalancePoller, get_or_create_poller
from framework.strategy_runtime.tick_size_service import (
    TickSizeFetchError,
    TickSizeService,
)
from framework.user_ws import UserWS, get_or_create_user_ws

logger = logging.getLogger(__name__)

DEFAULT_GTD_SEC = 1800
_KEEPALIVE_INTERVAL_SEC = 30


def generate_order_id() -> str:
    return str(uuid.uuid4())


class OrderExecutor:
    """Polymarket CLOB 下单执行器（集成余额轮询）。"""

    def __init__(
        self,
        proxy_wallet: str = "",
        *,
        tick_size_service: Optional[TickSizeService] = None,
    ) -> None:
        self._proxy_wallet = proxy_wallet
        self._tick_size_service = tick_size_service
        self._cached_params: Dict[str, Dict[str, Any]] = {}
        self._poller: Optional[BalancePoller] = None
        self._user_ws: Optional[UserWS] = None
        self._keepalive_task: Optional[asyncio.Task] = None

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

    async def ensure_user_ws(self) -> UserWS:
        """懒加载，首次下单时创建 WS 连接。"""
        if self._user_ws is None:
            self._user_ws = await get_or_create_user_ws(self._proxy_wallet)
        return self._user_ws

    async def warmup(self) -> None:
        """预热 HTTP 连接（建立 TCP+TLS）并启动保活任务。"""
        wallet = self._proxy_wallet.lower()
        try:
            await asyncio.to_thread(self._warmup_sync, wallet)
            logger.info("[OrderExecutor] HTTP connection warmed up for %s", wallet[:8])
        except Exception as e:
            logger.warning("[OrderExecutor] Warmup failed: %s", e)

        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _warmup_sync(self, wallet: str) -> None:
        client = get_client(wallet)
        client.get_server_time()

    async def _keepalive_loop(self) -> None:
        """每 30s 发一次 /time 请求保持 HTTP 连接热。"""
        wallet = self._proxy_wallet.lower()
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL_SEC)
            try:
                await asyncio.to_thread(self._warmup_sync, wallet)
            except Exception:
                pass

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
        validate_tick_size: bool = True,
    ) -> OrderResult:
        """下单。返回 OrderResult。

        check_balance=True 时，BUY 订单会先检查缓存余额，
        余额不足直接返回 insufficient_balance 而不发请求。
        validate_tick_size=False 用于风控紧急卖出：
        使用调用方提供的 tick_size，不额外请求或校验 tick size。
        """
        wallet = (proxy_wallet or self._proxy_wallet).lower()
        params = self._cached_params.get(token_id, {})
        nr = neg_risk if neg_risk is not None else params.get("neg_risk", True)

        order_id = generate_order_id()

        if self._tick_size_service is not None and validate_tick_size:
            try:
                authoritative_tick = await self._tick_size_service.get(token_id)
            except TickSizeFetchError as exc:
                logger.error(
                    "Tick size lookup failed before order: token=%s side=%s err=%s",
                    token_id, side, exc,
                )
                return OrderResult(
                    order_id=order_id,
                    status="failed",
                    filled_size="0",
                    filled_price=None,
                    error=f"tick size refresh failed: {exc}",
                )

            if tick_size is not None and Decimal(tick_size) != authoritative_tick:
                error = (
                    f"tick size mismatch: caller={tick_size}, "
                    f"authoritative={authoritative_tick}"
                )
                logger.error(
                    "Order rejected before send: token=%s side=%s %s",
                    token_id, side, error,
                )
                return OrderResult(
                    order_id=order_id,
                    status="failed",
                    filled_size="0",
                    filled_price=None,
                    error=error,
                )

            ts = str(authoritative_tick)
        else:
            ts = tick_size or params.get("tick_size", "0.01")

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
                place_limit_order,
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
                error=str(e),
            )

        parsed = self._parse_result(
            order_id, result, send_ns, Decimal(size), side.upper()
        )
        if parsed.status == "failed":
            asyncio.create_task(poller.refresh())
        return parsed

    async def cancel_order(self, order_id: str, *, proxy_wallet: str = "") -> bool:
        """撤单。成功返回 True。"""
        wallet = (proxy_wallet or self._proxy_wallet).lower()
        try:
            await asyncio.to_thread(_cancel_order, wallet, order_id)
            return True
        except Exception as e:
            logger.error("Cancel failed: order=%s err=%s", order_id, e)
            return False

    async def _get_order(self, order_id: str) -> dict:
        """GET /data/order/{orderID}"""
        wallet = self._proxy_wallet.lower()
        client = get_client(wallet)
        return await asyncio.to_thread(client.get_order, order_id)

    async def _get_order_after_cancel(
        self, order_id: str,
    ) -> tuple[Optional[dict], Optional[str]]:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                info = await self._get_order(order_id)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "get_order after cancel failed attempt=%s order=%s err=%s",
                    attempt + 1, order_id, exc,
                )
                await asyncio.sleep(0.25)
                continue

            if info is None:
                await asyncio.sleep(0.25)
                continue

            if info.get("status") == "LIVE":
                await asyncio.sleep(0.5)
                continue

            return info, None

        if last_error is not None:
            return None, str(last_error)
        return None, "order query did not return a final status"

    async def cancel_order_with_fill_check(self, order_id: str) -> CancelResult:
        """撤单 + REST 查询最终成交量。"""
        cancelled = await self.cancel_order(order_id)
        info, query_error = await self._get_order_after_cancel(order_id)
        if query_error or info is None:
            return CancelResult(
                order_id=order_id,
                cancelled=cancelled,
                final_matched=Decimal("-1"),
                status="query_failed",
                query_failed=True,
            )
        return CancelResult(
            order_id=order_id,
            cancelled=cancelled,
            final_matched=Decimal(info.get("size_matched", "0")),
            status=info.get("status", "unknown"),
        )

    def _parse_result(
        self, order_id: str, result: Optional[Dict[str, Any]], send_ns: int,
        order_size: Decimal,
        side: str,
    ) -> OrderResult:
        latency_ms = (time.monotonic_ns() - send_ns) / 1_000_000
        logger.debug("Order response latency: %.1fms", latency_ms)

        if not result:
            return OrderResult(
                order_id=order_id, status="failed", filled_size="0", filled_price=None,
                error="empty response from CLOB",
            )

        raw_status = result.get("status", "")
        clob_order_id = result.get("orderID", order_id)

        taking_str = str(result.get("takingAmount", 0)) if result else None
        making_str = str(result.get("makingAmount", 0)) if result else None

        if raw_status == "matched":
            taking = Decimal(taking_str or 0)
            making = Decimal(making_str or 0)
            # For BUY, taker amount is outcome shares; for SELL, maker amount
            # is outcome shares while taker amount is USDC proceeds.
            filled = taking if side == "BUY" else making
            if filled <= 0:
                return OrderResult(
                    order_id=clob_order_id,
                    status="failed",
                    filled_size="0",
                    filled_price=None,
                    error=f"matched CLOB response missing fill amount: {result}",
                    clob_status=raw_status,
                    clob_taking=taking_str,
                    clob_making=making_str,
                )
            # partial = some filled but remaining rests on book
            is_partial = filled < order_size
            return OrderResult(
                order_id=clob_order_id,
                status="partial" if is_partial else "filled",
                filled_size=str(filled),
                filled_price=None,
                clob_status=raw_status,
                clob_taking=taking_str,
                clob_making=making_str,
            )
        elif raw_status == "live":
            return OrderResult(
                order_id=clob_order_id,
                status="live",
                filled_size="0",
                filled_price=None,
                clob_status=raw_status,
                clob_taking=taking_str,
                clob_making=making_str,
            )
        else:
            return OrderResult(
                order_id=clob_order_id,
                status="failed",
                filled_size="0",
                filled_price=None,
                error=f"unexpected CLOB status: {raw_status} | {result}",
                clob_status=raw_status,
                clob_taking=taking_str,
                clob_making=making_str,
            )

    def balance_snapshot(self) -> Dict[str, Any]:
        """返回当前余额快照（用于日志/API 观察）。"""
        if self._poller is None:
            return {"status": "poller_not_initialized"}
        return self._poller.snapshot()
