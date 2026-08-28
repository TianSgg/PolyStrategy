"""FastAPI lifespan 集成：自动注册和注销 Consul 服务。"""
from contextlib import asynccontextmanager
from typing import List, Optional

from .registration import ConsulRegistration


@asynccontextmanager
async def consul_lifespan(service_name: str, port: int, tags: Optional[List[str]] = None):
    reg = ConsulRegistration(service_name=service_name, port=port, tags=tags)
    await reg.register()
    await reg.start_re_register()
    try:
        yield reg
    finally:
        await reg.deregister()
