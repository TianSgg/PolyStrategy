import asyncio
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from strategy_weather_sweep.internal.clob_book_bbo import ClobBookBboClient  # noqa: E402


def test_fetch_bbo_parses_clob_book_response():
    async def scenario():
        requested_token_ids = []

        async def handler(request):
            requested_token_ids.append(request.query["token_id"])
            return web.json_response({
                "timestamp": "1694020000000",
                "hash": "book-hash",
                "tick_size": "0.001",
                "bids": [
                    {"price": "0.97", "size": "20"},
                    {"price": "0.98", "size": "10"},
                    {"price": "0.96", "size": "0"},
                ],
                "asks": [
                    {"price": "0.99", "size": "30"},
                    {"price": "0.981", "size": "5"},
                ],
            })

        app = web.Application()
        app.router.add_get("/book", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        client = ClobBookBboClient(base_url=f"http://127.0.0.1:{port}")
        await client.start()
        try:
            snapshot = await client.fetch_bbo("token-id", 100)
        finally:
            await client.close()
            await runner.cleanup()

        return requested_token_ids, snapshot

    requested_token_ids, snapshot = asyncio.run(scenario())

    assert requested_token_ids == ["token-id"]
    assert snapshot["status"] == "ok"
    assert snapshot["source"] == "clob_book_api"
    assert snapshot["best_bid"] == 0.98
    assert snapshot["best_bid_size"] == 10
    assert snapshot["best_ask"] == 0.981
    assert snapshot["best_ask_size"] == 5
    assert snapshot["tick_size"] == "0.001"
    assert snapshot["book_hash"] == "book-hash"
    assert snapshot["server_timestamp"] == "1694020000000"
    assert snapshot["latency_ms"] >= 0
