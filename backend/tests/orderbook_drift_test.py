"""订单簿漂移 A/B 对比测试程序

同时运行两条 WebSocket 连接，监控相同的 token：
  - 组 A（fixed）：带应用层 PING 心跳（修复后逻辑）
  - 组 B（original）：仅协议层 ping（当前生产代码逻辑）

两组各自维护独立的内存订单簿，定期通过 HTTP REST API 对账。
最终对比两组的漂移率，验证 PING 心跳修复是否有效。

运行方式：
    cd backend && python tests/orderbook_drift_test.py

日志输出到 backend/logs/orderbook_drift_test/ 目录
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiohttp
import websockets

# --------------- 配置 ---------------

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_REST_URL = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"

PING_INTERVAL = 10        # 应用层心跳间隔（秒）
AUDIT_INTERVAL = 60       # 对账间隔（秒）
MAX_RUN_HOURS = 24        # 最大运行时长（小时）

# --------------- 日志设置 ---------------

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "orderbook_drift_test"
LOG_DIR.mkdir(parents=True, exist_ok=True)

timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOG_DIR / f"drift_test_{timestamp_str}.log"
DRIFT_FILE = LOG_DIR / f"drift_results_{timestamp_str}.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("drift_test")


# --------------- 本地订单簿 ---------------

@dataclass
class LocalBook:
    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    last_update: float = 0.0

    def apply_snapshot(self, bids: list, asks: list):
        self.bids = {float(r["price"]): float(r["size"]) for r in bids if float(r.get("size", 0)) > 0}
        self.asks = {float(r["price"]): float(r["size"]) for r in asks if float(r.get("size", 0)) > 0}
        self.last_update = time.time()

    def apply_change(self, price: str, size: str, side: str):
        levels = self.asks if side == "SELL" else self.bids if side == "BUY" else None
        if levels is None:
            return
        p, s = float(price), float(size)
        if s <= 0:
            levels.pop(p, None)
        else:
            levels[p] = s
        self.last_update = time.time()

    def best_bid(self) -> Optional[float]:
        return max(self.bids, default=None)

    def best_ask(self) -> Optional[float]:
        return min(self.asks, default=None)

    def level_count(self) -> int:
        return len(self.bids) + len(self.asks)


# --------------- 单组统计 ---------------

@dataclass
class GroupStats:
    total_audits: int = 0
    drift_detected: int = 0
    bbo_matches: int = 0
    messages_received: int = 0
    reconnects: int = 0
    pongs_received: int = 0


# --------------- 单组 WebSocket 连接 ---------------

class WSGroup:
    """一条 WebSocket 连接 + 对应的内存订单簿"""

    def __init__(self, name: str, tokens: List[str], use_app_ping: bool):
        self.name = name
        self.use_app_ping = use_app_ping
        self.tokens = tokens
        self.books: Dict[str, LocalBook] = {t: LocalBook() for t in tokens}
        self.stats = GroupStats()
        self._ws = None
        self._connected = False

    async def run(self):
        """WebSocket 连接循环"""
        delay = 1
        while True:
            try:
                if self.use_app_ping:
                    connect_kwargs = dict(ping_interval=None, ping_timeout=None)
                else:
                    connect_kwargs = dict(ping_interval=20, ping_timeout=20)

                async with websockets.connect(
                    MARKET_WS_URL,
                    max_size=4 * 1024 * 1024,
                    **connect_kwargs,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    delay = 1
                    logger.info("[%s] WebSocket 已连接", self.name)

                    # 订阅所有 token
                    for i in range(0, len(self.tokens), 50):
                        batch = self.tokens[i:i+50]
                        await ws.send(json.dumps({
                            "assets_ids": batch,
                            "type": "market",
                            "operation": "subscribe",
                            "level": 2,
                            "initial_dump": True,
                        }))
                        if i + 50 < len(self.tokens):
                            await asyncio.sleep(0.5)

                    # 如果是修复组，启动心跳
                    heartbeat = None
                    if self.use_app_ping:
                        heartbeat = asyncio.create_task(self._heartbeat(ws))

                    try:
                        async for raw in ws:
                            if raw == "PONG":
                                self.stats.pongs_received += 1
                                continue
                            self.stats.messages_received += 1
                            self._handle_message(raw)
                    finally:
                        if heartbeat:
                            heartbeat.cancel()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats.reconnects += 1
                logger.warning("[%s] WebSocket 断开: %s, %ds 后重连 (第 %d 次)",
                               self.name, e, delay, self.stats.reconnects)
                self._connected = False
                self._ws = None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _heartbeat(self, ws):
        """应用层心跳 - 每 10 秒发送 PING 文本帧"""
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.send("PING")
            except Exception:
                return

    def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            for snapshot in data:
                asset_id = snapshot.get("asset_id")
                if asset_id in self.books:
                    self.books[asset_id].apply_snapshot(
                        snapshot.get("bids", []),
                        snapshot.get("asks", []),
                    )
            return

        event_type = data.get("event_type")

        if event_type == "book":
            asset_id = data.get("asset_id")
            if asset_id in self.books:
                self.books[asset_id].apply_snapshot(
                    data.get("bids", []),
                    data.get("asks", []),
                )

        elif event_type == "price_change":
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                if asset_id in self.books:
                    self.books[asset_id].apply_change(
                        change["price"], change["size"], change["side"],
                    )

    def audit_token(self, token_id: str, http_bids: Dict[float, float], http_asks: Dict[float, float]) -> Optional[dict]:
        """对比单个 token，返回漂移详情或 None"""
        self.stats.total_audits += 1
        local = self.books.get(token_id)
        if not local or local.last_update == 0:
            return None

        http_best_bid = max(http_bids, default=None)
        http_best_ask = min(http_asks, default=None)
        local_best_bid = local.best_bid()
        local_best_ask = local.best_ask()

        bid_ok = _prices_match(local_best_bid, http_best_bid)
        ask_ok = _prices_match(local_best_ask, http_best_ask)

        if bid_ok and ask_ok:
            self.stats.bbo_matches += 1
            return None

        self.stats.drift_detected += 1
        return {
            "local_best_bid": local_best_bid,
            "local_best_ask": local_best_ask,
            "http_best_bid": http_best_bid,
            "http_best_ask": http_best_ask,
            "local_levels": local.level_count(),
            "http_levels": len(http_bids) + len(http_asks),
            "seconds_since_update": time.time() - local.last_update,
        }


def _prices_match(a: Optional[float], b: Optional[float]) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < 1e-9


# --------------- 主测试类 ---------------

class ABDriftTester:
    def __init__(self):
        self.token_info: Dict[str, dict] = {}
        self.tokens: List[str] = []
        self.group_fixed: Optional[WSGroup] = None
        self.group_original: Optional[WSGroup] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._start_time = time.time()

    async def start(self):
        logger.info("=" * 70)
        logger.info("订单簿漂移 A/B 对比测试")
        logger.info("  组 A (fixed)   : 应用层 PING 心跳, 禁用协议层 ping")
        logger.info("  组 B (original): 仅协议层 ping (模拟当前生产逻辑)")
        logger.info("  对账间隔: %ds | 最大运行: %d 小时", AUDIT_INTERVAL, MAX_RUN_HOURS)
        logger.info("  日志: %s", LOG_FILE)
        logger.info("  漂移记录: %s", DRIFT_FILE)
        logger.info("=" * 70)

        self._session = aiohttp.ClientSession()
        await self._discover_markets()

        if not self.tokens:
            logger.error("没有找到要监控的 token，退出")
            await self._session.close()
            return

        logger.info("监控 %d 个 token", len(self.tokens))
        for tid in self.tokens[:5]:
            info = self.token_info[tid]
            logger.info("  %s... | %s | %s", tid[:16], info.get("city", "?"), info.get("outcome", "?"))
        if len(self.tokens) > 5:
            logger.info("  ... 及其他 %d 个", len(self.tokens) - 5)

        self.group_fixed = WSGroup("fixed", self.tokens, use_app_ping=True)
        self.group_original = WSGroup("original", self.tokens, use_app_ping=False)

        tasks = [
            asyncio.create_task(self.group_fixed.run(), name="ws_fixed"),
            asyncio.create_task(self.group_original.run(), name="ws_original"),
            asyncio.create_task(self._audit_loop(), name="audit"),
            asyncio.create_task(self._stats_loop(), name="stats"),
            asyncio.create_task(self._timeout_guard(), name="timeout"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await self._session.close()
            self._print_final_report()

    async def _discover_markets(self):
        """获取要监控的市场 token"""
        # 尝试从本地服务获取
        try:
            async with self._session.get(
                "http://localhost:8001/api/weather/cities",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for city in data.get("cities", []):
                        for direction in city.get("directions", []):
                            for market in direction.get("markets", []):
                                for key, outcome in [("yes_token_id", "yes"), ("no_token_id", "no")]:
                                    tid = market.get(key)
                                    if tid:
                                        self.tokens.append(tid)
                                        self.token_info[tid] = {
                                            "market_slug": market.get("market_slug"),
                                            "outcome": outcome,
                                            "city": city.get("name"),
                                            "temperature_label": market.get("temperature_label"),
                                        }
                    if self.tokens:
                        logger.info("从本地服务发现 %d 个 token", len(self.tokens))
                        return
        except Exception as e:
            logger.warning("本地服务不可用: %s", e)

        # 回退: Gamma API — 取少量活跃二元市场用于测试
        logger.info("通过 Gamma API 搜索活跃市场（用于 A/B 测试）...")
        try:
            params = {"active": "true", "closed": "false", "limit": "30"}
            async with self._session.get(
                f"{GAMMA_API_URL}/markets",
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    markets = await resp.json()
                    count = 0
                    for market in markets:
                        # clobTokenIds 和 outcomes 是 JSON 字符串
                        tokens_raw = market.get("clobTokenIds", "[]")
                        outcomes_raw = market.get("outcomes", "[]")
                        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
                        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                        if len(tokens) == 2 and len(outcomes) == 2:
                            for i, tid in enumerate(tokens):
                                self.tokens.append(tid)
                                self.token_info[tid] = {
                                    "market_slug": market.get("conditionId", "")[:20],
                                    "outcome": outcomes[i].lower(),
                                    "city": market.get("question", "")[:40],
                                    "temperature_label": "",
                                }
                            count += 1
                            if count >= 10:
                                break
                    logger.info("从 Gamma API 发现 %d 个二元市场 (%d 个 token)", count, len(self.tokens))
        except Exception as e:
            logger.error("Gamma API 失败: %s", e)

    # --------------- 定期对账 ---------------

    async def _audit_loop(self):
        await asyncio.sleep(30)  # 等待 initial dump
        while True:
            await self._run_audit()
            await asyncio.sleep(AUDIT_INTERVAL)

    async def _run_audit(self):
        # 两组都连接上了才对账
        if not (self.group_fixed._connected and self.group_original._connected):
            logger.info("等待两组都连接...")
            return

        # 抽样
        active_tokens = [t for t in self.tokens
                         if self.group_fixed.books[t].last_update > 0
                         and self.group_original.books[t].last_update > 0]
        if not active_tokens:
            return

        sample = random.sample(active_tokens, min(10, len(active_tokens)))

        for token_id in sample:
            try:
                await self._audit_one_token(token_id)
            except Exception as e:
                logger.debug("对账异常 %s: %s", token_id[:12], e)
            await asyncio.sleep(1)

    async def _audit_one_token(self, token_id: str):
        """从 HTTP API 获取真实 book，分别与两组对比"""
        url = f"{CLOB_REST_URL}/book"
        async with self._session.get(
            url, params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return
            http_book = await resp.json()

        http_bids = {float(r["price"]): float(r["size"])
                     for r in http_book.get("bids", []) if float(r.get("size", 0)) > 0}
        http_asks = {float(r["price"]): float(r["size"])
                     for r in http_book.get("asks", []) if float(r.get("size", 0)) > 0}

        # 对比两组
        drift_fixed = self.group_fixed.audit_token(token_id, http_bids, http_asks)
        drift_original = self.group_original.audit_token(token_id, http_bids, http_asks)

        info = self.token_info.get(token_id, {})

        # 记录结果
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_hours": (time.time() - self._start_time) / 3600,
            "token_id": token_id,
            "city": info.get("city"),
            "outcome": info.get("outcome"),
            "fixed_drift": drift_fixed,
            "original_drift": drift_original,
        }
        with open(DRIFT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        # 日志高亮
        if drift_fixed or drift_original:
            f_status = "漂移" if drift_fixed else "一致"
            o_status = "漂移" if drift_original else "一致"
            logger.warning(
                "对账 %s [%s] | fixed=%s | original=%s",
                info.get("city", "?"), info.get("outcome", "?"),
                f_status, o_status,
            )

    # --------------- 统计 ---------------

    async def _stats_loop(self):
        while True:
            await asyncio.sleep(300)
            self._log_stats()

    def _log_stats(self):
        elapsed = (time.time() - self._start_time) / 3600
        f = self.group_fixed.stats
        o = self.group_original.stats
        logger.info("=" * 70)
        logger.info("统计 (%.1f 小时)", elapsed)
        logger.info("-" * 70)
        logger.info("%-20s %12s %12s", "", "fixed", "original")
        logger.info("%-20s %12d %12d", "消息数", f.messages_received, o.messages_received)
        logger.info("%-20s %12d %12d", "PONG 回复", f.pongs_received, o.pongs_received)
        logger.info("%-20s %12d %12d", "重连次数", f.reconnects, o.reconnects)
        logger.info("%-20s %12d %12d", "对账次数", f.total_audits, o.total_audits)
        logger.info("%-20s %12d %12d", "BBO 一致", f.bbo_matches, o.bbo_matches)
        logger.info("%-20s %12d %12d", "漂移次数", f.drift_detected, o.drift_detected)

        f_rate = f"{f.bbo_matches / f.total_audits * 100:.1f}%" if f.total_audits else "N/A"
        o_rate = f"{o.bbo_matches / o.total_audits * 100:.1f}%" if o.total_audits else "N/A"
        logger.info("%-20s %12s %12s", "一致率", f_rate, o_rate)
        logger.info("=" * 70)

    async def _timeout_guard(self):
        await asyncio.sleep(MAX_RUN_HOURS * 3600)
        logger.info("达到最大运行时长 %d 小时", MAX_RUN_HOURS)
        raise asyncio.CancelledError()

    def _print_final_report(self):
        elapsed = (time.time() - self._start_time) / 3600
        f = self.group_fixed.stats
        o = self.group_original.stats

        logger.info("")
        logger.info("=" * 70)
        logger.info("最终报告")
        logger.info("=" * 70)
        logger.info("运行时长: %.2f 小时", elapsed)
        logger.info("")
        logger.info("%-25s %12s %12s", "指标", "fixed(修复)", "original(原始)")
        logger.info("-" * 70)
        logger.info("%-25s %12d %12d", "收到消息总数", f.messages_received, o.messages_received)
        logger.info("%-25s %12d %12d", "PONG 回复数", f.pongs_received, o.pongs_received)
        logger.info("%-25s %12d %12d", "重连次数", f.reconnects, o.reconnects)
        logger.info("%-25s %12d %12d", "HTTP 对账次数", f.total_audits, o.total_audits)
        logger.info("%-25s %12d %12d", "BBO 一致次数", f.bbo_matches, o.bbo_matches)
        logger.info("%-25s %12d %12d", "漂移次数", f.drift_detected, o.drift_detected)

        f_rate = f.bbo_matches / f.total_audits * 100 if f.total_audits else 0
        o_rate = o.bbo_matches / o.total_audits * 100 if o.total_audits else 0
        logger.info("%-25s %11.1f%% %11.1f%%", "BBO 一致率", f_rate, o_rate)
        logger.info("")

        if f.total_audits > 0 and o.total_audits > 0:
            if f_rate > o_rate + 5:
                logger.info("结论: fixed 组一致率显著高于 original 组, PING 心跳修复有效!")
            elif abs(f_rate - o_rate) <= 5:
                logger.info("结论: 两组一致率接近, PING 心跳可能不是唯一原因, 需进一步分析")
            else:
                logger.info("结论: original 组表现更好 (异常), 需要检查测试逻辑")

        logger.info("")
        logger.info("漂移详情: %s", DRIFT_FILE)
        logger.info("完整日志: %s", LOG_FILE)
        logger.info("=" * 70)


# --------------- 入口 ---------------

async def main():
    tester = ABDriftTester()
    try:
        await tester.start()
    except KeyboardInterrupt:
        logger.info("手动中断")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
