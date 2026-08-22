from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Mapping, Optional

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


@dataclass(frozen=True)
class WeatherSweepSignal:
    """天气盘口扫单信号 — 检测到天气市场异常盘口活动。"""
    event_id: str
    token_id: str
    outcome: str  # "yes" | "no"
    occurred_at_ms: int
    received_at_ns: int
    city: str
    spread_before: Decimal
    spread_after: Decimal
    volume_spike: bool = False
    bid_depth_change: Optional[Decimal] = None
    ask_depth_change: Optional[Decimal] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> str:
        return f"weather:{self.event_id}"


@dataclass(frozen=True)
class WeatherMarketContext:
    """天气信号触发时的盘口快照 — 异步持久化，不阻塞入场。"""
    signal_event_id: str
    token_id: str
    observed_at_ms: int
    tick_size: Optional[Decimal] = None
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    bid_depth: Optional[Decimal] = None
    ask_depth: Optional[Decimal] = None
    bid_levels: Optional[int] = None
    ask_levels: Optional[int] = None

