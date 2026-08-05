"""MarketService - 市场频道 WebSocket + 订单簿管理"""
import asyncio
import json
import logging
from typing import Dict, List, Optional, Set, Tuple

import requests
import websockets

from py_clob_client_v2 import ClobClient

from shared.orderbook import OrderBook

logger = logging.getLogger(__name__)

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"

_market_service: Optional['MarketService'] = None


def get_market_service() -> 'MarketService':
    global _market_service
    if _market_service is None:
        _market_service = MarketService()
    return _market_service


class MarketService:
    """管理 Polymarket 市场频道 WebSocket 连接，维护订单簿和最新价格"""

    def __init__(self):
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_running: bool = False
        self._ws_task: Optional[asyncio.Task] = None
        self._subscribed: Set[str] = set()  # 期望订阅的资产
        self._pending_subscriptions: Set[str] = set()  # 已发送订阅，等待 snapshot 确认
        self._confirmed_subscriptions: Set[str] = set()  # 已收到 snapshot，可安全使用内存订单簿
        self._order_books: Dict[str, OrderBook] = {}
        self._tick_sizes: Dict[str, str] = {}      # {asset_id: tick_size_str}
        self._neg_risks: Dict[str, Optional[bool]] = {}  # {asset_id: neg_risk}
        self._token_pair_cache: Dict[str, Tuple[str, str]] = {}  # {asset_id: (condition_id, opposite_asset_id)}
        # 无 L1 认证的 CLOB Client，用于市场元信息兜底
        self._clob_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
        )

    # --- 同步查询接口 ---

    async def get_best_bid(self, asset_id: str) -> Optional[Tuple[str, str]]:
        """返回 (price, size) 或 None：内存优先，CLOB 兜底"""
        book = self.get_order_book(asset_id)
        if book:
            bid = book.get_best_bid()
            if bid and bid[0] is not None:
                return bid
        for attempt in range(3):
            try:
                ob = await asyncio.to_thread(self._clob_client.get_order_book, asset_id)
                bids = ob.get("bids", [])
                if bids:
                    best = bids[-1]  # bids是升序排列
                    return (best["price"], best["size"])
                return None, None
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"[Market] get_best_bid({asset_id[:8]}...) CLOB fallback failed: {e}")
                else:
                    await asyncio.sleep(0.1 * (attempt + 1))
        return None, None

    async def get_best_ask(self, asset_id: str) -> Optional[Tuple[str, str]]:
        """返回 (price, size) 或 None：内存优先，CLOB 兜底"""
        book = self.get_order_book(asset_id)
        if book:
            ask = book.get_best_ask()
            if ask and ask[0] is not None:
                return ask
        for attempt in range(3):
            try:
                ob = await asyncio.to_thread(self._clob_client.get_order_book, asset_id)
                asks = ob.get("asks", [])
                if asks:
                    best = asks[-1]  # asks是降序排列
                    return (best["price"], best["size"])
                return None, None
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"[Market] get_best_ask({asset_id[:8]}...) CLOB fallback failed: {e}")
                else:
                    await asyncio.sleep(0.1 * (attempt + 1))
        return None, None

    async def get_mid_price(self, asset_id: str) -> Optional[float]:
        """返回 (best_bid + best_ask) / 2 或 None"""
        bid = await self.get_best_bid(asset_id)
        ask = await self.get_best_ask(asset_id)
        if bid and ask and bid[0] is not None and ask[0] is not None:
            return (float(bid[0]) + float(ask[0])) / 2
        for attempt in range(3):
            try:
                mid = await asyncio.to_thread(self._clob_client.get_midpoint, asset_id)
                return float(mid["mid"])
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"[Market] get_mid_price({asset_id[:8]}...) CLOB fallback failed: {e}")
                else:
                    await asyncio.sleep(0.1 * (attempt + 1))
        return None

    async def get_price(self, asset_id: str) -> Optional[float]:
        """返回资产当前价格：优先 midprice，已结算市场返回 outcome price"""
        try:
            mid = await asyncio.to_thread(self._clob_client.get_midpoint, asset_id)
            return float(mid["mid"])
        except Exception:
            return await self._get_settlement_price(asset_id)

    async def _get_settlement_price(self, asset_id: str) -> Optional[float]:
        """通过 Gamma API 获取已结算市场的 outcome price"""
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{GAMMA_API_URL}/markets",
                params={"clob_token_ids": asset_id, "closed": "true"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            markets = resp.json()
            if not isinstance(markets, list) or not markets:
                return None

            market = markets[0]
            token_ids = self._parse_json_list(market.get("clobTokenIds"))
            outcome_prices = self._parse_json_list(market.get("outcomePrices"))
            idx = token_ids.index(asset_id)
            return round(float(outcome_prices[idx]), 3)
        except Exception as e:
            logger.warning(f"[Market] get_settlement_price({asset_id[:8]}...) failed: {e}")
            return None

    def get_order_book(self, asset_id: str) -> Optional[OrderBook]:
        """返回 OrderBook 或 None"""
        if asset_id not in self._confirmed_subscriptions:
            return None
        return self._order_books.get(asset_id)

    @staticmethod
    def _parse_json_list(value) -> List:
        """兼容 Gamma 字符串化 JSON 数组和真实 list。"""
        if isinstance(value, list):
            return value
        if value is None or value == "":
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return []

    def _extract_market_assets(self, market: dict) -> List[dict]:
        """从 Gamma market 解析 asset/question/outcome 映射。"""
        if not isinstance(market, dict):
            return []
        question = market.get("question", "") or market.get("title", "")
        token_ids = self._parse_json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
        outcomes = self._parse_json_list(market.get("outcomes"))
        assets = []

        for idx, token_id in enumerate(token_ids):
            outcome = outcomes[idx] if idx < len(outcomes) else ""
            token_id = str(token_id) if token_id else ""
            if token_id and question:
                assets.append({
                    "asset_id": token_id,
                    "question": question,
                    "outcome": str(outcome) if outcome is not None else "",
                })
        return assets

    @staticmethod
    def _extract_position_assets(positions: List[dict]) -> List[dict]:
        assets = []
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            title = pos.get("title", "") or pos.get("question", "")
            pos_asset = pos.get("asset", "")
            outcome = pos.get("outcome", "")
            if pos_asset and title:
                assets.append({"asset_id": pos_asset, "question": title, "outcome": outcome or ""})

            opposite_asset = pos.get("oppositeAsset", "")
            opposite_outcome = pos.get("oppositeOutcome", "")
            if opposite_asset and title:
                assets.append({
                    "asset_id": opposite_asset,
                    "question": title,
                    "outcome": opposite_outcome or "",
                })
        return assets

    @staticmethod
    async def _get_json(url: str, **kwargs):
        resp = await asyncio.to_thread(requests.get, url, **kwargs)
        if resp.status_code != 200:
            return None
        return resp.json()

    async def fetch_asset_question_assets(
        self,
        asset_id: str,
    ) -> List[dict]:
        """按 token 查 market 元数据，返回可缓存的 asset/question/outcome 列表。"""
        try:
            markets = await self._get_json(
                f"{GAMMA_API_URL}/markets",
                params={"clob_token_ids": asset_id},
                timeout=10,
            )
            for market in markets:
                assets = self._extract_market_assets(market)
                matched = next((a for a in assets if a["asset_id"] == asset_id), None)
                if matched:
                    logger.info(f"[Market] Gamma metadata found: {asset_id[:10]} - {matched['question']}[{matched['outcome']}]")
                    return assets
        except Exception as e:
            logger.warning(f"[Market] Gamma clob_token_ids metadata failed: asset={asset_id[:10]}, error={e}")

        try:
            market_by_token = await self._get_json(
                f"{CLOB_API_URL}/markets-by-token/{asset_id}",
                timeout=10,
            )
            condition_id = market_by_token.get("condition_id") if isinstance(market_by_token, dict) else None
            if condition_id:
                markets = await self._get_json(
                    f"{GAMMA_API_URL}/markets",
                    params={"condition_ids": condition_id},
                    timeout=10,
                )
                for market in markets if isinstance(markets, list) else [markets]:
                    assets = self._extract_market_assets(market)
                    matched = next((a for a in assets if a["asset_id"] == asset_id), None)
                    if matched:
                        logger.info(f"[Market] CLOB+Gamma metadata found: {asset_id[:10]} - {matched['question']}[{matched['outcome']}]")
                        return assets
        except Exception as e:
            logger.warning(f"[Market] CLOB token metadata failed: asset={asset_id[:10]}, error={e}")

        return []

    async def check_latency(self) -> Optional[int]:
        """返回当前 market WS ping 延迟（毫秒），未连接时返回 None。"""
        if not self._ws or not self._ws_running:
            return None
        try:
            pong_waiter = await asyncio.wait_for(self._ws.ping(), timeout=3.0)
            latency_sec = await pong_waiter
            return int(latency_sec * 1000)
        except asyncio.TimeoutError:
            logger.warning("[Market] latency check timed out")
            return None
        except Exception as e:
            logger.warning(f"[Market] latency check failed: {e}")
            return None

    async def get_tick_size(self, asset_id: str) -> Optional[str]:
        """返回 tick_size：WS 缓存优先，未命中时 CLOB REST 兜底"""
        if asset_id in self._tick_sizes:
            return self._tick_sizes[asset_id]
        for attempt in range(3):
            try:
                ts = await asyncio.to_thread(self._clob_client.get_tick_size, asset_id)
                self._tick_sizes[asset_id] = ts
                return ts
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"[Market] get_tick_size({asset_id[:8]}...) CLOB fallback failed: {e}")
                else:
                    await asyncio.sleep(0.1 * (attempt + 1))
        return None

    async def get_neg_risk(self, asset_id: str) -> Optional[bool]:
        """返回 neg_risk：缓存优先，未命中时 CLOB REST 兜底"""
        if asset_id in self._neg_risks:
            return self._neg_risks[asset_id]
        for attempt in range(3):
            try:
                neg_risk = await asyncio.to_thread(self._clob_client.get_neg_risk, asset_id)
                self._neg_risks[asset_id] = neg_risk
                return neg_risk
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"[Market] get_neg_risk({asset_id[:8]}...) CLOB fallback failed: {e}")
                else:
                    await asyncio.sleep(0.1 * (attempt + 1))
        return None

    async def get_condition_and_pair(self, asset_id: str) -> Optional[Tuple[str, str]]:
        """返回 (condition_id, opposite_asset_id)，带缓存。"""
        if asset_id in self._token_pair_cache:
            return self._token_pair_cache[asset_id]
        try:
            data = await self._get_json(
                f"{CLOB_API_URL}/markets-by-token/{asset_id}",
                timeout=10,
            )
            if not isinstance(data, dict):
                return None
            condition_id = data.get("condition_id")
            primary = data.get("primary_token_id")
            secondary = data.get("secondary_token_id")
            if not condition_id or not primary or not secondary:
                return None
            self._token_pair_cache[primary] = (condition_id, secondary)
            self._token_pair_cache[secondary] = (condition_id, primary)
            return self._token_pair_cache[asset_id]
        except Exception as e:
            logger.warning(f"[Market] get_condition_and_pair({asset_id[:10]}) failed: {e}")
        return None

    # --- 订阅管理 ---

    def subscribe(self, asset_ids: list[str]):
        """订阅市场（异步发送订阅消息）"""
        new_asset_ids = [asset_id for asset_id in asset_ids if asset_id not in self._subscribed]
        if not new_asset_ids:
            return
        self._subscribed.update(new_asset_ids)
        if self._ws and self._ws_running:
            self._mark_subscribe_pending(new_asset_ids)
            asyncio.create_task(self._send_subscribe(new_asset_ids))

    def unsubscribe(self, asset_id: str):
        """取消订阅"""
        if asset_id not in self._subscribed:
            return
        self._subscribed.discard(asset_id)
        self._pending_subscriptions.discard(asset_id)
        self._confirmed_subscriptions.discard(asset_id)
        self._order_books.pop(asset_id, None)
        if self._ws and self._ws_running:
            asyncio.create_task(self._send_unsubscribe(asset_id))

    def _mark_subscribe_pending(self, asset_ids: list[str]):
        pending_asset_ids = [asset_id for asset_id in asset_ids if asset_id in self._subscribed]
        if not pending_asset_ids:
            return
        self._pending_subscriptions.update(pending_asset_ids)
        self._confirmed_subscriptions.difference_update(pending_asset_ids)

    def _mark_subscribe_confirmed(self, asset_id: str):
        self._pending_subscriptions.discard(asset_id)
        self._confirmed_subscriptions.add(asset_id)
        logger.info(f"[Market] Subscribe confirmed for {asset_id[:10]}...")

    def _invalidate_live_subscriptions(self):
        """连接断开后，内存订单簿不再视为实时有效，直到新的 snapshot 到达。"""
        self._pending_subscriptions.clear()
        self._confirmed_subscriptions.clear()

    async def _send_subscribe(self, asset_ids: list[str]):
        if not self._ws:
            return
        if not asset_ids:
            return
        msg = {
            "operation": "subscribe",
            "assets_ids": asset_ids,
            "type": "market",
            "level": 2,
            "initial_dump": True,
            "custom_feature_enabled": True
        }
        try:
            await self._ws.send(json.dumps(msg))
            if len(asset_ids) == 1:
                logger.info(f"[Market] Subscribed to {asset_ids[0][:10]}...")
            else:
                logger.info(f"[Market] Batch subscribed to {len(asset_ids)} assets")
        except Exception as e:
            logger.error(f"[Market] Subscribe error: {e}")

    async def _send_unsubscribe(self, asset_id: str):
        if not self._ws:
            return
        msg = {
            "operation": "unsubscribe",
            "assets_ids": [asset_id],
            "type": "market"
        }
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as e:
            logger.error(f"[Market] Unsubscribe error: {e}")

    # --- 生命周期 ---

    async def start(self):
        """启动 WebSocket 连接循环"""
        if self._ws_running:
            return
        self._ws_running = True
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        logger.info("[Market] MarketService started")

    async def stop(self):
        """停止 WebSocket 连接"""
        self._ws_running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        self._ws = None
        self._invalidate_live_subscriptions()
        logger.info("[Market] MarketService stopped")

    async def _run_ws_loop(self):
        """WebSocket 连接循环（指数退避重连：1s → 2s → ... → 60s）"""
        delay = 1
        while self._ws_running:
            try:
                async with websockets.connect(POLYMARKET_WS_URL, ping_interval=10) as ws:
                    self._ws = ws
                    delay = 1
                    logger.info(f"[Market] Connected to Polymarket WS")

                    # 重连后重新订阅所有资产
                    if self._subscribed:
                        await self._resubscribe_all()

                    async for msg in ws:
                        if not self._ws_running:
                            break
                        await self._handle_message(msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Market] WS error: {e}")
            finally:
                self._ws = None
                self._invalidate_live_subscriptions()

            if self._ws_running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _resubscribe_all(self):
        logger.info(f"[Market] Resubscribing to {len(self._subscribed)} assets...")
        asset_ids = list(self._subscribed)
        self._mark_subscribe_pending(asset_ids)
        await self._send_subscribe(asset_ids)

    # --- 消息处理 ---

    async def _handle_message(self, msg: str):
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            self._process_snapshot(data)
        else:
            event_type = data.get("event_type")
            if event_type == "price_change":
                self._process_price_change(data)
            elif event_type == "tick_size_change":
                self._process_tick_size_change(data)

    def _process_snapshot(self, data: list):
        """处理初始快照（book 事件）"""
        if not data:
            return
        
        for item in data:
            asset_id = item.get("asset_id")
            if not asset_id:
                continue

            bids = item.get("bids", [])
            asks = item.get("asks", [])

            # 初始化订单簿
            if asset_id not in self._order_books:
                self._order_books[asset_id] = OrderBook()
            self._order_books[asset_id].from_book(bids, asks)
            self._mark_subscribe_confirmed(asset_id)

    def _process_price_change(self, data: dict):
        """处理 price_change 事件"""
        price_changes = data.get("price_changes", [])
        for change in price_changes:
            asset_id = change.get("asset_id")
            if not asset_id or asset_id not in self._confirmed_subscriptions:
                continue

            price = change.get("price")
            size = change.get("size")
            side = change.get("side")

            if price and side:
                if asset_id not in self._order_books:
                    continue
                self._order_books[asset_id].update(price, size, side)

    def _process_tick_size_change(self, data: dict):
        """处理 tick_size_change 事件"""
        asset_id = data.get("asset_id")
        new_tick_size = data.get("new_tick_size")
        if asset_id and new_tick_size:
            self._tick_sizes[asset_id] = new_tick_size
            logger.info(f"[Market] Updated tick_size for {asset_id[:8]}...: {new_tick_size}")
