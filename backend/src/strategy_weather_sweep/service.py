"""策略 1: Sweep 信号下单 — BUY@0.99 → tick exit SELL@0.999。

职责：策略业务逻辑 + 实例工厂方法。

架构：
  SweepStrategy — 实例级管理器，持有共享工具，管理多笔并行交易
  SweepTrade   — 单笔交易生命周期（per token_id）

流程 (每笔 trade):
  1. 收到合格 sweep → 快速 BUY@0.99 固定份额
  2. 启动风控 + 订单簿监听
  3. entry_wait_ms 后撤销未成交 BUY
  4. 无持仓 → 关闭；有持仓 → 等待 tick=0.001
  5. HTTP 校验通过 → SELL@0.999；风控触发 → 强制退出

Event log 三层分离（参见 doc/2026-08-28-event-log-storage-design.md）：
  挂单结果: order_placed / order_failed
  成交通知: buy_filled / sell_filled
  阶段终态: entry_complete / entry_timeout / sell_complete / sell_timeout

强制退出（配置变更/禁用）:
  - 撤销所有未成交买单
  - 已持份额按 1 - tick_size 的价格挂卖单
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from framework.strategy_runtime.event_logger import EventLogger
from framework.strategy_runtime.interfaces import Signal
from framework.strategy_runtime.order_executor import OrderExecutor
from framework.strategy_runtime.tick_verifier import TickVerifier
from framework.user_ws import FillEvent
from strategy_weather_sweep.dao import WeatherSweepTradeDAO
from strategy_weather_sweep.internal.risk_monitor import SweepRiskMonitor

logger = logging.getLogger(__name__)


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
        trade_dao: Optional[WeatherSweepTradeDAO] = None,
    ) -> None:
        self.token_id = token_id
        self.market_slug = market_slug
        self._config = config
        self._executor = executor
        self._orderbook_ws = orderbook_ws
        self._el = event_logger
        self._on_closed = on_closed
        self._trade_dao = trade_dao

        self.state = "entry_working"
        self.position_shares = Decimal("0")
        self.entry_order_id: Optional[str] = None
        self.exit_order_id: Optional[str] = None
        self._entry_timer: Optional[asyncio.Task] = None
        self._tick_verified = False
        self._tick_size = Decimal("0.01")
        self._event_start_ms = int(time.time() * 1000)
        self._entry_cost = Decimal("0")
        self._exit_revenue = Decimal("0")

        # Fill tracking
        self._order_size = Decimal("0")        # original buy order size
        self._order_placed_ms = 0              # buy order placed timestamp
        self._buy_fill_count = 0               # total buy_filled steps

        self.risk = SweepRiskMonitor(
            orderbook_ws=orderbook_ws,
            stop_loss_ratio=Decimal(config.get("stop_loss_ratio", "0.60")),
            on_trigger=self._risk_exit,
            on_tick_change=self._on_tick_change,
        )
        self.tick_verifier = TickVerifier()

    # ==================== Helpers ====================

    @staticmethod
    def _utc_str() -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}"

    def _snapshot_bbo(self, offset_origin_ms: int) -> dict:
        book = self._orderbook_ws.get_book(self.token_id) if self._orderbook_ws else None
        elapsed = int((time.time() * 1000) - offset_origin_ms)
        utc = self._utc_str()
        if book:
            bids = sorted(book.bids.items(), reverse=True)
            asks = sorted(book.asks.items())
            return {
                "utc": utc,
                "offset_ms": elapsed,
                "best_bid": bids[0][0] if bids else None,
                "best_bid_size": bids[0][1] if bids else None,
                "best_ask": asks[0][0] if asks else None,
                "best_ask_size": asks[0][1] if asks else None,
            }
        return {
            "utc": utc,
            "offset_ms": elapsed,
            "best_bid": None, "best_bid_size": None,
            "best_ask": None, "best_ask_size": None,
        }

    @staticmethod
    def _bbo_from_signal_snapshot(snapshot: dict, offset_origin_ms: int) -> dict:
        best_bid = snapshot.get("best_bid")
        best_ask = snapshot.get("best_ask")
        observed_ms = snapshot.get("observed_at_unix_ms")
        return {
            "utc": snapshot.get("observed_at", ""),
            "offset_ms": int(observed_ms - offset_origin_ms) if observed_ms else 0,
            "best_bid": float(best_bid["price"]) if best_bid else None,
            "best_bid_size": float(best_bid["size"]) if best_bid else None,
            "best_ask": float(best_ask["price"]) if best_ask else None,
            "best_ask_size": float(best_ask["size"]) if best_ask else None,
            "source": "signal_snapshot",
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

        # --- Layer 1: order_failed (no cash) ---
        if actual_shares <= 0:
            logger.warning("No available cash for %s, closing", self.token_id[:10])
            if self._el:
                self._el.log_step("order_failed", {
                    "status": "no_cash",
                    "requested_size": str(fixed_shares),
                    "available_cash": str(available),
                }, phase="entry")
            self._close("buy_failed", phase="entry", extra={
                "error": f"no_cash: requested={fixed_shares}, available={available}",
            })
            return

        order_task = asyncio.create_task(self._executor.place_order(
            token_id=self.token_id,
            side="BUY",
            price=str(buy_price),
            size=str(actual_shares),
            tick_size="0.01",
            neg_risk=True,
        ))
        risk_task = asyncio.create_task(
            self.risk.start(token_id=self.token_id, orderbook_snapshot=orderbook_snapshot)
        )

        await risk_task
        pre_bbo = self._bbo_from_signal_snapshot(orderbook_snapshot, enter_origin_ms)

        result = await order_task
        aft_bbo = self._snapshot_bbo(enter_origin_ms)

        # Track order metadata for entry_complete
        self._order_size = actual_shares
        self._order_placed_ms = int(time.time() * 1000)

        # --- Layer 1: order_failed (CLOB rejected) ---
        if result.status in ("failed", "insufficient_balance"):
            if self._el:
                self._el.log_step("order_failed", {
                    "status": result.status,
                    "error": result.error,
                    "order": self._order_obj(
                        result.order_id, "BUY", str(buy_price), str(actual_shares), enter_origin_ms,
                    ),
                    "pre_bbo": pre_bbo,
                    "aft_bbo": aft_bbo,
                }, phase="entry")
            self._close("buy_failed", phase="entry", extra={
                "error": result.error,
            })
            return

        # --- Layer 1: order_placed (CLOB accepted) ---
        self.entry_order_id = result.order_id
        if self._el:
            self._el.log_step("order_placed", {
                "order": self._order_obj(
                    result.order_id, "BUY", str(buy_price), str(actual_shares), enter_origin_ms,
                ),
                "clob_status": result.clob_status,
                "clob_taking": result.clob_taking,
                "clob_making": result.clob_making,
                "pre_bbo": pre_bbo,
                "aft_bbo": aft_bbo,
            }, phase="entry")

        # --- Layer 2: buy_filled (immediate fill from CLOB response) ---
        if result.status in ("filled", "partial"):
            filled = Decimal(result.filled_size)
            if filled > 0:
                self._record_buy_fill(result.order_id, filled, buy_price, source="clob_response")

        # --- Layer 3: entry_complete (all filled immediately) ---
        if result.status == "filled":
            self.entry_order_id = None
            self._record_entry_complete(result.order_id)
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
            "entry_price": str(price),
            "entry_shares": str(self.position_shares),
            "entry_cost": str(self._entry_cost),
            "entry_order_id": order_id,
            "entered_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })

    def _record_entry_complete(self, order_id: str) -> None:
        elapsed_ms = int(time.time() * 1000) - self._order_placed_ms if self._order_placed_ms else 0
        if self._el:
            self._el.log_step("entry_complete", {
                "order_id": order_id,
                "total_filled": str(self.position_shares),
                "total_position": str(self.position_shares),
                "fill_count": self._buy_fill_count,
                "elapsed_ms": elapsed_ms,
            }, phase="entry")
        self._update_trade_summary({"status": "exit_working"})

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
            self._record_entry_complete(event.order_id)

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
            self.entry_order_id = None
            if cancel_result.final_matched > 0 and cancel_result.final_matched > self.position_shares:
                missed = cancel_result.final_matched - self.position_shares
                self._record_buy_fill(order_id, missed, self.buy_price,
                                      source="cancel_reconcile")
                if self._el:
                    self._el.log_step("fill_reconcile", {
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
            self._close("timeout_no_fill", phase="entry")
        else:
            self.state = "exit_working"
            self._update_trade_summary({"status": "exit_working"})

    # ==================== Monitor & Exit ====================

    async def _on_tick_change(self, new_tick: Decimal) -> None:
        if self._tick_verified or self.state not in ("entry_working", "exit_working"):
            return
        if self._tick_size == new_tick:
            return

        if new_tick == Decimal("0.001"):
            self._tick_size = Decimal("0.001")
            if self._el:
                self._el.log_step("tick_detected", {
                    "tick_size": "0.001",
                    "source": "risk_ws",
                }, phase="monitor")

            result = await self.tick_verifier.verify(self.token_id)
            if self.state not in ("entry_working", "exit_working"):
                return
            if result.confirmed:
                self._tick_verified = True
                if self._el:
                    self._el.log_step("tick_verified", {
                        "token_id": self.token_id,
                        "confirmed": True,
                    }, phase="monitor")
                await self._tick_exit()

    async def _tick_exit(self) -> None:
        if self.position_shares <= 0:
            await self.risk.stop()
            self._close("tick_exit", phase="exit")
            return

        self.state = "exit_working"
        sell_price = Decimal("0.01")
        sell_timeout_s = 600
        sell_backoff_base = 2.0
        sell_backoff_cap = 60.0

        deadline = time.monotonic() + sell_timeout_s
        attempt = 0
        exit_origin_ms = int(time.time() * 1000)

        while time.monotonic() < deadline:
            if self.state == "closed":
                return

            attempt += 1
            sell_size = self.position_shares
            result = await self._executor.place_order(
                token_id=self.token_id,
                side="SELL",
                price=str(sell_price),
                size=str(sell_size),
                tick_size="0.01",
                neg_risk=True,
                check_balance=False,
            )

            # --- Layer 1: sell_order_failed ---
            if result.status in ("failed", "insufficient_balance"):
                if self._el:
                    self._el.log_step("sell_order_failed", {
                        "status": result.status,
                        "error": result.error,
                        "order": self._order_obj(
                            result.order_id, "SELL", str(sell_price), str(sell_size), exit_origin_ms,
                        ),
                        "attempt": attempt,
                    }, phase="exit")

                if time.monotonic() < deadline:
                    backoff = min(sell_backoff_base * (2 ** (attempt - 1)), sell_backoff_cap)
                    if self._el:
                        self._el.log_step("sell_retry_start", {
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
                    "reason": "tick_exit",
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
                    self._close("tick_exit", phase="exit")
                    return
                # partial was labeled "filled" but position remains — continue selling
                continue

            # live → WS 监听 + 等待全部成交或超时
            self.sell_price = sell_price
            sell_start_shares = self.position_shares
            user_ws = await self._executor.ensure_user_ws()
            sell_done = asyncio.Event()
            self._sell_done_event = sell_done

            user_ws.watch_order(
                order_id=result.order_id, token_id=self.token_id, side="SELL",
                price=self.sell_price,
                initial_matched=Decimal(result.filled_size),
                on_fill=self._on_sell_fill,
            )

            try:
                remaining = deadline - time.monotonic()
                await asyncio.wait_for(sell_done.wait(), timeout=max(remaining, 0))
            except asyncio.TimeoutError:
                user_ws.unwatch_order(result.order_id)
                cancel_result = await self._executor.cancel_order_with_fill_check(result.order_id)
                self.exit_order_id = None
                sold_by_clob = cancel_result.final_matched
                sold_by_memory = sell_start_shares - self.position_shares
                if sold_by_clob > sold_by_memory:
                    missed = sold_by_clob - sold_by_memory
                    self._record_sell_fill(result.order_id, missed, self.sell_price,
                                           source="cancel_reconcile")
                    if self._el:
                        self._el.log_step("fill_reconcile", {
                            "side": "SELL", "order_id": result.order_id,
                            "clob_matched": str(sold_by_clob),
                            "memory_before": str(sold_by_memory),
                            "reconciled": str(missed),
                        }, phase="exit")
                continue

            # 全部成交
            user_ws.unwatch_order(result.order_id)
            self.exit_order_id = None
            self._record_sell_complete(result.order_id, sell_fill_count, sell_placed_ms)
            await self.risk.stop()
            self._close("tick_exit", phase="exit")
            return

        await self.risk.stop()
        if self._el:
            self._el.log_step("sell_give_up", {
                "attempts": attempt,
                "timeout_sec": sell_timeout_s,
                "remaining_position": str(self.position_shares),
                "last_error": result.error if result else None,
            }, phase="exit")
        self._close("sell_failed", phase="exit")

    def _record_sell_fill(
        self, order_id: str, filled: Decimal, price: Decimal, *,
        source: str, sell_fill_count: int = 0, trade_id: Optional[str] = None,
    ) -> None:
        self.position_shares -= filled
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
            "exit_price": str(price),
            "exit_shares": str(self._exit_revenue / price) if price > 0 else None,
            "exit_revenue": str(self._exit_revenue),
            "exit_order_id": order_id,
            "exited_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })

    def _record_sell_complete(
        self, order_id: str, fill_count: int, placed_ms: int,
    ) -> None:
        elapsed_ms = int(time.time() * 1000) - placed_ms if placed_ms else 0
        if self._el:
            self._el.log_step("sell_complete", {
                "order_id": order_id,
                "total_filled": str(self._order_size - self.position_shares),
                "remaining_position": str(self.position_shares),
                "fill_count": fill_count,
                "elapsed_ms": elapsed_ms,
            }, phase="exit")

    async def _on_sell_fill(self, event: FillEvent) -> None:
        if self.state == "closed":
            return
        self._record_sell_fill(event.order_id, event.fill_size, event.fill_price,
                               source=event.source, trade_id=event.trade_id)
        if self.position_shares <= 0 and hasattr(self, '_sell_done_event'):
            self._sell_done_event.set()

    # ==================== Risk Exit ====================

    async def _risk_exit(self) -> None:
        if self.state == "closed":
            return

        prev_state = self.state
        self.state = "risk_exiting"

        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()

        if self._el:
            self._el.log_step("risk_triggered", {
                "reference_mid": str(self.risk.reference_mid),
                "threshold": str(self.risk.threshold),
                "stop_loss_ratio": self._config.get("stop_loss_ratio", "0.50"),
                "state_at_trigger": prev_state,
                "pending_buy": self.entry_order_id,
                "pending_sell": self.exit_order_id,
                "position_shares": str(self.position_shares),
            }, phase="exit_risk")

        user_ws = await self._executor.ensure_user_ws()

        # 缺口①: 撤买单 + REST 校准
        if self.entry_order_id:
            order_id = self.entry_order_id
            user_ws.unwatch_order(order_id)
            cancel_result = await self._executor.cancel_order_with_fill_check(order_id)
            if self._el:
                self._el.log_step("risk_cancel_buy", {
                    "order_id": order_id,
                    "success": cancel_result.cancelled,
                    "final_matched": str(cancel_result.final_matched),
                }, phase="exit_risk")
            self.entry_order_id = None
            if cancel_result.final_matched > 0 and cancel_result.final_matched > self.position_shares:
                missed = cancel_result.final_matched - self.position_shares
                self._record_buy_fill(order_id, missed,
                                      getattr(self, 'buy_price', Decimal("0.99")),
                                      source="cancel_reconcile")
                if self._el:
                    self._el.log_step("fill_reconcile", {
                        "side": "BUY", "order_id": order_id,
                        "clob_matched": str(cancel_result.final_matched),
                        "memory_before": str(self.position_shares - missed),
                        "reconciled": str(missed),
                    }, phase="exit_risk")

        # 缺口②: 撤卖单 + REST 校准
        if self.exit_order_id:
            order_id = self.exit_order_id
            user_ws.unwatch_order(order_id)
            cancel_result = await self._executor.cancel_order_with_fill_check(order_id)
            if self._el:
                self._el.log_step("risk_cancel_sell", {
                    "order_id": order_id,
                    "success": cancel_result.cancelled,
                    "final_matched": str(cancel_result.final_matched),
                }, phase="exit_risk")
            self.exit_order_id = None

        # 缺口③: 清仓循环 — live 时用 WS + asyncio.Event 等待
        if self.position_shares > 0:
            risk_origin_ms = int(time.time() * 1000)
            risk_deadline = time.monotonic() + 600
            risk_attempt = 0
            risk_sell_wait_s = 30

            while self.position_shares > 0 and time.monotonic() < risk_deadline:
                risk_attempt += 1
                sell_size = self.position_shares
                sell_start_shares = self.position_shares
                result = await self._executor.place_order(
                    token_id=self.token_id,
                    side="SELL",
                    price="0.01",
                    size=str(sell_size),
                    tick_size="0.01",
                    neg_risk=True,
                    check_balance=False,
                )

                if result.status in ("failed", "insufficient_balance"):
                    if self._el:
                        self._el.log_step("sell_order_failed", {
                            "status": result.status,
                            "error": result.error,
                            "order": self._order_obj(
                                result.order_id, "SELL", "0.01", str(sell_size), risk_origin_ms,
                            ),
                            "reason": "stop_loss",
                            "attempt": risk_attempt,
                        }, phase="exit_risk")
                    if time.monotonic() < risk_deadline:
                        backoff = min(2.0 * (2 ** (risk_attempt - 1)), 60.0)
                        await asyncio.sleep(backoff)
                    continue

                if self._el:
                    self._el.log_step("risk_sell_order_placed", {
                        "order": self._order_obj(
                            result.order_id, "SELL", "0.01", str(sell_size), risk_origin_ms,
                        ),
                        "clob_status": result.clob_status,
                        "clob_taking": result.clob_taking,
                        "clob_making": result.clob_making,
                        "reason": "stop_loss",
                        "attempt": risk_attempt,
                    }, phase="exit_risk")

                risk_fill_count = 0
                if result.status in ("filled", "partial"):
                    filled = Decimal(result.filled_size)
                    if filled > 0:
                        self._record_sell_fill_risk(
                            result.order_id, filled, Decimal("0.01"),
                        )
                        risk_fill_count += 1

                if result.status == "filled":
                    if self.position_shares <= 0:
                        if self._el:
                            self._el.log_step("sell_complete", {
                                "order_id": result.order_id,
                                "total_filled": str(sell_size),
                                "remaining_position": "0",
                                "fill_count": risk_fill_count,
                                "elapsed_ms": 0,
                            }, phase="exit_risk")
                        break
                    continue

                # live → WS 监听 + 等待
                sell_done = asyncio.Event()
                self._sell_done_event = sell_done

                user_ws.watch_order(
                    order_id=result.order_id, token_id=self.token_id, side="SELL",
                    price=Decimal("0.01"),
                    initial_matched=Decimal(result.filled_size),
                    on_fill=self._on_sell_fill_risk,
                )

                try:
                    remaining = min(risk_sell_wait_s, risk_deadline - time.monotonic())
                    await asyncio.wait_for(sell_done.wait(), timeout=max(remaining, 0))
                except asyncio.TimeoutError:
                    pass

                user_ws.unwatch_order(result.order_id)
                cancel_result = await self._executor.cancel_order_with_fill_check(result.order_id)
                sold_by_clob = cancel_result.final_matched
                sold_by_memory = sell_start_shares - self.position_shares
                if sold_by_clob > sold_by_memory:
                    missed = sold_by_clob - sold_by_memory
                    self._record_sell_fill_risk(result.order_id, missed, Decimal("0.01"))
                if self.position_shares <= 0:
                    break

        self._close("stop_loss", phase="exit_risk")

    def _record_sell_fill_risk(
        self, order_id: str, filled: Decimal, price: Decimal,
    ) -> None:
        self.position_shares -= filled
        self._exit_revenue += filled * price

        if self._el:
            self._el.log_step("sell_filled", {
                "order_id": order_id,
                "filled_size": str(filled),
                "fill_price": str(price),
                "remaining_position": str(self.position_shares),
                "source": "clob_response",
            }, phase="exit_risk")

        self._update_trade_summary({
            "exit_price": str(price),
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
        await self.risk.stop()

        if self._el:
            self._el.log_step("force_exit", {
                "reason": reason,
                "trigger": "user",
                "state_at_exit": self.state,
                "position_shares": str(self.position_shares),
            }, phase="exit_force")

        user_ws = await self._executor.ensure_user_ws()

        if self.entry_order_id:
            order_id = self.entry_order_id
            user_ws.unwatch_order(order_id)
            cancel_result = await self._executor.cancel_order_with_fill_check(order_id)
            if self._el:
                self._el.log_step("buy_cancelled", {
                    "order_id": order_id,
                    "success": cancel_result.cancelled,
                    "final_matched": str(cancel_result.final_matched),
                }, phase="exit_force")
            self.entry_order_id = None
            if cancel_result.final_matched > 0 and cancel_result.final_matched > self.position_shares:
                missed = cancel_result.final_matched - self.position_shares
                self._record_buy_fill(order_id, missed,
                                      getattr(self, 'buy_price', Decimal("0.99")),
                                      source="cancel_reconcile")

        if self.exit_order_id:
            order_id = self.exit_order_id
            user_ws.unwatch_order(order_id)
            cancel_result = await self._executor.cancel_order_with_fill_check(order_id)
            if self._el:
                self._el.log_step("sell_cancelled", {
                    "order_id": order_id,
                    "success": cancel_result.cancelled,
                    "final_matched": str(cancel_result.final_matched),
                }, phase="exit_force")
            self.exit_order_id = None

        self._close("force_exit", phase="exit_force")

    # ==================== Stop ====================

    async def stop(self) -> None:
        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()
        await self.risk.stop()

    # ==================== Close & Trade Summary ====================

    def _close(self, reason: str, phase: str = "exit", extra: dict | None = None) -> None:
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
            "status": "closed",
            "close_reason": reason,
            "pnl": str(pnl) if pnl is not None else None,
            "pnl_pct": str(pnl_pct) if pnl_pct is not None else None,
            "duration_ms": duration_ms,
            "closed_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })

        if self._el:
            detail = {
                "reason": reason,
                "total_position": str(self.position_shares),
                "duration_ms": duration_ms,
            }
            if extra:
                detail.update(extra)
            self._el.log_step("event_closed", detail, phase=phase)
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
                "status": "entry_working",
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
    ) -> SweepStrategy:
        """工厂方法 — 根据一条 DB 配置创建完整策略实例。"""
        proxy_wallet = config_data["proxy_wallet"]

        executor = OrderExecutor(proxy_wallet=proxy_wallet)
        await executor.ensure_poller(proxy_wallet)
        await executor.warmup()

        instance = cls()
        instance._config = config_data["params"]
        instance._executor = executor
        instance._orderbook_ws = orderbook_ws
        instance._proxy_wallet = proxy_wallet
        instance._owner_user_id = config_data["owner_user_id"]
        instance._config_id = config_data["id"]
        instance._config_snapshot = config_data.get("params")
        instance._draining = False
        instance._trades: dict[str, SweepTrade] = {}
        instance._trade_dao = WeatherSweepTradeDAO()
        return instance

    # ==================== Signal Dispatch ====================

    async def on_signal(self, signal: Signal) -> None:
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
            executor=self._executor,
            orderbook_ws=self._orderbook_ws,
            event_logger=el,
            on_closed=self._remove_trade,
            trade_dao=self._trade_dao,
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
