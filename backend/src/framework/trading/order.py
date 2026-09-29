"""下单/撤单 — 策略进程共用的交易执行接口。"""
import time
import logging
from typing import Any, Optional

from py_clob_client_v2.clob_types import PartialCreateOrderOptions, OrderType, OrderPayload
from py_clob_client_v2.clob_types import OrderArgsV2 as OrderArgs
from py_clob_client_v2.order_builder.constants import BUY, SELL

from framework.trading.provider import get_client, repair_client_signature

logger = logging.getLogger(__name__)

DEFAULT_GTD_EXPIRATION_SEC = 30 * 60

_SIGNATURE_ERROR_KEYWORDS = ("signature does not match", "invalid poly_1271", "invalid signature")


def _is_signature_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in _SIGNATURE_ERROR_KEYWORDS)


def _do_place(client, token_id, side, size, price, tick_size, neg_risk, gtd_expiration_sec):
    if side == "BUY":
        exp_sec = gtd_expiration_sec or DEFAULT_GTD_EXPIRATION_SEC
        expiration = int(time.time()) + exp_sec
        return client.create_and_post_order(
            OrderArgs(token_id=token_id, side=BUY, size=size, price=price, expiration=expiration),
            PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.GTD,
        )
    else:
        return client.create_and_post_order(
            OrderArgs(token_id=token_id, side=SELL, size=size, price=price),
            PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.GTC,
        )


def place_limit_order(
    proxy_wallet: str,
    token_id: str,
    side: str,
    size: float,
    price: float,
    tick_size: str = None,
    neg_risk: bool = None,
    gtd_expiration_sec: int = None,
) -> Optional[dict]:
    client = get_client(proxy_wallet)
    try:
        return _do_place(client, token_id, side, size, price, tick_size, neg_risk, gtd_expiration_sec)
    except Exception as first_err:
        if not _is_signature_error(first_err):
            raise
        logger.warning("Signature error for %s, attempting auto-repair: %s", proxy_wallet[:8], first_err)
        new_type = repair_client_signature(proxy_wallet)
        if new_type is None:
            raise
        client = get_client(proxy_wallet)
        return _do_place(client, token_id, side, size, price, tick_size, neg_risk, gtd_expiration_sec)


def place_limit_order_fast(
    proxy_wallet: str,
    token_id: str,
    side: str,
    size: float,
    price: float,
    tick_size: str,
    neg_risk: bool,
    gtd_expiration_sec: int = None,
    trace: Optional[dict[str, Any]] = None,
) -> Optional[dict]:
    """Build with explicit market parameters and post without metadata lookups."""
    client = get_client(proxy_wallet)
    if trace is not None:
        trace["client_ready_ns"] = time.monotonic_ns()

    def submit(current_client):
        if side == "BUY":
            expiration = int(time.time()) + (gtd_expiration_sec or DEFAULT_GTD_EXPIRATION_SEC)
            args = OrderArgs(token_id=token_id, side=BUY, size=size, price=price, expiration=expiration)
            order_type = OrderType.GTD
        else:
            args = OrderArgs(token_id=token_id, side=SELL, size=size, price=price)
            order_type = OrderType.GTC
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        if trace is not None:
            trace["sign_started_ns"] = time.monotonic_ns()
        signed = current_client.create_order(args, options)
        if trace is not None:
            trace["sign_finished_ns"] = time.monotonic_ns()
        try:
            return current_client.post_order(signed, order_type)
        finally:
            if trace is not None:
                trace["post_finished_ns"] = time.monotonic_ns()

    def submit_with_version_retry(current_client):
        # py_clob_client_v2 calls get_tick_size even when options.tick_size is set.
        # Prime its per-client cache from the strategy's already selected tick.
        tick_cache = getattr(current_client, "_ClobClient__tick_sizes", None)
        if isinstance(tick_cache, dict):
            tick_cache[token_id] = tick_size
        retry_on_version_update = getattr(current_client, "_retry_on_version_update", None)
        if callable(retry_on_version_update):
            return retry_on_version_update(lambda: submit(current_client))
        return submit(current_client)

    try:
        return submit_with_version_retry(client)
    except Exception as first_err:
        if not _is_signature_error(first_err):
            raise
        if trace is not None:
            trace["signature_retry_ns"] = time.monotonic_ns()
        new_type = repair_client_signature(proxy_wallet)
        if new_type is None:
            raise
        return submit_with_version_retry(get_client(proxy_wallet))


def cancel_order(proxy_wallet: str, order_id: str) -> dict:
    client = get_client(proxy_wallet)
    return client.cancel_order(OrderPayload(orderID=order_id))
