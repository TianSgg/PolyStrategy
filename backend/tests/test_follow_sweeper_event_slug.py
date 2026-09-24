import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from strategy_follow_weather_sweeper import api  # noqa: E402


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_resolves_event_slug_from_market_details(monkeypatch):
    calls = []
    api._market_event_cache.clear()

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"events": [{"slug": "highest-temperature-in-panama-city-on-september-23-2026"}]})

    monkeypatch.setattr(api.http_requests, "get", fake_get)

    event_slug = api._fetch_event_slug_by_market_slug(
        "highest-temperature-in-panama-city-on-september-23-2026-33c"
    )

    assert event_slug == "highest-temperature-in-panama-city-on-september-23-2026"
    assert calls[0][0].endswith(
        "/markets/slug/highest-temperature-in-panama-city-on-september-23-2026-33c"
    )


def test_resolved_event_slug_is_cached(monkeypatch):
    calls = []
    api._market_event_cache.clear()

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse({"events": [{"slug": "weather-event"}]})

    monkeypatch.setattr(api.http_requests, "get", fake_get)
    market_slug = "some-market-31c"

    assert api._fetch_event_slug_by_market_slug(market_slug) == "weather-event"
    assert api._fetch_event_slug_by_market_slug(market_slug) == "weather-event"
    assert len(calls) == 1
