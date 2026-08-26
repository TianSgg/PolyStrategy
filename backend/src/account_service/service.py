"""AccountService — 账户管理 + ClobClient 池。

职责：
- 账户增删改查（DB 持久化）
- 私钥加解密
- ClobClient 实例缓存（按 proxy_wallet）
- 签名类型自动检测
"""
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds, BalanceAllowanceParams, AssetType, PartialCreateOrderOptions,
    BuilderConfig, OrderType, OrderPayload
)
from py_clob_client_v2.clob_types import OrderArgsV2 as OrderArgs
from py_clob_client_v2.order_builder.constants import BUY, SELL

from account_service.dao import AccountDao
from account_service.internal.crypto import encrypt, decrypt
from eth_keys import keys
from typing import List, Dict, Optional

import pymysql
import requests
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"


def get_proxy_wallet_and_type(eoa_address: str, private_key: str) -> tuple:
    """从 Gamma API 获取代理钱包地址，通过余额查询自动检测签名类型。"""
    try:
        resp = requests.get(f"{GAMMA_API_URL}/public-profile?address={eoa_address}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            proxy_wallet = data.get("proxyWallet", eoa_address)
        else:
            return None, None
    except Exception as e:
        logger.error(f"Failed to get proxy wallet: {e}")
        return None, None

    detected_type = _detect_signature_type(private_key, proxy_wallet)
    return proxy_wallet, detected_type


def _detect_signature_type(private_key: str, proxy_wallet: str) -> int:
    """通过调用 get_balance_allowance 自动检测签名类型。"""
    temp_client = ClobClient("https://clob.polymarket.com", chain_id=137, key=private_key)
    try:
        creds = temp_client.derive_api_key(0)
    except Exception as e:
        logger.warning(f"[Account] derive_api_key failed during type detection: {e}")
        return 2

    for sig_type in [3, 2, 1, 0]:
        try:
            client = ClobClient(
                "https://clob.polymarket.com",
                chain_id=137,
                key=private_key,
                signature_type=sig_type,
                funder=proxy_wallet if sig_type != 0 else None,
            )
            client.set_api_creds(creds)
            result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            balance = int(result.get("balance", "0"))
            allowances = result.get("allowances", {})
            has_allowance = any(int(v) > 0 for v in allowances.values())
            if balance > 0 or has_allowance:
                logger.info(f"[Account] Detected signature_type={sig_type} (balance={balance})")
                return sig_type
        except Exception:
            continue

    logger.info("[Account] All types returned zero balance, defaulting to type 3")
    return 3


class AccountService:
    """账户服务 — 管理账户持久化和 ClobClient 运行时缓存"""

    def __init__(self):
        self._clients: Dict[str, ClobClient] = {}
        self._secure_clients: Dict[str, object] = {}
        self._proxy_to_name: Dict[str, str] = {}
        self._build_proxy_cache()

    def _build_proxy_cache(self):
        accounts = self.get_all_accounts()
        for acc in accounts:
            pw = acc.get("proxy_wallet", "")
            self._proxy_to_name[pw.lower() if pw else pw] = acc.get("name") or ""
        logger.info(f"[Account] loaded {len(accounts)} account names")

    # ==================== 名称缓存 ====================

    def get_acc_name(self, proxy_wallet: str) -> str:
        addr = proxy_wallet.lower()
        if addr not in self._proxy_to_name:
            asyncio.create_task(self._warm_account_cache(addr))
            return proxy_wallet[:8]
        cached = self._proxy_to_name[addr]
        if cached == "__loading__":
            return proxy_wallet[:8]
        return cached

    async def _warm_account_cache(self, addr: str):
        marker = "__loading__"
        if self._proxy_to_name.get(addr) == marker:
            return
        self._proxy_to_name[addr] = marker

        name = await asyncio.to_thread(self._fetch_account_name_from_db, addr)
        if name:
            self._proxy_to_name[addr] = name
            return

        name = await asyncio.to_thread(self._fetch_account_name_from_poly, addr)
        if name:
            await asyncio.to_thread(self._upsert_account_name, addr, name)
            self._proxy_to_name[addr] = name
        else:
            self._proxy_to_name.pop(addr, None)

    def _fetch_account_name_from_db(self, proxy_wallet: str) -> Optional[str]:
        acc = AccountDao.get_by_proxy_wallet(proxy_wallet)
        return acc.get("name") if acc else None

    def _fetch_account_name_from_poly(self, address: str) -> str:
        try:
            resp = requests.get(f"{GAMMA_API_URL}/public-profile?address={address}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("name") or data.get("pseudonym") or ""
        except Exception as e:
            logger.warning(f"[Account] Failed to fetch profile for {address[:8]}...: {e}")
        return ""

    def _upsert_account_name(self, proxy_wallet: str, name: str):
        AccountDao.update_name(proxy_wallet, name)

    # ==================== 账户 CRUD ====================

    def add_account(self, private_key: str, owner_user_id: Optional[int], signature_type: Optional[int] = None, builder_code: Optional[str] = None) -> dict:
        private_key_hex = private_key.replace("0x", "")
        try:
            private_key_bytes = bytes.fromhex(private_key_hex)
        except Exception:
            raise ValueError(f"无效的私钥格式: {private_key[:8]}...")
        if len(private_key_bytes) != 32:
            raise ValueError(f"私钥长度错误，期望 32 字节，实际 {len(private_key_bytes)}")
        key = keys.PrivateKey(private_key_bytes)
        eoa_address = key.public_key.to_checksum_address()

        proxy_wallet, detected_type = get_proxy_wallet_and_type(eoa_address, private_key)
        if not proxy_wallet:
            raise RuntimeError("无法获取代理钱包地址，请检查网络后重试")
        if signature_type is None:
            signature_type = detected_type if detected_type is not None else 3

        proxy_wallet = proxy_wallet.lower()
        name = self._fetch_account_name_from_poly(proxy_wallet) or proxy_wallet[:8] + "..."

        temp_client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=private_key)
        try:
            creds = temp_client.derive_api_key(0)
        except Exception as e:
            raise RuntimeError(f"无法获取 Builder API 凭据: {e}")

        try:
            account_id = AccountDao.insert(
                name=name,
                wallet_address=eoa_address,
                proxy_wallet=proxy_wallet,
                encrypted_private_key=encrypt(private_key),
                builder_api_key=creds.api_key,
                encrypted_builder_secret=encrypt(creds.api_secret),
                encrypted_builder_passphrase=encrypt(creds.api_passphrase),
                builder_code=builder_code,
                owner_user_id=owner_user_id,
                signature_type=signature_type,
            )
        except pymysql.err.IntegrityError:
            raise ValueError(f"钱包 {eoa_address} 已被添加，不能重复归属")
        except Exception as e:
            raise RuntimeError(f"数据库写入失败: {e}")

        client = self._build_clob_client(private_key, creds, proxy_wallet, signature_type, builder_code)
        self._clients[proxy_wallet] = client
        self._proxy_to_name[proxy_wallet] = name

        logger.info(f"[Account] Added: {eoa_address} (proxy: {proxy_wallet})")
        return {"id": account_id, "name": name, "wallet_address": eoa_address, "proxy_wallet": proxy_wallet}

    def get_all_accounts(self, owner_user_id: Optional[int] = None, owner_user_ids: Optional[List[int]] = None) -> List[dict]:
        return AccountDao.get_all(owner_user_id=owner_user_id, owner_user_ids=owner_user_ids)

    def get_account(self, account_id: int) -> Optional[dict]:
        return AccountDao.get_by_id(account_id)

    def get_account_by_proxy_wallet(self, proxy_wallet: str) -> Optional[dict]:
        return AccountDao.get_by_proxy_wallet(proxy_wallet)

    def delete_account(self, proxy_wallet: str) -> bool:
        self._clients.pop(proxy_wallet.lower(), None)
        self._proxy_to_name.pop(proxy_wallet.lower(), None)
        AccountDao.delete(proxy_wallet)
        return True

    def update_builder_code(self, account_id: int, builder_code: str) -> bool:
        ok = AccountDao.update_builder_code(account_id, builder_code)
        if ok:
            acc = AccountDao.get_by_id(account_id)
            if acc and acc.get("proxy_wallet"):
                self.invalidate(acc["proxy_wallet"])
        return ok

    def update_relayer_api_key(self, account_id: int, relayer_api_key: str) -> bool:
        ok = AccountDao.update_relayer_api_key(account_id, relayer_api_key)
        if ok:
            acc = AccountDao.get_by_id(account_id)
            if acc and acc.get("proxy_wallet"):
                self._secure_clients.pop(acc["proxy_wallet"].lower(), None)
        return ok

    def get_account_credentials_by_proxy_wallet(self, proxy_wallet: str) -> Optional[ApiCreds]:
        account = AccountDao.get_by_proxy_wallet(proxy_wallet)
        if not account:
            return None
        try:
            secret = decrypt(account["encrypted_builder_secret"])
            passphrase = decrypt(account["encrypted_builder_passphrase"])
            return ApiCreds(
                api_key=account["builder_api_key"],
                api_secret=secret,
                api_passphrase=passphrase,
            )
        except Exception as e:
            logger.error(f"[Account] Failed to decrypt credentials for {self.get_acc_name(proxy_wallet)}: {e}")
            return None

    def account_belongs_to_user(self, proxy_wallet: str, owner_user_id: int) -> bool:
        return AccountDao.is_owned_by(proxy_wallet, owner_user_id)

    # ==================== ClobClient 缓存 ====================

    def _build_clob_client(self, private_key: str, creds: ApiCreds, proxy_wallet: str, signature_type: int, builder_code: Optional[str] = None) -> ClobClient:
        builder_config = None
        if builder_code:
            builder_config = BuilderConfig(builder_address=proxy_wallet, builder_code=builder_code)
        return ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            creds=creds,
            signature_type=signature_type,
            funder=proxy_wallet,
            builder_config=builder_config,
        )

    def get_or_create_clob_client(self, proxy_wallet: str) -> Optional[ClobClient]:
        addr = proxy_wallet.lower()
        if addr in self._clients:
            return self._clients[addr]

        account = AccountDao.get_by_proxy_wallet(proxy_wallet)
        if not account:
            logger.error(f"[Account] Account with proxy {proxy_wallet} not found")
            return None

        private_key = decrypt(account["encrypted_private_key"])
        if not private_key:
            logger.error(f"[Account] private_key empty after decrypt for account {account['id']}")
            return None

        builder_secret = decrypt(account["encrypted_builder_secret"])
        builder_passphrase = decrypt(account["encrypted_builder_passphrase"])

        try:
            creds = ApiCreds(api_key=account["builder_api_key"], api_secret=builder_secret, api_passphrase=builder_passphrase)
            client = self._build_clob_client(private_key, creds, account["proxy_wallet"], account["signature_type"], account.get("builder_code"))
            self._clients[addr] = client
            return client
        except Exception as e:
            logger.error(f"[Account] Failed to create ClobClient for account {account['id']}: {e}")
            return None

    async def get_secure_client(self, proxy_wallet: str):
        addr = proxy_wallet.lower()
        if addr in self._secure_clients:
            return self._secure_clients[addr]

        account = await asyncio.to_thread(AccountDao.get_by_proxy_wallet, addr)
        if not account:
            return None
        relayer_key = account.get("relayer_api_key")
        if not relayer_key:
            return None
        private_key = decrypt(account["encrypted_private_key"])
        if not private_key:
            return None

        try:
            from polymarket import AsyncSecureClient, RelayerApiKey
            client = await AsyncSecureClient.create(
                private_key=private_key,
                wallet=account["proxy_wallet"],
                api_key=RelayerApiKey(key=relayer_key, address=account["wallet_address"]),
            )
            self._secure_clients[addr] = client
            return client
        except Exception as e:
            logger.error(f"[Account] Failed to create SecureClient for {self.get_acc_name(addr)}: {e}")
            return None

    def invalidate(self, proxy_wallet: str):
        self._clients.pop(proxy_wallet.lower(), None)

    # ==================== 启动验证 ====================

    def _verify_all_accounts(self):
        accounts = AccountDao.get_all()
        for acc in accounts:
            full = AccountDao.get_by_id(acc.get("id"))
            if not full:
                continue
            pk = decrypt(full.get("encrypted_private_key", ""))
            if pk:
                logger.info(f"[Account] ✓ #{full['id']} '{full.get('name', '')}' decrypt OK")
            else:
                logger.error(f"[Account] ✗ #{full['id']} '{full.get('name', '')}' decrypt FAILED")


_account_service: Optional[AccountService] = None


def get_account_service() -> AccountService:
    global _account_service
    if _account_service is None:
        _account_service = AccountService()
        _account_service._verify_all_accounts()
    return _account_service
