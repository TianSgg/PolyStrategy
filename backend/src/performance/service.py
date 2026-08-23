import logging
import time
import asyncio
import aiohttp
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional

from shared.frontend_ws import get_frontend_ws_manager
from shared.time_utils import now_utc8_str

logger = logging.getLogger(__name__)

POLYMARKET_APIS = {
    "data_api": "https://data-api.polymarket.com",
    "clob_api": "https://clob.polymarket.com",
    "gamma_api": "https://gamma-api.polymarket.com/markets?limit=1",
}

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PerformanceService:
    _instance = None

    def __init__(self):
        self.ws_latencies: Dict[str, str] = {
            "ws_market": "--",
        }
        self.http_latency: Dict[str, str] = {
            "data_api": "--",
            "clob_api": "--",
            "gamma_api": "--",
        }
        self._task: asyncio.Task = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self):
        self._task = asyncio.create_task(self._auto_check_loop())
        logger.info("[Performance] Service started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[Performance] Service stopped")

    async def _auto_check_loop(self):
        while True:
            await asyncio.sleep(30)
            await self.check_all()
            await self.broadcast()

    async def check_all(self):
        await self.check_ws_latencies()
        await self.check_http_latencies()

    async def check_ws_latencies(self):
        # 1. Polymarket Market WS
        try:
            from market import get_market_service
            latency = await get_market_service().check_latency()
            self.ws_latencies["ws_market"] = str(latency) if latency is not None else "--"
        except Exception as e:
            logger.debug(f"[Performance] ws_market error: {e}")
            self.ws_latencies["ws_market"] = "--"

    async def check_http_latencies(self):
        # 1. Polymarket HTTP APIs（主动探测）
        tasks = [self._check_http_latency(name, url) for name, url in POLYMARKET_APIS.items()]

        await asyncio.gather(*tasks)

    async def _check_http_latency(self, name: str, url: str):
        try:
            start = time.time()
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    await resp.text()
            latency_ms = int((time.time() - start) * 1000)
            self.http_latency[name] = str(latency_ms)
        except Exception as e:
            self.http_latency[name] = "--"

    async def broadcast(self):
        await get_frontend_ws_manager().broadcast({
            "event_type": "latency",
            "ws": self.ws_latencies,
            "http": self.http_latency,
        })

    def get_all_latency(self) -> dict:
        return {
            "ws": self.ws_latencies,
            "http": self.http_latency,
        }

    def get_cache_summary(self) -> dict:
        """Return a safe, compact snapshot of in-memory caches."""
        generated_at = now_utc8_str()
        services = [
            self._market_summary(),
            self._account_summary(),
            self._leader_summary(),
            self._frontend_ws_summary(),
            self._performance_summary(),
        ]
        return {
            "generated_at": generated_at,
            "services": [svc for svc in services if svc is not None],
        }

    def get_cache_detail(
        self,
        service: str,
        cache: str,
        limit: int = 100,
        offset: int = 0,
        asset_id: Optional[str] = None,
    ) -> dict:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        generated_at = now_utc8_str()
        items = self._cache_items(service, cache, asset_id=asset_id)
        total = len(items)
        paged = items[offset:offset + limit]
        return {
            "service": service,
            "cache": cache,
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": offset + limit < total,
            "generated_at": generated_at,
            "items": paged,
        }

    def _service_block(self, service: str, label: str, caches: List[dict], status: str = "ok") -> dict:
        return {
            "service": service,
            "label": label,
            "status": status,
            "total_items": sum(int(cache.get("total", 0)) for cache in caches),
            "caches": caches,
        }

    def _cache_block(self, service: str, cache: str, label: str, items: List[Any], summary: Optional[dict] = None) -> dict:
        return {
            "service": service,
            "cache": cache,
            "label": label,
            "total": len(items),
            "summary": summary or {},
            "sample": self._sample(items),
        }

    def _sample(self, items: List[Any], size: int = 3) -> List[Any]:
        return [self._safe_value(item) for item in items[:size]]

    def _safe_value(self, value: Any) -> Any:
        if is_dataclass(value):
            value = asdict(value)
        if isinstance(value, dict):
            blocked = {"private_key", "encrypted_private_key", "encrypted_builder_secret",
                       "encrypted_builder_passphrase", "api_secret", "api_passphrase",
                       "secret", "passphrase", "creds", "client"}
            return {
                str(k): self._safe_value(v)
                for k, v in value.items()
                if str(k).lower() not in blocked
            }
        if isinstance(value, (list, tuple, set)):
            return [self._safe_value(v) for v in list(value)]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _set_items(self, values) -> List[dict]:
        return [{"value": value} for value in sorted(list(values))]

    def _dict_items(self, mapping: dict, key_name: str = "key", value_name: str = "value") -> List[dict]:
        return [{key_name: key, value_name: self._safe_value(value)} for key, value in sorted(mapping.items())]

    def _nested_amount_items(self, mapping: dict, owner_name: str, value_name: str) -> List[dict]:
        items = []
        for owner, assets in sorted(mapping.items()):
            for asset_id, value in sorted(assets.items()):
                items.append({owner_name: owner, "asset_id": asset_id, value_name: value})
        return items

    def _market_summary(self) -> dict:
        from market import get_market_service

        service = get_market_service()
        caches = [
            self._cache_block("market", "subscriptions", "Subscriptions", self._market_subscription_items(service), {
                "subscribed": len(service._subscribed),
                "pending": len(service._pending_subscriptions),
                "confirmed": len(service._confirmed_subscriptions),
            }),
            self._cache_block("market", "tick_sizes", "Tick Sizes", self._dict_items(service._tick_sizes, "asset_id", "tick_size")),
            self._cache_block("market", "neg_risks", "Negative Risk", self._dict_items(service._neg_risks, "asset_id", "neg_risk")),
            self._cache_block("market", "exit_watches", "Exit Watches", self._set_items(service._exit_watches)),
            self._cache_block("market", "runtime", "Runtime", [
                {"name": "ws_running", "value": service._ws_running},
                {"name": "ws_connected", "value": service._ws is not None},
                {"name": "ws_task_running", "value": self._task_alive(service._ws_task)},
            ]),
        ]
        return self._service_block("market", "Market", caches)

    def _market_subscription_items(self, service) -> List[dict]:
        assets = sorted(service._subscribed | service._pending_subscriptions | service._confirmed_subscriptions)
        return [{
            "asset_id": asset,
            "subscribed": asset in service._subscribed,
            "pending": asset in service._pending_subscriptions,
            "confirmed": asset in service._confirmed_subscriptions,
        } for asset in assets]


    def _account_summary(self) -> dict:
        from account.service import get_account_service

        service = get_account_service()
        caches = [
            self._cache_block("account", "clob_clients", "CLOB Clients",
                              [{"proxy_wallet": key} for key in sorted(service._clients.keys())]),
            self._cache_block("account", "names", "Account Names",
                              self._dict_items(service._proxy_to_name, "proxy_wallet", "name")),
        ]
        return self._service_block("account", "Account", caches)

    def _leader_summary(self) -> dict:
        from signal_leader_activity.service import get_leader_service

        service = get_leader_service()
        caches = [
            self._cache_block("leader", "names", "Leader Names",
                              self._dict_items(service._cache, "proxy_wallet", "name")),
        ]
        return self._service_block("leader", "Leader", caches)


    def _frontend_ws_summary(self) -> dict:
        manager = get_frontend_ws_manager()
        return self._service_block("frontend_ws", "Frontend WS", [
            self._cache_block("frontend_ws", "connections", "Connections", [
                {"name": "connection_count", "value": manager.connection_count}
            ])
        ])

    def _performance_summary(self) -> dict:
        return self._service_block("performance", "Performance", [
            self._cache_block("performance", "latency", "Latency Cache", self._latency_items()),
            self._cache_block("performance", "runtime", "Runtime", [
                {"name": "auto_check_running", "value": self._task_alive(self._task)}
            ]),
        ])

    def _latency_items(self) -> List[dict]:
        return [
            {"group": "ws", "name": key, "value": value}
            for key, value in sorted(self.ws_latencies.items())
        ] + [
            {"group": "http", "name": key, "value": value}
            for key, value in sorted(self.http_latency.items())
        ]

    def _task_alive(self, task) -> bool:
        return task is not None and not task.done()

    def _cache_items(self, service: str, cache: str, asset_id: Optional[str] = None) -> List[dict]:
        if service == "market":
            from market import get_market_service
            market = get_market_service()
            mapping = {
                "subscriptions": lambda: self._market_subscription_items(market),
                "tick_sizes": lambda: self._dict_items(market._tick_sizes, "asset_id", "tick_size"),
                "neg_risks": lambda: self._dict_items(market._neg_risks, "asset_id", "neg_risk"),
                "exit_watches": lambda: self._set_items(market._exit_watches),
                "runtime": lambda: [
                    {"name": "ws_running", "value": market._ws_running},
                    {"name": "ws_connected", "value": market._ws is not None},
                    {"name": "ws_task_running", "value": self._task_alive(market._ws_task)},
                ],
            }
            return mapping[cache]()

        if service == "account":
            from account.service import get_account_service
            account = get_account_service()
            mapping = {
                "clob_clients": lambda: [{"proxy_wallet": key} for key in sorted(account._clients.keys())],
                "names": lambda: self._dict_items(account._proxy_to_name, "proxy_wallet", "name"),
            }
            return mapping[cache]()

        if service == "leader":
            from signal_leader_activity.service import get_leader_service
            leader = get_leader_service()
            return {"names": lambda: self._dict_items(leader._cache, "proxy_wallet", "name")}[cache]()

        if service == "frontend_ws":
            return {
                "connections": lambda: [{"name": "connection_count", "value": get_frontend_ws_manager().connection_count}]
            }[cache]()

        if service == "performance":
            mapping = {
                "latency": lambda: self._latency_items(),
                "runtime": lambda: [{"name": "auto_check_running", "value": self._task_alive(self._task)}],
            }
            return mapping[cache]()

        raise KeyError(f"Unknown cache: {service}/{cache}")


def get_performance_service() -> PerformanceService:
    return PerformanceService.get_instance()
