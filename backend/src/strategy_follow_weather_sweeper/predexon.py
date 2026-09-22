"""Predexon Trades WebSocket — 监听 leader 钱包链上活动。

移植自 WeatherTaker copy_trading/predexon.py，改为回调模式：
  on_signal(payload) 由外部传入，不再引用具体 service。
"""
import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable, Optional

import websockets

from framework.strategy_runtime.interfaces import Signal
from strategy_follow_weather_sweeper.dao import FollowWeatherSweeperSignalDAO

logger = logging.getLogger(__name__)

_signal_write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="signal-dao")

PREDEXON_WS_URL = "wss://wss.predexon.com/"
PREDEXON_API_KEY = os.getenv("PREDEXON_API_KEY", "")


class PredexonAdapter:
    """将 Predexon order_filled 事件转为 framework Signal。"""

    def adapt(self, event: dict[str, Any]) -> Optional[Signal]:
        side = event.get("side", "").upper()
        outcome = event.get("outcome", "").lower()
        if side != "BUY":
            return None

        token_id = event.get("token_id", "")
        tx_hash = event.get("tx_hash", "")
        if not token_id:
            return None

        return Signal(
            signal_id=f"predexon:{tx_hash}:{token_id}",
            signal_type="sweep",
            token_id=token_id,
            market_slug=event.get("market_slug", ""),
            occurred_at_ms=int(time.time() * 1000),
            source="predexon",
            payload={
                "outcome": outcome,
                "leader_wallet": event.get("user", "").lower(),
                "leader_price": event.get("price", 0),
                "leader_size": event.get("shares_normalized", 0),
                "tx_hash": tx_hash,
                "role": event.get("role", ""),
                "condition_id": event.get("condition_id", ""),
                "title": event.get("title", ""),
                "token_label": event.get("token_label", ""),
                "is_neg_risk": event.get("is_neg_risk", False),
                "orderbook_snapshot": {},
            },
        )


class PredexonClient:
    """Predexon Trades WS — 监听 leader 钱包链上活动（回调模式）。"""

    def __init__(self) -> None:
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self._running = False
        self._subscription_id: Optional[str] = None
        self._signal_dao = FollowWeatherSweeperSignalDAO()
        self._leader_addresses: set[str] = set()

    async def start(
        self,
        on_signal: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if not PREDEXON_API_KEY:
            logger.warning("[Predexon] PREDEXON_API_KEY not set, skipping")
            return
        self._on_signal = on_signal
        logger.info("[Predexon] Starting Predexon WS connection...")
        await self._connect_loop()

    def stop(self) -> None:
        logger.info("[Predexon] Stopping Predexon WS connection...")
        self._running = False

    def add_leader(self, address: str) -> None:
        addr = address.lower()
        if addr in self._leader_addresses:
            return
        self._leader_addresses.add(addr)
        if self.connected and self._subscription_id:
            task = asyncio.create_task(self._update_subscription())
            task.add_done_callback(self._on_subscription_update_done)

    def remove_leader(self, address: str) -> None:
        addr = address.lower()
        self._leader_addresses.discard(addr)
        if self.connected and self._subscription_id:
            task = asyncio.create_task(self._update_subscription())
            task.add_done_callback(self._on_subscription_update_done)

    def sync_leaders(self, addresses: set[str]) -> None:
        normalized = {a.lower() for a in addresses}
        if normalized == self._leader_addresses:
            return
        self._leader_addresses = normalized
        if self.connected and self._subscription_id:
            task = asyncio.create_task(self._update_subscription())
            task.add_done_callback(self._on_subscription_update_done)

    def _on_subscription_update_done(self, task: asyncio.Task) -> None:
        if task.exception():
            logger.error("[Predexon] Failed to update subscription: %s", task.exception())

    async def _connect_loop(self) -> None:
        self._running = True
        delay = 1
        while self._running:
            try:
                ws_url = f"{PREDEXON_WS_URL}?api_key={PREDEXON_API_KEY}"
                async with websockets.connect(
                    ws_url, ping_interval=10, open_timeout=15,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    delay = 1
                    logger.info("[Predexon] Connected to Predexon Trades WS")

                    await self._subscribe()

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except asyncio.CancelledError:
                logger.info("[Predexon] Cancelled, exiting connect loop")
                break
            except Exception as e:
                logger.error("[Predexon] Connection error: %s", e)
                self.connected = False
                self.ws = None
                self._subscription_id = None

            if self._running:
                logger.info("[Predexon] Reconnecting in %ds (exp backoff)...", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

        self._running = False
        self.connected = False
        self.ws = None
        logger.info("[Predexon] Connection loop ended")

    async def _subscribe(self) -> None:
        subscription = {
            "action": "subscribe",
            "platform": "polymarket",
            "version": 1,
            "type": "orders",
            "filters": {
                "users": list(self._leader_addresses),
                "status": "pending",
            },
        }
        await self.ws.send(json.dumps(subscription))
        logger.info(
            "[Predexon] Subscription sent for %d leaders",
            len(self._leader_addresses),
        )

    async def _update_subscription(self) -> None:
        if not self._subscription_id:
            return
        update_msg = {
            "action": "update",
            "subscription_id": self._subscription_id,
            "filters": {
                "users": list(self._leader_addresses),
                "status": "pending",
            },
        }
        await self.ws.send(json.dumps(update_msg))
        logger.info(
            "[Predexon] Updated subscription %s with %d leaders",
            self._subscription_id,
            len(self._leader_addresses),
        )

    async def _handle_message(self, message: str) -> None:
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "event":
                event = data["data"]
                event_type = event.get("event_type")
                if event_type != "order_filled":
                    return
                self._persist_signal(event)
                await self._on_signal(event)
                return

            if msg_type == "connected":
                return
            if msg_type == "ack":
                self._subscription_id = data.get("subscription_id")
                logger.info(
                    "[Predexon] Subscribed: subscription_id=%s",
                    self._subscription_id,
                )
                return

        except json.JSONDecodeError:
            logger.warning("[Predexon] Invalid JSON: %s", message[:100])
        except Exception as e:
            logger.error("[Predexon] Error handling message: %s", e)

    def _persist_signal(self, event: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(_signal_write_pool, self._signal_dao.insert, event)
        except Exception:
            logger.debug("[Predexon] Failed to persist signal", exc_info=True)
