"""地址总值查询：balance via CLOB API, position_value via Polymarket /value API"""
import asyncio

import aiohttp


async def fetch_position_value(addr: str) -> float:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://data-api.polymarket.com/value?user={addr}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        return float(data[0].get("value", 0))
                    return 0.0
    except Exception:
        pass
    return 0.0


def fetch_clob_balance(proxy_wallet: str) -> float:
    from account_service.service import get_account_service
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

    service = get_account_service()
    client = service.get_or_create_clob_client(proxy_wallet)
    if not client:
        return 0.0
    try:
        result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(result.get("balance", "0")) / 1_000_000
    except Exception:
        return 0.0


async def fetch_address_value(addr: str) -> tuple:
    balance = await asyncio.to_thread(fetch_clob_balance, addr)
    position_value = await fetch_position_value(addr)
    return balance, position_value
