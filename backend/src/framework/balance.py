"""地址总值查询：balance via CLOB API, position_value via Polymarket /value API"""
import asyncio
import os

import aiohttp

POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon.drpc.org")
USDC_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


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


async def fetch_usdc_balance_rpc(addr: str) -> float:
    try:
        padded = addr.replace("0x", "").lower().zfill(64)
        call_data = "0x70a08231" + padded
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": USDC_CONTRACT, "data": call_data}, "latest"],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                POLYGON_RPC_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    hex_val = result.get("result", "0x0") or "0x0"
                    return int(hex_val, 16) / 1_000_000
    except Exception:
        pass
    return 0.0


def fetch_clob_balance(proxy_wallet: str) -> float:
    from framework.trading.provider import get_client
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

    try:
        client = get_client(proxy_wallet)
    except RuntimeError:
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


async def fetch_public_address_value(addr: str) -> tuple:
    """Fetch balance + position value for any address (no CLOB auth needed)."""
    balance, position_value = await asyncio.gather(
        fetch_usdc_balance_rpc(addr),
        fetch_position_value(addr),
    )
    return balance, position_value
