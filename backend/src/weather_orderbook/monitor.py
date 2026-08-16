from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from time import time
from typing import Literal

from weather_orderbook.types import MarketCandidate
from weather_orderbook.types import WeatherEvent
from weather_orderbook.orderbook import LocalOrderBook

logger = logging.getLogger(__name__)

CONFIRM_SECONDS = int(os.environ.get("HIGH_CERTAINTY_CONFIRM_SECONDS", "60"))
MAX_HIGH_CERTAINTY_TICK = 0.001
MIN_HIGH_CERTAINTY_ASK = 0.999
MIN_HIGH_CERTAINTY_BID_WITH_ASK = 0.995
SWEEP_ASK_THRESHOLDS = (0.99, 0.98)
SWEEP_TICK_SIZE = 0.01
EventHandler = Callable[[WeatherEvent], Awaitable[None]]
BookUpdateHandler = Callable[[dict], None]


class WeatherOrderBookMonitor:
    """Evaluation engine for one candidate market (YES + NO).

    Does NOT own a WebSocket connection. Receives raw messages via deliver()
    from a SharedMarketWebSocket instance.
    """

    def __init__(
        self,
        candidate: MarketCandidate,
        on_event: EventHandler,
        on_book_update: BookUpdateHandler | None = None,
        mode: Literal["full", "sweep_only"] = "full",
    ):
        self.candidate = candidate
        self.mode = mode
        self._on_event = on_event
        self._on_book_update = on_book_update
        self._assets = {candidate.yes_asset.asset_id: candidate.yes_asset, candidate.no_asset.asset_id: candidate.no_asset}
        self._books: dict[str, LocalOrderBook] = {asset_id: LocalOrderBook() for asset_id in self._assets}
        self._last: dict[str, dict] = {}
        self._ticks: dict[str, float | None] = {asset_id: None for asset_id in self._assets}
        self._confirmations: dict[str, asyncio.Task] = {}
        self.running = False

    def favored_outcome(self) -> str | None:
        """Return 'yes' or 'no' based on which token has higher mid-price."""
        prices: dict[str, float] = {}
        for asset_id, book in self._books.items():
            price = self._reference_price(book.top_of_book())
            if price is not None:
                prices[asset_id] = price
        if len(prices) != 2:
            return None
        max_asset = max(prices, key=prices.get)
        return self._assets[max_asset].outcome

    def subscription_token_id(self) -> str:
        """The single token ID to subscribe on the wire (NO token)."""
        return self.candidate.no_asset.asset_id

    def registered_asset_ids(self) -> list[str]:
        """All asset IDs this monitor wants routed to it (YES + NO)."""
        return list(self._assets.keys())

    def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False
        confirmations = tuple(self._confirmations.values())
        for task in confirmations:
            task.cancel()
        if confirmations:
            await asyncio.gather(*confirmations, return_exceptions=True)
        self._confirmations.clear()

    def status(self) -> dict:
        return {
            "city": self.candidate.city,
            "direction": self.candidate.direction,
            "temperature_label": self.candidate.temperature_label,
            "market_slug": self.candidate.market_slug,
            "mode": self.mode,
            "running": self.running,
        }

    def live_payload(self) -> dict:
        """Complete local L2 books for the dashboard."""
        if not self._books:
            return {"city": self.candidate.city, "direction": self.candidate.direction,
                    "market_slug": self.candidate.market_slug, "books": {}}
        observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"
        books: dict[str, dict] = {}
        for asset_id, asset in self._assets.items():
            book = self._books.get(asset_id)
            if book is None:
                continue
            bids = [{"price": price, "size": size} for price, size in sorted(book.bids.items(), reverse=True)]
            asks = [{"price": price, "size": size} for price, size in sorted(book.asks.items())]
            books[asset.outcome] = {
                "token_id": asset_id,
                "observed_at": observed_at,
                "tick_size": self._ticks.get(asset_id),
                "best_bid": bids[0] if bids else None,
                "best_ask": asks[0] if asks else None,
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "bids": bids,
                "asks": asks,
            }
        return {
            "city": self.candidate.city,
            "direction": self.candidate.direction,
            "market_slug": self.candidate.market_slug,
            "mode": self.mode,
            "books": books,
        }

    def deliver(self, raw: str) -> None:
        """Entry point: receive a raw WS message from SharedMarketWebSocket."""
        if not self.running:
            return
        data = json.loads(raw)
        if isinstance(data, list):
            for snapshot in data:
                asset_id = snapshot.get("asset_id")
                if asset_id in self._assets:
                    self._books[asset_id].apply_snapshot(snapshot.get("bids", []), snapshot.get("asks", []))
                    self._set_tick(asset_id, snapshot.get("tick_size") or snapshot.get("min_tick_size"))
                    self._evaluate(asset_id)
            self._publish_book_update()
            return
        if data.get("event_type") == "book":
            asset_id = data.get("asset_id")
            if asset_id in self._assets:
                previous_books = {
                    item_id: book.top_of_book() for item_id, book in self._books.items()
                }
                previous_asks = self._books[asset_id].asks.copy()
                previous_tick = self._ticks[asset_id]
                self._books[asset_id].apply_snapshot(data.get("bids", []), data.get("asks", []))
                self._set_tick(asset_id, data.get("tick_size") or data.get("min_tick_size"))
                self._evaluate(
                    asset_id,
                    previous_asks,
                    self._higher_probability_asset(previous_books),
                    previous_tick,
                )
                self._publish_book_update()
            return
        if data.get("event_type") == "tick_size_change":
            asset_id = data.get("asset_id")
            if asset_id in self._assets:
                self._set_tick(asset_id, data.get("new_tick_size"))
                self._publish_book_update()
            return
        if data.get("event_type") == "price_change":
            previous_books = {
                asset_id: book.top_of_book() for asset_id, book in self._books.items()
            }
            previous_asks: dict[str, dict[float, float]] = {}
            previous_ticks: dict[str, float | None] = {}
            affected_assets: set[str] = set()
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                if asset_id in self._assets:
                    if asset_id not in previous_asks:
                        previous_asks[asset_id] = self._books[asset_id].asks.copy()
                        previous_ticks[asset_id] = self._ticks[asset_id]
                    self._books[asset_id].apply_change(change["price"], change["size"], change["side"])
                    affected_assets.add(asset_id)
            high_probability_asset = self._higher_probability_asset(previous_books)
            for asset_id in affected_assets:
                self._evaluate(asset_id, previous_asks[asset_id], high_probability_asset, previous_ticks[asset_id])
            if affected_assets:
                self._publish_book_update()

    def _evaluate(
        self,
        asset_id: str,
        previous_asks: dict[float, float] | None = None,
        high_probability_asset: str | None = None,
        previous_tick: float | None = None,
    ) -> None:
        book = self._books[asset_id]
        previous, current = self._last.get(asset_id), {
            **book.top_of_book(),
            "observed_at": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + " UTC",
            "observed_at_unix_ms": int(time() * 1000),
        }
        if (
            previous_asks is not None
            and asset_id == high_probability_asset
            and previous_tick == SWEEP_TICK_SIZE
            and self._ticks[asset_id] == SWEEP_TICK_SIZE
        ):
            for threshold in SWEEP_ASK_THRESHOLDS:
                before_exists = any(price <= threshold and size > 0 for price, size in previous_asks.items())
                after_exists = any(price <= threshold and size > 0 for price, size in book.asks.items())
                if before_exists and not after_exists:
                    asset = self._assets[asset_id]
                    logger.info(
                        "[Monitor] SWEEP detected: %s %s %s outcome=%s threshold=%.2f tick=%s",
                        asset.city, asset.temperature_label, asset_id[:8], asset.outcome, threshold, self._ticks[asset_id],
                    )
                    self._publish("sweep", asset_id, previous, current, f"ask_levels_through_{threshold:.2f}_cleared")
                    break
        self._last[asset_id] = current
        # High-certainty confirmation only in full mode
        if self.mode == "full":
            if self._high(asset_id):
                if asset_id not in self._confirmations:
                    asset = self._assets[asset_id]
                    logger.info(
                        "[Monitor] High-certainty timer started: %s %s %s outcome=%s (%ds)",
                        asset.city, asset.temperature_label, asset_id[:8], asset.outcome, CONFIRM_SECONDS,
                    )
                    self._confirmations[asset_id] = asyncio.create_task(self._confirm(asset_id, previous))
            elif task := self._confirmations.pop(asset_id, None):
                asset = self._assets[asset_id]
                logger.info("[Monitor] High-certainty cancelled: %s %s %s", asset.city, asset.temperature_label, asset_id[:8])
                task.cancel()

    def check_high_certainty(self) -> None:
        """Re-evaluate high-certainty state for all assets after mode promotion."""
        for asset_id in self._assets:
            if self._high(asset_id) and asset_id not in self._confirmations:
                previous = self._last.get(asset_id)
                self._confirmations[asset_id] = asyncio.create_task(self._confirm(asset_id, previous))

    def _high(self, asset_id: str) -> bool:
        book = self._books.get(asset_id)
        if book is None:
            return False
        if self._ticks.get(asset_id) != MAX_HIGH_CERTAINTY_TICK:
            return False
        best_ask = min(book.asks, default=None)
        best_bid = max(book.bids, default=None)
        return (
            best_ask is None
            or (
                best_ask >= MIN_HIGH_CERTAINTY_ASK
                and best_bid is not None
                and best_bid >= MIN_HIGH_CERTAINTY_BID_WITH_ASK
            )
        )

    @staticmethod
    def _reference_price(book: dict) -> float | None:
        ask = book.get("best_ask")
        bid = book.get("best_bid")
        ask_price = float(ask["price"]) if ask is not None else None
        bid_price = float(bid["price"]) if bid is not None else None
        if ask_price is not None and bid_price is not None:
            return (ask_price + bid_price) / 2
        return ask_price if ask_price is not None else bid_price

    def _higher_probability_asset(self, books: dict[str, dict]) -> str | None:
        prices = {asset_id: self._reference_price(book) for asset_id, book in books.items()}
        available = [(price, asset_id) for asset_id, price in prices.items() if price is not None]
        if len(available) != 2:
            return None
        first, second = available
        if first[0] == second[0]:
            return None
        return max(available)[1]

    async def _confirm(self, asset_id: str, before: dict | None) -> None:
        try:
            await asyncio.sleep(CONFIRM_SECONDS)
            after = self._last.get(asset_id)
            if after and self._high(asset_id):
                event_type = "market_resolved" if self._assets[asset_id].outcome == "yes" else "no_longer_possible"
                asset = self._assets[asset_id]
                logger.info(
                    "[Monitor] High-certainty confirmed → %s: %s %s %s outcome=%s",
                    event_type, asset.city, asset.temperature_label, asset_id[:8], asset.outcome,
                )
                self._publish(event_type, asset_id, before, after, f"high_certainty_maintained_{CONFIRM_SECONDS}s")
        except asyncio.CancelledError:
            raise
        finally:
            self._confirmations.pop(asset_id, None)

    def _publish(self, event_type: str, asset_id: str, before: dict | None, after: dict, reason: str) -> None:
        event = WeatherEvent(event_type=event_type, asset=self._assets[asset_id], previous_orderbook=before, current_orderbook=after, reason=reason)
        asyncio.create_task(self._on_event(event))

    def _publish_book_update(self) -> None:
        if self._on_book_update:
            self._on_book_update(self.live_payload())

    def _set_tick(self, asset_id: str | None, value: object) -> None:
        if asset_id in self._ticks:
            try:
                self._ticks[asset_id] = float(value)
            except (TypeError, ValueError):
                pass
