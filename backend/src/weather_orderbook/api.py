from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from weather_orderbook.dao import WeatherNotificationRepository
from weather_orderbook.types import WeatherNotificationRecord
from weather_orderbook.service import WeatherOrderBookService

router = APIRouter(prefix="/api/weather", tags=["weather-orderbook"])


class WeatherNotificationResponse(BaseModel):
    """Frontend contract for one persisted weather Telegram notification."""

    model_config = ConfigDict(extra="forbid")

    id: int
    notification_key: str
    occurred_at: datetime
    event_type: str
    event_slug: str
    market_slug: str | None
    main_market_slug: str | None
    main_temperature_label: str | None
    main_outcome: str | None
    city: str
    city_slug: str
    direction: str
    local_date: str
    temperature_label: str | None
    outcome: str | None
    token_id: str | None
    status: str | None
    reason: str | None
    message: str
    payload: dict[str, Any]
    created_at: datetime | None


class WeatherNotificationsResponse(BaseModel):
    event_slug: str
    notifications: list[WeatherNotificationResponse]
    next_before_id: int | None


class WeatherRecentNotificationsResponse(BaseModel):
    notifications: list[WeatherNotificationResponse]
    next_before_id: int | None


class WeatherOrderbookResponse(BaseModel):
    token_id: str
    observed_at: str
    tick_size: str | float | None
    best_bid: dict | None
    best_ask: dict | None
    bid_levels: int
    ask_levels: int
    bids: list[dict]
    asks: list[dict]


def _service(request: Request) -> WeatherOrderBookService:
    return request.app.state.weather_service


def _notification_repository(request: Request) -> WeatherNotificationRepository:
    return request.app.state.weather_notification_repository


def notification_response(record: WeatherNotificationRecord) -> WeatherNotificationResponse:
    return WeatherNotificationResponse(
        id=record.id or 0,
        notification_key=record.notification_key,
        occurred_at=record.occurred_at,
        event_type=record.event_type,
        event_slug=record.event_slug,
        market_slug=record.market_slug,
        main_market_slug=record.main_market_slug,
        main_temperature_label=record.main_temperature_label,
        main_outcome=record.main_outcome,
        city=record.city,
        city_slug=record.city_slug,
        direction=record.direction,
        local_date=record.local_date.isoformat(),
        temperature_label=record.temperature_label,
        outcome=record.outcome,
        token_id=record.token_id,
        status=record.status,
        reason=record.reason,
        message=record.message,
        payload=record.payload,
        created_at=record.created_at,
    )


@router.get("/cities")
async def weather_cities(request: Request) -> dict:
    return {"cities": await _service(request).dashboard()}


@router.get("/cities/{city_slug}/{direction}")
async def weather_direction(city_slug: str, direction: str, request: Request) -> dict:
    payload = _service(request).direction_detail(city_slug, direction)
    if payload is None:
        raise HTTPException(status_code=404, detail="Configured city direction not found")
    return payload


@router.get(
    "/events/{event_slug}/notifications",
    response_model=WeatherNotificationsResponse,
)
async def weather_event_notifications(
    request: Request,
    event_slug: str = Path(..., pattern=r"^[a-z0-9-]{1,255}$"),
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
) -> WeatherNotificationsResponse:
    if before_id is None:
        cached = _service(request).cached_notifications_for_event(event_slug)
        if cached is not None:
            page = cached[:limit]
            has_more = len(cached) > limit
            return WeatherNotificationsResponse(
                event_slug=event_slug,
                notifications=[notification_response(r) for r in page],
                next_before_id=page[-1].id if has_more and page else None,
            )
    records = await _notification_repository(request).list_for_event(event_slug, limit + 1, before_id)
    has_more = len(records) > limit
    page = records[:limit]
    return WeatherNotificationsResponse(
        event_slug=event_slug,
        notifications=[notification_response(record) for record in page],
        next_before_id=page[-1].id if has_more and page else None,
    )


@router.get(
    "/notifications/recent",
    response_model=WeatherRecentNotificationsResponse,
)
async def weather_recent_notifications(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
) -> WeatherRecentNotificationsResponse:
    records = await _notification_repository(request).list_recent(limit + 1, before_id)
    has_more = len(records) > limit
    page = records[:limit]
    return WeatherRecentNotificationsResponse(
        notifications=[notification_response(record) for record in page],
        next_before_id=page[-1].id if has_more and page else None,
    )


@router.get("/orderbook/{token_id}", response_model=WeatherOrderbookResponse)
async def weather_orderbook(token_id: str, request: Request) -> WeatherOrderbookResponse:
    book = await _service(request).fetch_orderbook(token_id)
    if book is None:
        raise HTTPException(status_code=502, detail="CLOB orderbook request failed")
    bids = sorted(book.get("bids") or [], key=lambda row: float(row["price"]), reverse=True)
    asks = sorted(book.get("asks") or [], key=lambda row: float(row["price"]))
    return WeatherOrderbookResponse(
        token_id=token_id,
        observed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC",
        tick_size=book.get("tick_size") or book.get("min_tick_size"),
        best_bid=bids[0] if bids else None,
        best_ask=asks[0] if asks else None,
        bid_levels=len(bids),
        ask_levels=len(asks),
        bids=bids,
        asks=asks,
    )


@router.get("/live")
async def weather_live_orderbooks(request: Request) -> StreamingResponse:
    """SSE stream for in-memory L2 books of currently monitored markets."""
    service = _service(request)

    async def event_stream():
        queue = service.subscribe_live_orderbooks()
        try:
            snapshot = service.live_snapshot()
            yield f"event: snapshot\ndata: {json.dumps(snapshot, separators=(',', ':'))}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except (TimeoutError, asyncio.CancelledError):
                    yield ": keepalive\n\n"
                    continue
                yield f"event: orderbook\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            service.unsubscribe_live_orderbooks(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/notification-counts/live")
async def weather_notification_counts_live(request: Request) -> StreamingResponse:
    """SSE stream for cached notification totals of active weather events."""
    service = _service(request)

    async def event_stream():
        queue = service.subscribe_notification_counts()
        try:
            yield f"event: snapshot\ndata: {json.dumps(service.notification_count_snapshot(), separators=(',', ':'))}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except (TimeoutError, asyncio.CancelledError):
                    yield ": keepalive\n\n"
                    continue
                yield f"event: notification-count\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            service.unsubscribe_notification_counts(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
