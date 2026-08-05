"""链上 Convert 事件监听（支持多 leader）"""
import asyncio
import json
import logging
import os
from collections import defaultdict
from typing import Dict, List, Optional, Set

import time
import aiohttp

import websockets

logger = logging.getLogger(__name__)

# ==================== 常量 ====================

POLYGON_WS_URL = os.getenv("POLYGON_WS_URL", "wss://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY")
POLYGON_HTTP_URL = os.getenv("POLYGON_HTTP_URL", "https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY")
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ace5EA0476045"

# ERC20/ERC1155 转账相关常量
USDC_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
ERC1155_CONTRACT = "0x4d97dcd97ec945f40cF65F87097ace5EA0476045"
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ERC1155_TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
ERC1155_TRANSFER_BATCH_TOPIC = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"

POSITIONS_CONVERTED_TOPIC = "0xb03d19dddbc72a87e735ff0ea3b57bef133ebe44e1894284916a84044deb367e"
POSITION_SPLIT_TOPIC = "0x2e6bb91f8cbcda0c93623c54d0403a43514fabc40084ec96b6d5379a74786298"
POSITIONS_MERGE_TOPIC = "0x6f13ca62553fcc2bcd2372180a43949c1e4cebba603901ede2f4e14f36b282ca"

GAMMA_API_URL = "https://gamma-api.polymarket.com"


# ==================== 工具函数 ====================

def format_address_to_topic(address: str) -> str:
    """将地址转换为事件主题格式 (32字节 padding)"""
    addr_hex = address.replace("0x", "").lower()
    return "0x" + addr_hex.zfill(64)


def parse_hex_to_int(hex_str: str) -> int:
    """解析 hex 字符串为整数"""
    if not hex_str or hex_str == "0x":
        return 0
    hex_str = hex_str[2:] if hex_str.startswith("0x") else hex_str
    return int(hex_str, 16)


def topic_to_address(topic: str) -> str:
    """从 topic 提取地址（最后 40 字符）"""
    return "0x" + topic[-40:]


# ==================== WebSocket RPC 客户端 ====================

class WebSocketRPC:
    """WebSocket JSON-RPC 客户端"""

    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.request_id = 0

    def connected(self) -> bool:
        """检查连接是否存活"""
        return self.ws is not None and self.ws.state.name == "OPEN"

    async def connect(self):
        self.ws = await websockets.connect(self.url, open_timeout=10)
        logger.info(f"[ChainMonitor] Connected to {self.url}")

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def send_request(self, method: str, params: list = None) -> int:
        """发送请求（不等待响应），返回 request_id"""
        if not self.connected():
            raise ConnectionError("WebSocket not connected")
        if params is None:
            params = []
        self.request_id += 1
        request_id = self.request_id
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params
        }
        logger.debug(f"[ChainMonitor] Sending request: method={method}, id={request_id}, params={json.dumps(params)[:200]}")
        await self.ws.send(json.dumps(request))
        return request_id

    async def eth_unsubscribe(self, subscription_id: str):
        """取消订阅（发完不等）"""
        if not self.connected():
            return
        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "eth_unsubscribe",
            "params": [subscription_id]
        }
        await self.ws.send(json.dumps(request))


# ==================== 链上事件监听器 ====================

class CopyTradingChainMonitor:
    """
    多 leader 的链上 Convert + Transfer 事件监听器

    使用 Polygon WebSocket 订阅，每个 leader 独立订阅：
    - PositionsConverted（NegRisk Adapter）
    - USDC ERC20 Transfer（from/to）
    - ERC1155 TransferSingle（from/to）
    - ERC1155 TransferBatch（from/to）
    """

    def __init__(self, ws_url: str = POLYGON_WS_URL):
        self.ws_url = ws_url
        self.rpc = WebSocketRPC(ws_url)
        self._running = False
        # leader 地址小写 → [subscription_id, ...]
        self._leader_subs: Dict[str, List[str]] = {}
        # subscription_id → leader 地址
        self._sub_to_leader: Dict[str, str] = {}
        # follower 地址小写 → [subscription_id, ...]
        self._follower_subs: Dict[str, List[str]] = {}
        # leader 地址 → [request_id, ...]（等待订阅确认）
        self._pending_subs: Dict[str, List[int]] = {}
        # request_id → leader 地址（反向索引）
        self._pending_subs_rev: Dict[int, str] = {}
        # 已处理的 tx_hash 集合（链上事件去重）
        self._processed_txs: Set[str] = set()
        # HTTP RPC URL（用于获取 receipt）
        self._rpc_http_url = POLYGON_HTTP_URL
        # Gamma API URL（用于查询 event_slug）
        self._gamma_api_url = GAMMA_API_URL
        # service 引用（惰性导入，避免循环依赖）
        self.service = None

    def add_leader(self, address: str):
        """添加 leader 订阅（运行时追加到已有连接）"""
        addr = address.lower()
        if addr in self._leader_subs:
            return
        self._leader_subs[addr] = []
        # 如果已在运行中，立即创建订阅
        if self._running and self.rpc.connected():
            asyncio.create_task(self._subscribe_leader(addr))

    def remove_leader(self, address: str):
        """移除 leader 订阅"""
        addr = address.lower()
        if addr not in self._leader_subs:
            return
        logger.info(f"[ChainMonitor] Removing leader {addr[:10]} with {len(self._leader_subs[addr])} subscriptions")
        for sub_id in self._leader_subs[addr]:
            asyncio.create_task(self.rpc.eth_unsubscribe(sub_id))
            self._sub_to_leader.pop(sub_id, None)
        self._leader_subs.pop(addr, None)
        # 清理 pending 记录
        for rid in self._pending_subs.pop(addr, []):
            self._pending_subs_rev.pop(rid, None)

    def add_follower(self, address: str):
        """添加 follower 订阅（可扩展：当前无订阅事件）"""
        addr = address.lower()
        if addr in self._follower_subs:
            return
        self._follower_subs[addr] = []
        if self._running and self.rpc.connected():
            asyncio.create_task(self._subscribe_follower(addr))

    def remove_follower(self, address: str):
        """移除 follower 订阅"""
        addr = address.lower()
        if addr not in self._follower_subs:
            return
        logger.info(f"[ChainMonitor] Removing follower {addr[:10]} with {len(self._follower_subs[addr])} subscriptions")
        for sub_id in self._follower_subs[addr]:
            asyncio.create_task(self.rpc.eth_unsubscribe(sub_id))
        self._follower_subs.pop(addr, None)
        # 清理 pending 记录
        for rid in self._pending_subs.pop(addr, []):
            self._pending_subs_rev.pop(rid, None)

    async def _subscribe_follower(self, addr: str):
        """为单个 follower 创建事件订阅（按需扩展订阅列表）"""
        # follower_topic = format_address_to_topic(addr)
        subscriptions = [
            # 按需添加 follower 订阅，例如：
            # {"address": ERC1155_CONTRACT.lower(), "topics": [ERC1155_TRANSFER_SINGLE_TOPIC, None, follower_topic]},
        ]
        for filter_params in subscriptions:
            request_id = await self.rpc.send_request("eth_subscribe", ["logs", filter_params])
            self._pending_subs.setdefault(addr, []).append(request_id)
            self._pending_subs_rev[request_id] = ("follower", addr)

    async def run(self):
        """启动监听（长期运行，指数退避重连：1s → 2s → 4s → ... → 60s）"""
        self._running = True
        delay = 1

        while self._running:
            try:
                await self.rpc.connect()

                # 初始化所有 leader 的订阅
                for addr in list(self._leader_subs.keys()):
                    await self._subscribe_leader(addr)
                # 初始化所有 follower 的订阅
                for addr in list(self._follower_subs.keys()):
                    await self._subscribe_follower(addr)

                delay = 1  # 连接成功重置退避
                logger.info(f"[ChainMonitor] Started, tracking {len(self._leader_subs)} leaders, {len(self._follower_subs)} followers")

                async for message in self.rpc.ws:
                    if not self._running:
                        break
                    try:
                        data = json.loads(message)
                        await self._handle_message(data)
                    except json.JSONDecodeError:
                        logger.warning(f"[ChainMonitor] Invalid JSON: {message[:100]}")
                    except Exception as e:
                        logger.error(f"[ChainMonitor] Error handling message: {e}")

            except asyncio.CancelledError:
                logger.info("[ChainMonitor] Cancelled, exiting")
                break
            except Exception as e:
                logger.error(f"[ChainMonitor] Connection error: {e}")

            # 重连前清空所有旧订阅 ID（已失效，避免重复订阅），但保留地址 key 作为期望订阅集合。
            # 注意：eth_unsubscribe 是 fire-and-forget，但 webrtc 的 _processed_txs 去重能挡住在此期间收到的重复消息
            for addr, sub_ids in self._leader_subs.items():
                for sid in sub_ids:
                    asyncio.create_task(self.rpc.eth_unsubscribe(sid))
                    logger.debug(f"[ChainMonitor] Unsubbing leader sub: {sid[:10]}")
                self._leader_subs[addr] = []
            self._sub_to_leader.clear()

            for addr, sub_ids in self._follower_subs.items():
                for sid in sub_ids:
                    asyncio.create_task(self.rpc.eth_unsubscribe(sid))
                    logger.debug(f"[ChainMonitor] Unsubbing follower sub: {sid[:10]}")
                self._follower_subs[addr] = []

            # 清空 pending（重连后这些 request_id 已无效）
            self._pending_subs.clear()
            self._pending_subs_rev.clear()

            if self._running:
                logger.info(f"[ChainMonitor] Reconnecting in {delay}s (exp backoff)")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

        self._running = False
        await self.rpc.close()
        logger.info("[ChainMonitor] Connection loop ended")

    def stop(self):
        self._running = False

    async def _subscribe_leader(self, addr: str):
        """为单个 leader 创建多个事件订阅"""
        leader_topic = format_address_to_topic(addr)
        subscriptions = [
            # PositionsConverted（NegRisk Adapter）— 按地址过滤
            # {"address": NEG_RISK_ADAPTER.lower(), "topics": [POSITIONS_CONVERTED_TOPIC, leader_topic]},

            # # USDC ERC20 Transfer — from=leader（leader 付出 USDC，买入 token）
            # {"address": USDC_CONTRACT.lower(), "topics": [ERC20_TRANSFER_TOPIC, leader_topic]},
            # # USDC ERC20 Transfer — to=leader（leader 收到 USDC，卖出 token）
            # {"address": USDC_CONTRACT.lower(), "topics": [ERC20_TRANSFER_TOPIC, None, leader_topic]},

            # # ERC1155 TransferSingle — from=leader（leader 付出 token）
            # {"address": ERC1155_CONTRACT.lower(), "topics": [ERC1155_TRANSFER_SINGLE_TOPIC, None, leader_topic]},
            # # ERC1155 TransferSingle — to=leader（leader 收到 token）
            # {"address": ERC1155_CONTRACT.lower(), "topics": [ERC1155_TRANSFER_SINGLE_TOPIC, None, None, leader_topic]},

            # # ERC1155 TransferBatch — from=leader
            # {"address": ERC1155_CONTRACT.lower(), "topics": [ERC1155_TRANSFER_BATCH_TOPIC, None, leader_topic]},
            # # ERC1155 TransferBatch — to=leader
            # {"address": ERC1155_CONTRACT.lower(), "topics": [ERC1155_TRANSFER_BATCH_TOPIC, None, None, leader_topic]},
        ]
        for filter_params in subscriptions:
            request_id = await self.rpc.send_request("eth_subscribe", ["logs", filter_params])
            self._pending_subs.setdefault(addr, []).append(request_id)
            self._pending_subs_rev[request_id] = addr

    async def _handle_message(self, data: dict):
        """处理所有消息：订阅确认 或 订阅通知"""
        # 订阅确认（JSON-RPC 响应，有 id 但无 params）
        if "id" in data and "params" not in data:
            request_id = data["id"]
            rev = self._pending_subs_rev.pop(request_id, None)
            if rev:
                # rev 可以是 str (leader) 或 tuple ("follower", addr)
                if isinstance(rev, tuple):
                    sub_type, addr = rev
                else:
                    sub_type, addr = "leader", rev
                self._pending_subs.get(addr, []).remove(request_id)
            sub_id = data.get("result")
            if rev and sub_id:
                if sub_type == "leader":
                    self._leader_subs.setdefault(addr, []).append(sub_id)
                    self._sub_to_leader[sub_id] = addr
                else:
                    self._follower_subs.setdefault(addr, []).append(sub_id)
                pending = self._pending_subs.get(addr, [])
                logger.debug(f"[ChainMonitor] Subscribed {addr[:10]} ({sub_type} confirmed), pending: {len(pending)}")
            return

        # 订阅通知消息
        log = data["params"].get("result", {})
        topics = log.get("topics", [])
        if not topics:
            return

        topic0 = topics[0]
        tx_hash = log.get('transactionHash')
        if tx_hash in self._processed_txs:
            # logger.debug(f"[ChainMonitor] Transfer tx {tx_hash[:10]} already processed, skipping")
            return
        self._processed_txs.add(tx_hash)

        logger.debug(f"[ChainMonitor] Received event: topic0={topic0[:10]}, tx={log.get('transactionHash', 'unknown')[:10]}")

        if topic0 == POSITIONS_CONVERTED_TOPIC:
            event = self._parse_positions_converted(log, topics)
            logger.info(f"[ChainMonitor] Parsed PositionsConverted: user={event.get('user', '')[:10]}, amount={event.get('amount_decimal')}")

            # 直接调用 service.process_convert
            if not self.service:
                from .service import get_copy_trading_service
                self.service = get_copy_trading_service()
            asyncio.create_task(self.service.process_convert(event))

        elif topic0 in (ERC20_TRANSFER_TOPIC, ERC1155_TRANSFER_SINGLE_TOPIC, ERC1155_TRANSFER_BATCH_TOPIC):
            subscription_id = data.get("params", {}).get("subscription", "")
            asyncio.create_task(self._handle_transfer_event(log, topics, subscription_id))

    # ==================== Transfer 事件处理 ====================

    async def _handle_transfer_event(self, log: dict, topics: list, subscription_id: str = ""):
        """处理 Transfer 事件：获取 receipt → 解析 → 转换为 RTDS payload → 调用 process_signal"""
        tx_hash = log.get("transactionHash", "")
        if not tx_hash:
            logger.debug(f"[ChainMonitor] Transfer: empty tx_hash, skipping")
            return

        # 找到对应的 leader 地址
        # USDC ERC20（有地址过滤）：从 _sub_to_leader 查 subscription_id
        # ERC1155（有地址过滤，topic[2]/topic[3]）：从 _sub_to_leader 查 subscription_id
        addr = None
        if subscription_id and subscription_id in self._sub_to_leader:
            addr = self._sub_to_leader[subscription_id]
        if not addr and topics and topics[0] == ERC20_TRANSFER_TOPIC:
            # ERC20 通知里也尝试从 topics 提取地址
            addr = self._find_leader_from_topics(topics)

        if not addr:
            logger.debug(f"[ChainMonitor] Transfer: cannot find leader addr for tx={tx_hash[:10]}, sub={subscription_id[:10] if subscription_id else 'none'}")
            return

        logger.debug(f"[ChainMonitor] Transfer: addr={addr[:10]}, tx={tx_hash[:10]}, topic={topics[0][:10] if topics else 'none'}")

        erc20 = []
        erc1155 = []
        payload = None
        try:
            # 1. 获取完整 receipt
            receipt = await self._fetch_receipt(tx_hash)
            if not receipt:
                logger.debug(f"[ChainMonitor] Transfer: receipt empty for tx={tx_hash[:10]}")
                return

            # 2. 解析 transfers
            erc20, erc1155 = self._parse_receipt_transfers(receipt.get("logs", []))
            logger.debug(f"[ChainMonitor] Transfer: parsed erc20={len(erc20)} erc1155={len(erc1155)} for tx={tx_hash[:10]}")

            if not erc20 and not erc1155:
                logger.debug(f"[ChainMonitor] Transfer: no relevant transfers for tx={tx_hash[:10]}")
                return

            # 3. 还原 RTDS 格式 payload
            payload = await self._build_rtds_payload(addr, erc20, erc1155, tx_hash)
            if not payload:
                logger.debug(f"[ChainMonitor] Transfer: payload build failed for tx={tx_hash[:10]}")
                return
            payload["source"] = "chain"
            payload["proxyWallet"] = addr  # chain 信号需要手动填入 leader 地址

            logger.debug(f"[ChainMonitor] Transfer signal: {addr[:10]}, {payload['side']} {payload['size']} @ {payload['price']}, asset={payload.get('asset', '')[:10]}, tx={tx_hash[:10]}")

            # 4. 直接调用 process_signal（去重由其内部的 placed_order_hashes 处理）
            if self.service is None:
                from .service import get_copy_trading_service
                self.service = get_copy_trading_service()
            await self.service.process_signal(payload)
        except Exception as e:
            logger.exception(
                f"[ChainMonitor] Transfer handle error: {e}; "
                f"context={self._summarize_transfer_context(addr, tx_hash, erc20, erc1155, payload)}"
            )

    async def _fetch_receipt(self, tx_hash: str) -> Optional[dict]:
        """通过 HTTP JSON-RPC 获取 transaction receipt，带快速重试"""
        import aiohttp
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
            "id": 1
        }
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self._rpc_http_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        result = await resp.json()
                        receipt = result.get("result")
                        if receipt:
                            logger.debug(f"[ChainMonitor] Receipt fetched for tx={tx_hash[:10]} (attempt {attempt + 1})")
                            return receipt
                        if attempt < 2:
                            logger.debug(f"[ChainMonitor] Receipt not ready for tx={tx_hash[:10]}, retry {attempt + 1}/3")
                            await asyncio.sleep(0.5)
                        else:
                            logger.debug(f"[ChainMonitor] Receipt not ready after 3 attempts for tx={tx_hash[:10]}")
                            return None
            except Exception as e:
                if attempt < 2:
                    logger.debug(f"[ChainMonitor] Receipt fetch error {tx_hash[:10]}, retry {attempt + 1}/3: {e}")
                    await asyncio.sleep(0.5)
                else:
                    logger.error(f"[ChainMonitor] Failed to fetch receipt {tx_hash[:10]}: {e}")
                    return None
        return None

    def _find_leader_from_topics(self, topics: list) -> Optional[str]:
        """从 topics 中查找已订阅的 leader 地址"""
        for addr in self._leader_subs.keys():
            addr_lower = addr.lower()
            addr_topic = format_address_to_topic(addr_lower)
            # 检查 leader 是否出现在任何 topic 位置
            for t in topics:
                if t and t.lower() == addr_topic:
                    return addr_lower
        return None

    def _summarize_transfer_context(
        self,
        wallet_addr: str,
        tx_hash: str,
        erc20_transfers: list,
        erc1155_transfers: list,
        payload: Optional[dict] = None,
    ) -> dict:
        """汇总 transfer 解析上下文，便于定位 price/size/asset 异常。"""
        wallet = (wallet_addr or "").lower()
        usdc_out = sum(t.get("value", 0) for t in erc20_transfers if t.get("from", "").lower() == wallet)
        usdc_in = sum(t.get("value", 0) for t in erc20_transfers if t.get("to", "").lower() == wallet)

        in_by_id = defaultdict(int)
        out_by_id = defaultdict(int)
        for t in erc1155_transfers:
            token_id = t.get("token_id")
            if t.get("to", "").lower() == wallet:
                in_by_id[token_id] += t.get("value", 0)
            if t.get("from", "").lower() == wallet:
                out_by_id[token_id] += t.get("value", 0)

        best_in = max(in_by_id.items(), key=lambda x: x[1], default=(None, 0))
        best_out = max(out_by_id.items(), key=lambda x: x[1], default=(None, 0))
        return {
            "tx": tx_hash,
            "wallet": wallet_addr,
            "payload": payload,
            "usdc_in_raw": usdc_in,
            "usdc_out_raw": usdc_out,
            "best_in": best_in,
            "best_out": best_out,
            "erc20_count": len(erc20_transfers),
            "erc1155_count": len(erc1155_transfers),
            "erc20_sample": erc20_transfers[:3],
            "erc1155_sample": erc1155_transfers[:3],
        }

    def _parse_receipt_transfers(self, logs: list) -> tuple:
        """从 receipt logs 中提取 ERC20 和 ERC1155 transfers"""
        erc20 = []
        erc1155 = []
        for log in logs:
            addr = log.get("address", "").lower()
            topics = log.get("topics", [])
            if not topics:
                continue
            t0 = topics[0].lower()
            data = log.get("data", "0x")

            if addr == USDC_CONTRACT.lower() and t0 == ERC20_TRANSFER_TOPIC and len(topics) >= 3:
                erc20.append({
                    "from": topic_to_address(topics[1]),
                    "to": topic_to_address(topics[2]),
                    "value": parse_hex_to_int(data)
                })
            elif t0 == ERC1155_TRANSFER_SINGLE_TOPIC and len(topics) >= 4:
                erc1155.append(self._parse_erc1155_single(data, topics))
            elif t0 == ERC1155_TRANSFER_BATCH_TOPIC and len(topics) >= 4:
                erc1155.extend(self._parse_erc1155_batch(data, topics))
        return erc20, erc1155

    def _parse_erc1155_single(self, data: str, topics: list) -> dict:
        """解析 ERC1155 TransferSingle"""
        d = data[2:] if data.startswith("0x") else data
        token_id = parse_hex_to_int("0x" + d[0:64])
        value = parse_hex_to_int("0x" + d[64:128])
        return {
            "from": topic_to_address(topics[2]),
            "to": topic_to_address(topics[3]),
            "token_id": token_id,
            "value": value
        }

    def _parse_erc1155_batch(self, data: str, topics: list) -> list:
        """解析 ERC1155 TransferBatch"""
        d = data[2:] if data.startswith("0x") else data
        from_addr = topic_to_address(topics[2])
        to_addr = topic_to_address(topics[3])
        off_ids = int(d[0:64], 16)
        off_vals = int(d[64:128], 16)
        n_ids = int(d[off_ids * 2:off_ids * 2 + 64], 16)
        result = []
        for i in range(n_ids):
            id_offset = off_ids * 2 + 64 + i * 64
            val_offset = off_vals * 2 + 64 + i * 64
            token_id = int(d[id_offset:id_offset + 64], 16)
            value = int(d[val_offset:val_offset + 64], 16)
            result.append({"from": from_addr, "to": to_addr, "token_id": token_id, "value": value})
        return result

    async def _build_rtds_payload(self, wallet_addr: str, erc20_transfers: list, erc1155_transfers: list, tx_hash: str) -> Optional[dict]:
        """
        从 transfers 还原 RTDS 格式 payload：
        - BUY: 收到 ERC1155 token + 付出 USDC
        - SELL: 付出 ERC1155 token + 收到 USDC
        - 找到最大流入/流出 token_id 作为 asset
        - price = usdc_amount / token_amount
        """
        wallet = wallet_addr.lower()

        usdc_out = sum(t["value"] for t in erc20_transfers if t["from"].lower() == wallet)
        usdc_in = sum(t["value"] for t in erc20_transfers if t["to"].lower() == wallet)

        in_by_id = defaultdict(int)
        out_by_id = defaultdict(int)
        for t in erc1155_transfers:
            if t["to"].lower() == wallet:
                in_by_id[t["token_id"]] += t["value"]
            if t["from"].lower() == wallet:
                out_by_id[t["token_id"]] += t["value"]

        best_in_id, best_in_val = max(in_by_id.items(), key=lambda x: x[1], default=(None, 0))
        best_out_id, best_out_val = max(out_by_id.items(), key=lambda x: x[1], default=(None, 0))

        logger.debug(
            f"[ChainMonitor] _build_payload inputs: wallet={wallet[:10]}, "
            f"usdc_out={usdc_out}, usdc_in={usdc_in}, "
            f"best_in=({str(best_in_id)[:10] if best_in_id is not None else None}, {best_in_val}), "
            f"best_out=({str(best_out_id)[:10] if best_out_id is not None else None}, {best_out_val}), "
            f"tx={tx_hash[:10]}"
        )

        side = None
        asset_id = None
        size_raw = 0
        usdc_raw = 0

        if best_in_id is not None and best_in_val > 0 and usdc_out > 0:
            side = "BUY"
            asset_id = str(best_in_id)
            size_raw = best_in_val
            usdc_raw = usdc_out
        elif best_out_id is not None and best_out_val > 0 and usdc_in > 0:
            side = "SELL"
            asset_id = str(best_out_id)
            size_raw = best_out_val
            usdc_raw = usdc_in

        if not side:
            logger.debug(f"[ChainMonitor] Cannot determine side: tx={tx_hash[:10]}")
            return None

        size = size_raw / 1e6
        price = (usdc_raw / 1e6) / size if size > 0 else 0
        logger.debug(
            f"[ChainMonitor] _build_payload result: side={side}, asset={asset_id[:10]}, "
            f"size_raw={size_raw}, usdc_raw={usdc_raw}, size={size}, price={price}, tx={tx_hash[:10]}"
        )

        return {
            "side": side,
            "size": size,
            "price": round(price, 5),
            "asset": asset_id,
            "transactionHash": tx_hash
        }

    # ==================== Convert 事件解析 ====================

    def _parse_positions_converted(self, log: dict, topics: list) -> dict:
        """解析 PositionsConverted 事件"""
        return {
            "event_type": "PositionsConverted",
            "user": "0x" + topics[1][-40:],
            "neg_risk_market_id": topics[2],
            "index_set": parse_hex_to_int(topics[3]),
            "amount_decimal": parse_hex_to_int(log.get("data", "0x")) / 1e6,
            "transaction_hash": log.get("transactionHash", ""),
            "block_number": parse_hex_to_int(log.get("blockNumber", "0x0")),
        }

    def _parse_position_split(self, log: dict, topics: list) -> dict:
        """解析 PositionSplit 事件"""
        data = log.get("data", "0x")[2:] if log.get("data", "0x").startswith("0x") else log.get("data", "0x")
        partition_offset = int(data[0:64], 16) if data else 0
        partition_length = int(data[64:128], 16) if data else 0
        partition_start = (partition_offset + 32) * 2
        partition_data = data[partition_start: partition_start + partition_length * 64]
        partition = [int(partition_data[i*64:(i+1)*64], 16) for i in range(partition_length)]
        amount = int(data[partition_start + partition_length * 64: partition_start + partition_length * 64 + 64], 16) if data else 0
        return {
            "event_type": "PositionSplit",
            "user": "0x" + topics[1][-40:],
            "condition_id": topics[3],
            "partition": partition,
            "amount_decimal": amount / 1e6,
            "transaction_hash": log.get("transactionHash", ""),
            "block_number": parse_hex_to_int(log.get("blockNumber", "0x0")),
        }

    def _parse_position_merge(self, log: dict, topics: list) -> dict:
        """解析 PositionsMerge 事件（结构同 PositionSplit）"""
        return self._parse_position_split(log, topics)

    async def check_ws_latency(self) -> int:
        """返回 polygon_ws 延迟（毫秒）"""
        if not self._running or not self.rpc.ws or not self.rpc.connected():
            raise Exception("WS not connected")
        try:
            pong_waiter = await asyncio.wait_for(self.rpc.ws.ping(), timeout=3.0)
            latency_sec = await pong_waiter
            return int(latency_sec * 1000)
        except asyncio.TimeoutError:
            raise Exception("Polygon WS ping timeout")

    async def check_http_latency(self) -> int:
        """返回 polygon_http 延迟"""
        start = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.get(self._rpc_http_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                await resp.text()
        return int((time.time() - start) * 1000)


# 全局单例
_chain_monitor: Optional['CopyTradingChainMonitor'] = None


def get_copy_trading_chain_monitor() -> 'CopyTradingChainMonitor':
    """获取或创建链上监听器单例"""
    global _chain_monitor
    if _chain_monitor is None:
        _chain_monitor = CopyTradingChainMonitor()
    return _chain_monitor
