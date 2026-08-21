import asyncio
from unittest.mock import AsyncMock

import pytest

from copy_trading import service as service_module
from copy_trading.service import CopyTradingService
from copy_trading.types import CopyTradingOrder


def _order(status="CANCELED", size_matched=11.85):
    return CopyTradingOrder(
        id="order-1",
        config_id=1,
        leader="leader",
        follower="0xfollower",
        leader_tx_hash="tx",
        asset_id="asset-1",
        side="BUY",
        leader_size=0,
        leader_price=0,
        follow_size=14,
        follow_price=0.99,
        size_matched=size_matched,
        status=status,
        created_at=None,
        updated_at=None,
    )


def _service():
    service = CopyTradingService.__new__(CopyTradingService)
    service._processed_canceled_order_ids = set()
    service._addr_locks = {}
    service._order_id_to_config_id = {"order-1": 1}
    service._order_post_filled = {}
    service._follower_positions = {"0xfollower": {}}
    service._pending_buy_orders = {"0xfollower": {"asset-1": 11.85}}
    service._pending_sell_orders = {}
    service._config_balances = {1: 0}
    service._sweep_exited_orders = {}
    service._save_pending_buy_with_question = AsyncMock()
    service._save_pending_sell_with_question = AsyncMock()
    service._asset_label = lambda asset_id: asset_id
    return service


@pytest.mark.parametrize("status", ["CANCELED", "MATCHED"])
def test_terminal_order_does_not_accumulate_late_trade(monkeypatch, status):
    async def scenario():
        service = _service()
        order = _order(status=status)
        updates = []
        monkeypatch.setattr(
            service_module,
            "update_copy_trading_order",
            lambda **kwargs: updates.append(kwargs) or True,
        )

        result = await service._patch_order_match_from_trade(
            "order-1", matched_amount=10, order=order
        )

        assert result is order
        assert order.size_matched == 11.85
        assert order.status == status
        assert updates == []

    asyncio.run(scenario())


def test_runtime_cancel_guard_closes_async_db_update_window(monkeypatch):
    async def scenario():
        service = _service()
        service._processed_canceled_order_ids.add("order-1")
        db_reads = []
        monkeypatch.setattr(
            service_module,
            "get_order_by_id",
            lambda order_id: db_reads.append(order_id),
        )

        result = await service._patch_order_match_from_trade(
            "order-1", matched_amount=10
        )

        assert result is None
        assert db_reads == []

    asyncio.run(scenario())


def test_cancellation_event_persists_canceled_status(monkeypatch):
    async def scenario():
        service = _service()
        tasks = []
        updates = []
        real_create_task = asyncio.create_task

        monkeypatch.setattr(
            service_module,
            "get_order_by_id",
            lambda order_id: _order(status="LIVE"),
        )
        monkeypatch.setattr(
            service_module,
            "update_copy_trading_order",
            lambda **kwargs: updates.append(kwargs) or True,
        )
        monkeypatch.setattr(
            service_module.asyncio,
            "create_task",
            lambda coroutine: tasks.append(real_create_task(coroutine)) or tasks[-1],
        )

        await service.handle_order_event(
            "0xfollower",
            "asset-1",
            original_size=14,
            size_matched=11.85,
            type="CANCELLATION",
            status="CANCELED",
            side="BUY",
            order_id="order-1",
            price=0.99,
        )
        await asyncio.gather(*tasks)

        assert updates == [
            {
                "order_id": "order-1",
                "size_matched": 11.85,
                "status": "CANCELED",
                "err_msg": "canceled",
            }
        ]

    asyncio.run(scenario())


def test_late_trade_still_updates_position_and_pending(monkeypatch):
    async def scenario():
        service = _service()
        order = _order()
        updates = []
        monkeypatch.setattr(service_module, "get_order_by_id", lambda order_id: order)
        monkeypatch.setattr(
            service_module,
            "update_copy_trading_order",
            lambda **kwargs: updates.append(kwargs) or True,
        )
        monkeypatch.setattr(service_module, "upsert_follower_position", lambda *args: None)

        await service.handle_trade_confirmed(
            "0xfollower",
            "asset-1",
            matched_amount=10,
            side="BUY",
            price=0.99,
            order_id="order-1",
        )

        assert updates == []
        assert service._follower_positions["0xfollower"]["asset-1"] == 10
        assert service._pending_buy_orders["0xfollower"]["asset-1"] == pytest.approx(1.85)

    asyncio.run(scenario())
