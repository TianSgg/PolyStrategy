from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

import aiohttp
import websockets

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


# ==================== HTTP REST ====================


class PolymarketMarketClient:
    """Shared REST access. Discovery details stay isolated from listener logic."""

    gamma_url = "https://gamma-api.polymarket.com"
    clob_url = "https://clob.polymarket.com"

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def fetch_event(self, slug: str) -> dict | None:
        payload = await self._get_json(f"{self.gamma_url}/events", {"slug": slug})
        return payload[0] if isinstance(payload, list) and payload else None

    async def fetch_orderbook(self, token_id: str) -> dict | None:
        payload = await self._get_json(f"{self.clob_url}/book", {"token_id": token_id})
        return payload if isinstance(payload, dict) else None

    async def _get_json(self, url: str, params: dict) -> object | None:
        """A temporary API failure must not abort all city monitor startup."""
        for attempt in range(3):
            try:
                async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.warning("HTTP %s from %s", response.status, url)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("HTTP request failed for %s: %s", url, exc)
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
        return None


# ==================== WebSocket ====================


class MonitorProtocol(Protocol):
    def deliver(self, raw: str) -> None: ...


class SharedMarketWebSocket:
    """Single CLOB Market Channel connection shared by all weather monitors."""

    def __init__(self, on_reconnect: Callable[[], Awaitable[None]] | None = None) -> None:
        self._subscribed_tokens: set[str] = set()
        self._routing: dict[str, list[MonitorProtocol]] = {}
        self._pending_unsub: set[str] = set()
        self._on_reconnect = on_reconnect
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.connected = False
        self.reconnects = 0
        self.last_message_at: str | None = None
        self._ws: ClientConnection | None = None

    async def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run(), name="shared-market-ws")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._subscribed_tokens.clear()
        self._routing.clear()
        self.connected = False

    async def subscribe(self, token_id: str, monitor: MonitorProtocol, asset_ids: list[str]) -> None:
        """Subscribe one token on the wire; register asset_ids in the routing table."""
        async with self._lock:
            for asset_id in asset_ids:
                monitors = self._routing.setdefault(asset_id, [])
                if monitor not in monitors:
                    monitors.append(monitor)

            if token_id not in self._subscribed_tokens:
                self._subscribed_tokens.add(token_id)
                if self._ws and self.connected:
                    await self._ws.send(json.dumps({
                        "assets_ids": [token_id],
                        "type": "market",
                        "operation": "subscribe",
                        "level": 2,
                        "initial_dump": True,
                    }))

    async def unsubscribe(self, token_id: str, monitor: MonitorProtocol, asset_ids: list[str]) -> None:
        """Remove token from wire subscription; clean routing table entries."""
        async with self._lock:
            for asset_id in asset_ids:
                monitors = self._routing.get(asset_id)
                if monitors:
                    try:
                        monitors.remove(monitor)
                    except ValueError:
                        pass
                    if not monitors:
                        del self._routing[asset_id]

            if token_id in self._subscribed_tokens:
                still_needed = bool(self._routing.get(token_id))
                if not still_needed:
                    self._subscribed_tokens.discard(token_id)
                    if self._ws and self.connected:
                        await self._ws.send(json.dumps({
                            "assets_ids": [token_id],
                            "type": "market",
                            "operation": "unsubscribe",
                        }))

    async def subscribe_for_initial_dump(self, token_id: str) -> None:
        """Temporarily subscribe a token just to get its initial dump, then auto-unsubscribe."""
        async with self._lock:
            self._pending_unsub.add(token_id)
            if self._ws and self.connected:
                await self._ws.send(json.dumps({
                    "assets_ids": [token_id],
                    "type": "market",
                    "operation": "subscribe",
                    "level": 2,
                    "initial_dump": True,
                }))

    async def _unsubscribe_wire(self, token_id: str) -> None:
        """Send unsubscribe frame without touching the routing table."""
        if self._ws and self.connected:
            await self._ws.send(json.dumps({
                "assets_ids": [token_id],
                "operation": "unsubscribe",
            }))

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "last_message_at": self.last_message_at,
            "subscribed_tokens": len(self._subscribed_tokens),
            "routed_assets": len(self._routing),
        }

    async def _run(self) -> None:
        delay = 1
        while True:
            try:
                async with websockets.connect(
                    MARKET_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    delay = 1
                    logger.info(
                        "SharedMarketWebSocket connected, re-subscribing %d tokens",
                        len(self._subscribed_tokens),
                    )
                    if self._subscribed_tokens:
                        await ws.send(json.dumps({
                            "assets_ids": list(self._subscribed_tokens),
                            "type": "market",
                            "operation": "subscribe",
                            "level": 2,
                            "initial_dump": True,
                        }))
                    if self._on_reconnect:
                        asyncio.create_task(self._on_reconnect())
                    async for raw in ws:
                        self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.reconnects += 1
                logger.warning(
                    "SharedMarketWebSocket disconnected, reconnecting in %ds (reconnects=%d)",
                    delay, self.reconnects,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            finally:
                self._ws = None
                self.connected = False

    def _dispatch(self, raw: str) -> None:
        from datetime import UTC, datetime

        self.last_message_at = datetime.now(UTC).isoformat()
        data = json.loads(raw)

        if isinstance(data, list):
            for snapshot in data:
                asset_id = snapshot.get("asset_id")
                if asset_id:
                    self._deliver_to(asset_id, raw)
                    if asset_id in self._pending_unsub:
                        self._pending_unsub.discard(asset_id)
                        asyncio.create_task(self._unsubscribe_wire(asset_id))
            return

        event_type = data.get("event_type")

        if event_type == "price_change":
            delivered: set[int] = set()
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                if asset_id:
                    for monitor in self._routing.get(asset_id, []):
                        mid = id(monitor)
                        if mid not in delivered:
                            delivered.add(mid)
                            monitor.deliver(raw)
            return

        if event_type in ("book", "tick_size_change"):
            asset_id = data.get("asset_id")
            if asset_id:
                self._deliver_to(asset_id, raw)
            return

        if event_type == "last_trade_price":
            asset_id = data.get("asset_id")
            if asset_id:
                self._deliver_to(asset_id, raw)

    def _deliver_to(self, asset_id: str, raw: str) -> None:
        for monitor in self._routing.get(asset_id, []):
            monitor.deliver(raw)
