"""Text shown to the agent (and, through it, the human) when Seatbelt acts. Short, structured, actionable."""

from __future__ import annotations

from typing import Optional

from .engine import Decision
from .intent import TradeIntent
from .policy import Policy


def _describe(intent: TradeIntent) -> str:
    size = "unknown size" if intent.notional_usd is None else f"${intent.notional_usd:,.2f}"
    parts = [intent.side or "", intent.symbol or "", f"on {intent.venue}", f"({size}"]
    lev = f", {intent.leverage:g}x)" if intent.leverage else ")"
    return " ".join(p for p in parts if p) + lev


def _budget(decision: Decision, policy: Policy) -> str:
    if decision.remaining_orders is None:
        return ""
    return (f" Today's remaining budget: {decision.remaining_orders} orders, "
            f"${decision.remaining_notional:,.2f} of ${policy.daily_max_notional_usd:,.2f}.")


def block_message(intent: TradeIntent, decision: Decision, policy: Policy) -> str:
    return (f"Seatbelt stopped this order: {_describe(intent)}. Reason: {decision.reason}."
            f"{_budget(decision, policy)} Do not retry the same order. Try a smaller size within the limits, "
            f"or tell the user which limit is in the way (they can change it with /seatbelt limits). "
            f"See everything with /seatbelt log.")


def ask_message(intent: TradeIntent, decision: Decision) -> str:
    return f"Seatbelt needs your approval: {_describe(intent)}. {decision.reason}."


def clamp_note(intent: TradeIntent, decision: Decision) -> str:
    return f"Seatbelt adjusted this order: {'; '.join(decision.reasons)}."


def halted_message() -> str:
    return ("Seatbelt is HALTED: all trading actions are blocked until the user runs /seatbelt resume. "
            "Do not try to work around this; tell the user trading is paused.")


def unknown_tool_block_message(tool_name: str, paper: bool) -> str:
    if paper:
        return (f"Seatbelt is in paper mode and '{tool_name}' looks like a trading tool Seatbelt has no adapter for, "
                f"so it was not run (it could move real money). Tell the user to run /seatbelt learn to add an adapter, "
                f"or /seatbelt live to trade for real.")
    return (f"Seatbelt blocked '{tool_name}': it looks like a trading tool with no adapter and the policy is set "
            f"to block unknown trading tools. The user can add an adapter (see /seatbelt learn).")


def tamper_message() -> str:
    return ("Seatbelt blocked this action: it would modify or disable Seatbelt's own policy, state or plugin files. "
            "Only the human user may change Seatbelt settings, using /seatbelt commands.")


def error_message(tool_name: str) -> str:
    return (f"Seatbelt could not evaluate '{tool_name}' safely, so it was blocked (fail-closed). "
            f"Check the Hermes log for 'seatbelt' and try again.")
