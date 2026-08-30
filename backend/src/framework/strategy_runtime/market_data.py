"""MarketData — Polymarket 市场数据查询（价格、tick_size、neg_risk）。

无状态查询接口，策略按需调用。可继承重写以支持不同市场。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
from py_clob_client_v2 import ClobClient

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"


class MarketData:
    """Polymarket 市场数据查询。"""

    def __init__(self) -> None:
        self._clob_client = ClobClient(host=CLOB_API_URL, chain_id=137)
        self._tick_size_cache: Dict[str, str] = {}
        self._neg_risk_cache: Dict[str, bool] = {}

    async def get_tick_size(self, token_id: str) -> Optional[str]:
        if token_id in self._tick_size_cache:
            return self._tick_size_cache[token_id]
        for attempt in range(3):
            try:
                result = await asyncio.to_thread(
                    self._clob_client.get_tick_size, token_id
                )
                ts = str(result)
                self._tick_size_cache[token_id] = ts
                return ts
            except Exception as e:
                if attempt == 2:
                    logger.warning("get_tick_size(%s) failed: %s", token_id[:8], e)
                else:
                    await asyncio.sleep(0.1 * (attempt + 1))
        return None

    async def get_neg_risk(self, token_id: str) -> Optional[bool]:
        if token_id in self._neg_risk_cache:
            return self._neg_risk_cache[token_id]
        for attempt in range(3):
            try:
                neg_risk = await asyncio.to_thread(
                    self._clob_client.get_neg_risk, token_id
                )
                self._neg_risk_cache[token_id] = neg_risk
                return neg_risk
            except Exception as e:
                if attempt == 2:
                    logger.warning("get_neg_risk(%s) failed: %s", token_id[:8], e)
                else:
                    await asyncio.sleep(0.1 * (attempt + 1))
        return None

    async def get_mid_price(self, token_id: str) -> Optional[float]:
        for attempt in range(3):
            try:
                mid = await asyncio.to_thread(
                    self._clob_client.get_midpoint, token_id
                )
                return float(mid["mid"])
            except Exception as e:
                if attempt == 2:
                    logger.warning("get_mid_price(%s) failed: %s", token_id[:8], e)
                else:
                    await asyncio.sleep(0.1 * (attempt + 1))
        return None

    def cache_tick_size(self, token_id: str, tick_size: str) -> None:
        self._tick_size_cache[token_id] = tick_size

    def cache_neg_risk(self, token_id: str, neg_risk: bool) -> None:
        self._neg_risk_cache[token_id] = neg_risk
