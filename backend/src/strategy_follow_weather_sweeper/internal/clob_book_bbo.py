"""CLOB order-book BBO client for entry observations."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp


class ClobBookBboClient:
    """Fetch best bid/ask snapshots from the CLOB ``/book`` endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_sec: float = 3.0,
    ) -> None:
        self._base_url = (base_url or os.getenv(
            "CLOB_API_URL",
            "https://clob.polymarket.com",
        )).rstrip("/")
        self._timeout_sec = timeout_sec
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch_bbo(self, token_id: str, offset_origin_ms: int) -> dict[str, Any]:
        """Return a structured BBO snapshot; HTTP failures do not raise."""
        if self._session is None or self._session.closed:
            return self._error_snapshot(
                token_id,
                offset_origin_ms,
                "ClobBookBboClient is not started",
            )

        request_started_at_ms = int(time.time() * 1000)
        request_started_monotonic_ns = time.monotonic_ns()
        try:
            async with self._session.get(
                f"{self._base_url}/book",
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
            ) as response:
                response_at_ms = int(time.time() * 1000)
                latency_ms = (
                    time.monotonic_ns() - request_started_monotonic_ns
                ) / 1_000_000
                if response.status != 200:
                    return {
                        **self._error_snapshot(
                            token_id,
                            offset_origin_ms,
                            f"CLOB /book returned HTTP {response.status}",
                        ),
                        "request_started_at_ms": request_started_at_ms,
                        "response_at_ms": response_at_ms,
                        "latency_ms": round(latency_ms, 3),
                    }
                payload = await response.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response_at_ms = int(time.time() * 1000)
            latency_ms = (
                time.monotonic_ns() - request_started_monotonic_ns
            ) / 1_000_000
            return {
                **self._error_snapshot(
                    token_id,
                    offset_origin_ms,
                    f"CLOB /book request failed: {exc}",
                ),
                "request_started_at_ms": request_started_at_ms,
                "response_at_ms": response_at_ms,
                "latency_ms": round(latency_ms, 3),
            }

        bids = self._sorted_levels(payload.get("bids"), reverse=True)
        asks = self._sorted_levels(payload.get("asks"), reverse=False)
        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None
        response_at_ms = int(time.time() * 1000)
        latency_ms = (
            time.monotonic_ns() - request_started_monotonic_ns
        ) / 1_000_000

        return {
            "status": "ok",
            "source": "clob_book_api",
            "token_id": token_id,
            "utc": self._utc_str(response_at_ms),
            "request_started_at_ms": request_started_at_ms,
            "response_at_ms": response_at_ms,
            "captured_at_ms": response_at_ms,
            "offset_ms": response_at_ms - offset_origin_ms,
            "latency_ms": round(latency_ms, 3),
            "server_timestamp": payload.get("timestamp"),
            "book_hash": payload.get("hash"),
            "tick_size": payload.get("tick_size") or payload.get("min_tick_size"),
            "best_bid": best_bid["price"] if best_bid else None,
            "best_bid_size": best_bid["size"] if best_bid else None,
            "best_ask": best_ask["price"] if best_ask else None,
            "best_ask_size": best_ask["size"] if best_ask else None,
        }

    @staticmethod
    def _sorted_levels(levels: Any, *, reverse: bool) -> list[dict[str, Any]]:
        if not isinstance(levels, list):
            return []
        parsed: list[dict[str, Any]] = []
        for level in levels:
            if not isinstance(level, dict):
                continue
            try:
                price = float(level["price"])
                size = float(level.get("size", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                parsed.append({"price": price, "size": size})
        return sorted(parsed, key=lambda item: item["price"], reverse=reverse)

    @staticmethod
    def _utc_str(epoch_ms: int) -> str:
        now = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _error_snapshot(
        self,
        token_id: str,
        offset_origin_ms: int,
        message: str,
    ) -> dict[str, Any]:
        captured_at_ms = int(time.time() * 1000)
        return {
            "status": "error",
            "source": "clob_book_api",
            "token_id": token_id,
            "utc": self._utc_str(captured_at_ms),
            "captured_at_ms": captured_at_ms,
            "offset_ms": captured_at_ms - offset_origin_ms,
            "error": message,
        }
