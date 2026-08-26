"""Consul 服务发现（带缓存）"""
import logging
import time
from typing import Optional

import httpx

from .registration import CONSUL_HTTP_ADDR, CONSUL_HTTP_TOKEN

logger = logging.getLogger(__name__)


class ConsulDiscovery:
    def __init__(self, consul_addr: Optional[str] = None):
        self.consul_addr = consul_addr or CONSUL_HTTP_ADDR
        self._cache: dict = {}
        self._cache_ttl = 10
        self._cache_ts: dict = {}

    def _headers(self) -> dict:
        return {"X-Consul-Token": CONSUL_HTTP_TOKEN} if CONSUL_HTTP_TOKEN else {}

    async def get_service_url(self, service_name: str) -> Optional[str]:
        now = time.time()
        if service_name in self._cache and (now - self._cache_ts.get(service_name, 0)) < self._cache_ttl:
            return self._cache[service_name]

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.consul_addr}/v1/health/service/{service_name}",
                    params={"passing": "true"},
                    headers=self._headers(),
                    timeout=5,
                )
                if resp.status_code == 200:
                    services = resp.json()
                    if services:
                        svc = services[0]["Service"]
                        url = f"http://{svc['Address']}:{svc['Port']}"
                        self._cache[service_name] = url
                        self._cache_ts[service_name] = now
                        return url
        except Exception as e:
            logger.warning(f"[Consul] Discovery failed for {service_name}: {e}")

        return self._cache.get(service_name)

    def get_service_url_sync(self, service_name: str) -> Optional[str]:
        now = time.time()
        if service_name in self._cache and (now - self._cache_ts.get(service_name, 0)) < self._cache_ttl:
            return self._cache[service_name]

        try:
            resp = httpx.get(
                f"{self.consul_addr}/v1/health/service/{service_name}",
                params={"passing": "true"},
                headers=self._headers(),
                timeout=5,
            )
            if resp.status_code == 200:
                services = resp.json()
                if services:
                    svc = services[0]["Service"]
                    url = f"http://{svc['Address']}:{svc['Port']}"
                    self._cache[service_name] = url
                    self._cache_ts[service_name] = now
                    return url
        except Exception as e:
            logger.warning(f"[Consul] Sync discovery failed for {service_name}: {e}")

        return self._cache.get(service_name)


_discovery: Optional[ConsulDiscovery] = None


def get_discovery() -> ConsulDiscovery:
    global _discovery
    if _discovery is None:
        _discovery = ConsulDiscovery()
    return _discovery
