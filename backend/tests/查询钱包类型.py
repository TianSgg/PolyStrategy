"""查询 Polymarket 账户的钱包类型和代理钱包地址。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "py-clob-client-v2"))

import requests
from eth_keys import keys
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
PRIVATE_KEY = "0x95c1cbc58d1cd025c779d8a7acb8ded9b854c9845d52f5b9dc7baa8ffc5e8adb"

WALLET_TYPES = {
    0: ("EOA", "外部账户 — 私钥直接控制，无代理钱包"),
    1: ("POLY_PROXY", "Polymarket 代理钱包 — 浏览器注册用户"),
    2: ("GNOSIS_SAFE", "Gnosis Safe 多签钱包"),
    3: ("DEPOSIT_WALLET", "Deposit Wallet — Builder API 创建"),
}


def eoa_address(private_key: str) -> str:
    pk = private_key.removeprefix("0x")
    return keys.PrivateKey(bytes.fromhex(pk)).public_key.to_checksum_address()


def get_proxy_wallet(eoa: str) -> str | None:
    try:
        resp = requests.get(f"{GAMMA}/public-profile?address={eoa}", timeout=10)
        if resp.status_code == 200:
            return resp.json().get("proxyWallet")
    except Exception as e:
        print(f"  查询代理钱包失败: {e}")
    return None


def detect_type(private_key: str, proxy_wallet: str | None) -> tuple[int, dict]:
    temp = ClobClient(CLOB, chain_id=137, key=private_key)
    try:
        creds = temp.derive_api_key(0)
    except Exception as e:
        print(f"  derive_api_key 失败: {e}")
        return -1, {}

    for sig_type in [0, 1, 2, 3]:
        funder = proxy_wallet if sig_type != 0 else None
        try:
            client = ClobClient(
                CLOB, chain_id=137, key=private_key,
                signature_type=sig_type, funder=funder,
            )
            client.set_api_creds(creds)
            result = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            balance = int(result.get("balance", "0"))
            allowances = result.get("allowances", {})
            has_allowance = any(int(v) > 0 for v in allowances.values())
            if balance > 0 or has_allowance:
                return sig_type, result
        except Exception:
            continue

    return -1, {}


def main():
    pk = PRIVATE_KEY
    if not pk:
        print("请在脚本顶部设置 PRIVATE_KEY")
        return
    eoa = eoa_address(pk)
    print(f"EOA 地址:    {eoa}")

    proxy = get_proxy_wallet(eoa)
    print(f"代理钱包:    {proxy or '无 (可能是 EOA 类型)'}")

    print("检测签名类型...")
    sig_type, balance_info = detect_type(pk, proxy)

    if sig_type < 0:
        print("未能检测到有效签名类型 (所有类型余额为零或 API 调用失败)")
        return

    name, desc = WALLET_TYPES.get(sig_type, ("UNKNOWN", "未知类型"))
    print()
    print(f"签名类型:    {sig_type} ({name})")
    print(f"说明:        {desc}")
    print(f"交易钱包:    {proxy if sig_type != 0 else eoa}")

    if balance_info:
        balance_usdc = int(balance_info.get("balance", "0")) / 1e6
        print(f"USDC 余额:   {balance_usdc:.2f}")


if __name__ == "__main__":
    main()
