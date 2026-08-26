"""BalancePoller — 后台轮询 Polymarket API 缓存账户余额。

设计原则：
  - 每 1 秒轮询一次 CLOB API（balance_allowance + open_orders）
  - 策略读取缓存值（instant, 零延迟）
  - 下单失败时调用 refresh() 立即刷新
  - 同一个 proxy_wallet 全局共享一个 poller 实例（多策略共用）
  - 真实可用 = USDC 余额 - 所有挂单 BUY 锁定金额
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Dict, Optional

import requests

from framework.trading.provider import get_client
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

logger = logging.getLogger(__name__)

DATA_API_URL = "https://data-api.polymarket.com"
POSITION_POLL_INTERVAL = 5.0
DEFAULT_POLL_INTERVAL = 1.0
SAFETY_BUFFER = Decimal("2")


class BalancePoller:
    """单个 proxy_wallet 的余额轮询缓存。

    属性（instant read，无 API 调用）：
      available_cash — 可用于新 BUY 订单的 USDC
      positions — token_id → share 数量
      open_orders — 当前所有挂单快照
    """

    def __init__(
        self,
        proxy_wallet: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        safety_buffer: Decimal = SAFETY_BUFFER,
    ) -> None:
        self._proxy_wallet = proxy_wallet.lower()
        self._poll_interval = poll_interval
        self._safety_buffer = safety_buffer

        self._collateral_balance: Decimal = Decimal("0")
        self._open_buy_locked: Decimal = Decimal("0")
        self._open_orders: list = []
        self._positions: Dict[str, Decimal] = {}
        self._last_balance_poll: float = 0
        self._last_position_poll: float = 0

        self._poll_task: Optional[asyncio.Task] = None
        self._position_task: Optional[asyncio.Task] = None
        self._running = False
        self._ready = asyncio.Event()

    @property
    def available_cash(self) -> Decimal:
        """可用于新买单的 USDC（instant read）。"""
        return max(
            Decimal("0"),
            self._collateral_balance - self._open_buy_locked - self._safety_buffer,
        )

    @property
    def collateral_balance(self) -> Decimal:
        """总 USDC 余额（含挂单锁定）。"""
        return self._collateral_balance

    @property
    def open_buy_locked(self) -> Decimal:
        """当前所有 BUY 挂单锁定的 USDC 总额。"""
        return self._open_buy_locked

    @property
    def open_orders(self) -> list:
        return self._open_orders

    def position_size(self, token_id: str) -> Decimal:
        """某个 token 的持仓数量。"""
        return self._positions.get(token_id, Decimal("0"))

    @property
    def positions(self) -> Dict[str, Decimal]:
        return dict(self._positions)

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        """等待首次轮询完成。"""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("[BalancePoller] %s: first poll timed out", self._proxy_wallet[:8])
            return False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(
            self._balance_poll_loop(), name=f"balance_poll:{self._proxy_wallet[:8]}"
        )
        self._position_task = asyncio.create_task(
            self._position_poll_loop(), name=f"position_poll:{self._proxy_wallet[:8]}"
        )
        logger.info("[BalancePoller] started for %s", self._proxy_wallet[:8])

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._position_task:
            self._position_task.cancel()
        await asyncio.gather(
            self._poll_task or asyncio.sleep(0),
            self._position_task or asyncio.sleep(0),
            return_exceptions=True,
        )

    async def refresh(self) -> None:
        """下单失败后立即刷新余额（跳过等待周期）。"""
        await self._poll_balance_and_orders()

    async def _balance_poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_balance_and_orders()
                if not self._ready.is_set():
                    self._ready.set()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[BalancePoller] %s: poll error", self._proxy_wallet[:8])
            await asyncio.sleep(self._poll_interval)

    async def _position_poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_positions()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[BalancePoller] %s: position poll error", self._proxy_wallet[:8])
            await asyncio.sleep(POSITION_POLL_INTERVAL)

    async def _poll_balance_and_orders(self) -> None:
        """轮询 CLOB: balance_allowance + open_orders。"""
        try:
            client = get_client(self._proxy_wallet)
        except RuntimeError:
            logger.warning("[BalancePoller] no client for %s", self._proxy_wallet[:8])
            return

        balance_raw, orders_raw = await asyncio.gather(
            asyncio.to_thread(self._fetch_balance, client),
            asyncio.to_thread(self._fetch_open_orders, client),
        )

        if balance_raw is not None:
            self._collateral_balance = Decimal(str(balance_raw))

        if orders_raw is not None:
            self._open_orders = orders_raw
            locked = Decimal("0")
            for order in orders_raw:
                if order.get("side") == "BUY" or order.get("buy") is True:
                    price = Decimal(str(order.get("price", "0")))
                    resting = Decimal(str(order.get("original_size", order.get("size", "0"))))
                    matched = Decimal(str(order.get("size_matched", "0")))
                    remaining = resting - matched
                    if remaining > 0:
                        locked += price * remaining
            self._open_buy_locked = locked

        self._last_balance_poll = time.monotonic()
        logger.debug(
            "[BalancePoller] %s: balance=%s locked=%s available=%s",
            self._proxy_wallet[:8],
            self._collateral_balance,
            self._open_buy_locked,
            self.available_cash,
        )

    def _fetch_balance(self, client: Any) -> Optional[str]:
        try:
            result = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if isinstance(result, dict):
                return result.get("allowance") or result.get("balance") or "0"
            return str(result) if result else None
        except Exception as e:
            logger.warning("[BalancePoller] balance fetch error: %s", e)
            return None

    def _fetch_open_orders(self, client: Any) -> Optional[list]:
        try:
            return client.get_open_orders()
        except Exception as e:
            logger.warning("[BalancePoller] open_orders fetch error: %s", e)
            return None

    async def _poll_positions(self) -> None:
        """轮询 Data API 获取持仓。"""
        positions = await asyncio.to_thread(self._fetch_positions)
        if positions is not None:
            new_pos: Dict[str, Decimal] = {}
            for p in positions:
                token_id = p.get("asset", "")
                size = Decimal(str(p.get("size", 0)))
                if token_id and size > 0:
                    new_pos[token_id] = size
            self._positions = new_pos
            self._last_position_poll = time.monotonic()

    def _fetch_positions(self) -> Optional[list]:
        try:
            resp = requests.get(
                f"{DATA_API_URL}/positions",
                params={"user": self._proxy_wallet, "sizeThreshold": "0.1"},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("[BalancePoller] positions API %d", resp.status_code)
            return None
        except Exception as e:
            logger.warning("[BalancePoller] positions fetch error: %s", e)
            return None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "proxy_wallet": self._proxy_wallet[:8],
            "collateral_balance": str(self._collateral_balance),
            "open_buy_locked": str(self._open_buy_locked),
            "available_cash": str(self.available_cash),
            "open_orders_count": len(self._open_orders),
            "positions_count": len(self._positions),
            "last_balance_poll_ago": f"{time.monotonic() - self._last_balance_poll:.1f}s",
        }


# ============================================================
# 全局 poller 注册表 — 同一 proxy_wallet 共享实例
# ============================================================

_pollers: Dict[str, BalancePoller] = {}
_pollers_lock = asyncio.Lock()


async def get_or_create_poller(
    proxy_wallet: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    auto_start: bool = True,
) -> BalancePoller:
    """获取或创建指定账户的 BalancePoller（全局单例）。"""
    addr = proxy_wallet.lower()
    async with _pollers_lock:
        if addr not in _pollers:
            poller = BalancePoller(addr, poll_interval=poll_interval)
            _pollers[addr] = poller
            if auto_start:
                await poller.start()
        return _pollers[addr]


async def stop_all_pollers() -> None:
    """关闭所有 poller（shutdown 时调用）。"""
    async with _pollers_lock:
        for poller in _pollers.values():
            await poller.stop()
        _pollers.clear()
