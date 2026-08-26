"""Consul 服务注册：启动时注册，关闭时注销。"""
import asyncio
import logging
import os
import socket
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

CONSUL_HTTP_ADDR = os.getenv("CONSUL_HTTP_ADDR", "http://localhost:8500")


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class ConsulRegistration:
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
