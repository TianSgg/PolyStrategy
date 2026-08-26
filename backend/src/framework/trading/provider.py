"""ClobClient provider — 由 account_service 启动时注入实现。

framework 内部通过 get_client(proxy_wallet) 获取 ClobClient 实例，
不再直接依赖 account_service。
"""
from typing import Callable, Optional, Protocol

from py_clob_client_v2 import ClobClient


class ClientProvider(Protocol):
    def get_or_create_clob_client(self, proxy_wallet: str) -> Optional[ClobClient]: ...
    def get_acc_name(self, proxy_wallet: str) -> str: ...


_provider: Optional[ClientProvider] = None


def set_client_provider(provider: ClientProvider) -> None:
    global _provider
    _provider = provider


def get_client(proxy_wallet: str) -> ClobClient:
    if _provider is None:
        raise RuntimeError("ClientProvider not initialized — call set_client_provider() at startup")
    client = _provider.get_or_create_clob_client(proxy_wallet)
    if not client:
        name = _provider.get_acc_name(proxy_wallet)
        raise RuntimeError(f"No client for {name}")
    return client


def get_account_name(proxy_wallet: str) -> str:
    if _provider is None:
        return proxy_wallet[:8]
    return _provider.get_acc_name(proxy_wallet)
