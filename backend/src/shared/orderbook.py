"""订单簿类（SortedDict 实现 — bids/asks 两棵有序 Map）"""
from sortedcontainers import SortedDict


class OrderBook:
    def __init__(self):
        self.asks = SortedDict()  # {price_str: size_str}
        self.bids = SortedDict()  # {price_str: size_str}

    def from_book(self, bids: list, asks: list):
        """从 book 事件初始化"""
        self.asks.clear()
        self.bids.clear()
        for a in asks:
            self.asks[a["price"]] = a["size"]
        for b in bids:
            self.bids[b["price"]] = b["size"]

    def update(self, price: str, size: str, side: str):
        """从 price_change 事件更新订单簿"""
        if side == "SELL":
            if size == "0":
                self.asks.pop(price, None)
            else:
                self.asks[price] = size
        elif side == "BUY":
            if size == "0":
                self.bids.pop(price, None)
            else:
                self.bids[price] = size

    def get_best_bid(self):
        """获取最佳 bid O(1)"""
        if self.bids:
            p = self.bids.keys()[-1]
            return p, self.bids[p]
        return None, "0"

    def get_best_bid_info(self):
        p, s = self.get_best_bid()
        return {"price": p, "size": s} if p else {}

    def get_best_ask(self):
        """获取最佳 ask O(1)"""
        if self.asks:
            p = self.asks.keys()[0]
            return p, self.asks[p]
        return None, "0"

    def get_best_ask_info(self):
        p, s = self.get_best_ask()
        return {"price": p, "size": s} if p else {}

    def debug(self):
        return {
            "asks": {"heap": list(self.asks.keys()), "pos_map": {}, "size_map": dict(self.asks),
                     "best_ask": self.get_best_ask()},
            "bids": {"heap": list(self.bids.keys()), "pos_map": {}, "size_map": dict(self.bids),
                     "best_bid": self.get_best_bid()},
        }