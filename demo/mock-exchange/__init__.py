"""DEMO ONLY: a fake exchange so Seatbelt can be shown with nothing real at stake.

No network, no credentials, no money. Orders it receives are appended to a local file so a demo can prove
which orders actually "reached the exchange" (a blocked or paper-mode order never does).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_PRICES = {"BTC": 50000.0, "ETH": 2500.0, "DOGE": 0.2}


def _orders_file() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir("mock-exchange") / "orders.jsonl"
    except Exception:
        return Path.home() / ".mock-exchange-orders.jsonl"


def _create_order(args: dict, **_kwargs) -> str:
    try:
        order = {
            "ts": time.strftime("%H:%M:%S"),
            "symbol": str(args.get("symbol", "")),
            "side": str(args.get("side", "")),
            "amount": args.get("amount"),
            "price": args.get("price"),
            "leverage": args.get("leverage"),
            "exchange": str(args.get("exchange", "mock")),
        }
        with _orders_file().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(order) + "\n")
        return json.dumps({"success": True, "mock_exchange": "ORDER RECEIVED BY THE (FAKE) EXCHANGE", "order": order})
    except Exception as err:                                   # tool handlers must never raise
        return json.dumps({"error": f"mock exchange failed: {err}"})


def _get_price(args: dict, **_kwargs) -> str:
    symbol = str(args.get("symbol", "BTC")).upper()
    return json.dumps({"symbol": symbol, "price": _PRICES.get(symbol), "note": "fake price"})


def _list_orders(args: dict, **_kwargs) -> str:
    try:
        path = _orders_file()
        orders = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
        return json.dumps({"orders_received_by_exchange": len(orders), "orders": orders[-10:]})
    except Exception as err:
        return json.dumps({"error": f"mock exchange failed: {err}"})


_ORDER_SCHEMA = {
    "name": "create_order",
    "description": "Place an order on the MOCK exchange (demo only).",
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "e.g. BTC, ETH, DOGE"},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "amount": {"type": "number", "description": "quantity in coins"},
            "price": {"type": "number", "description": "limit price in USD"},
            "leverage": {"type": "number", "description": "leverage multiple, e.g. 3"},
            "exchange": {"type": "string", "description": "venue name, e.g. mockex"},
        },
        "required": ["symbol", "side", "amount"],
    },
}
_PRICE_SCHEMA = {
    "name": "get_price",
    "description": "Get the fake price of a coin on the MOCK exchange.",
    "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
}
_LIST_SCHEMA = {
    "name": "list_orders",
    "description": "List the orders the MOCK exchange has actually received.",
    "parameters": {"type": "object", "properties": {}},
}


def register(ctx) -> None:
    ctx.register_tool(name="create_order", toolset="mock_exchange", schema=_ORDER_SCHEMA, handler=_create_order)
    ctx.register_tool(name="get_price", toolset="mock_exchange", schema=_PRICE_SCHEMA, handler=_get_price)
    ctx.register_tool(name="list_orders", toolset="mock_exchange", schema=_LIST_SCHEMA, handler=_list_orders)
