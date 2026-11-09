"""The decision engine: pure logic, no Hermes imports. TradeIntent + Policy + counters -> Decision.

Precedence: block > clamp > ask > allow. Anything uncertain (unknown size, unlisted venue/symbol,
unmatched trading tool) is escalated, never silently allowed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from .intent import TradeIntent
from .policy import Policy

ALLOW, BLOCK, ASK, CLAMP = "allow", "block", "ask", "clamp"


@dataclass
class Decision:
    action: str
    reasons: list = field(default_factory=list)       # human-readable, for the agent and the log
    rule_key: str = ""                                # stable id for Hermes' "always allow" memory
    modified_args: Optional[dict] = None              # for clamp
    remaining_orders: Optional[int] = None
    remaining_notional: Optional[float] = None
    notional_to_count: Optional[float] = None         # what to add to today's totals when the order proceeds

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _fmt(value: Optional[float]) -> str:
    return "unknown" if value is None else f"${value:,.2f}"


def decide(intent: TradeIntent, policy: Policy, *, orders_today: int, notional_today: float,
           paper: bool = False) -> Decision:
    blocks: list[str] = []
    asks: list[str] = []
    ask_codes: list[str] = []
    clamp_args: dict = {}
    clamp_notes: list[str] = []

    notional = intent.notional_usd

    # --- hard limits -----------------------------------------------------------------------------
    if notional is None:
        asks.append("order size could not be determined")
        ask_codes.append("size-unknown")
    else:
        if notional > policy.max_order_usd:
            blocks.append(f"order {_fmt(notional)} exceeds the per-order limit of {_fmt(policy.max_order_usd)}")
        elif notional > policy.ask_above_usd:
            asks.append(f"order {_fmt(notional)} is above the {_fmt(policy.ask_above_usd)} auto-approve threshold")
            ask_codes.append("size-large")

    if intent.leverage_unparseable:
        asks.append("a leverage setting was present but could not be read as a number")
        ask_codes.append("leverage-unknown")
    if intent.leverage is not None and intent.leverage > policy.max_leverage:
        note = f"leverage {intent.leverage:g}x exceeds the limit of {policy.max_leverage:g}x"
        cap = int(policy.max_leverage) if float(policy.max_leverage).is_integer() else policy.max_leverage
        if policy.leverage_action == "clamp" and intent.leverage_keys and not intent.leverage_nested:
            for key in intent.leverage_keys:
                clamp_args[key] = cap
            clamp_notes.append(note + f" - reduced to {policy.max_leverage:g}x")
        else:
            blocks.append(note + (" (it is nested where Seatbelt cannot safely lower it)" if intent.leverage_nested else ""))

    if orders_today >= policy.daily_max_orders:
        blocks.append(f"daily order limit reached ({orders_today}/{policy.daily_max_orders})")
    if notional is not None and notional_today + notional > policy.daily_max_notional_usd:
        blocks.append(f"would exceed the daily total of {_fmt(policy.daily_max_notional_usd)} "
                      f"(used {_fmt(notional_today)}, this order {_fmt(notional)})")

    # --- allow-lists (default-deny style: unlisted -> ask or block) ------------------------------
    venue = (intent.venue or "").lower()
    if policy.venues_allow and venue not in [v.lower() for v in policy.venues_allow]:
        msg = f"venue '{intent.venue}' is not in the allowed list ({', '.join(policy.venues_allow)})"
        (blocks if policy.on_unlisted == "block" else asks).append(msg)
        if policy.on_unlisted != "block":
            ask_codes.append("venue-unlisted")
    base_symbol = (intent.symbol or "").upper().replace("-", "/").split("/")[0].replace("PERP", "").strip(":")
    allowed_symbols = [s.upper() for s in policy.symbols_allow]
    symbol_rules_apply = intent.kind in ("order", "swap")     # prediction markets / NFTs have no ticker to allow-list
    if not symbol_rules_apply:
        pass
    elif allowed_symbols and intent.symbol and base_symbol not in allowed_symbols and intent.symbol.upper() not in allowed_symbols:
        msg = f"symbol '{intent.symbol}' is not in the allowed list ({', '.join(policy.symbols_allow)})"
        (blocks if policy.on_unlisted == "block" else asks).append(msg)
        if policy.on_unlisted != "block":
            ask_codes.append("symbol-unlisted")
    elif allowed_symbols and not intent.symbol:
        asks.append("order has no recognizable symbol")
        ask_codes.append("symbol-missing")

    # --- verdict ---------------------------------------------------------------------------------
    remaining_orders = max(0, policy.daily_max_orders - orders_today)
    remaining_notional = max(0.0, policy.daily_max_notional_usd - notional_today)
    base = dict(remaining_orders=remaining_orders, remaining_notional=remaining_notional,
                notional_to_count=notional)

    if blocks:
        return Decision(BLOCK, blocks + asks, **base)
    if paper:
        asks, ask_codes = [], []          # paper trades cost nothing; the human is not interrupted (hard limits still block)
    if asks:
        codes = sorted(set(ask_codes))
        key = f"seatbelt:{intent.venue}:{(intent.symbol or '*').upper()}:{'+'.join(codes)}"
        if {"size-unknown", "leverage-unknown"} & set(codes):
            # An open-ended question must never become a blanket "always allow": tie the key to these exact args.
            blob = json.dumps(intent.args, sort_keys=True, default=str)
            key += ":" + hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]
        return Decision(ASK, asks + clamp_notes, rule_key=key, modified_args=clamp_args or None, **base)
    if clamp_args:
        return Decision(CLAMP, clamp_notes, modified_args=clamp_args, **base)
    return Decision(ALLOW, [], **base)
