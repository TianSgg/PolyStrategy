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
            "ws_user": "--",
            "polygon_ws": "--",
            "predexon": "--",
        }
        self.http_latency: Dict[str, str] = {
            "data_api": "--",
            "clob_api": "--",
            "gamma_api": "--",
            "polygon_http": "--",
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
        # 1. Polymarket Market WS - CopyTrading MarketService
        try:
            from market import get_market_service
            latency = await get_market_service().check_latency()
            self.ws_latencies["ws_market"] = str(latency) if latency is not None else "--"
        except Exception as e:
            logger.debug(f"[Performance] ws_market error: {e}")
            self.ws_latencies["ws_market"] = "--"

        # 2. ws_user - CopyTradingWS（只测一个实例）
        try:
            from copy_trading.ws import get_all_copy_trading_ws
            ws_instances = get_all_copy_trading_ws()
            if ws_instances:
                latency = await next(iter(ws_instances.values())).check_latency()
                self.ws_latencies["ws_user"] = str(latency)
            else:
                self.ws_latencies["ws_user"] = "--"
        except Exception as e:
            logger.debug(f"[Performance] ws_user error: {e}")
            self.ws_latencies["ws_user"] = "--"

        # 3. predexon - CopyTradingPredexon
        try:
            from copy_trading.predexon import get_copy_trading_predexon
            predexon = get_copy_trading_predexon()
            latency = await predexon.check_latency()
            self.ws_latencies["predexon"] = str(latency)
        except Exception as e:
            logger.debug(f"[Performance] predexon error: {e}")
            self.ws_latencies["predexon"] = "--"

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
            self._copy_trading_summary(),
            self._market_summary(),
            self._account_summary(),
            self._leader_summary(),
            self._copy_trading_ws_summary(),
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

    def _copy_trading_summary(self) -> dict:
        from copy_trading.service import get_copy_trading_service

        service = get_copy_trading_service()
        configs = [self._config_item(config) for config in service._config_id_to_config.values()]
        caches = [
            self._cache_block("copy_trading", "configs", "Configs", configs, {
                "enabled": sum(1 for item in configs if item["enabled"]),
                "disabled": sum(1 for item in configs if not item["enabled"]),
            }),
            self._cache_block("copy_trading", "follower_positions", "Follower Positions",
                              self._nested_amount_items(service._follower_positions, "follower", "size")),
            self._cache_block("copy_trading", "pending_buy_orders", "Pending BUY",
                              self._nested_amount_items(service._pending_buy_orders, "follower", "pending")),
            self._cache_block("copy_trading", "pending_sell_orders", "Pending SELL",
                              self._nested_amount_items(service._pending_sell_orders, "follower", "pending")),
            self._cache_block("copy_trading", "processed_txs", "Processed TXs", self._set_items(service._processed_txs)),
            self._cache_block("copy_trading", "processed_orders", "Processed Orders", self._processed_order_items(service)),
            self._cache_block("copy_trading", "runtime", "Runtime", self._copy_trading_runtime_items(service)),
        ]
        return self._service_block("copy_trading", "Copy Trading", caches)

    def _config_item(self, config) -> dict:
        return {
            "id": config.id,
            "leader_proxy_wallet": config.leader_proxy_wallet,
            "follower_proxy_wallet": config.follower_proxy_wallet,
            "enabled": config.enabled,
            "owner_user_id": config.owner_user_id,
            "gtd_expiration_sec": config.gtd_expiration_sec,
            "buy_size": config.buy_size,
        }

    def _processed_order_items(self, service) -> List[dict]:
        groups = {
            "live_on_post": service._order_live_on_post_ids,
            "delayed_on_post": service._order_delayed_on_post_ids,
            "matched_on_post": set(service._order_post_filled.keys()),
            "canceled": service._processed_canceled_order_ids,
        }
        items = []
        for group, values in groups.items():
            for order_id in sorted(values):
                items.append({"group": group, "order_id": order_id})
        return items

    def _copy_trading_runtime_items(self, service) -> List[dict]:
        return [
            {"name": "leaders", "value": len(service._leader_addr_to_configs)},
            {"name": "followers", "value": len(service._followers)},
            {"name": "tx_locks", "value": len(service._tx_locks)},
            {"name": "addr_locks", "value": len(service._addr_locks)},
            {"name": "asset_fetch_events", "value": len(service._asset_fetch_events)},
            {"name": "follower_poller_running", "value": self._task_alive(service._follower_poller_task)},
            {"name": "pending_poller_running", "value": self._task_alive(service._pending_poller_task)},
        ]

    def _market_summary(self) -> dict:
        from market import get_market_service

        service = get_market_service()
        order_book_items = self._order_book_items(service)
        caches = [
            self._cache_block("market", "subscriptions", "Subscriptions", self._market_subscription_items(service), {
                "subscribed": len(service._subscribed),
                "pending": len(service._pending_subscriptions),
                "confirmed": len(service._confirmed_subscriptions),
            }),
            self._cache_block("market", "order_books", "Order Books", order_book_items),
            self._cache_block("market", "tick_sizes", "Tick Sizes", self._dict_items(service._tick_sizes, "asset_id", "tick_size")),
            self._cache_block("market", "neg_risks", "Negative Risk", self._dict_items(service._neg_risks, "asset_id", "neg_risk")),
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

    def _order_book_items(self, service, asset_id: Optional[str] = None) -> List[dict]:
        items = []
        for asset, book in sorted(service._order_books.items()):
            if asset_id and asset != asset_id:
                continue
            best_bid = book.get_best_bid()
            best_ask = book.get_best_ask()
            items.append({
                "asset_id": asset,
                "bid_levels": len(book.bids),
                "ask_levels": len(book.asks),
                "best_bid": {"price": best_bid[0], "size": best_bid[1]} if best_bid else {},
                "best_ask": {"price": best_ask[0], "size": best_ask[1]} if best_ask else {},
            })
        return items

    def _order_book_level_items(self, service, asset_id: str) -> List[dict]:
        book = service._order_books.get(asset_id)
        if not book:
            return []
        items = []
        for price, size in reversed(list(book.bids.items())):
            items.append({"asset_id": asset_id, "side": "BUY", "price": price, "size": size})
        for price, size in book.asks.items():
            items.append({"asset_id": asset_id, "side": "SELL", "price": price, "size": size})
        return items

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
        from leader.service import get_leader_service

        service = get_leader_service()
        caches = [
            self._cache_block("leader", "names", "Leader Names",
                              self._dict_items(service._cache, "proxy_wallet", "name")),
        ]
        return self._service_block("leader", "Leader", caches)


    def _copy_trading_ws_summary(self) -> dict:
        from copy_trading.ws import get_all_copy_trading_ws

        items = self._copy_trading_ws_items(get_all_copy_trading_ws())
        return self._service_block("copy_trading_ws", "Follower User WS", [
            self._cache_block("copy_trading_ws", "instances", "Instances", items)
        ])

    def _copy_trading_ws_items(self, ws_instances: dict) -> List[dict]:
        items = []
        for follower, ws in sorted(ws_instances.items()):
            items.append({
                "follower": follower,
                "running": ws._running,
                "connected": ws._ws is not None,
                "task_running": self._task_alive(ws._task),
                "processed_trades": len(getattr(ws, "_processed_trades", set())),
            })
        return items

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
        if service == "copy_trading":
            from copy_trading.service import get_copy_trading_service
            ct = get_copy_trading_service()
            mapping = {
                "configs": lambda: [self._config_item(config) for config in ct._config_id_to_config.values()],
                "follower_positions": lambda: self._nested_amount_items(ct._follower_positions, "follower", "size"),
                "pending_buy_orders": lambda: self._nested_amount_items(ct._pending_buy_orders, "follower", "pending"),
                "pending_sell_orders": lambda: self._nested_amount_items(ct._pending_sell_orders, "follower", "pending"),
                "processed_txs": lambda: self._set_items(ct._processed_txs),
                "processed_orders": lambda: self._processed_order_items(ct),
                "runtime": lambda: self._copy_trading_runtime_items(ct),
            }
            return mapping[cache]()

        if service == "market":
            from market import get_market_service
            market = get_market_service()
            mapping = {
                "subscriptions": lambda: self._market_subscription_items(market),
                "order_books": lambda: self._order_book_level_items(market, asset_id) if asset_id else self._order_book_items(market),
                "tick_sizes": lambda: self._dict_items(market._tick_sizes, "asset_id", "tick_size"),
                "neg_risks": lambda: self._dict_items(market._neg_risks, "asset_id", "neg_risk"),
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
            from leader.service import get_leader_service
            leader = get_leader_service()
            return {"names": lambda: self._dict_items(leader._cache, "proxy_wallet", "name")}[cache]()

        if service == "copy_trading_ws":
            from copy_trading.ws import get_all_copy_trading_ws
            return {"instances": lambda: self._copy_trading_ws_items(get_all_copy_trading_ws())}[cache]()

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
