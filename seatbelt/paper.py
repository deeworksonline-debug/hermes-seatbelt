"""Paper mode: simulate a trade instead of sending it. Nothing leaves the machine."""

from __future__ import annotations

import json

from .intent import TradeIntent
from .state import State


def simulate(intent: TradeIntent, state: State, adjusted: bool = False) -> str:
    """Record a pretend fill and return a tool-result JSON string that is honest about being simulated."""
    state.add_paper_fill(
        venue=intent.venue, symbol=intent.symbol, side=intent.side, quantity=intent.quantity,
        price=intent.price, notional=intent.notional_usd, leverage=intent.leverage)
    summary = state.paper_summary()
    return json.dumps({
        "success": True,
        "seatbelt_paper_mode": True,
        "seatbelt_adjusted": ("Seatbelt lowered this order's leverage to your limit before simulating it." if adjusted else None),
        "message": (f"SIMULATED by Seatbelt paper mode: this {intent.side or 'order'} on {intent.venue} was NOT sent. "
                    f"No real money moved. Paper portfolio now has {summary['fills']} simulated fills."),
        "simulated_fill": {
            "venue": intent.venue, "symbol": intent.symbol, "side": intent.side, "quantity": intent.quantity,
            "price": intent.price, "notional_usd": intent.notional_usd, "leverage": intent.leverage,
        },
    })
