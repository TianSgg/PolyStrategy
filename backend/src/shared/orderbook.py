"""订单簿类（SortedDict 实现 — bids/asks 两棵有序 Map，内部用 float 存储）"""
from sortedcontainers import SortedDict
from typing import Optional, Tuple


class OrderBook:
    def __init__(self):
        self.asks = SortedDict()  # {float_price: float_size} 升序
        self.bids = SortedDict()  # {float_price: float_size} 升序

    def from_book(self, bids: list, asks: list):
        """从 book 事件初始化"""
        self.asks.clear()
        self.bids.clear()
        for a in asks:
            self.asks[float(a["price"])] = float(a["size"])
        for b in bids:
            self.bids[float(b["price"])] = float(b["size"])

    def update(self, price: str, size: str, side: str):
        """从 price_change 事件更新订单簿"""
        p = float(price)
        if side == "SELL":
            if size == "0":
                self.asks.pop(p, None)
            else:
                self.asks[p] = float(size)
        elif side == "BUY":
            if size == "0":
                self.bids.pop(p, None)
            else:
                self.bids[p] = float(size)

    def get_best_bid(self) -> Optional[Tuple[float, float]]:
        if self.bids:
            p = self.bids.keys()[-1]
            return p, self.bids[p]
        return None

    def get_best_ask(self) -> Optional[Tuple[float, float]]:
        if self.asks:
            p = self.asks.keys()[0]
            return p, self.asks[p]
        return None