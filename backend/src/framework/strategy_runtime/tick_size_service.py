"""Process-wide authoritative tick size lookup.

The service intentionally bypasses ClobClient's permanent market-parameter cache.
WS tick changes invalidate this cache, while a successful HTTP refresh provides
the authoritative value used by verification and order placement.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Dict, Optional, Tuple

import requests


CLOB_API_URL = "https://clob.polymarket.com"
DEFAULT_MAX_AGE_MS = 5_000
REQUEST_TIMEOUT_S = 5.0


class TickSizeFetchError(RuntimeError):
    """Raised when an authoritative tick size cannot be fetched."""


class TickSizeConsensusError(TickSizeFetchError):
    """Raised when the WS, tick API, and order-book sources disagree."""

    def __init__(
        self,
        message: str,
        *,
        ws_tick_size: Optional[Decimal],
        tick_api_size: Optional[Decimal],
        book_tick_size: Optional[Decimal],
    ) -> None:
        super().__init__(message)
        self.ws_tick_size = ws_tick_size
        self.tick_api_size = tick_api_size
        self.book_tick_size = book_tick_size


class TickSizeService:
    """Short-TTL HTTP cache for per-token minimum tick sizes."""

    def __init__(
        self,
        *,
        clob_api_url: str = CLOB_API_URL,
        default_max_age_ms: int = DEFAULT_MAX_AGE_MS,
    ) -> None:
        self._clob_api_url = clob_api_url.rstrip("/")
        self._default_max_age_ms = default_max_age_ms
        self._cache: Dict[str, Tuple[Decimal, float]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get(
        self,
        token_id: str,
        *,
        max_age_ms: Optional[int] = None,
    ) -> Decimal:
        """Return a cached tick when fresh enough, otherwise fetch HTTP."""
        max_age = self._default_max_age_ms if max_age_ms is None else max_age_ms
        cached = self._cache.get(token_id)
        if cached is not None and time.monotonic() - cached[1] <= max_age / 1000:
            return cached[0]
        return await self._refresh(token_id, force=False, max_age_ms=max_age)

    async def refresh(self, token_id: str) -> Decimal:
        """Force an HTTP refresh and return the authoritative tick size."""
        return await self._refresh(token_id, force=True)

    def invalidate(self, token_id: str) -> None:
        """Drop the cached value, typically after a WS tick-size change."""
        self._cache.pop(token_id, None)

    async def refresh_book_tick_size(self, token_id: str) -> Decimal:
        """Force-fetch the tick size exposed by CLOB /book."""
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return await asyncio.to_thread(
                    self._fetch_book_tick_size_sync, token_id
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))

        raise TickSizeFetchError(
            f"Failed to fetch /book tick size for {token_id[:8]}: {last_error}"
        ) from last_error

    async def refresh_consensus(
        self,
        token_id: str,
        ws_tick_size: Optional[Decimal],
    ) -> Decimal:
        """Refresh WS, /tick-size, and /book and return their agreed value."""
        tick_api_size, book_tick_size = await asyncio.gather(
            self.refresh(token_id),
            self.refresh_book_tick_size(token_id),
        )
        values = (ws_tick_size, tick_api_size, book_tick_size)
        if any(value is None for value in values) or len(set(values)) != 1:
            raise TickSizeConsensusError(
                "Tick sources disagree",
                ws_tick_size=ws_tick_size,
                tick_api_size=tick_api_size,
                book_tick_size=book_tick_size,
            )
        return tick_api_size

    async def _refresh(
        self,
        token_id: str,
        *,
        force: bool,
        max_age_ms: Optional[int] = None,
    ) -> Decimal:
        lock = self._locks.get(token_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[token_id] = lock

        async with lock:
            # A concurrent caller may have refreshed this token while this
            # coroutine waited for the lock. Only force-refresh bypasses that.
            if not force:
                cached = self._cache.get(token_id)
                if cached is not None:
                    age_s = time.monotonic() - cached[1]
                    if max_age_ms is None or age_s <= max_age_ms / 1000:
                        return cached[0]

            tick_size = await self._fetch_tick_size(token_id)
            self._cache[token_id] = (tick_size, time.monotonic())
            return tick_size

    async def _fetch_tick_size(self, token_id: str) -> Decimal:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._fetch_tick_size_sync, token_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))

        raise TickSizeFetchError(
            f"Failed to fetch tick size for {token_id[:8]}: {last_error}"
        ) from last_error

    def _fetch_tick_size_sync(self, token_id: str) -> Decimal:
        response = requests.get(
            f"{self._clob_api_url}/tick-size",
            params={"token_id": token_id},
            timeout=REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
        raw_tick_size = payload.get("minimum_tick_size") if isinstance(payload, dict) else None
        if raw_tick_size is None:
            raise ValueError("tick-size response missing minimum_tick_size")

        tick_size = Decimal(str(raw_tick_size))
        if tick_size <= 0:
            raise ValueError(f"invalid tick size: {tick_size}")
        return tick_size

    def _fetch_book_tick_size_sync(self, token_id: str) -> Decimal:
        response = requests.get(
            f"{self._clob_api_url}/book",
            params={"token_id": token_id},
            timeout=REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
        raw_tick_size = payload.get("tick_size") if isinstance(payload, dict) else None
        if raw_tick_size is None:
            raise ValueError("/book response missing tick_size")

        tick_size = Decimal(str(raw_tick_size))
        if tick_size <= 0:
            raise ValueError(f"invalid /book tick size: {tick_size}")
        return tick_size
