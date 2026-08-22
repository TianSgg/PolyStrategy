"""OrderExecutor — 快速 BUY 和常规下单的唯一执行入口。

快速路径：信号入场专用，预热客户端，不做额外 HTTP 查询。
常规路径：撤单、退场 SELL、重试，允许按需校验。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from account.service import get_account_service
from shared.time_utils import now_utc8_dt
from strategy_execution.contracts import OrderReport, OrderRequest
from strategy_execution.enums import (
    ExecutionMode,
    OrderPurpose,
    OrderSide,
    OrderStatus,
)
from strategy_execution.repository import StrategyOrderRepository

logger = logging.getLogger(__name__)

# 固定下单参数
FAST_BUY_PRICE = Decimal("0.99")
TICK_EXIT_PRICE = Decimal("0.999")
SELL_099_PRICE = Decimal("0.99")
DEFAULT_GTD_SEC = 1800


class OrderExecutor:
    """策略订单的唯一执行入口。"""

    def __init__(
        self,
        order_repo: Optional[StrategyOrderRepository] = None,
    ) -> None:
        self._account_service = get_account_service()
        self._order_repo = order_repo or StrategyOrderRepository()
        # 预热的客户端参数缓存: {proxy_wallet: {token_id: {tick_size, neg_risk}}}
        self._cached_params: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def cache_market_params(
        self,
        proxy_wallet: str,
        token_id: str,
        tick_size: str,
        neg_risk: bool,
    ) -> None:
        """启动/配置启用时预热下单参数。"""
        wallet_cache = self._cached_params.setdefault(proxy_wallet.lower(), {})
        wallet_cache[token_id] = {"tick_size": tick_size, "neg_risk": neg_risk}

    async def place_fast_buy(
        self,
        *,
        proxy_wallet: str,
        request: OrderRequest,
        gtd_sec: int = DEFAULT_GTD_SEC,
    ) -> OrderReport:
        """快速 BUY — 信号入场专用。不做额外网络查询。"""
        assert request.execution_mode == ExecutionMode.FAST
        assert request.side == OrderSide.BUY

        # 获取预热参数
        wallet_lower = proxy_wallet.lower()
        params = self._cached_params.get(wallet_lower, {}).get(request.token_id)
        tick_size = params["tick_size"] if params else "0.01"
        neg_risk = params["neg_risk"] if params else False

        # 持久化订单记录
        order_record = {
            "id": request.client_order_id,
            "run_id": request.run_id,
            "client_order_id": request.client_order_id,
            "purpose": request.purpose.value,
            "execution_mode": ExecutionMode.FAST.value,
            "side": OrderSide.BUY.value,
            "status": OrderStatus.PENDING.value,
            "limit_price": request.limit_price,
            "requested_size": request.size,
            "sent_at": now_utc8_dt(),
        }
        self._order_repo.create(order_record)

        # 执行下单
        send_ns = time.monotonic_ns()
        try:
            result = await asyncio.to_thread(
                self._account_service.place_limit_order,
                wallet_lower,
                request.token_id,
                "BUY",
                float(request.size),
                float(request.limit_price),
                tick_size,
                neg_risk,
                gtd_sec,
            )
        except Exception as e:
            logger.error("Fast BUY failed for run %s: %s", request.run_id, e)
            self._order_repo.update_response(
                request.client_order_id,
                clob_order_id=None,
                status=OrderStatus.FAILED.value,
                responded_at=now_utc8_dt(),
                error_message=str(e)[:512],
            )
            return OrderReport(
                client_order_id=request.client_order_id,
                clob_order_id=None,
                status=OrderStatus.FAILED,
                matched_size=Decimal("0"),
                avg_matched_price=None,
                timestamp_ms=int(time.time() * 1000),
            )

        return self._process_result(request.client_order_id, result, send_ns)

    async def place_normal_order(
        self,
        *,
        proxy_wallet: str,
        request: OrderRequest,
        tick_size: Optional[str] = None,
        neg_risk: Optional[bool] = None,
        gtd_sec: int = DEFAULT_GTD_SEC,
    ) -> OrderReport:
        """常规下单 — 撤单后退场、SELL、风控退出。"""
        # 如果没有显式传参，从缓存获取
        wallet_lower = proxy_wallet.lower()
        if tick_size is None or neg_risk is None:
            params = self._cached_params.get(wallet_lower, {}).get(request.token_id, {})
            tick_size = tick_size or params.get("tick_size", "0.01")
            neg_risk = neg_risk if neg_risk is not None else params.get("neg_risk", False)

        order_record = {
            "id": request.client_order_id,
            "run_id": request.run_id,
            "client_order_id": request.client_order_id,
            "purpose": request.purpose.value,
            "execution_mode": ExecutionMode.NORMAL.value,
            "side": request.side.value,
            "status": OrderStatus.PENDING.value,
            "limit_price": request.limit_price,
            "requested_size": request.size,
            "sent_at": now_utc8_dt(),
        }
        self._order_repo.create(order_record)

        send_ns = time.monotonic_ns()
        try:
            result = await asyncio.to_thread(
                self._account_service.place_limit_order,
                wallet_lower,
                request.token_id,
                request.side.value,
                float(request.size),
                float(request.limit_price),
                tick_size,
                neg_risk,
                gtd_sec if request.side == OrderSide.BUY else None,
            )
        except Exception as e:
            logger.error("Normal order failed for run %s: %s", request.run_id, e)
            self._order_repo.update_response(
                request.client_order_id,
                clob_order_id=None,
                status=OrderStatus.FAILED.value,
                responded_at=now_utc8_dt(),
                error_message=str(e)[:512],
            )
            return OrderReport(
                client_order_id=request.client_order_id,
                clob_order_id=None,
                status=OrderStatus.FAILED,
                matched_size=Decimal("0"),
                avg_matched_price=None,
                timestamp_ms=int(time.time() * 1000),
            )

        return self._process_result(request.client_order_id, result, send_ns)

    async def cancel_order(
        self,
        proxy_wallet: str,
        clob_order_id: str,
        client_order_id: str,
    ) -> bool:
        """撤单。成功返回 True。"""
        try:
            result = await asyncio.to_thread(
                self._account_service.cancel_order,
                proxy_wallet.lower(),
                clob_order_id,
            )
            logger.info("Cancel order %s result: %s", clob_order_id, result)
            self._order_repo.update_response(
                client_order_id,
                clob_order_id=clob_order_id,
                status=OrderStatus.CANCELED.value,
                responded_at=now_utc8_dt(),
            )
            return True
        except Exception as e:
            logger.error("Cancel failed for order %s: %s", clob_order_id, e)
            return False

    def _process_result(
        self,
        client_order_id: str,
        result: Optional[Dict[str, Any]],
        send_ns: int,
    ) -> OrderReport:
        """解析 CLOB 返回并更新订单记录。"""
        latency_ms = (time.monotonic_ns() - send_ns) / 1_000_000
        logger.debug("Order response latency: %.1fms", latency_ms)

        if not result:
            self._order_repo.update_response(
                client_order_id,
                clob_order_id=None,
                status=OrderStatus.FAILED.value,
                responded_at=now_utc8_dt(),
                error_message="Empty response",
            )
            return OrderReport(
                client_order_id=client_order_id,
                clob_order_id=None,
                status=OrderStatus.FAILED,
                matched_size=Decimal("0"),
                avg_matched_price=None,
                timestamp_ms=int(time.time() * 1000),
            )

        raw_status = result.get("status", "")
        order_id = result.get("orderID")

        if raw_status == "live":
            status = OrderStatus.LIVE
            matched = Decimal("0")
        elif raw_status == "matched":
            taking = Decimal(str(result.get("takingAmount", 0)))
            making = Decimal(str(result.get("makingAmount", 0)))
            matched = taking if taking > 0 else making
            status = OrderStatus.MATCHED
        elif raw_status == "delayed":
            status = OrderStatus.DELAYED
            matched = Decimal("0")
        else:
            status = OrderStatus.FAILED
            matched = Decimal("0")

        self._order_repo.update_response(
            client_order_id,
            clob_order_id=order_id,
            status=status.value,
            responded_at=now_utc8_dt(),
        )

        if matched > 0:
            self._order_repo.update_fill(
                client_order_id,
                matched_size=matched,
                avg_matched_price=None,
                status=status.value,
            )

        return OrderReport(
            client_order_id=client_order_id,
            clob_order_id=order_id,
            status=status,
            matched_size=matched,
            avg_matched_price=None,
            timestamp_ms=int(time.time() * 1000),
        )


def generate_client_order_id() -> str:
    """生成唯一的 client_order_id。"""
    return str(uuid.uuid4())
