from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from weather_orderbook.gateway import PolymarketMarketClient
from weather_orderbook.types import MarketCandidate, WeatherAsset, WeatherCity

MAX_HIGH_CERTAINTY_TICK = 0.001
MIN_HIGH_CERTAINTY_ASK = 0.999
MIN_HIGH_CERTAINTY_BID_WITH_ASK = 0.995


class WeatherDiscovery:
    """HTTP-only discovery and candidate selection before real-time WS monitoring."""

    def __init__(self, market_client: PolymarketMarketClient):
        self._market_client = market_client

    async def plans_for_city(self, city: WeatherCity, target_date=None) -> dict[str, list[MarketCandidate]]:
        target_date = target_date or await self.local_date(city)
        tasks = [self._event_plan(city, direction, target_date) for direction in city.directions]
        plans = await asyncio.gather(*tasks)
        return dict(zip(city.directions, plans))

    async def _event_plan(self, city: WeatherCity, direction: str, target_date) -> list[MarketCandidate]:
        slug = self.event_slug(city, direction, target_date)
        event = await self._market_client.fetch_event(slug)
        if not event:
            return []
        candidates = [self._candidate(city.name, direction, slug, market) for market in event.get("markets", [])]
        candidates = [candidate for candidate in candidates if candidate]
        candidates.sort(key=lambda candidate: self._temperature(candidate.temperature_label), reverse=direction == "lowest")
        return candidates

    @staticmethod
    def event_slug(city: WeatherCity, direction: str, target_date) -> str:
        return (
            f"{direction}-temperature-in-{city.slug}-on-"
            f"{target_date.strftime('%B').lower()}-{target_date.day}-{target_date.year}"
        )

    async def select_candidate(self, candidates: list[MarketCandidate], start_index: int = 0) -> tuple[int, MarketCandidate] | None:
        """Skip NO markets already high-certainty using CLOB HTTP snapshots."""
        for index in range(start_index, len(candidates)):
            book = await self._market_client.fetch_orderbook(candidates[index].no_asset.asset_id)
            if book is None:
                raise RuntimeError("CLOB orderbook request failed during candidate selection")
            if not self._is_high_certainty(book):
                return index, candidates[index]
        return None

    async def local_date(self, city: WeatherCity):
        return datetime.now(UTC).astimezone(ZoneInfo(city.timezone)).date()

    async def timezone_name(self, city: WeatherCity) -> str:
        return city.timezone

    @staticmethod
    def _candidate(city: str, direction: str, event_slug: str, market: dict) -> MarketCandidate | None:
        if market.get("closed") or market.get("active") is False:
            return None
        outcomes = WeatherDiscovery._list(market.get("outcomes"))
        token_ids = WeatherDiscovery._list(market.get("clobTokenIds") or market.get("clob_token_ids"))
        pairs = {str(outcome).lower(): str(token_id) for outcome, token_id in zip(outcomes, token_ids)}
        if not pairs.get("yes") or not pairs.get("no"):
            return None
        common = dict(city=city, event_slug=event_slug, market_slug=market.get("slug", ""), temperature_label=market.get("groupItemTitle") or market.get("question", ""))
        return MarketCandidate(direction=direction, yes_asset=WeatherAsset(asset_id=pairs["yes"], outcome="yes", **common), no_asset=WeatherAsset(asset_id=pairs["no"], outcome="no", **common), **common)

    @staticmethod
    def _is_high_certainty(book: dict | None) -> bool:
        if not isinstance(book, dict):
            return False
        try:
            tick = float(book.get("tick_size") or book.get("min_tick_size"))
        except (TypeError, ValueError):
            return False
        asks = [item for item in book.get("asks", []) if float(item.get("size", 0)) > 0]
        bids = [item for item in book.get("bids", []) if float(item.get("size", 0)) > 0]
        best_ask = min((float(item["price"]) for item in asks), default=None)
        best_bid = max((float(item["price"]) for item in bids), default=None)
        return tick <= MAX_HIGH_CERTAINTY_TICK and (
            best_ask is None
            or (
                best_ask >= MIN_HIGH_CERTAINTY_ASK
                and best_bid is not None
                and best_bid >= MIN_HIGH_CERTAINTY_BID_WITH_ASK
            )
        )

    @staticmethod
    def _list(value) -> list:
        if isinstance(value, list): return value
        if isinstance(value, str):
            try: return json.loads(value)
            except json.JSONDecodeError: return []
        return []

    @staticmethod
    def _temperature(value: str) -> float:
        import re
        # Interval labels put the unit after the second endpoint, for example
        # ``88-89°F``.  Read the unit from anywhere in the label, then use
        # the first endpoint as the ordering boundary.  This also covers
        # labels such as ``83°F or below`` and ``102°F or higher``.
        number_match = re.search(r"-?\d+(?:\.\d+)?", value or "")
        unit_match = re.search(r"°?\s*([CF])\b", value or "", re.IGNORECASE)
        if not number_match or not unit_match:
            return float("inf")
        number = float(number_match.group())
        return number if unit_match.group(1).upper() == "C" else (number - 32) * 5 / 9
