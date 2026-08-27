from __future__ import annotations

import asyncio
import logging

import aiohttp
logger = logging.getLogger(__name__)


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
