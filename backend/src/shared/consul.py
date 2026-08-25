"""Consul 服务注册与发现"""
import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

CONSUL_HTTP_ADDR = os.getenv("CONSUL_HTTP_ADDR", "http://localhost:8500")


def _get_local_ip() -> str:
    """获取本机 IP（用于服务注册）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class ConsulRegistration:
    """服务注册：启动时注册，关闭时注销，后台 TTL 心跳。"""

    def __init__(
        self,
        service_name: str,
        port: int,
        tags: Optional[List[str]] = None,
        health_path: str = "/health",
        service_id: Optional[str] = None,
        address: Optional[str] = None,
        ttl_seconds: int = 15,
    ):
        self.service_name = service_name
        self.service_id = service_id or os.getenv("SERVICE_ID", f"{service_name}-{port}")
        self.port = port
        self.tags = tags or []
        self.health_path = health_path
        self.address = address or os.getenv("SERVICE_ADDRESS") or _get_local_ip()
        self.ttl_seconds = ttl_seconds
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def register(self):
        """向 Consul 注册服务"""
        payload = {
            "ID": self.service_id,
            "Name": self.service_name,
            "Address": self.address,
            "Port": self.port,
            "Tags": self.tags,
            "Check": {
                "HTTP": f"http://{self.address}:{self.port}{self.health_path}",
                "Interval": f"{self.ttl_seconds}s",
                "Timeout": "5s",
                "DeregisterCriticalServiceAfter": "60s",
            },
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.put(
                    f"{CONSUL_HTTP_ADDR}/v1/agent/service/register",
                    json=payload,
                    timeout=5,
                )
                if resp.status_code == 200:
                    logger.info(f"[Consul] Registered: {self.service_name} ({self.address}:{self.port})")
                else:
                    logger.error(f"[Consul] Register failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.warning(f"[Consul] Register failed (Consul unreachable): {e}")

    async def deregister(self):
        """从 Consul 注销服务"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.put(
                    f"{CONSUL_HTTP_ADDR}/v1/agent/service/deregister/{self.service_id}",
                    timeout=5,
                )
                if resp.status_code == 200:
                    logger.info(f"[Consul] Deregistered: {self.service_name}")
                else:
                    logger.warning(f"[Consul] Deregister response: {resp.status_code}")
        except Exception as e:
            logger.warning(f"[Consul] Deregister failed: {e}")


class ConsulDiscovery:
    """从 Consul 发现健康服务实例"""

    def __init__(self, consul_addr: Optional[str] = None):
        self.consul_addr = consul_addr or CONSUL_HTTP_ADDR
        self._cache: dict = {}
        self._cache_ttl = 10
        self._cache_ts: dict = {}

    async def get_service_url(self, service_name: str) -> Optional[str]:
        """获取某服务的一个健康实例 URL"""
        import time
        now = time.time()
        if service_name in self._cache and (now - self._cache_ts.get(service_name, 0)) < self._cache_ttl:
            return self._cache[service_name]

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.consul_addr}/v1/health/service/{service_name}",
                    params={"passing": "true"},
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
        """同步版本：从 Consul 获取服务地址"""
        import time
        now = time.time()
        if service_name in self._cache and (now - self._cache_ts.get(service_name, 0)) < self._cache_ttl:
            return self._cache[service_name]

        try:
            resp = httpx.get(
                f"{self.consul_addr}/v1/health/service/{service_name}",
                params={"passing": "true"},
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


@asynccontextmanager
async def consul_lifespan(service_name: str, port: int, tags: Optional[List[str]] = None):
    """FastAPI lifespan 集成：自动注册和注销。

    用法:
        @asynccontextmanager
        async def lifespan(app):
            async with consul_lifespan("my-service", 8010, tags=["traefik.enable=true"]):
                yield
    """
    reg = ConsulRegistration(service_name=service_name, port=port, tags=tags)
    await reg.register()
    try:
        yield reg
    finally:
        await reg.deregister()


# 全局 discovery 实例
_discovery: Optional[ConsulDiscovery] = None


def get_discovery() -> ConsulDiscovery:
    global _discovery
    if _discovery is None:
        _discovery = ConsulDiscovery()
    return _discovery
