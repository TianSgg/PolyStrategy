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

        self.risk = SweepRiskMonitor(
            orderbook_ws=orderbook_ws,
            stop_loss_ratio=Decimal(config.get("stop_loss_ratio", "0.60")),
            on_trigger=self._risk_exit,
            on_tick_change=self._on_tick_change,
        )
        self.tick_verifier = TickVerifier()

    # ==================== Entry ====================

    def _snapshot_bbo(self, offset_origin_ms: int) -> dict:
        book = self._orderbook_ws.get_book(self.token_id) if self._orderbook_ws else None
        now = datetime.now(timezone.utc)
        elapsed = int((time.time() * 1000) - offset_origin_ms)
        if book:
            bids = sorted(book.bids.items(), reverse=True)
            asks = sorted(book.asks.items())
            return {
                "best_bid": bids[0][0] if bids else None,
                "best_bid_size": bids[0][1] if bids else None,
                "best_ask": asks[0][0] if asks else None,
                "best_ask_size": asks[0][1] if asks else None,
                "utc": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}",
                "offset_ms": elapsed,
            }
        return {
            "best_bid": None, "best_bid_size": None,
            "best_ask": None, "best_ask_size": None,
            "utc": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}",
            "offset_ms": elapsed,
        }

    async def enter(self, signal: Signal) -> None:
        orderbook_snapshot = signal.payload.get("orderbook_snapshot", {})
        enter_origin_ms = int(time.time() * 1000)

        fixed_shares = Decimal(self._config.get("fixed_entry_shares", "100"))
        buy_price = Decimal("0.99")
        available = self._executor.available_cash
        max_shares = int(available / buy_price) if buy_price > 0 else 0
        actual_shares = min(fixed_shares, Decimal(str(max_shares)))

        if actual_shares <= 0:
            logger.warning("No available cash for %s, closing", self.token_id[:10])
            if self._el:
                self._el.start_event(
                    signal_id=signal.signal_id,
                    token_id=signal.token_id,
                    market_slug=signal.market_slug,
                    event_slug=signal.payload.get("event_slug"),
                )
                self._el.log_step("buy_failed", {
                    "reason": "no_cash",
                    "requested_size": str(fixed_shares),
                    "available_cash": str(available),
                }, phase="entry")
            self._close("buy_failed", phase="entry")
            return

        order_task = asyncio.create_task(self._executor.place_order(
            token_id=self.token_id,
            side="BUY",
            price=str(buy_price),
            size=str(actual_shares),
        ))
        risk_task = asyncio.create_task(
            self.risk.start(token_id=self.token_id, orderbook_snapshot=orderbook_snapshot)
        )

        await risk_task
        pre_bbo = self._snapshot_bbo(enter_origin_ms)

        result = await order_task
        now_ret = datetime.now(timezone.utc)
        order_returned = {
            "utc": now_ret.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_ret.microsecond // 1000:03d}",
            "offset_ms": int((time.time() * 1000) - enter_origin_ms),
        }
        aft_bbo = self._snapshot_bbo(enter_origin_ms)

        if self._el:
            self._el.start_event(
                signal_id=signal.signal_id,
                token_id=signal.token_id,
                market_slug=signal.market_slug,
                event_slug=signal.payload.get("event_slug"),
            )
            self._el.log_step("signal_received", {
                "signal_id": signal.signal_id,
                "token_id": signal.token_id,
                "market_slug": signal.market_slug,
                "event_slug": signal.payload.get("event_slug"),
                "city": signal.payload.get("city"),
                "direction": signal.payload.get("direction"),
                "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}",
                "risk_ref_mid": str(self.risk.reference_mid),
                "risk_threshold": str(self.risk.threshold),
            }, phase="entry")
            self._insert_trade_summary(signal)
        self.entry_order_id = result.order_id

        if result.status in ("failed", "insufficient_balance"):
            if self._el:
                self._el.log_step("buy_failed", {
                    "reason": result.status,
                    "error": result.error,
                    "requested_size": str(actual_shares),
                    "pre_bbo": pre_bbo,
                    "order_returned": order_returned,
                    "aft_bbo": aft_bbo,
                }, phase="entry")
            self._close("buy_failed", phase="entry")
            return

        if self._el:
            self._el.log_step("buy_placed", {
                "order_id": result.order_id,
                "price": str(buy_price),
                "size": str(actual_shares),
                "status": result.status,
                "pre_bbo": pre_bbo,
                "order_returned": order_returned,
                "aft_bbo": aft_bbo,
            }, phase="entry")

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            self.position_shares += filled
            self._entry_cost += filled * buy_price
            self.entry_order_id = None
            if self._el:
                self._el.log_step("buy_filled", {
                    "order_id": result.order_id,
                    "filled_size": str(filled),
                    "total_position": str(self.position_shares),
                    "fill_price": str(buy_price),
                }, phase="entry")
            self._update_trade_summary({
                "entry_price": str(buy_price),
                "entry_shares": str(self.position_shares),
                "entry_cost": str(self._entry_cost),
                "entry_order_id": result.order_id,
                "entered_at": datetime.now(timezone.utc).replace(tzinfo=None),
            })

        entry_wait_ms = int(self._config.get("entry_wait_ms", 1200000))
        self._entry_timer = asyncio.create_task(
            self._entry_timeout(entry_wait_ms / 1000.0)
        )

    async def _entry_timeout(self, wait_sec: float) -> None:
        await asyncio.sleep(wait_sec)
        if self.state == "closed":
            return

        if self.entry_order_id:
            await self._executor.cancel_order(self.entry_order_id)
            self.entry_order_id = None

        if self._el:
            self._el.log_step("entry_timeout", {
                "wait_ms": int(wait_sec * 1000),
                "final_position": str(self.position_shares),
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
        sell_price = Decimal("1") - self._tick_size
        sell_timeout_s = 600
        sell_backoff_base = 2.0
        sell_backoff_cap = 60.0

        import time as _time
        deadline = _time.monotonic() + sell_timeout_s
        attempt = 0

        while _time.monotonic() < deadline:
            if self.state == "closed":
                return

            attempt += 1
            result = await self._executor.place_order(
                token_id=self.token_id,
                side="SELL",
                price=str(sell_price),
                size=str(self.position_shares),
                check_balance=False,
            )

            if self._el:
                self._el.log_step("sell_placed", {
                    "order_id": result.order_id,
                    "price": str(sell_price),
                    "size": str(self.position_shares),
                    "reason": "tick_exit",
                    "attempt": attempt,
                }, phase="exit")

            if result.status == "filled":
                filled = Decimal(result.filled_size)
                self.position_shares -= filled
                self._exit_revenue += filled * sell_price
                if self._el:
                    self._el.log_step("sell_filled", {
                        "order_id": result.order_id,
                        "filled_size": str(filled),
                        "fill_price": str(sell_price),
                        "remaining_position": str(self.position_shares),
                    }, phase="exit")
                self._update_trade_summary({
                    "exit_price": str(sell_price),
                    "exit_shares": str(filled),
                    "exit_revenue": str(self._exit_revenue),
                    "exit_order_id": result.order_id,
                    "exited_at": datetime.now(timezone.utc).replace(tzinfo=None),
                })

            if result.status == "live":
                self.exit_order_id = result.order_id
                return

            if result.status == "filled" and self.position_shares <= 0:
                await self.risk.stop()
                self._close("tick_exit", phase="exit")
                return

            if result.status == "failed" and _time.monotonic() < deadline:
                backoff = min(sell_backoff_base * (2 ** (attempt - 1)), sell_backoff_cap)
                if self._el and attempt == 1:
                    self._el.log_step("sell_retry_start", {
                        "attempt": attempt,
                        "status": result.status,
                        "error": result.error,
                    }, phase="exit")
                await asyncio.sleep(backoff)

        await self.risk.stop()
        if self._el:
            self._el.log_step("sell_give_up", {
                "attempts": attempt,
                "timeout_sec": sell_timeout_s,
                "remaining_position": str(self.position_shares),
                "last_error": result.error if result else None,
            }, phase="exit")
        self._close("sell_failed", phase="exit")

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

        if self.entry_order_id:
            cancelled = await self._executor.cancel_order(self.entry_order_id)
            if self._el:
                self._el.log_step("risk_cancel_buy", {
                    "order_id": self.entry_order_id,
                    "success": cancelled,
                }, phase="exit_risk")
            self.entry_order_id = None

        if self.exit_order_id:
            cancelled = await self._executor.cancel_order(self.exit_order_id)
            if self._el:
                self._el.log_step("risk_cancel_sell", {
                    "order_id": self.exit_order_id,
                    "success": cancelled,
                }, phase="exit_risk")
            self.exit_order_id = None

        if self.position_shares > 0:
            import time as _time
            risk_deadline = _time.monotonic() + 600
            risk_attempt = 0
            while self.position_shares > 0 and _time.monotonic() < risk_deadline:
                risk_attempt += 1
                result = await self._executor.place_order(
                    token_id=self.token_id,
                    side="SELL",
                    price="0.01",
                    size=str(self.position_shares),
                    check_balance=False,
                )
                if self._el and (risk_attempt == 1 or result.status == "filled"):
                    self._el.log_step("risk_force_sell", {
                        "order_id": result.order_id,
                        "price": "0.01",
                        "size": str(self.position_shares),
                        "status": result.status,
                        "attempt": risk_attempt,
                    }, phase="exit_risk")
                if result.status == "filled":
                    filled = Decimal(result.filled_size)
                    self.position_shares -= filled
                    self._exit_revenue += filled * Decimal("0.01")
                elif result.status == "failed" and _time.monotonic() < risk_deadline:
                    backoff = min(2.0 * (2 ** (risk_attempt - 1)), 60.0)
                    await asyncio.sleep(backoff)
                else:
                    break

        self._close("stop_loss", phase="exit_risk")

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

        if self.entry_order_id:
            cancelled = await self._executor.cancel_order(self.entry_order_id)
            if self._el:
                self._el.log_step("buy_cancelled", {
                    "order_id": self.entry_order_id,
                    "success": cancelled,
                }, phase="exit_force")
            self.entry_order_id = None

        if self.exit_order_id:
            cancelled = await self._executor.cancel_order(self.exit_order_id)
            if self._el:
                self._el.log_step("sell_cancelled", {
                    "order_id": self.exit_order_id,
                    "success": cancelled,
                }, phase="exit_force")
            self.exit_order_id = None

        self._close("force_exit", phase="exit_force")

    # ==================== Stop ====================

    async def stop(self) -> None:
        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()
        await self.risk.stop()

    # ==================== Helpers ====================

    def _close(self, reason: str, phase: str = "exit") -> None:
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
            self._el.log_step("event_closed", {
                "reason": reason,
                "total_position": str(self.position_shares),
                "duration_ms": duration_ms,
            }, phase=phase)
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
