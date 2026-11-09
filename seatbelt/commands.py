"""Slash commands. Human-only controls: /seatbelt <sub>, plus /halt.

Sensitive subcommands (live, limits, resume, reset) require a trusted caller: the local CLI, or a
gateway user whose id is listed in the policy's admin_user_ids. Everything else fails closed.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import hermes_env
from .policy import Policy, coerce_override, load_policy
from .state import MODE_LIVE, MODE_PAPER, State

LIVE_CONFIRM = "I-ACCEPT-RISK"
_SENSITIVE = {"live", "limits", "resume", "reset", "setup"}

HELP = """Seatbelt keeps your AI agent inside limits YOU choose. Nothing real is traded unless you switch to live mode.

Getting started
  /seatbelt setup     answer 3 simple questions to set your limits
  /seatbelt status    what is on, and what you have used today
  /seatbelt log       everything the agent tried, and what Seatbelt decided

Stop / go
  /seatbelt halt      stop ALL trading right now (also: /halt)
  /seatbelt resume    start again
  /seatbelt paper     practice mode: orders are simulated, nothing real is sent (the default)
  /seatbelt live I-ACCEPT-RISK   allow real orders, still inside your limits

More
  /seatbelt limits [name value]  see or change one limit, e.g.  limits max_order_usd 50
  /seatbelt learn     trading tools Seatbelt does not recognise yet
  /seatbelt reset     put the limits back to the defaults
  /seatbelt reset usage   start today's counters again from zero (limits unchanged)"""

WELCOME = ("Welcome to Seatbelt. It sits between your AI agent and your money: every order is checked against your limits, "
           "and nothing real is traded until you type /seatbelt live. Type /seatbelt setup to choose your limits in 3 quick "
           "questions, or /seatbelt help for everything it can do.\n")

_NO_CHANGE = "keep"
_SETUP_STEPS = [
    ("How much money may the agent put into ONE order?",
     [("25", "Up to $25 - very cautious"), ("100", "Up to $100 - balanced (the default)"),
      ("250", "Up to $250"), (_NO_CHANGE, "Keep what I have now")]),
    ("How much in TOTAL per day?",
     [("100", "Up to $100 a day"), ("500", "Up to $500 a day (the default)"),
      ("2000", "Up to $2,000 a day"), (_NO_CHANGE, "Keep what I have now")]),
    ("Leverage is borrowed money that magnifies wins AND losses. How much may the agent use?",
     [("1", "None (1x) - the safest"), ("3", "Up to 3x (the default)"),
      ("5", "Up to 5x"), (_NO_CHANGE, "Keep what I have now")]),
]


def _trusted(platform: Optional[str], source: Any, policy: Policy) -> bool:
    # "cli": classic terminal. "local": the local-machine adapter the gateway serves `hermes chat` through.
    # "tui"/"desktop": local UIs, which pass no remote source. Telegram/Discord/etc. are never trusted by default.
    if platform in ("cli", "local") or (platform in ("tui", "desktop") and source is None):
        return True
    user_id = getattr(source, "user_id", None)
    return bool(user_id) and str(user_id) in {str(u) for u in policy.admin_user_ids}


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _approvals_line(policy: Policy) -> str:
    mode = hermes_env.approval_mode()
    if hermes_env.humans_decide(mode):
        return "Approvals: manual - mid-size orders will ask YOU."
    if not policy.require_manual_approvals:
        return f"Approvals: Hermes mode is '{mode}' - mid-size orders are sent to Hermes' approval step, which may approve them for you."
    return (f"Approvals: Hermes mode is '{mode}', so a person is not guaranteed to decide. Mid-size orders are BLOCKED "
            f"instead of asking. For real approval prompts run: {hermes_env.FIX_HINT}")


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
        _approvals_line(policy),
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


def _setup(state: State, ask) -> str:
    """Three plain questions -> limits. Nothing is changed unless every question is answered."""
    if ask is None:
        return "Guided setup is not available here. Use '/seatbelt limits' instead (see /seatbelt help)."
    answers = []
    for question, options in _SETUP_STEPS:
        choices = [{"value": value, "label": label} for value, label in options]
        answer = ask(question, choices)
        if answer is None:
            return "Setup stopped (no answer was given), so nothing was changed. Run /seatbelt setup to try again."
        answers.append(str(answer))
    per_order, per_day, leverage = answers
    changes: dict = {}
    notes: list = []
    if per_order != _NO_CHANGE:
        value = float(per_order)
        changes["max_order_usd"] = value
        changes["ask_above_usd"] = value / 2
    if per_day != _NO_CHANGE:
        changes["daily_max_notional_usd"] = float(per_day)
    if leverage != _NO_CHANGE:
        changes["max_leverage"] = float(leverage)
    current = load_policy(state.overrides())
    final_order = changes.get("max_order_usd", current.max_order_usd)
    final_day = changes.get("daily_max_notional_usd", current.daily_max_notional_usd)
    if final_day < final_order:
        changes["daily_max_notional_usd"] = final_order
        notes.append(f"Your daily total was raised to ${final_order:,.0f} so it is not smaller than one order.")
    if not changes:
        return "Nothing changed: you kept your current limits. /seatbelt status shows them."
    for key, value in changes.items():
        state.set_override(key, coerce_override(key, value))
    state.set_flag("welcomed")
    summary = _status(state)
    mode = "You are still in PRACTICE mode, so nothing real is traded. When you are ready: /seatbelt live I-ACCEPT-RISK"
    return "Done. Your limits are set.\n\n" + summary + ("\n\n" + "\n".join(notes) if notes else "") + "\n\n" + mode


def make_handler(state: State, ask=None):
    def handle(raw_args: str = "", *, platform: Optional[str] = None, source: Any = None, **_: Any) -> str:
        parts = (raw_args or "").strip().split()
        sub = parts[0].lower() if parts else "status"
        rest = parts[1:]
        policy = load_policy(state.overrides())

        if sub in _SENSITIVE and not _trusted(platform, source, policy):
            return f"Seatbelt: '/seatbelt {sub}' can only be used from the local terminal or by a configured admin."

        if sub in ("help", "?"):
            return HELP
        if sub == "setup":
            return _setup(state, ask)
        if sub == "status":
            first_time = not state.get_flag("welcomed")
            if first_time:
                state.set_flag("welcomed")
            return (WELCOME if first_time else "") + _status(state)
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
        if sub == "reset" and rest[:1] == ["usage"]:
            state.reset_usage()
            return "Today's usage was reset to zero (0 orders, $0). Your limits were not changed."
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
