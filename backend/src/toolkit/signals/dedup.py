from __future__ import annotations

import time
from collections import OrderedDict

DEDUP_TTL_SEC = 300
DEDUP_MAX_SIZE = 10000


class TTLDeduplicator:
    """基于时间的事件去重器。"""

    def __init__(
        self, ttl_sec: float = DEDUP_TTL_SEC, max_size: int = DEDUP_MAX_SIZE
    ) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl_sec
        self._max_size = max_size

    def is_duplicate(self, key: str) -> bool:
        now = time.time()
        self._evict(now)
        if key in self._seen:
            return True
        self._seen[key] = now
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return False

    def _evict(self, now: float) -> None:
        while self._seen:
            oldest_key, oldest_time = next(iter(self._seen.items()))
            if now - oldest_time > self._ttl:
                self._seen.popitem(last=False)
            else:
                break
