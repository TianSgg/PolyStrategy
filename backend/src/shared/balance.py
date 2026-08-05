"""地址总值查询：position_value via Polymarket /value API, USDC balance via RPC"""
import asyncio
import os

import aiohttp

POLYGON_HTTP_URL = os.getenv("POLYGON_HTTP_URL", "https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY")
USDC_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


async def _fetch_position_value(session: aiohttp.ClientSession, addr: str):
    try:
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
    return None


async def _fetch_usdc_balance(session: aiohttp.ClientSession, addr: str):
    try:
        padded = addr.replace("0x", "").lower().zfill(64)
        call_data = "0x70a08231" + padded
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": USDC_CONTRACT, "data": call_data}, "latest"]
        }
        async with session.post(
            POLYGON_HTTP_URL, json=payload,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                hex_val = result.get("result", "0x0") or "0x0"
                return int(hex_val, 16) / 1_000_000
    except Exception:
        pass
    return None


async def fetch_address_value(addr: str) -> tuple:
    """异步并发获取某地址的 (usdc_balance, position_value)，失败返回 (None, None)"""
    async with aiohttp.ClientSession() as session:
        balance, position_value = await asyncio.gather(
            _fetch_usdc_balance(session, addr),
            _fetch_position_value(session, addr),
        )
    return balance, position_value
