from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds, BalanceAllowanceParams, AssetType, PartialCreateOrderOptions, BuilderConfig, OrderType, OrderPayload
)
from py_clob_client_v2.clob_types import OrderArgsV2 as OrderArgs
from py_clob_client_v2.order_builder.constants import BUY, SELL

from .dao import AccountDao
from eth_keys import keys
from typing import List, Dict, Optional
from shared.crypto_utils import encrypt, decrypt

import pymysql
import requests
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Data API URL
DATA_API_URL = "https://data-api.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"


def get_proxy_wallet(eoa_address: str) -> str:
    """从 Gamma API 获取代理钱包地址"""
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/public-profile?address={eoa_address}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("proxyWallet", eoa_address)
    except Exception as e:
        logger.error(f"Failed to get proxy wallet: {e}")
    return None


class AccountService:
    """账户服务 - 管理账户持久化和运行时状态"""

    def __init__(self):
        # ClobClient 缓存（按 proxy_wallet 索引）
        self._clients: Dict[str, ClobClient] = {}
        # AsyncSecureClient 缓存（用于 split/merge）
        self._secure_clients: Dict[str, object] = {}
        # proxy_wallet → name 缓存
        self._proxy_to_name: Dict[str, str] = {}
        # 启动时预热缓存
        self._build_proxy_cache()

    def _build_proxy_cache(self):
        """从数据库加载所有 account 的 proxy_wallet → name"""
        accounts = self.get_all_accounts()
        for acc in accounts:
            pw = acc.get("proxy_wallet", "")
            self._proxy_to_name[pw.lower() if pw else pw] = acc.get("name") or ""
        
        logger.info(f"[Account] loaded {len(accounts)} account names")

    def get_acc_name(self, proxy_wallet: str) -> str:
        """同步快速查找：缓存优先，未命中返回截断地址，同时异步构建缓存"""
        addr = proxy_wallet.lower()
        if addr not in self._proxy_to_name:
            asyncio.create_task(self._warm_account_cache(addr))
            return proxy_wallet[:8]
        cached = self._proxy_to_name[addr]
        # loading marker → 当作未命中处理
        if cached == "__loading__":
            return proxy_wallet[:8]
        return cached

    async def _warm_account_cache(self, addr: str):
        """异步填充单个账户名缓存：DB → Gamma API → 落库"""
        marker = "__loading__"
        if self._proxy_to_name.get(addr) == marker:
            return
        self._proxy_to_name[addr] = marker

        name = await asyncio.to_thread(self._fetch_account_name_from_db, addr)
        if name:
            self._proxy_to_name[addr] = name
            return

        # DB 没有，查 Gamma API（同步 HTTP，丢到线程池）
        name = await asyncio.to_thread(self._fetch_account_name_from_poly, addr)
        if name:
            await asyncio.to_thread(self._upsert_account_name, addr, name)
            self._proxy_to_name[addr] = name
        else:
            self._proxy_to_name.pop(addr, None)

    def get_account_by_proxy_wallet(self, proxy_wallet: str) -> Optional[dict]:
        """根据 proxy_wallet 查账户（含加密字段）"""
        return AccountDao.get_by_proxy_wallet(proxy_wallet)

    # ==================== 数据库操作 ====================

    def _verify_all_accounts(self):
        """启动时验证所有账户的加密凭据能否正确解密"""
        accounts = AccountDao.get_all()
        for acc in accounts:
            account_id = acc.get("id")
            name = acc.get("name", "")
            wallet_addr = acc.get("wallet_address", "")
            # 需要解密 encrypted_private_key，需要查完整记录
            full = AccountDao.get_by_id(account_id)
            if not full:
                continue
            enc_pk = full.get("encrypted_private_key", "")
            pk = decrypt(enc_pk)
            if pk:
                logger.info(f"[Account] ✓ Account #{account_id} '{name}' decrypt OK: {wallet_addr}")
            else:
                logger.error(f"[Account] ✗ Account #{account_id} '{name}' decrypt FAILED: {wallet_addr}")

    def _upsert_account_name(self, proxy_wallet: str, name: str):
        """将账户名落库"""
        AccountDao.update_name(proxy_wallet, name)

    def _fetch_account_name_from_db(self, proxy_wallet: str) -> Optional[str]:
        """从 DB 查询账户名"""
        acc = AccountDao.get_by_proxy_wallet(proxy_wallet)
        return acc.get("name") if acc else None

    def _fetch_account_name_from_poly(self, address: str) -> str:
        """从 Gamma API 获取账户显示名，查不到用地址前缀"""
        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/public-profile?address={address}",
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("name") or data.get("pseudonym") or ""
        except Exception as e:
            logger.warning(f"[Account] Failed to fetch profile for {address[:8]}...: {e}")
        return ""

    def add_account(self, private_key: str, owner_user_id: Optional[int], signature_type: int, builder_code: Optional[str] = None) -> dict:
        """添加账户，失败时抛出异常供调用方重试"""
        # 1. 从私钥解析 EOA 地址
        private_key_hex = private_key.replace("0x", "")
        try:
            private_key_bytes = bytes.fromhex(private_key_hex)
        except Exception:
            raise ValueError(f"无效的私钥格式: {private_key[:8]}...")
        if len(private_key_bytes) != 32:
            raise ValueError(f"私钥长度错误，期望 32 字节，实际 {len(private_key_bytes)}")
        key = keys.PrivateKey(private_key_bytes)
        eoa_address = key.public_key.to_checksum_address()

        # 2. 获取代理钱包地址
        proxy_wallet = get_proxy_wallet(eoa_address)
        if not proxy_wallet:
            raise RuntimeError(f"无法获取代理钱包地址，请检查网络后重试")

        proxy_wallet = proxy_wallet.lower()

        # 3. 从 Gamma API 获取账户名
        name = self._fetch_account_name_from_poly(proxy_wallet)
        if not name:
            name = proxy_wallet[:8] + "..."  # fallback to address prefix

        # 4. 创建临时 ClobClient 获取 Builder Creds
        temp_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key
        )
        try:
            creds = temp_client.derive_api_key(0)
        except Exception as e:
            raise RuntimeError(f"无法获取 Builder API 凭据，请检查私钥是否有效后重试: {e}")

        # 5. 保存到数据库
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
        except pymysql.err.IntegrityError as e:
            raise ValueError(f"钱包 {eoa_address} 已被添加，不能重复归属到多个用户") from e
        except Exception as e:
            raise RuntimeError(f"数据库写入失败，请重试: {e}")

        # 6. 缓存 ClobClient
        client = self._build_clob_client(private_key, creds, proxy_wallet, signature_type, builder_code)
        self._clients[proxy_wallet] = client
        self._proxy_to_name[proxy_wallet] = name

        logger.info(f"[Account] Added account: {eoa_address} (proxy: {proxy_wallet})")

        return {"id": account_id, "name": name, "wallet_address": eoa_address, "proxy_wallet": proxy_wallet}

    def get_all_accounts(self, owner_user_id: Optional[int] = None, owner_user_ids: Optional[List[int]] = None) -> List[dict]:
        """获取账户列表（不包含私钥和加密字段）"""
        return AccountDao.get_all(owner_user_id=owner_user_id, owner_user_ids=owner_user_ids)

    def get_account(self, account_id: int) -> Optional[dict]:
        """获取账户详情（含加密字段）"""
        return AccountDao.get_by_id(account_id)

    def get_account_credentials_by_proxy_wallet(self, proxy_wallet: str) -> Optional[ApiCreds]:
        """获取账户 API credentials（按 proxy_wallet 索引），供 User Channel WS 使用"""
        account = AccountDao.get_by_proxy_wallet(proxy_wallet)
        if not account:
            return None
        try:
            secret = decrypt(account["encrypted_builder_secret"])
            passphrase = decrypt(account["encrypted_builder_passphrase"])
            return ApiCreds(
                api_key=account["builder_api_key"],
                api_secret=secret,
                api_passphrase=passphrase
            )
        except Exception as e:
            logger.error(f"[Account] Failed to decrypt credentials for {self.get_acc_name(proxy_wallet)}: {e}")
            return None

    def delete_account(self, proxy_wallet: str) -> bool:
        """删除账户（根据 proxy_wallet）"""
        # 清理 ClobClient 缓存（按 proxy_wallet 索引）
        self._clients.pop(proxy_wallet.lower(), None)
        # 清理 name 缓存
        self._proxy_to_name.pop(proxy_wallet.lower(), None)
        # 按 proxy_wallet 删除
        AccountDao.delete(proxy_wallet)
        return True

    def account_belongs_to_user(self, proxy_wallet: str, owner_user_id: int) -> bool:
        return AccountDao.is_owned_by(proxy_wallet, owner_user_id)

    # ==================== ClobClient 操作 ====================

    def _build_clob_client(
        self,
        private_key: str,
        creds: ApiCreds,
        proxy_wallet: str,
        signature_type: int,
        builder_code: Optional[str] = None,
    ) -> ClobClient:
        """构建带 BuilderConfig 的 ClobClient"""
        builder_config = None
        if builder_code:
            builder_config = BuilderConfig(
                builder_address=proxy_wallet,
                builder_code=builder_code,
            )
        return ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            creds=creds,
            signature_type=signature_type,
            funder=proxy_wallet,
            builder_config=builder_config,
        )

    def _get_or_create_clob_client_by_id(self, account_id: int) -> Optional[ClobClient]:
        """内部方法：按 account_id 获取 ClobClient"""
        acc = AccountDao.get_by_id(account_id)
        if not acc or not acc.get("proxy_wallet"):
            return None
        return self.get_or_create_clob_client(acc["proxy_wallet"])

    def get_or_create_clob_client(self, proxy_wallet: str) -> Optional[ClobClient]:
        """获取账户的 ClobClient"""
        addr = proxy_wallet.lower()
        if addr in self._clients:
            return self._clients[addr]

        account = AccountDao.get_by_proxy_wallet(proxy_wallet)
        if not account:
            logger.error(f"[Account] Account with proxy {proxy_wallet} not found")
            return None

        private_key = decrypt(account["encrypted_private_key"])
        if not private_key:
            logger.error(f"[Account] Account {account['id']} private_key is empty after decrypt, encrypted value: {account['encrypted_private_key'][:50]}...")
            return None

        builder_secret = decrypt(account["encrypted_builder_secret"])
        builder_passphrase = decrypt(account["encrypted_builder_passphrase"])

        try:
            creds = ApiCreds(
                api_key=account["builder_api_key"],
                api_secret=builder_secret,
                api_passphrase=builder_passphrase
            )
            client = self._build_clob_client(private_key, creds, account["proxy_wallet"], account["signature_type"], account.get("builder_code"))
            self._clients[addr] = client
            return client
        except Exception as e:
            logger.error(f"[Account] Failed to create ClobClient for account {account['id']}: {e}")
            return None

    def update_builder_code(self, account_id: int, builder_code: str) -> bool:
        """更新账户的 builder_code，并使 ClobClient 缓存失效"""
        ok = AccountDao.update_builder_code(account_id, builder_code)
        if ok:
            # 用 account_id 查 proxy_wallet 再清理缓存
            acc = AccountDao.get_by_id(account_id)
            if acc and acc.get("proxy_wallet"):
                self.invalidate(acc["proxy_wallet"])
        return ok

    def update_relayer_api_key(self, account_id: int, relayer_api_key: str) -> bool:
        """更新账户的 relayer_api_key"""
        ok = AccountDao.update_relayer_api_key(account_id, relayer_api_key)
        if ok:
            acc = AccountDao.get_by_id(account_id)
            if acc and acc.get("proxy_wallet"):
                self._secure_clients.pop(acc["proxy_wallet"].lower(), None)
        return ok

    async def get_secure_client(self, proxy_wallet: str):
        """获取 AsyncSecureClient（用于 split/merge），缓存复用。无 relayer_api_key 时返回 None。"""
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
                api_key=RelayerApiKey(
                    key=relayer_key,
                    address=account["wallet_address"],
                ),
            )
            self._secure_clients[addr] = client
            return client
        except Exception as e:
            logger.error(f"[Account] Failed to create SecureClient for {self.get_acc_name(addr)}: {e}")
            return None


    # ==================== 下单操作 ====================
    DEFAULT_GTD_EXPIRATION_SEC = 30 * 60

    def place_limit_order(
        self,
        f_addr: str,
        token_id: str,
        side: str,
        size: float,
        price: float,
        tick_size: str=None,
        neg_risk: bool=None,
        gtd_expiration_sec: int=None,
    ) -> Optional[dict]:
        """下 GTD 限价单，返回订单结果或 None"""
        client = self.get_or_create_clob_client(f_addr)
        if not client:
            raise RuntimeError(f"No client for {self.get_acc_name(f_addr)}")
        expiration = int(time.time()) + (gtd_expiration_sec or self.DEFAULT_GTD_EXPIRATION_SEC)
        result = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                side=BUY if side == "BUY" else SELL,
                size=size,
                price=price,
                expiration=expiration,
            ),
            PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.GTD,
        )
        return result
 

    def cancel_order(self, f_addr: str, order_id: str) -> dict:
        """取消指定订单，返回 cancel 响应"""
        client = self.get_or_create_clob_client(f_addr)
        if not client:
            raise RuntimeError(f"No client for {self.get_acc_name(f_addr)}")
        return client.cancel_order(OrderPayload(orderID=order_id))

    # ==================== 缓存管理 ====================

    def invalidate(self, proxy_wallet: str):
        """清除账户 ClobClient 缓存"""
        addr = proxy_wallet.lower()
        if addr in self._clients:
            del self._clients[addr]


# 全局服务实例（供其他模块使用）
_account_service: Optional['AccountService'] = None


def get_account_service() -> 'AccountService':
    """获取或创建账户服务实例"""
    global _account_service
    if _account_service is None:
        _account_service = AccountService()
        # 启动时验证所有账户解密
        _account_service._verify_all_accounts()
    return _account_service
