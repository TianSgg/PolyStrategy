"""ClobClient provider — 由 account_service 启动时注入实现。

framework 内部通过 get_client(proxy_wallet) 获取 ClobClient 实例，
不再直接依赖 account_service。
"""
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from py_clob_client_v2 import ClobClient


class ClientProvider(Protocol):
    def get_or_create_clob_client(self, proxy_wallet: str) -> Optional[ClobClient]: ...
    def get_acc_name(self, proxy_wallet: str) -> str: ...
    def repair_signature_type(self, proxy_wallet: str) -> Optional[int]: ...


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


def repair_client_signature(proxy_wallet: str) -> Optional[int]:
    if _provider is None:
        return None
    return _provider.repair_signature_type(proxy_wallet)


def get_account_name(proxy_wallet: str) -> str:
    if _provider is None:
        return proxy_wallet[:8]
    return _provider.get_acc_name(proxy_wallet)


@dataclass(frozen=True)
class ClobCredentials:
    api_key: str
    api_secret: str
    api_passphrase: str


def get_clob_credentials(proxy_wallet: str) -> ClobCredentials:
    """从已有 ClobClient 提取 CLOB API 凭证，用于 User WS 认证。"""
    client = get_client(proxy_wallet)
    creds = client.creds
    return ClobCredentials(
        api_key=creds.api_key,
        api_secret=creds.api_secret,
        api_passphrase=creds.api_passphrase,
    )
