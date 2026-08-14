from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal

WeatherEventType = Literal["sweep", "no_longer_possible", "market_resolved"]


@dataclass(frozen=True)
class WeatherCity:
    name: str
    slug: str
    timezone: str
    directions: tuple[str, ...]


@dataclass(frozen=True)
class WeatherAsset:
    asset_id: str
    city: str
    event_slug: str
    market_slug: str
    temperature_label: str
    outcome: Literal["yes", "no"]


@dataclass(frozen=True)
class MarketCandidate:
    city: str
    direction: str
    event_slug: str
    market_slug: str
    temperature_label: str
    yes_asset: WeatherAsset
    no_asset: WeatherAsset


@dataclass(frozen=True)
class WeatherEvent:
    event_type: WeatherEventType
    asset: WeatherAsset
    previous_orderbook: dict[str, Any] | None
    current_orderbook: dict[str, Any]
    reason: str

    def payload(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "asset": asdict(self.asset),
            "previous_orderbook": self.previous_orderbook,
            "current_orderbook": self.current_orderbook,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WeatherNotificationRecord:
    """One durable weather Telegram notification and its structured context."""

    notification_key: str
    occurred_at: datetime
    event_type: str
    event_slug: str
    city: str
    city_slug: str
    direction: str
    local_date: date
    market_slug: str | None
    temperature_label: str | None
    outcome: str | None
    main_market_slug: str | None
    main_temperature_label: str | None
    main_outcome: str | None
    token_id: str | None
    status: str | None
    reason: str | None
    message: str
    payload: dict[str, Any]
    id: int | None = None
    created_at: datetime | None = None

