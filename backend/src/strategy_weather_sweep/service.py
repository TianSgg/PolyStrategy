"""策略 1: Sweep 信号下单 — BUY@0.99 → tick exit SELL@0.999。

职责：策略业务逻辑 + 实例工厂方法。

架构：
  SweepStrategy — 实例级管理器，持有共享工具，管理多笔并行交易
  SweepTrade   — 单笔交易生命周期（per token_id）

流程 (每笔 trade):
  1. 收到合格 sweep → 快速 BUY@0.99 固定份额
  2. 启动风控 + 订单簿监听
  3. entry_wait_ms 后撤销未成交 BUY
  4. 入场超时无持仓 → timeout_no_fill；有持仓 → 等待 tick=0.001
  5. HTTP 校验通过 → SELL@(1 - tick_size)；风控触发 → 强制退出

Event log 三层分离（参见 doc/2026-08-31-event-naming-convention.md）：
  挂单结果: buy_order_placed / buy_order_failed / sell_order_placed / sell_order_failed
  成交通知: buy_filled / sell_filled
  阶段终态: entry_complete / entry_timeout / sell_complete / sell_failed

强制退出（配置变更/禁用）:
  - 撤销所有未成交买单
  - 已持份额按 1 - tick_size 的价格挂卖单
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Optional

from framework.strategy_runtime.event_logger import EventLogger
from framework.strategy_runtime.interfaces import CancelResult, OrderResult, Signal
from framework.strategy_runtime.order_executor import OrderExecutor
from framework.strategy_runtime.tick_size_service import (
    TickSizeConsensusError,
    TickSizeFetchError,
    TickSizeService,
)
from framework.strategy_runtime.tick_verifier import TickVerifier
from framework.user_ws import FillEvent
from strategy_weather_sweep.dao import WeatherSweepConfigDAO, WeatherSweepTradeDAO
from strategy_weather_sweep.internal.risk_monitor import SweepRiskMonitor
from strategy_weather_sweep.internal.sell_failure import (
    SellErrorCircuitBreaker,
    SellFailureSnapshot,
    SellFailureTracker,
    classify_sell_error,
)

logger = logging.getLogger(__name__)

INVALID_TICK_RETRY_WAIT_S = (120.0, 300.0, 600.0)
BALANCE_LAG_RETRY_WAIT_S = (120.0, 300.0, 600.0)
DEFAULT_FIRST_SELL_GRACE_MS = 2000


# ============================================================
# SweepTrade — 单笔交易的完整生命周期
# ============================================================


class SweepTrade:
    """单笔交易：从信号触发到平仓退出的完整状态机。"""

    def __init__(
        self,
        token_id: str,
        market_slug: str,
        config: dict[str, Any],
        executor: OrderExecutor,
        orderbook_ws: Any,
        event_logger: Optional[EventLogger],
        on_closed: Callable[[str], None],
        params_version: int = 1,
        config_snapshot: Optional[dict[str, Any]] = None,
        on_exit_failed: Optional[Callable[[str, str], Optional[dict[str, Any]]]] = None,
        trade_dao: Optional[WeatherSweepTradeDAO] = None,
        tick_size_service: Optional[TickSizeService] = None,
    ) -> None:
        self.token_id = token_id
        self.market_slug = market_slug
        self._config = config
        self._params_version = params_version
        self._config_snapshot = config_snapshot or dict(config)
        self._executor = executor
        self._orderbook_ws = orderbook_ws
        self._el = event_logger
        self._on_closed = on_closed
        self._on_exit_failed = on_exit_failed
        self._trade_dao = trade_dao
        self._tick_size_service = tick_size_service or TickSizeService()

        self.state = "entry"
        self.position_shares = Decimal("0")
        self.entry_order_id: Optional[str] = None
        self.exit_order_id: Optional[str] = None
        self._entry_timer: Optional[asyncio.Task] = None
        self._tick_verified = False
        self._tick_verifying = False
        self._normal_exit_started = False
        self._tick_size = Decimal("0.01")
        self._event_start_ms = int(time.time() * 1000)
        self._entry_cost = Decimal("0")
        self._exit_revenue = Decimal("0")
        self._exit_filled_shares = Decimal("0")
        self._exit_failure_reported = False
        self._exit_started = False
        self._market_settled_requested = False
        self._last_buy_fill_ms = 0
        self._balance_lag_retry_index = 0
        self._normal_sell_cancelled = False
        self._normal_sell_matched: dict[str, Decimal] = {}

        # Fill tracking
        self._order_size = Decimal("0")        # original buy order size
        self._order_placed_ms = 0              # buy order placed timestamp
        self._buy_fill_count = 0               # total buy_filled steps

        self.risk = SweepRiskMonitor(
            orderbook_ws=orderbook_ws,
            stop_loss_ratio=Decimal(config.get("stop_loss_ratio", "0.60")),
            on_trigger=self._risk_exit,
            on_tick_change=self._on_tick_change,
            tick_size_service=self._tick_size_service,
        )
        self.tick_verifier = TickVerifier(tick_size_service=self._tick_size_service)

    # ==================== Helpers ====================

    @staticmethod
    def _utc_str() -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _snapshot_bbo(self, offset_origin_ms: int) -> dict:
        snapshot = None
        if self._orderbook_ws:
            getter = getattr(self._orderbook_ws, "get_bbo_snapshot", None)
            if getter:
                snapshot = getter(self.token_id)
            if snapshot is None:
                book = self._orderbook_ws.get_book(self.token_id)
                if book:
                    bids = sorted(book.bids.items(), reverse=True)
                    asks = sorted(book.asks.items())
                    snapshot = {
                        "best_bid": bids[0][0] if bids else None,
                        "best_bid_size": bids[0][1] if bids else None,
                        "best_ask": asks[0][0] if asks else None,
                        "best_ask_size": asks[0][1] if asks else None,
                    }
        captured_at_ms = int(time.time() * 1000)
        return {
            "source": "market_ws",
            "utc": self._utc_str(),
            "captured_at_ms": captured_at_ms,
            "offset_ms": captured_at_ms - offset_origin_ms,
            "best_bid": snapshot.get("best_bid") if snapshot else None,
            "best_bid_size": snapshot.get("best_bid_size") if snapshot else None,
            "best_ask": snapshot.get("best_ask") if snapshot else None,
            "best_ask_size": snapshot.get("best_ask_size") if snapshot else None,
        }

    def _event_phase(self) -> str:
        return "exit" if self._exit_started or self.exit_order_id else "entry"

    def _mark_exit_started(self) -> None:
        """Cross the phase boundary immediately before the first SELL attempt."""
        if self._exit_started:
            return
        self._exit_started = True
        self.state = "exit"
        self._update_trade_summary({
            "phase": "exit",
            "exit_started_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })

    async def _stop_for_cancel_query_failure(
        self, *, phase: str, side: str, order_id: str,
        cancel_result: CancelResult,
    ) -> None:
        if self._el:
            self._el.log_step("fill_reconcile_failed", {
                "side": side,
                "order_id": order_id,
                "cancelled": cancel_result.cancelled,
                "utc": self._utc_str(),
                **self._cancel_error_detail(cancel_result),
            }, phase=phase)
        await self.risk.stop()
        self._close_exit_failed("fill_reconcile_failed", phase=phase, extra={
            "order_id": order_id,
            "side": side,
            "query_status": cancel_result.status,
            **self._cancel_error_detail(cancel_result),
        })

    async def _stop_for_cancel_failure(
        self, *, phase: str, side: str, order_id: str,
        cancel_result: CancelResult,
    ) -> None:
        failure_reason = "buy_cancel_failed" if side.upper() == "BUY" else "sell_cancel_failed"
        if self._el:
            self._el.log_step(failure_reason, {
                "side": side,
                "order_id": order_id,
                "utc": self._utc_str(),
                **self._cancel_error_detail(cancel_result),
            }, phase=phase)
        await self.risk.stop()
        self._close_exit_failed(failure_reason, phase=phase, extra={
            "order_id": order_id,
            "side": side,
            "query_status": cancel_result.status,
            **self._cancel_error_detail(cancel_result),
        })

    @staticmethod
    def _sell_failure_reason(result: Optional[OrderResult]) -> str:
        if result and result.error and "missing fill amount" in result.error:
            return "sell_fill_parse_error"
        return "sell_placement_failed"

    async def _load_min_order_size(self) -> Optional[Decimal]:
        if not self._orderbook_ws:
            return None

        value = self._orderbook_ws.get_min_order_size(self.token_id)
        if value is not None:
            return Decimal(str(value))

        refresh = getattr(self._orderbook_ws, "refresh_min_order_size", None)
        if refresh is not None:
            value = await refresh(self.token_id)
            if value is not None:
                return Decimal(str(value))
        return None

    async def _close_if_dust_position(
        self, min_order_size: Optional[Decimal], *, phase: str, trigger: str,
    ) -> bool:
        if (
            min_order_size is None
            or self.position_shares <= 0
            or self.position_shares >= min_order_size
        ):
            return False

        if self._el:
            self._el.log_step("dust_position_detected", {
                "trigger": trigger,
                "position_shares": str(self.position_shares),
                "min_order_size": str(min_order_size),
                "manual_action_required": True,
                "utc": self._utc_str(),
            }, phase=phase)
        await self.risk.stop()
        self._close("dust_position", phase=phase, extra={
            "trigger": trigger,
            "position_shares": str(self.position_shares),
            "min_order_size": str(min_order_size),
        })
        return True

    @staticmethod
    def _bbo_from_market_snapshot(snapshot: Optional[dict], offset_origin_ms: int) -> dict:
        captured_ms = snapshot.get("captured_at_ms") if snapshot else None
        now_ms = int(time.time() * 1000)
        return {
            "source": "market_ws",
            "utc": snapshot.get("utc") if snapshot and snapshot.get("utc") else SweepTrade._utc_str(),
            "captured_at_ms": captured_ms,
            "offset_ms": int((captured_ms or now_ms) - offset_origin_ms),
            "best_bid": snapshot.get("best_bid") if snapshot else None,
            "best_bid_size": snapshot.get("best_bid_size") if snapshot else None,
            "best_ask": snapshot.get("best_ask") if snapshot else None,
            "best_ask_size": snapshot.get("best_ask_size") if snapshot else None,
        }

    def _order_obj(
        self, order_id: str, side: str, price: str, size: str, offset_origin_ms: int,
    ) -> dict:
        return {
            "order_id": order_id,
            "side": side,
            "price": price,
            "size": size,
            "utc": self._utc_str(),
            "offset_ms": int((time.time() * 1000) - offset_origin_ms),
        }

    @staticmethod
    def _order_error_detail(
        result: OrderResult, *, error_signature: Optional[str] = None,
    ) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "status": result.status,
            "error": result.error,
        }
        if result.error_status_code is not None:
            detail["status_code"] = result.error_status_code
        if result.error_message:
            detail["error_message"] = result.error_message
        if error_signature:
            detail["error_signature"] = error_signature
        return detail

    @staticmethod
    def _cancel_error_detail(cancel_result: CancelResult) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "query_status": cancel_result.status,
            "final_matched": str(cancel_result.final_matched),
        }
        if cancel_result.cancel_error:
            detail["cancel_error"] = cancel_result.cancel_error
            detail["error"] = cancel_result.cancel_error
            detail["error_signature"] = classify_sell_error("failed", cancel_result.cancel_error)
        if cancel_result.cancel_error_status_code is not None:
            detail["cancel_status_code"] = cancel_result.cancel_error_status_code
            detail.setdefault("status_code", cancel_result.cancel_error_status_code)
        if cancel_result.cancel_error_message:
            detail["cancel_error_message"] = cancel_result.cancel_error_message
            detail.setdefault("error_message", cancel_result.cancel_error_message)
        if cancel_result.query_error:
            detail["query_error"] = cancel_result.query_error
        if cancel_result.query_error_status_code is not None:
            detail["query_status_code"] = cancel_result.query_error_status_code
        if cancel_result.query_error_message:
            detail["query_error_message"] = cancel_result.query_error_message
        return detail

    async def _refresh_tick_after_invalid_sell(
        self, old_tick: Decimal, *, phase: str, reason: str,
    ) -> Optional[Decimal]:
        try:
            new_tick = await self._tick_size_service.refresh_consensus(
                self.token_id, self._latest_ws_tick_size()
            )
        except TickSizeConsensusError as exc:
            logger.error(
                "Tick source mismatch after invalid tick SELL: token=%s "
                "ws=%s tick_api=%s book=%s",
                self.token_id, exc.ws_tick_size, exc.tick_api_size,
                exc.book_tick_size,
            )
            if self._el:
                self._el.log_step("tick_refresh_failed", {
                    "reason": reason,
                    "error": "tick_source_mismatch",
                    "ws_tick_size": str(exc.ws_tick_size),
                    "http_tick_size": str(exc.tick_api_size),
                    "book_tick_size": str(exc.book_tick_size),
                    "action": "stop_event",
                    "utc": self._utc_str(),
                }, phase=phase)
            return None
        except TickSizeFetchError as exc:
            logger.error(
                "Tick refresh failed after invalid tick SELL: token=%s err=%s",
                self.token_id, exc,
            )
            if self._el:
                self._el.log_step("tick_refresh_failed", {
                    "reason": reason,
                    "error": str(exc),
                    "action": "stop_event",
                    "utc": self._utc_str(),
                }, phase=phase)
            return None

        self._tick_size = new_tick
        if self._el:
            self._el.log_step("tick_refreshed", {
                "reason": "invalid_tick_size",
                "old_tick_size": str(old_tick),
                "new_tick_size": str(new_tick),
                "source": "ws+tick-size+book",
                "utc": self._utc_str(),
            }, phase=phase)
        return new_tick

    def _latest_ws_tick_size(self) -> Optional[Decimal]:
        if not self._orderbook_ws:
            return None
        getter = getattr(self._orderbook_ws, "get_tick_size", None)
        if getter is None:
            return None
        value = getter(self.token_id)
        return Decimal(str(value)) if value is not None else None

    def _log_tick_source_verified(
        self,
        *,
        ws_tick_size: Decimal,
        tick_api_size: Decimal,
        book_tick_size: Decimal,
    ) -> None:
        if not self._el:
            return

        sources = (
            ("market_ws", ws_tick_size),
            ("tick_size_api", tick_api_size),
            ("book_api", book_tick_size),
        )
        for source, tick_size in sources:
            self._el.log_step("tick_verified", {
                "token_id": self.token_id,
                "source": source,
                "tick_size": str(tick_size),
                "confirmed": True,
                "utc": self._utc_str(),
            }, phase="entry")

    def _log_sell_circuit_breaker(
        self,
        snapshot: SellFailureSnapshot,
        *,
        phase: str,
        reason: str,
        stop_reason: str,
    ) -> None:
        if not self._el:
            return
        self._el.log_step("sell_circuit_breaker_triggered", {
            "attempt_count": snapshot.attempt_count,
            "consecutive_same_error": snapshot.consecutive_same_error,
            "error_signature": snapshot.error_signature,
            "last_error": snapshot.last_error,
            "elapsed_ms": snapshot.elapsed_ms,
            "position_shares": str(self.position_shares),
            "trigger": reason,
            "stop_reason": stop_reason,
            "action": "stop_event",
            "utc": self._utc_str(),
        }, phase=phase)

    def _first_sell_grace_ms(self) -> int:
        value = self._config.get("clob_sync_grace_ms", DEFAULT_FIRST_SELL_GRACE_MS)
        try:
            return max(0, int(value))
        except Exception:
            return DEFAULT_FIRST_SELL_GRACE_MS

    def _next_balance_lag_wait_s(self) -> float:
        index = min(
            self._balance_lag_retry_index,
            len(BALANCE_LAG_RETRY_WAIT_S) - 1,
        )
        wait_s = BALANCE_LAG_RETRY_WAIT_S[index]
        self._balance_lag_retry_index += 1
        return wait_s

    @staticmethod
    def _parse_balance_lag_sellable(error: Optional[str]) -> Optional[Decimal]:
        if not error:
            return None
        text = error.lower()
        if "not enough balance" not in text and "not enough allowance" not in text:
            return None

        balance_match = re.search(r"balance:\s*(\d+)", error)
        if not balance_match:
            return None

        matched_match = re.search(r"sum of matched orders:\s*(\d+)", error)
        balance_raw = int(balance_match.group(1))
        matched_raw = int(matched_match.group(1)) if matched_match else 0
        actual = (Decimal(balance_raw) - Decimal(matched_raw)) / Decimal("1000000")
        if actual <= 0:
            return None
        return actual.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    async def _wait_before_first_sell_attempt(
        self,
        *,
        phase: str,
        trigger: str,
        exit_origin_ms: int,
    ) -> None:
        grace_ms = self._first_sell_grace_ms()
        if grace_ms <= 0 or self._last_buy_fill_ms <= 0:
            return

        elapsed_ms = int(time.time() * 1000) - self._last_buy_fill_ms
        wait_ms = grace_ms - elapsed_ms
        if wait_ms <= 0:
            return

        if self._el:
            self._el.log_step("exit_trigger_deferred", {
                "trigger": trigger,
                "reason": "clob_sync_grace",
                "last_buy_fill_ms": self._last_buy_fill_ms,
                "grace_ms": grace_ms,
                "wait_ms": wait_ms,
                "position_shares": str(self.position_shares),
                "utc": self._utc_str(),
                "order": self._order_obj(
                    self.entry_order_id or self.exit_order_id or "pending",
                    "SELL",
                    str(Decimal("1") - self._tick_size),
                    str(self.position_shares),
                    exit_origin_ms,
                ),
            }, phase=phase)
        await asyncio.sleep(wait_ms / 1000.0)

    # ==================== Entry ====================

    async def enter(self, signal: Signal) -> None:
        orderbook_snapshot = signal.payload.get("orderbook_snapshot", {})
        enter_origin_ms = int(time.time() * 1000)

        fixed_shares = Decimal(self._config.get("fixed_entry_shares", "100"))
        buy_price = Decimal("0.99")
        available = self._executor.available_cash
        max_shares = int(available / buy_price) if buy_price > 0 else 0
        actual_shares = min(fixed_shares, Decimal(str(max_shares)))

        if self._el:
            self._el.start_event(
                signal_id=signal.signal_id,
                token_id=signal.token_id,
                market_slug=signal.market_slug,
                event_slug=signal.payload.get("event_slug"),
            )
            snap_bid = orderbook_snapshot.get("best_bid")
            snap_ask = orderbook_snapshot.get("best_ask")
            bid_price = Decimal(str(snap_bid["price"])) if snap_bid else None
            ask_price = Decimal(str(snap_ask["price"])) if snap_ask else None
            if bid_price is not None and ask_price is not None:
                snap_mid = (bid_price + ask_price) / 2
            else:
                snap_mid = bid_price or ask_price
            stop_ratio = Decimal(self._config.get("stop_loss_ratio", "0.60"))
            snap_threshold = snap_mid * stop_ratio if snap_mid else None
            self._el.log_step("signal_received", {
                "signal_id": signal.signal_id,
                "token_id": signal.token_id,
                "market_slug": signal.market_slug,
                "event_slug": signal.payload.get("event_slug"),
                "city": signal.payload.get("city"),
                "direction": signal.payload.get("direction"),
                "utc": self._utc_str(),
                "risk_ref_mid": str(snap_mid),
                "risk_threshold": str(snap_threshold),
            }, phase="entry")
            self._insert_trade_summary(signal)

        # --- Layer 1: buy_order_skipped (no cash) ---
        if actual_shares <= 0:
            logger.warning("No available cash for %s, closing", self.token_id[:10])
            if self._el:
                self._el.log_step("buy_order_skipped", {
                    "status": "no_cash",
                    "requested_size": str(fixed_shares),
                    "available_cash": str(available),
                }, phase="entry")
            self._close("no_cash", phase="entry", extra={
                "requested_size": str(fixed_shares),
                "available_cash": str(available),
                "status": "no_cash",
            })
            return

        try:
            entry_tick_size = await self._tick_size_service.get(self.token_id)
        except TickSizeFetchError as exc:
            logger.error("Tick size lookup failed before BUY: token=%s err=%s", self.token_id, exc)
            if self._el:
                self._el.log_step("buy_order_failed", {
                    "status": "tick_refresh_failed",
                    "error": str(exc),
                    "error_message": str(exc),
                    "error_signature": classify_sell_error("failed", str(exc)),
                    "requested_size": str(actual_shares),
                }, phase="entry")
            self._close("tick_refresh_failed", phase="entry", extra={
                "error": str(exc),
                "error_message": str(exc),
                "error_signature": classify_sell_error("failed", str(exc)),
            })
            return

        order_task = asyncio.create_task(self._executor.place_order(
            token_id=self.token_id,
            side="BUY",
            price=str(buy_price),
            size=str(actual_shares),
            tick_size=str(entry_tick_size),
            neg_risk=True,
        ))
        risk_task = asyncio.create_task(
            self.risk.start(token_id=self.token_id, orderbook_snapshot=orderbook_snapshot)
        )

        await risk_task
        await self.risk.wait_for_first_bbo()
        pre_bbo = self._bbo_from_market_snapshot(
            self.risk.first_bbo_snapshot(), enter_origin_ms,
        )

        result = await order_task
        aft_bbo = self._snapshot_bbo(enter_origin_ms)

        # Track order metadata for entry_complete
        self._order_size = actual_shares
        self._order_placed_ms = int(time.time() * 1000)
        self._update_trade_summary({"entry_order_size": str(actual_shares)})

        # --- Layer 1: buy_order_skipped (balance became insufficient after sizing) ---
        if result.status == "insufficient_balance":
            await self.risk.stop()
            current_available_cash = self._executor.available_cash
            if self._el:
                self._el.log_step("buy_order_skipped", {
                    "status": "insufficient_balance",
                    "requested_size": str(actual_shares),
                    "available_cash": str(current_available_cash),
                    "order_id": result.order_id,
                    "pre_bbo": pre_bbo,
                    "aft_bbo": aft_bbo,
                }, phase="entry")
            self._close("no_cash", phase="entry", extra={
                "requested_size": str(actual_shares),
                "available_cash": str(current_available_cash),
                "status": "insufficient_balance",
            })
            return

        # --- Layer 1: buy_order_failed (CLOB rejected) ---
        if result.status == "failed":
            await self.risk.stop()
            error_signature = classify_sell_error(result.status, result.error)
            if self._el:
                self._el.log_step("buy_order_failed", {
                    **self._order_error_detail(result, error_signature=error_signature),
                    "order": self._order_obj(
                        result.order_id, "BUY", str(buy_price), str(actual_shares), enter_origin_ms,
                    ),
                    "pre_bbo": pre_bbo,
                    "aft_bbo": aft_bbo,
                }, phase="entry")
            self._close("buy_placement_failed", phase="entry", extra={
                "error": result.error,
                "error_message": result.error_message,
                "status_code": result.error_status_code,
                "error_signature": error_signature,
            })
            return

        # --- Layer 1: buy_order_placed (CLOB accepted) ---
        self.entry_order_id = result.order_id
        if self._el:
            self._el.log_step("buy_order_placed", {
                "order": self._order_obj(
                    result.order_id, "BUY", str(buy_price), str(actual_shares), enter_origin_ms,
                ),
                "clob_status": result.clob_status,
                "clob_taking": result.clob_taking,
                "clob_making": result.clob_making,
                "pre_bbo": pre_bbo,
                "aft_bbo": aft_bbo,
            }, phase="entry")
        self._update_trade_summary({
            "entry_started_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })

        # --- Layer 2: buy_filled (immediate fill from CLOB response) ---
        if result.status in ("filled", "partial"):
            filled = Decimal(result.filled_size)
            if filled > 0:
                self._record_buy_fill(result.order_id, filled, buy_price, source="clob_response")

        # --- Layer 3: entry_complete (all filled immediately) ---
        if result.status == "filled":
            self.entry_order_id = None
            await self._record_entry_complete(result.order_id)
            return

        # partial or live → WS 监听 + 超时计时器
        self.buy_price = buy_price
        user_ws = await self._executor.ensure_user_ws()
        user_ws.watch_order(
            order_id=result.order_id, token_id=self.token_id, side="BUY",
            price=self.buy_price,
            initial_matched=Decimal(result.filled_size),
            on_fill=self._on_buy_fill,
        )
        entry_wait_ms = int(self._config.get("entry_wait_ms", 1200000))
        self._entry_timer = asyncio.create_task(
            self._entry_timeout(entry_wait_ms / 1000.0)
        )

    def _record_buy_fill(
        self, order_id: str, filled: Decimal, price: Decimal, *,
        source: str, trade_id: Optional[str] = None,
    ) -> None:
        self.position_shares += filled
        self._entry_cost += filled * price
        self._buy_fill_count += 1
        self._last_buy_fill_ms = int(time.time() * 1000)

        if self._el:
            detail: dict[str, Any] = {
                "order_id": order_id,
                "filled_size": str(filled),
                "fill_price": str(price),
                "total_position": str(self.position_shares),
                "source": source,
            }
            if trade_id:
                detail["trade_id"] = trade_id
            self._el.log_step("buy_filled", detail, phase="entry")

        self._update_trade_summary({
            "entry_price": str((self._entry_cost / self.position_shares).quantize(Decimal("0.0001"))),
            "entry_shares": str(self.position_shares),
            "entry_cost": str(self._entry_cost),
            "entry_order_id": order_id,
        })

    async def _record_entry_complete(self, order_id: str) -> None:
        if self.state == "closed":
            return

        elapsed_ms = int(time.time() * 1000) - self._order_placed_ms if self._order_placed_ms else 0
        if self._el:
            self._el.log_step("entry_complete", {
                "order_id": order_id,
                "total_filled": str(self.position_shares),
                "total_position": str(self.position_shares),
                "fill_count": self._buy_fill_count,
                "elapsed_ms": elapsed_ms,
            }, phase="entry")
        self._update_trade_summary({
            "entered_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })
        if self._tick_verified:
            await self._start_normal_exit()

    async def _on_buy_fill(self, event: FillEvent) -> None:
        if self.state == "closed":
            return
        self._record_buy_fill(event.order_id, event.fill_size, event.fill_price,
                              source=event.source, trade_id=event.trade_id)
        if event.total_matched >= self._order_size:
            if self._entry_timer and not self._entry_timer.done():
                self._entry_timer.cancel()
            self.entry_order_id = None
            (await self._executor.ensure_user_ws()).unwatch_order(event.order_id)
            await self._record_entry_complete(event.order_id)

    async def _entry_timeout(self, wait_sec: float) -> None:
        await asyncio.sleep(wait_sec)
        if self.state == "closed":
            return

        cancelled_id = None
        if self.entry_order_id:
            order_id = self.entry_order_id
            cancelled_id = order_id
            user_ws = await self._executor.ensure_user_ws()
            user_ws.unwatch_order(order_id)
            cancel_result = await self._executor.cancel_order_with_fill_check(order_id)
            if not cancel_result.cancelled:
                await self._stop_for_cancel_failure(
                    phase="entry", side="BUY", order_id=order_id,
                    cancel_result=cancel_result,
                )
                return
            self.entry_order_id = None
            if cancel_result.query_failed:
                if self._el:
                    self._el.log_step("fill_reconcile_unavailable", {
                        "side": "BUY",
                        "order_id": order_id,
                        "cancelled": True,
                        "memory_position": str(self.position_shares),
                        "action": "continue_with_memory_position",
                        "non_fatal": True,
                        "utc": self._utc_str(),
                        **self._cancel_error_detail(cancel_result),
                    }, phase="entry")
            elif cancel_result.final_matched > 0 and cancel_result.final_matched > self.position_shares:
                missed = cancel_result.final_matched - self.position_shares
                self._record_buy_fill(order_id, missed, self.buy_price,
                                      source="cancel_reconcile")
                if self._el:
                    self._el.log_step("fill_reconciled", {
                        "side": "BUY", "order_id": order_id,
                        "clob_matched": str(cancel_result.final_matched),
                        "memory_before": str(self.position_shares - missed),
                        "reconciled": str(missed),
                    }, phase="entry")

        unfilled = self._order_size - self.position_shares

        if self._el:
            self._el.log_step("entry_timeout", {
                "wait_ms": int(wait_sec * 1000),
                "cancelled_order_id": cancelled_id,
                "final_position": str(self.position_shares),
                "unfilled_size": str(unfilled),
            }, phase="entry")

        if self.position_shares <= 0:
            await self.risk.stop()
            self._close("timeout_no_fill", phase="entry")
        else:
            self._update_trade_summary({
                "entered_at": datetime.now(timezone.utc).replace(tzinfo=None),
            })
            if self._tick_verified:
                await self._start_normal_exit()

    # ==================== Monitor & Exit ====================

    async def _on_tick_change(self, new_tick: Decimal, source: str = "market_ws") -> None:
        if self._tick_verified or self.state not in ("entry", "exit"):
            return
        if self._tick_verifying:
            return

        if new_tick == Decimal("0.001"):
            self._tick_verifying = True

            def current_ws_tick_size() -> Optional[Decimal]:
                latest = self._latest_ws_tick_size()
                if latest is not None:
                    return latest
                if source == "market_ws":
                    return new_tick
                return None

            try:
                result = await self.tick_verifier.verify(
                    self.token_id,
                    ws_tick_size=current_ws_tick_size(),
                    ws_tick_size_getter=current_ws_tick_size,
                )
            finally:
                self._tick_verifying = False
            if self.state not in ("entry", "exit"):
                return
            if result.confirmed:
                self._tick_verified = True
                self._tick_size = result.actual_tick or new_tick
                self._log_tick_source_verified(
                    ws_tick_size=result.ws_tick_size or new_tick,
                    tick_api_size=result.tick_api_size or result.actual_tick or new_tick,
                    book_tick_size=result.book_tick_size or result.actual_tick or new_tick,
                )
                await self._start_normal_exit()
            elif self._el:
                self._el.log_step("tick_verify_failed", {
                    "confirmed": False,
                    "ws_tick_size": str(result.ws_tick_size) if result.ws_tick_size is not None else None,
                    "http_tick_size": str(result.tick_api_size) if result.tick_api_size is not None else None,
                    "book_tick_size": str(result.book_tick_size) if result.book_tick_size is not None else None,
                    "error": result.error,
                    "utc": self._utc_str(),
                }, phase="entry")

    async def _start_normal_exit(self) -> None:
        if self.state == "closed" or self._normal_exit_started:
            return

        # Tick changing before the entry order reaches a terminal state is only
        # a trigger. The entry order still owns the entry_wait_ms lifecycle.
        # The final close_reason for a successful exit is always normal_exit.
        if self.position_shares <= 0 or self.entry_order_id:
            if self._el:
                self._el.log_step("exit_trigger_deferred", {
                    "trigger": "tick_size_change",
                    "reason": "no_position" if self.position_shares <= 0 else "entry_order_pending",
                    "position_shares": str(self.position_shares),
                    "entry_order_id": self.entry_order_id,
                }, phase="entry")
            return

        self._normal_exit_started = True

        try:
            self._tick_size = await self._tick_size_service.refresh_consensus(
                self.token_id, self._latest_ws_tick_size()
            )
        except TickSizeConsensusError as exc:
            logger.error(
                "Tick source mismatch before SELL: token=%s ws=%s tick_api=%s book=%s",
                self.token_id, exc.ws_tick_size, exc.tick_api_size,
                exc.book_tick_size,
            )
            if self._el:
                self._el.log_step("tick_refresh_failed", {
                    "error": "tick_source_mismatch",
                    "ws_tick_size": str(exc.ws_tick_size),
                    "http_tick_size": str(exc.tick_api_size),
                    "book_tick_size": str(exc.book_tick_size),
                    "action": "stop_event",
                    "utc": self._utc_str(),
                }, phase=self._event_phase())
            await self.risk.stop()
            self._close_exit_failed("tick_refresh_failed", phase=self._event_phase(), extra={
                "error": "tick_source_mismatch",
                "error_signature": classify_sell_error("failed", "tick size mismatch"),
            })
            return
        except TickSizeFetchError as exc:
            logger.error("Tick size refresh failed before SELL: token=%s err=%s", self.token_id, exc)
            if self._el:
                self._el.log_step("tick_refresh_failed", {
                    "error": str(exc),
                    "action": "stop_event",
                    "utc": self._utc_str(),
                }, phase=self._event_phase())
            await self.risk.stop()
            self._close_exit_failed("tick_refresh_failed", phase=self._event_phase(), extra={
                "error": str(exc),
                "error_signature": classify_sell_error("failed", str(exc)),
            })
            return

        min_order_size = await self._load_min_order_size()
        if await self._close_if_dust_position(
            min_order_size, phase=self._event_phase(), trigger="normal_exit",
        ):
            return

        sell_price = Decimal("1") - self._tick_size
        sell_backoff_base = 2.0
        sell_backoff_cap = 60.0

        attempt = 0
        exit_origin_ms = int(time.time() * 1000)
        failure_tracker = SellFailureTracker()
        invalid_tick_retries = 0
        balance_retry_size: Optional[Decimal] = None

        while self.position_shares > 0:
            if self.state not in ("entry", "exit"):
                return

            attempt += 1
            if await self._close_if_dust_position(
                min_order_size, phase="exit", trigger="normal_exit",
            ):
                return
            if attempt == 1:
                await self._wait_before_first_sell_attempt(
                    phase="exit",
                    trigger="normal_exit",
                    exit_origin_ms=exit_origin_ms,
                )
                if self.state == "closed":
                    return
            sell_size = balance_retry_size or self.position_shares
            balance_retry_size = None
            if attempt == 1:
                self._update_trade_summary({"exit_order_size": str(sell_size)})
            self._mark_exit_started()
            result = await self._executor.place_order(
                token_id=self.token_id,
                side="SELL",
                price=str(sell_price),
                size=str(sell_size),
                tick_size=str(self._tick_size),
                neg_risk=True,
                check_balance=False,
            )

            # --- Layer 1: sell_order_failed ---
            if result.status in ("failed", "insufficient_balance"):
                error_signature = classify_sell_error(result.status, result.error)
                if self._el:
                    self._el.log_step("sell_order_failed", {
                        **self._order_error_detail(result, error_signature=error_signature),
                        "order": self._order_obj(
                            result.order_id, "SELL", str(sell_price), str(sell_size), exit_origin_ms,
                        ),
                        "attempt": attempt,
                    }, phase="exit")

                if error_signature == "insufficient_balance":
                    observed_sellable = self._parse_balance_lag_sellable(result.error)
                    if (
                        observed_sellable is not None
                        and min_order_size is not None
                        and observed_sellable >= min_order_size
                    ):
                        retry_size = min(self.position_shares, observed_sellable)
                        if retry_size < sell_size:
                            balance_retry_size = retry_size.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                            if self._el:
                                self._el.log_step("balance_settlement_retry", {
                                    "attempt": attempt,
                                    "error": result.error,
                                    "error_signature": error_signature,
                                    "observed_sellable": str(observed_sellable),
                                    "retry_size": str(balance_retry_size),
                                    "wait_ms": 0,
                                    "reason": "balance_lag_partial_retry",
                                    "retry_index": self._balance_lag_retry_index,
                                    "utc": self._utc_str(),
                                }, phase="exit")
                            continue

                    wait_s = self._next_balance_lag_wait_s()
                    if self._el:
                        self._el.log_step("balance_settlement_retry", {
                            "attempt": attempt,
                            "error": result.error,
                            "error_signature": error_signature,
                            "observed_sellable": str(observed_sellable) if observed_sellable is not None else None,
                            "wait_ms": int(wait_s * 1000),
                            "reason": "balance_lag",
                            "retry_index": self._balance_lag_retry_index,
                            "utc": self._utc_str(),
                        }, phase="exit")
                    await asyncio.sleep(wait_s)
                    continue

                failure = failure_tracker.record(error_signature, result.error)

                if (
                    error_signature == "invalid_tick_size"
                    and invalid_tick_retries < len(INVALID_TICK_RETRY_WAIT_S)
                ):
                    wait_s = INVALID_TICK_RETRY_WAIT_S[invalid_tick_retries]
                    invalid_tick_retries += 1
                    if self._el:
                        self._el.log_step("sell_retry_started", {
                            "attempt": attempt,
                            "error": result.error,
                            "error_signature": error_signature,
                            "wait_ms": int(wait_s * 1000),
                            "utc": self._utc_str(),
                        }, phase="exit")
                    await asyncio.sleep(wait_s)

                    old_tick = self._tick_size
                    new_tick = await self._refresh_tick_after_invalid_sell(
                        old_tick, phase="exit", reason="normal_exit",
                    )
                    if new_tick is None:
                        await self.risk.stop()
                        self._close_exit_failed(self._sell_failure_reason(result), phase="exit", extra={
                            "error": result.error,
                            "error_message": result.error_message,
                            "status_code": result.error_status_code,
                            "error_signature": error_signature,
                            "stop_reason": "tick_refresh_failed",
                        })
                        return
                    sell_price = Decimal("1") - new_tick
                    continue

                stop_reason = failure.stop_reason
                if error_signature == "invalid_tick_size":
                    stop_reason = "invalid_tick_retry_exhausted"
                if stop_reason:
                    self._log_sell_circuit_breaker(
                        failure, phase="exit", reason="normal_exit", stop_reason=stop_reason,
                    )
                    await self.risk.stop()
                    self._close_exit_failed(self._sell_failure_reason(result), phase="exit", extra={
                        "error": result.error,
                        "error_message": result.error_message,
                        "status_code": result.error_status_code,
                        "error_signature": error_signature,
                        "stop_reason": stop_reason,
                    })
                    return

                backoff = (
                    2.0 if error_signature == "insufficient_balance"
                    else min(sell_backoff_base * (2 ** (attempt - 1)), sell_backoff_cap)
                )
                if self._el:
                    self._el.log_step("sell_retry_started", {
                        "attempt": attempt,
                        "error": result.error,
                    }, phase="exit")
                await asyncio.sleep(backoff)
                continue

            # --- Layer 1: sell_order_placed ---
            self.exit_order_id = result.order_id
            sell_fill_count = 0
            sell_placed_ms = int(time.time() * 1000)

            if self._el:
                self._el.log_step("sell_order_placed", {
                    "order": self._order_obj(
                        result.order_id, "SELL", str(sell_price), str(sell_size), exit_origin_ms,
                    ),
                    "clob_status": result.clob_status,
                    "clob_taking": result.clob_taking,
                    "clob_making": result.clob_making,
                    "trigger": "tick_size_change",
                    "attempt": attempt,
                }, phase="exit")

            # --- Layer 2: sell_filled (immediate fill from CLOB response) ---
            if result.status in ("filled", "partial"):
                filled = Decimal(result.filled_size)
                if filled > 0:
                    self._record_sell_fill(
                        result.order_id, filled, sell_price,
                        source="clob_response", sell_fill_count=sell_fill_count,
                    )
                    sell_fill_count += 1

            # --- Layer 3: sell_complete (all filled immediately) ---
            if result.status == "filled":
                self.exit_order_id = None
                if self.position_shares <= 0:
                    self._record_sell_complete(result.order_id, sell_fill_count, sell_placed_ms)
                    await self.risk.stop()
                    self._close("normal_exit", phase="exit")
                    return
                # partial was labeled "filled" but position remains — continue selling
                continue

            # live → WS 监听 + 等待全部成交或超时
            self.sell_price = sell_price
            user_ws = await self._executor.ensure_user_ws()
            sell_done = asyncio.Event()
            self._sell_done_event = sell_done
            self._normal_sell_cancelled = False
            self._normal_sell_matched[result.order_id] = Decimal(result.filled_size)

            user_ws.watch_order(
                order_id=result.order_id, token_id=self.token_id, side="SELL",
                price=self.sell_price,
                initial_matched=Decimal(result.filled_size),
                on_fill=self._on_sell_fill,
                on_cancel=self._on_normal_sell_cancel,
            )

            # Keep the accepted order live. Replacing an unchanged order after a
            # fixed timeout loses queue position and does not improve execution.
            await sell_done.wait()

            user_ws.unwatch_order(result.order_id)
            self._normal_sell_matched.pop(result.order_id, None)
            self.exit_order_id = None

            if self.state == "closed":
                return

            # A CLOB cancellation is terminal for this order, but not for the
            # trade. The User WS cancellation payload supplies final matched
            # size, so retry only the still-open position.
            if self._normal_sell_cancelled:
                if self.position_shares <= 0:
                    self._record_sell_complete(result.order_id, sell_fill_count, sell_placed_ms)
                    await self.risk.stop()
                    self._close("normal_exit", phase="exit")
                    return
                continue

            # 全部成交
            self._record_sell_complete(result.order_id, sell_fill_count, sell_placed_ms)
            await self.risk.stop()
            self._close("normal_exit", phase="exit")
            return

        await self.risk.stop()
        self._close("normal_exit", phase="exit")

    def _record_sell_fill(
        self, order_id: str, filled: Decimal, price: Decimal, *,
        source: str, sell_fill_count: int = 0, trade_id: Optional[str] = None,
    ) -> None:
        self.position_shares -= filled
        self._exit_filled_shares += filled
        self._exit_revenue += filled * price

        if self._el:
            detail: dict[str, Any] = {
                "order_id": order_id,
                "filled_size": str(filled),
                "fill_price": str(price),
                "remaining_position": str(self.position_shares),
                "source": source,
            }
            if trade_id:
                detail["trade_id"] = trade_id
            self._el.log_step("sell_filled", detail, phase="exit")

        self._update_trade_summary({
            "exit_price": str((self._exit_revenue / self._exit_filled_shares).quantize(Decimal("0.0001"))),
            "exit_shares": str(self._exit_filled_shares),
            "exit_revenue": str(self._exit_revenue),
            "exit_order_id": order_id,
            **({
                "exited_at": datetime.now(timezone.utc).replace(tzinfo=None),
            } if self.position_shares <= 0 else {}),
        })

    def _record_sell_complete(
        self, order_id: str, fill_count: int, placed_ms: int,
    ) -> None:
        elapsed_ms = int(time.time() * 1000) - placed_ms if placed_ms else 0
        if self._el:
            self._el.log_step("sell_complete", {
                "order_id": order_id,
                "total_filled": str(self._exit_filled_shares),
                "remaining_position": str(self.position_shares),
                "fill_count": fill_count,
                "elapsed_ms": elapsed_ms,
            }, phase="exit")

    async def _on_sell_fill(self, event: FillEvent) -> None:
        if self.state == "closed":
            return
        self._normal_sell_matched[event.order_id] = max(
            self._normal_sell_matched.get(event.order_id, Decimal("0")),
            event.total_matched,
        )
        self._record_sell_fill(event.order_id, event.fill_size, event.fill_price,
                               source=event.source, trade_id=event.trade_id)
        if self.position_shares <= 0 and hasattr(self, '_sell_done_event'):
            self._sell_done_event.set()

    async def _on_normal_sell_cancel(self, event: Any) -> None:
        self._normal_sell_cancelled = True
        known_matched = self._normal_sell_matched.get(event.order_id, Decimal("0"))
        if event.size_matched > known_matched:
            self._record_sell_fill(
                event.order_id,
                event.size_matched - known_matched,
                self.sell_price,
                source="user_ws_cancel",
            )
        self._normal_sell_matched.pop(event.order_id, None)
        if self._el:
            self._el.log_step("sell_cancelled", {
                "order_id": event.order_id,
                "success": True,
                "size_matched": str(event.size_matched),
                "source": "user_ws",
            }, phase="exit")
        if hasattr(self, "_sell_done_event"):
            self._sell_done_event.set()

    # ==================== Risk Exit ====================

    async def _cancel_order_fast(self, order_id: str, side: str, trigger: str) -> CancelResult:
        user_ws = getattr(self._executor, "_user_ws", None)
        if user_ws:
            user_ws.unwatch_order(order_id)

        detailed_cancel = getattr(self._executor, "cancel_order_detailed", None)
        if detailed_cancel is not None:
            cancel_result = await detailed_cancel(order_id)
        else:
            try:
                cancelled = await self._executor.cancel_order(order_id)
            except Exception as exc:
                cancel_result = CancelResult(
                    order_id=order_id,
                    cancelled=False,
                    final_matched=Decimal("-1"),
                    status="cancel_failed",
                    cancel_error=str(exc),
                    cancel_error_message=str(exc),
                )
            else:
                cancel_result = CancelResult(
                    order_id=order_id,
                    cancelled=cancelled,
                    final_matched=Decimal("-1"),
                    status="cancelled" if cancelled else "cancel_failed",
                    cancel_error=None if cancelled else "cancel_order returned false",
                    cancel_error_message=None if cancelled else "cancel_order returned false",
                )

        if self._el:
            if cancel_result.cancelled:
                step = "buy_cancelled" if side.upper() == "BUY" else "sell_cancelled"
            else:
                step = "buy_cancel_failed" if side.upper() == "BUY" else "sell_cancel_failed"
            detail = {
                "order_id": order_id,
                "success": cancel_result.cancelled,
                "trigger": trigger,
                "fast_path": True,
                **self._cancel_error_detail(cancel_result),
            }
            self._el.log_step(step, detail, phase=self._event_phase())
        return cancel_result

    async def _on_risk_sell_cancel(self, _event: Any) -> None:
        if hasattr(self, "_sell_done_event"):
            self._sell_done_event.set()

    async def _risk_exit(self, trigger: str = "stop_loss") -> None:
        """快速风控退出：撤单后只提交一次地板价 SELL。"""
        if self.state == "closed":
            return

        prev_state = self.state
        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()

        if self._el and trigger == "stop_loss":
            self._el.log_step("risk_triggered", {
                "reference_mid": str(self.risk.reference_mid),
                "threshold": str(self.risk.threshold),
                "stop_loss_ratio": self._config.get("stop_loss_ratio", "0.50"),
                "state_at_trigger": prev_state,
                "pending_buy": self.entry_order_id,
                "pending_sell": self.exit_order_id,
                "position_shares": str(self.position_shares),
            }, phase=self._event_phase())

        await self.risk.stop()
        cancellation_failures: list[dict[str, Any]] = []

        if self.entry_order_id:
            order_id = self.entry_order_id
            cancel_result = await self._cancel_order_fast(order_id, "BUY", trigger)
            if cancel_result.cancelled:
                self.entry_order_id = None
            else:
                cancel_detail = self._cancel_error_detail(cancel_result)
                error = cancel_detail.get("error") or "cancel_order returned false"
                cancellation_failures.append({
                    "side": "BUY",
                    "order_id": order_id,
                    "error": error,
                    **cancel_detail,
                })

        if self.exit_order_id:
            order_id = self.exit_order_id
            cancel_result = await self._cancel_order_fast(order_id, "SELL", trigger)
            if cancel_result.cancelled:
                self.exit_order_id = None
            else:
                cancel_detail = self._cancel_error_detail(cancel_result)
                error = cancel_detail.get("error") or "cancel_order returned false"
                cancellation_failures.append({
                    "side": "SELL",
                    "order_id": order_id,
                    "error": error,
                    **cancel_detail,
                })

        if self.position_shares <= 0:
            if cancellation_failures:
                failure = cancellation_failures[0]
                self._close_exit_failed(
                    f"{failure['side'].lower()}_cancel_failed",
                    phase=self._event_phase(),
                    extra={
                        **failure,
                        "error_signature": classify_sell_error("failed", failure["error"]),
                        "fast_path": True,
                    },
                )
            else:
                self._close(trigger, phase=self._event_phase())
            return

        self._mark_exit_started()
        risk_origin_ms = int(time.time() * 1000)
        sell_size = self.position_shares
        self.sell_price = Decimal("0.01")
        self._update_trade_summary({"exit_order_size": str(sell_size)})

        result = await self._executor.place_order(
            token_id=self.token_id,
            side="SELL",
            price="0.01",
            size=str(sell_size),
            tick_size=str(self._tick_size),
            neg_risk=True,
            check_balance=False,
            validate_tick_size=False,
        )

        if result.status in ("failed", "insufficient_balance"):
            error_signature = classify_sell_error(result.status, result.error)
            if self._el:
                self._el.log_step("sell_order_failed", {
                    **self._order_error_detail(result, error_signature=error_signature),
                    "order": self._order_obj(
                        result.order_id, "SELL", "0.01", str(sell_size), risk_origin_ms,
                    ),
                    "reason": trigger,
                    "attempt": 1,
                    "fast_path": True,
                }, phase="exit")
            self._close_exit_failed(self._sell_failure_reason(result), phase="exit", extra={
                "error": result.error,
                "error_message": result.error_message,
                "status_code": result.error_status_code,
                "error_signature": error_signature,
                "fast_path": True,
                "cancel_failures": cancellation_failures,
            })
            return

        self.exit_order_id = result.order_id
        if self._el:
            self._el.log_step("sell_order_placed", {
                "order": self._order_obj(
                    result.order_id, "SELL", "0.01", str(sell_size), risk_origin_ms,
                ),
                "clob_status": result.clob_status,
                "clob_taking": result.clob_taking,
                "clob_making": result.clob_making,
                "reason": trigger,
                "trigger": trigger,
                "attempt": 1,
                "fast_path": True,
            }, phase="exit")

        if result.status in ("filled", "partial"):
            filled = Decimal(result.filled_size)
            if filled > 0:
                self._record_sell_fill_risk(result.order_id, filled, Decimal("0.01"))

        if self.position_shares <= 0:
            self.exit_order_id = None
            if self._el:
                self._el.log_step("sell_complete", {
                    "order_id": result.order_id,
                    "total_filled": str(self._exit_filled_shares),
                    "remaining_position": "0",
                    "fast_path": True,
                }, phase="exit")
            if cancellation_failures:
                failure = cancellation_failures[0]
                self._close_exit_failed(
                    f"{failure['side'].lower()}_cancel_failed",
                    phase="exit",
                    extra={
                        **failure,
                        "error_signature": classify_sell_error("failed", failure["error"]),
                        "fast_path": True,
                    },
                )
            else:
                self._close(trigger, phase="exit")
            return

        try:
            user_ws = await self._executor.ensure_user_ws()
        except Exception as exc:
            self._close_exit_failed("sell_placement_failed", phase="exit", extra={
                "error": f"unable to watch risk SELL: {exc}",
                "error_signature": classify_sell_error("failed", str(exc)),
                "fast_path": True,
            })
            return

        sell_done = asyncio.Event()
        self._sell_done_event = sell_done
        user_ws.watch_order(
            order_id=result.order_id,
            token_id=self.token_id,
            side="SELL",
            price=Decimal("0.01"),
            initial_matched=Decimal(result.filled_size),
            on_fill=self._on_sell_fill_risk,
            on_cancel=self._on_risk_sell_cancel,
        )

        try:
            await asyncio.wait_for(sell_done.wait(), timeout=30)
        except asyncio.TimeoutError:
            if self._el:
                self._el.log_step("exit_order_unfilled", {
                    "order_id": result.order_id,
                    "remaining_position": str(self.position_shares),
                    "wait_ms": 30000,
                    "fast_path": True,
                }, phase="exit")
            user_ws.unwatch_order(result.order_id)
            cancel_result = await self._cancel_order_fast(result.order_id, "SELL", trigger)
            if cancel_result.cancelled:
                self.exit_order_id = None
            else:
                cancel_detail = self._cancel_error_detail(cancel_result)
                error = cancel_detail.get("error") or "cancel_order returned false"
                cancellation_failures.append({
                    "side": "SELL",
                    "order_id": result.order_id,
                    "error": error,
                    **cancel_detail,
                })
        else:
            user_ws.unwatch_order(result.order_id)
            self.exit_order_id = None

        if self.position_shares <= 0 and not cancellation_failures and self._el:
            self._el.log_step("sell_complete", {
                "order_id": result.order_id,
                "total_filled": str(self._exit_filled_shares),
                "remaining_position": "0",
                "fast_path": True,
            }, phase="exit")

        if cancellation_failures:
            failure = cancellation_failures[0]
            self._close_exit_failed(
                f"{failure['side'].lower()}_cancel_failed",
                phase="exit",
                extra={
                    **failure,
                    "error_signature": classify_sell_error("failed", failure["error"]),
                    "fast_path": True,
                },
            )
        else:
            self._close(trigger, phase="exit")

    def _record_sell_fill_risk(
        self, order_id: str, filled: Decimal, price: Decimal,
    ) -> None:
        self.position_shares -= filled
        self._exit_filled_shares += filled
        self._exit_revenue += filled * price

        if self._el:
            self._el.log_step("sell_filled", {
                "order_id": order_id,
                "filled_size": str(filled),
                "fill_price": str(price),
                "remaining_position": str(self.position_shares),
                "source": "clob_response",
            }, phase="exit")

        self._update_trade_summary({
            "exit_price": str(price),
            "exit_shares": str(self._exit_filled_shares),
            "exit_revenue": str(self._exit_revenue),
            "exit_order_id": order_id,
            "exited_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })

    async def _on_sell_fill_risk(self, event: FillEvent) -> None:
        if self.state == "closed":
            return
        self._record_sell_fill_risk(event.order_id, event.fill_size, event.fill_price)
        if self.position_shares <= 0 and hasattr(self, '_sell_done_event'):
            self._sell_done_event.set()

    # ==================== Force Exit ====================

    async def force_exit(self, reason: str = "config_disabled") -> None:
        if self.state == "closed":
            return

        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()

        if self._el:
            self._el.log_step("force_exit_requested", {
                "reason": reason,
                "trigger": "user",
                "state_at_exit": self.state,
                "position_shares": str(self.position_shares),
            }, phase=self._event_phase())

        self._mark_exit_started()

        if self.entry_order_id:
            order_id = self.entry_order_id
            user_ws = await self._executor.ensure_user_ws()
            user_ws.unwatch_order(order_id)
            cancel_result = await self._executor.cancel_order_with_fill_check(order_id)
            if not cancel_result.cancelled:
                await self._stop_for_cancel_failure(
                    phase=self._event_phase(), side="BUY", order_id=order_id,
                    cancel_result=cancel_result,
                )
                return
            if cancel_result.query_failed:
                if cancel_result.cancelled:
                    self.entry_order_id = None
                await self._stop_for_cancel_query_failure(
                    phase=self._event_phase(), side="BUY", order_id=order_id,
                    cancel_result=cancel_result,
                )
                return
            if self._el:
                self._el.log_step("buy_cancelled", {
                    "order_id": order_id,
                    "success": cancel_result.cancelled,
                    "final_matched": str(cancel_result.final_matched),
                    "trigger": reason,
                }, phase=self._event_phase())
            self.entry_order_id = None
            if cancel_result.final_matched > 0 and cancel_result.final_matched > self.position_shares:
                missed = cancel_result.final_matched - self.position_shares
                self._record_buy_fill(
                    order_id,
                    missed,
                    getattr(self, 'buy_price', Decimal("0.99")),
                    source="force_exit_reconcile",
                )
                if self._el:
                    self._el.log_step("fill_reconciled", {
                        "side": "BUY",
                        "order_id": order_id,
                        "clob_matched": str(cancel_result.final_matched),
                        "memory_before": str(self.position_shares - missed),
                        "reconciled": str(missed),
                    }, phase=self._event_phase())

        if self.exit_order_id:
            order_id = self.exit_order_id
            sell_start_shares = self.position_shares
            user_ws = await self._executor.ensure_user_ws()
            user_ws.unwatch_order(order_id)
            cancel_result = await self._executor.cancel_order_with_fill_check(order_id)
            if not cancel_result.cancelled:
                await self._stop_for_cancel_failure(
                    phase=self._event_phase(), side="SELL", order_id=order_id,
                    cancel_result=cancel_result,
                )
                return
            if cancel_result.query_failed:
                if cancel_result.cancelled:
                    self.exit_order_id = None
                await self._stop_for_cancel_query_failure(
                    phase=self._event_phase(), side="SELL", order_id=order_id,
                    cancel_result=cancel_result,
                )
                return
            if self._el:
                self._el.log_step("sell_cancelled", {
                    "order_id": order_id,
                    "success": cancel_result.cancelled,
                    "final_matched": str(cancel_result.final_matched),
                    "trigger": reason,
                }, phase=self._event_phase())
            self.exit_order_id = None
            sold_by_memory = sell_start_shares - self.position_shares
            if cancel_result.final_matched > sold_by_memory:
                missed = cancel_result.final_matched - sold_by_memory
                self._record_sell_fill(
                    order_id,
                    missed,
                    getattr(self, "sell_price", Decimal("0.01")),
                    source="force_exit_reconcile",
                )
                if self._el:
                    self._el.log_step("fill_reconciled", {
                        "side": "SELL",
                        "order_id": order_id,
                        "clob_matched": str(cancel_result.final_matched),
                        "memory_before": str(sold_by_memory),
                        "reconciled": str(missed),
                    }, phase="exit")

        await self.risk.stop()
        self._close("force_exit", phase=self._event_phase())

    async def market_settled(self, signal: Signal) -> None:
        """Close this trade after its market reaches a terminal state."""
        if self.state == "closed" or self._market_settled_requested:
            return
        self._market_settled_requested = True

        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()
        if self._el:
            self._el.log_step("market_settled", {
                "signal_id": signal.signal_id,
                "market_slug": signal.market_slug,
                "event_slug": signal.payload.get("event_slug"),
                "reason": signal.payload.get("reason"),
                "position_shares": str(self.position_shares),
                "utc": self._utc_str(),
            }, phase=self._event_phase())
        await self.risk.stop()
        await self._risk_exit(trigger="market_settled")

    # ==================== Stop ====================

    async def stop(self) -> None:
        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()
        await self.risk.stop()

    # ==================== Close & Trade Summary ====================

    def _close_exit_failed(
        self, reason: str, phase: str = "exit", extra: dict | None = None,
    ) -> None:
        detail = dict(extra or {})
        detail.setdefault("position_shares", str(self.position_shares))
        detail.setdefault("position_open", self.position_shares > 0)

        error_signature = detail.get("error_signature")
        if (
            self._on_exit_failed
            and error_signature
            and not self._exit_failure_reported
        ):
            self._exit_failure_reported = True
            try:
                pause_detail = self._on_exit_failed(self.token_id, error_signature)
            except Exception:
                logger.exception("Failed to report SELL failure to strategy")
                pause_detail = None
            if pause_detail is not None and self._el:
                self._el.log_step("strategy_paused", {
                    "reason": "sell_error_circuit_breaker",
                    "trigger_token_id": self.token_id,
                    "utc": self._utc_str(),
                    **pause_detail,
                }, phase=phase)

        self._close(
            reason,
            phase=phase,
            extra=detail,
        )

    def _close(
        self,
        reason: str,
        phase: str = "exit",
        extra: dict | None = None,
        *,
        event_step: str = "event_closed",
    ) -> None:
        if self.state == "closed":
            return

        sell_done = getattr(self, "_sell_done_event", None)
        if sell_done is not None:
            sell_done.set()

        user_ws = getattr(self._executor, '_user_ws', None)
        if user_ws:
            for oid in (self.entry_order_id, self.exit_order_id):
                if oid:
                    user_ws.unwatch_order(oid)

        duration_ms = int(time.time() * 1000) - self._event_start_ms if self._event_start_ms else 0

        pnl = None
        pnl_pct = None
        if self._entry_cost > 0 and self._exit_revenue > 0:
            pnl = self._exit_revenue - self._entry_cost
            pnl_pct = (pnl / self._entry_cost * 100).quantize(Decimal("0.01"))

        self._update_trade_summary({
            "phase": "closed",
            "close_reason": reason,
            "pnl": str(pnl) if pnl is not None else None,
            "pnl_pct": str(pnl_pct) if pnl_pct is not None else None,
            "duration_ms": duration_ms,
            "closed_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })

        if self._el:
            detail = {
                "reason": reason,
                "close_reason": reason,
                "total_position": str(self.position_shares),
                "position_shares": str(self.position_shares),
                "position_open": self.position_shares > 0,
                "duration_ms": duration_ms,
            }
            if extra:
                detail.update(extra)
            self._el.log_step(event_step, detail, phase=phase)
            self._el.end_event()

        self.state = "closed"
        self._on_closed(self.token_id)

    def _insert_trade_summary(self, signal: Signal) -> None:
        if not self._trade_dao or not self._el or not self._el.event_id:
            return
        try:
            self._trade_dao.insert({
                "event_id": self._el.event_id,
                "config_id": self._el._config_id,
                "owner_user_id": self._el._owner_user_id,
                "proxy_wallet": self._el._proxy_wallet,
                "signal_id": signal.signal_id,
                "token_id": signal.token_id,
                "market_slug": signal.market_slug,
                "event_slug": signal.payload.get("event_slug"),
                "city": signal.payload.get("city"),
                "direction": signal.payload.get("direction"),
                "outcome": signal.payload.get("outcome"),
                "temperature_label": signal.payload.get("temperature_label"),
                "is_from_main": 1 if signal.payload.get("is_from_main", True) else 0,
                "params_version": self._params_version,
                "config_snapshot": json.dumps(self._config_snapshot, ensure_ascii=False, default=str),
                "phase": "entry",
                "started_at": datetime.now(timezone.utc).replace(tzinfo=None),
            })
        except Exception:
            logger.exception("Failed to insert trade summary")

    def _update_trade_summary(self, data: dict[str, Any]) -> None:
        if not self._trade_dao or not self._el or not self._el.event_id:
            return
        try:
            self._trade_dao.update_by_event_id(self._el.event_id, data)
        except Exception:
            logger.exception("Failed to update trade summary")


# ============================================================
# SweepStrategy — 实例级交易管理器
# ============================================================


class SweepStrategy:
    """策略实例：管理多笔并行交易，按 token_id 去重。"""

    EVENTS_TABLE = "strategy_weather_sweep_events"

    # ==================== 工厂方法 ====================

    @classmethod
    async def create(
        cls,
        config_data: dict[str, Any],
        orderbook_ws: Any,
        tick_size_service: Optional[TickSizeService] = None,
    ) -> SweepStrategy:
        """工厂方法 — 根据一条 DB 配置创建完整策略实例。"""
        proxy_wallet = config_data["proxy_wallet"]
        tick_size_service = tick_size_service or TickSizeService()

        executor = OrderExecutor(
            proxy_wallet=proxy_wallet,
            tick_size_service=tick_size_service,
        )
        await executor.ensure_poller(proxy_wallet)
        await executor.warmup()

        instance = cls()
        instance._config = config_data["params"]
        instance._executor = executor
        instance._tick_size_service = tick_size_service
        instance._orderbook_ws = orderbook_ws
        instance._proxy_wallet = proxy_wallet
        instance._owner_user_id = config_data["owner_user_id"]
        instance._config_id = config_data["id"]
        instance._config_snapshot = config_data.get("params")
        instance._params_version = config_data.get("params_version", 1)
        instance._draining = False
        instance._trades: dict[str, SweepTrade] = {}
        instance._trade_dao = WeatherSweepTradeDAO()
        instance._config_dao = WeatherSweepConfigDAO()
        instance._sell_error_breaker = SellErrorCircuitBreaker()
        instance._sell_error_window_sec = 300.0
        return instance

    # ==================== Signal Dispatch ====================

    async def on_signal(self, signal: Signal) -> None:
        if signal.signal_type == "market_resolved":
            if self._draining:
                return
            for trade in list(self._trades.values()):
                if trade.market_slug == signal.market_slug:
                    await trade.market_settled(signal)
            return
        if signal.signal_type != "sweep":
            return
        if self._draining:
            return
        if signal.token_id in self._trades:
            return
        if not self._should_accept_signal(signal):
            return

        el = EventLogger(
            table=self.EVENTS_TABLE,
            owner_user_id=self._owner_user_id,
            proxy_wallet=self._proxy_wallet,
            config_id=self._config_id,
            config_snapshot=self._config_snapshot,
        )

        trade = SweepTrade(
            token_id=signal.token_id,
            market_slug=signal.market_slug,
            config=self._config,
            params_version=self._params_version,
            config_snapshot=self._config_snapshot,
            executor=self._executor,
            orderbook_ws=self._orderbook_ws,
            event_logger=el,
            on_closed=self._remove_trade,
            on_exit_failed=self._on_exit_failed,
            trade_dao=self._trade_dao,
            tick_size_service=self._tick_size_service,
        )
        self._trades[signal.token_id] = trade
        await trade.enter(signal)

    def _should_accept_signal(self, signal: Signal) -> bool:
        payload = signal.payload
        cfg = self._config

        outcome_filter = cfg.get("sweep_outcome_filter", "no")
        if outcome_filter != "all" and payload.get("outcome", "") != outcome_filter:
            return False

        source_filter = cfg.get("signal_source_filter", "main")
        if source_filter == "main" and not payload.get("is_from_main", True):
            return False
        if source_filter == "next" and payload.get("is_from_main", True):
            return False

        threshold_filter = cfg.get("signal_threshold_filter", "all")
        if threshold_filter != "all":
            reason = payload.get("reason", "")
            if f"through_{threshold_filter}_cleared" not in reason:
                return False

        direction_filter = cfg.get("direction_filter", "all")
        if direction_filter != "all":
            if payload.get("direction", "") != direction_filter:
                return False

        return True

    def _on_exit_failed(
        self, token_id: str, error_signature: str,
    ) -> Optional[dict[str, Any]]:
        if not self._sell_error_breaker.record_event(token_id, error_signature):
            return None

        self._draining = True
        config_disabled = True
        try:
            self._config_dao.update(self._config_id, {"enabled": 0})
        except Exception:
            config_disabled = False
            logger.exception("Failed to disable config after SELL error breaker")

        return {
            "error_signature": error_signature,
            "event_count": self._sell_error_breaker.event_count(error_signature),
            "window_sec": int(self._sell_error_window_sec),
            "config_disabled": config_disabled,
            "action": "pause_strategy",
        }

    # ==================== Lifecycle ====================

    async def destroy(self, reason: str) -> None:
        await self.force_exit(reason)

    async def force_exit(self, reason: str = "config_disabled") -> None:
        for trade in list(self._trades.values()):
            await trade.force_exit(reason)
        self._trades.clear()

    async def drain(self) -> None:
        self._draining = True

    def _remove_trade(self, token_id: str) -> None:
        self._trades.pop(token_id, None)

    @property
    def active_trade_count(self) -> int:
        return len(self._trades)

    @property
    def active_tokens(self) -> list[str]:
        return list(self._trades.keys())
