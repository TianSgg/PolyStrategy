"""UTC+8 time helpers for logs, DB writes, and API display."""
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

UTC8 = timezone(timedelta(hours=8))
UTC8_DB_NOW_SQL = "UTC_TIMESTAMP(3) + INTERVAL 8 HOUR"
UTC8_DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def now_utc8_dt() -> datetime:
    """Return a naive UTC+8 wall-clock datetime for MySQL DATETIME columns."""
    return datetime.now(UTC8).replace(tzinfo=None)


def to_utc8_dt(value: Union[datetime, int, float, str, None]) -> Optional[datetime]:
    """Normalize a datetime-like value to a naive UTC+8 wall-clock datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000, UTC8)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC8)
    return dt.replace(tzinfo=None)


def format_utc8(value: Union[datetime, int, float, str, None]) -> Optional[str]:
    """Format a datetime-like value as 'YYYY-MM-DD HH:mm:ss.SSS' in UTC+8."""
    dt = to_utc8_dt(value)
    if dt is None:
        return None
    return dt.strftime(UTC8_DISPLAY_FORMAT)[:-3]


def now_utc8_str() -> str:
    return format_utc8(now_utc8_dt()) or ""
