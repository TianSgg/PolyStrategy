"""天气模块启动引导：封装初始化、事件处理、通知持久化逻辑"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as _tz
from zoneinfo import ZoneInfo

from collections.abc import Awaitable, Callable

import aiohttp
import asyncmy

from framework.db import MYSQL_CONFIG
from .dao import WeatherCityRepository, WeatherSignalEventRepository
from .gateway import PolymarketMarketClient
from .service import WeatherOrderBookService
from .types import WeatherEvent, WeatherSignalRecord

logger = logging.getLogger(__name__)



def _build_signal_record(
    event: WeatherEvent, city, main_ctx, next_candidate_orderbook=None
) -> WeatherSignalRecord:
    occurred_at_ms = event.current_orderbook.get("observed_at_unix_ms")
    try:
        occurred_at = datetime.fromtimestamp(int(occurred_at_ms) / 1000, _tz.utc)
    except (TypeError, ValueError, OSError):
        occurred_at = datetime.now(_tz.utc)
    direction = event.asset.event_slug.split("-temperature-in-", 1)[0]
    local_date = occurred_at.astimezone(ZoneInfo(city.timezone)).date()
    payload = event.payload()
    if main_ctx:
        payload["main_monitor"] = main_ctx
    if next_candidate_orderbook:
        payload["next_candidate_orderbook"] = next_candidate_orderbook
    return WeatherSignalRecord(
        signal_id=f"{event.event_type}:{event.asset.event_slug}:{event.asset.asset_id}:{int(occurred_at.timestamp() * 1000)}",
        occurred_at=occurred_at,
        signal_type=event.event_type,
        event_slug=event.asset.event_slug,
        city=event.asset.city,
        city_slug=city.slug,
        direction=direction,
        local_date=local_date,
        market_slug=event.asset.market_slug,
        temperature_label=event.asset.temperature_label,
        outcome=event.asset.outcome,
        main_market_slug=main_ctx.get("main_market_slug") if main_ctx else None,
        main_temperature_label=main_ctx.get("main_temperature_label") if main_ctx else None,
        main_outcome=main_ctx.get("main_outcome") if main_ctx else None,
        token_id=event.asset.asset_id,
        status=None,
        reason=event.reason,
        payload=payload,
    )


class WeatherBootstrap:
    """天气模块生命周期管理：初始化资源、注册事件回调、启停服务"""

    def __init__(self):
        self.service: WeatherOrderBookService | None = None
        self.signal_event_repository: WeatherSignalEventRepository | None = None
        self._mysql_pool = None
        self._http_session: aiohttp.ClientSession | None = None
        self._city_by_name: dict = {}

    async def start(self, on_broadcast: Callable[[str, dict], Awaitable[None]] | None = None) -> WeatherOrderBookService:
        self._mysql_pool = await asyncmy.create_pool(
            host=MYSQL_CONFIG["host"],
            port=MYSQL_CONFIG["port"],
            user=MYSQL_CONFIG["user"],
            password=MYSQL_CONFIG["password"],
            db=MYSQL_CONFIG["database"],
            minsize=3,
            maxsize=10,
            pool_recycle=1800,
            autocommit=True,
            connect_timeout=5,
        )

        city_repository = WeatherCityRepository(self._mysql_pool)
        cities = await city_repository.list_enabled()
        logger.info("Weather: loaded %d cities from database", len(cities))

        self.signal_event_repository = WeatherSignalEventRepository(self._mysql_pool)
        await self.signal_event_repository.ping()

        self._http_session = aiohttp.ClientSession()
        market_client = PolymarketMarketClient(self._http_session)

        self._city_by_name = {city.name: city for city in cities}

        self.service = WeatherOrderBookService(
            cities,
            market_client,
            self._on_weather_event,
            signal_event_repository=self.signal_event_repository,
            on_broadcast=on_broadcast,
        )
        await self.service.start()
        logger.info("Weather OrderBook service started")
        return self.service

    async def stop(self):
        if self.service:
            await self.service.stop()
        if self._http_session:
            await self._http_session.close()
        if self._mysql_pool:
            self._mysql_pool.close()
            await self._mysql_pool.wait_closed()

    async def _on_weather_event(self, event: WeatherEvent, main_ctx=None, next_candidate_orderbook=None):
        logger.debug("Weather event: %s %s", event.event_type, event.asset.event_slug)

        city = self._city_by_name.get(event.asset.city)
        if city is None:
            logger.error("Cannot persist weather notification: unknown city=%s", event.asset.city)
            return
        try:
            record = _build_signal_record(event, city, main_ctx, next_candidate_orderbook)
            inserted = await self.signal_event_repository.insert_if_absent(record)
        except Exception:
            logger.exception("Failed to persist weather notification event=%s", event.asset.event_slug)
            return
        if inserted and self.service:
            self.service.note_persisted_signal(event.asset.event_slug, record)
