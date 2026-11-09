"""Slash commands. Human-only controls: /seatbelt <sub>, plus /halt.

Sensitive subcommands (live, limits, resume, reset) require a trusted caller: the local CLI, or a
gateway user whose id is listed in the policy's admin_user_ids. Everything else fails closed.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .policy import Policy, coerce_override, load_policy
from .state import MODE_LIVE, MODE_PAPER, State

LIVE_CONFIRM = "I-ACCEPT-RISK"
_SENSITIVE = {"live", "limits", "resume", "reset"}

HELP = """Seatbelt - your limits on what an agent can trade.
  /seatbelt status             mode, halt state, today's usage, active limits
  /seatbelt log [n]            last n decisions (default 15)
  /seatbelt halt               block ALL trading actions now      (also: /halt)
  /seatbelt resume             allow trading again
  /seatbelt paper              simulate trades; nothing is sent (default)
  /seatbelt live I-ACCEPT-RISK send real orders (still limited by your limits)
  /seatbelt limits [key value] show or change a limit, e.g. max_order_usd 50
  /seatbelt learn              trading-looking tools Seatbelt has no adapter for
  /seatbelt reset              clear limit changes back to defaults"""


def _trusted(platform: Optional[str], source: Any, policy: Policy) -> bool:
    # "cli": classic terminal. "local": the local-machine adapter the gateway serves `hermes chat` through.
    # "tui"/"desktop": local UIs, which pass no remote source. Telegram/Discord/etc. are never trusted by default.
    if platform in ("cli", "local") or (platform in ("tui", "desktop") and source is None):
        return True
    user_id = getattr(source, "user_id", None)
    return bool(user_id) and str(user_id) in {str(u) for u in policy.admin_user_ids}


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _status(state: State) -> str:
    policy = load_policy(state.overrides())
    orders, notional = state.totals()
    paper = state.paper_summary()
    lines = [
        f"Mode: {state.mode().upper()}" + ("  (nothing real is sent)" if state.mode() == MODE_PAPER else "  (REAL orders, within limits)"),
        f"Trading: {'HALTED' if state.is_halted() else 'running'}",
        f"Today: {orders}/{policy.daily_max_orders} orders, ${notional:,.2f}/${policy.daily_max_notional_usd:,.2f}",
        f"Per order: ask above ${policy.ask_above_usd:,.2f}, block above ${policy.max_order_usd:,.2f}; "
        f"leverage max {policy.max_leverage:g}x ({policy.leverage_action})",
        f"Allowed symbols: {', '.join(policy.symbols_allow) or 'any'}; venues: {', '.join(policy.venues_allow) or 'any'}; unlisted -> {policy.on_unlisted}",
        f"Paper portfolio: {paper['fills']} simulated fills, ${paper['notional']:,.2f} notional",
    ]
    return "\n".join(lines)


def _log(state: State, count: int) -> str:
    rows = state.recent(count)
    if not rows:
        return "Seatbelt log is empty - nothing has been checked yet."
    out = []
    for r in rows:
        size = "" if r["notional"] is None else f" ${r['notional']:,.2f}"
        what = " ".join(x for x in (r["side"], r["symbol"], f"@{r['venue']}" if r["venue"] else "") if x) or r["tool"]
        reason = f" - {r['reason']}" if r["reason"] else ""
        out.append(f"{_fmt_ts(r['ts'])} [{r['mode'] or '-'}] {r['decision'].upper():5} {what}{size}{reason}")
    return "\n".join(out)


def make_handler(state: State):
    def handle(raw_args: str = "", *, platform: Optional[str] = None, source: Any = None, **_: Any) -> str:
        parts = (raw_args or "").strip().split()
        sub = parts[0].lower() if parts else "status"
        rest = parts[1:]
        policy = load_policy(state.overrides())

        if sub in _SENSITIVE and not _trusted(platform, source, policy):
            return f"Seatbelt: '/seatbelt {sub}' can only be used from the local terminal or by a configured admin."

        if sub in ("help", "?"):
            return HELP
        if sub == "status":
            return _status(state)
        if sub == "log":
            try:
                count = max(1, min(100, int(rest[0]))) if rest else 15
            except ValueError:
                count = 15
            return _log(state, count)
        if sub == "halt":
            state.set_halted(True)
            return "Seatbelt HALTED. All trading actions are blocked until /seatbelt resume."
        if sub == "resume":
            state.set_halted(False)
            return "Seatbelt resumed. Trading actions are checked against your limits again."
        if sub == "paper":
            state.set_mode(MODE_PAPER)
            return "Paper mode ON. Trades are simulated; nothing is sent."
        if sub == "live":
            if rest[:1] != [LIVE_CONFIRM]:
                return (f"Going live lets the agent send REAL orders (still limited by your limits). "
                        f"To confirm, type: /seatbelt live {LIVE_CONFIRM}")
            state.set_mode(MODE_LIVE)
            return "LIVE mode ON. Real orders can be sent within your limits. Use /seatbelt paper or /seatbelt halt to stop."
        if sub == "learn":
            tools = state.learn_list()
            if not tools:
                return "No unrecognized trading tools seen yet."
            lines = ["Trading-looking tools with no adapter (add one in adapters/ to include them):"]
            lines += [f"  {t['tool']}  (seen {t['count']}x; args: {t['arg_keys']})" for t in tools]
            return "\n".join(lines)
        if sub == "reset":
            state.clear_overrides()
            return "Limit changes cleared; defaults restored."
        if sub == "limits":
            if len(rest) < 2:
                return _status(state)
            key, value = rest[0], " ".join(rest[1:])
            if key not in Policy.EDITABLE:
                return f"Unknown limit '{key}'. Editable: {', '.join(Policy.EDITABLE)}"
            try:
                stored = coerce_override(key, value)
                merged = load_policy({**state.overrides(), key: stored})
                if merged.ask_above_usd > merged.max_order_usd:
                    raise ValueError("ask_above_usd cannot be higher than max_order_usd")
                if merged.max_leverage <= 0:
                    raise ValueError("max_leverage must be above 0")
            except (TypeError, ValueError) as err:
                return f"Invalid value for {key}: {err}"
            state.set_override(key, stored)
            return f"{key} = {stored}"
        return f"Unknown subcommand '{sub}'.\n{HELP}"

    return handle


def make_halt_handler(state: State):
    def halt(raw_args: str = "", **_: Any) -> str:
        state.set_halted(True)
        return "Seatbelt HALTED. All trading actions are blocked until /seatbelt resume."
    return halt
