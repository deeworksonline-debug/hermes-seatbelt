"""Glue between Hermes hooks and the pure engine. Hermes-independent so it can be tested directly.

Key design points (found by reading Hermes' dispatch code and by adversarial review):

* ONE verdict per tool call. Hermes runs `pre_tool_call` and `tool_execution` middleware in different orders
  on different code paths (the agent loop wraps the hook INSIDE the middleware; `handle_function_call` runs
  the hook first). Both entry points ask `decide_once`, which caches the verdict by tool_call_id, so limits
  are enforced the same way in either order and nothing is counted twice.
* Fail closed: if anything goes wrong while evaluating a trading-looking call, the call is blocked. Unrelated
  tools are never affected by a Seatbelt bug.
* Asks reserve budget immediately (so a batch of parallel asks cannot overshoot the daily caps); the
  reservation is released if the order is denied or fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from . import hermes_env, messages
from .adapters import Adapter, find_adapter
from .engine import ALLOW, ASK, BLOCK, CLAMP, decide
from .guard_self import SelfGuard
from .intent import TradeIntent, code_mentions_trading, looks_like_trade, redact_args
from .paper import simulate
from .policy import Policy, load_policy
from .state import MODE_PAPER, State

logger = logging.getLogger(__name__)

_PENDING_TTL = 600.0       # an ask whose tool never reported back stops counting after 10 minutes
_VERDICT_TTL = 120.0
_VERDICT_CAP = 2048


@dataclass
class Verdict:
    kind: str                                   # block | ask | allow
    key: str = ""
    message: str = ""                           # text for block / ask
    rule_key: str = ""
    modified_args: Optional[dict] = None        # leverage clamp
    intent: Optional[TradeIntent] = None
    adapter: Optional[Adapter] = None
    paper: bool = False
    pre_seen: bool = False                      # the pre_tool_call hook already returned this verdict to Hermes
    created: float = field(default_factory=time.time)


class Runtime:
    def __init__(self, state: State, adapters: list[Adapter], guard: SelfGuard) -> None:
        self.state = state
        self.adapters = adapters
        self.guard = guard
        self._pending: dict[str, list[tuple[Optional[float], float]]] = {}
        self._verdicts: dict[str, Verdict] = {}
        self.approval_mode_fn = hermes_env.approval_mode      # injectable for tests

    # --- helpers ---------------------------------------------------------------------------------
    def policy(self) -> Policy:
        return load_policy(self.state.overrides())

    @staticmethod
    def _key(tool_name: str, args: dict, tool_call_id: str) -> str:
        if tool_call_id:
            return f"id:{tool_call_id}"
        blob = json.dumps(args, sort_keys=True, default=str)
        return "h:" + hashlib.sha256((tool_name + blob).encode("utf-8", "replace")).hexdigest()[:24]

    @staticmethod
    def _is_trade_like(tool_name: str, args: dict) -> bool:
        """A tool that looks like it moves money, by name, or because shell/code text places orders."""
        if looks_like_trade(tool_name):
            return True
        if tool_name in ("execute_code", "terminal"):
            text = args.get("code") if tool_name == "execute_code" else args.get("command")
            return code_mentions_trading(text if isinstance(text, str) else "")
        return False

    def _log(self, tool: str, decision: str, reason: str, session_id: str, intent: Optional[TradeIntent], args: dict, mode: str) -> None:
        try:
            self.state.log(
                tool=tool, decision=decision, reason=reason, session_id=session_id or "", mode=mode,
                venue=intent.venue if intent else "", symbol=intent.symbol if intent else "",
                side=intent.side if intent else "", notional=intent.notional_usd if intent else None,
                leverage=intent.leverage if intent else None, args_preview=redact_args(args))
        except Exception:
            logger.exception("seatbelt: audit log write failed")

    def _no_human_note(self) -> str:
        mode = self.approval_mode_fn()
        return (f"this needs your approval, but Hermes is set so that a person is not guaranteed to decide "
                f"(approvals.mode = {mode}); to get a real prompt run: {hermes_env.FIX_HINT}")

    def _asks_reach_a_human(self, policy: Policy) -> bool:
        return (not policy.require_manual_approvals) or hermes_env.humans_decide(self.approval_mode_fn())

    # --- pending reservations (asks) ---------------------------------------------------------------
    def _prune_pending(self) -> None:
        cutoff = time.time() - _PENDING_TTL
        for key in list(self._pending):
            kept = [(n, c) for n, c in self._pending[key] if c >= cutoff]
            if kept:
                self._pending[key] = kept
            else:
                self._pending.pop(key, None)

    def _pending_totals(self) -> tuple[int, float]:
        self._prune_pending()
        entries = [e for lst in self._pending.values() for e in lst]
        return len(entries), sum((n or 0.0) for n, _ in entries)

    def _settle(self, key: str, ok: bool) -> None:
        """An ask's reservation either becomes a real count (the order ran) or is released (denied / failed)."""
        with self.state.lock:
            entries = self._pending.get(key)
            if not entries:
                return
            notional, _ = entries.pop(0)
            if not entries:
                self._pending.pop(key, None)
            if ok:
                self.state.add_totals(notional)

    # --- the single decision point -------------------------------------------------------------------
    def decide_once(self, tool_name: str, args: dict, session_id: str = "", tool_call_id: str = "") -> Optional[Verdict]:
        key = self._key(tool_name, args, tool_call_id)
        if not tool_call_id:
            # No id to tell calls apart: never reuse a decision (a stale ALLOW could outlive a /seatbelt halt, a
            # mode switch or a limit change). Costs at worst a double count, which errs on the safe side.
            with self.state.lock:
                return self._compute(tool_name, args, session_id, key)
        with self.state.lock:
            cached = self._verdicts.get(key)
            if cached is not None and time.time() - cached.created < _VERDICT_TTL:
                return cached
            verdict = self._compute(tool_name, args, session_id, key)
            if verdict is not None:
                if len(self._verdicts) >= _VERDICT_CAP:
                    cutoff = time.time() - _VERDICT_TTL
                    self._verdicts = {k: v for k, v in self._verdicts.items() if v.created >= cutoff}
                self._verdicts[key] = verdict
            return verdict

    def _compute(self, tool_name: str, args: dict, session_id: str, key: str) -> Optional[Verdict]:
        mode = self.state.mode()
        paper = mode == MODE_PAPER

        if self.guard.violates(tool_name, args):
            self._log(tool_name, "block", "tamper attempt", session_id, None, args, mode)
            return Verdict(BLOCK, key, messages.tamper_message(), paper=paper)

        adapter = find_adapter(self.adapters, tool_name, args)
        if adapter is not None and adapter.kind == "read":
            return None
        if adapter is None and not self._is_trade_like(tool_name, args):
            return None

        policy = self.policy()
        if self.state.is_halted():
            self._log(tool_name, "block", "halted", session_id, None, args, mode)
            return Verdict(BLOCK, key, messages.halted_message(), paper=paper)

        if adapter is None:                                   # trading-looking tool we cannot read
            self.state.learn_add(tool_name, list(args.keys()))
            if paper or policy.unknown_trade_tools == "block":
                self._log(tool_name, "block", "unknown trading tool", session_id, None, args, mode)
                return Verdict(BLOCK, key, messages.unknown_tool_block_message(tool_name, paper), paper=paper)
            if not self._asks_reach_a_human(policy):
                self._log(tool_name, "block", "unknown trading tool; no human approval available", session_id, None, args, mode)
                return Verdict(BLOCK, key, f"Seatbelt blocked '{tool_name}': it looks like a trading tool Seatbelt cannot read, and {self._no_human_note()}.", paper=paper)
            blob = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode("utf-8", "replace")).hexdigest()[:16]
            self._log(tool_name, "ask", "unknown trading tool", session_id, None, args, mode)
            self._pending.setdefault(key, []).append((None, time.time()))
            return Verdict(ASK, key, f"Seatbelt: '{tool_name}' looks like a trading tool it cannot read, so it needs your approval.",
                           rule_key=f"seatbelt:unknown:{tool_name}:{blob}", paper=paper)

        intent = adapter.build_intent(tool_name, args, policy.unit_prices_usd)
        orders, notional_today = self.state.totals()
        pend_n, pend_sum = self._pending_totals()
        decision = decide(intent, policy, orders_today=orders + pend_n, notional_today=notional_today + pend_sum, paper=paper)
        if decision.action == ASK and not self._asks_reach_a_human(policy):
            decision = replace(decision, action=BLOCK, reasons=decision.reasons + [self._no_human_note()],
                               rule_key="", modified_args=None)

        if decision.action in (ALLOW, CLAMP):
            self.state.add_totals(decision.notional_to_count)
        elif decision.action == ASK:
            self._pending.setdefault(key, []).append((decision.notional_to_count, time.time()))

        shown = decision.reason
        if decision.action in (ALLOW, CLAMP) and decision.modified_args:
            shown = decision.reason or "leverage reduced"
        self._log(tool_name, decision.action, shown, session_id, intent, args, mode)

        if decision.action == BLOCK:
            return Verdict(BLOCK, key, messages.block_message(intent, decision, policy), intent=intent, adapter=adapter, paper=paper)
        if decision.action == ASK:
            return Verdict(ASK, key, messages.ask_message(intent, decision), rule_key=decision.rule_key,
                           modified_args=decision.modified_args, intent=intent, adapter=adapter, paper=paper)
        return Verdict(ALLOW, key, modified_args=decision.modified_args if decision.action == CLAMP else None,
                       intent=intent, adapter=adapter, paper=paper)

    # --- Hermes entry points ---------------------------------------------------------------------
    def pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "", tool_call_id: str = "", **_: Any) -> Optional[dict]:
        """block / approve. (Leverage clamps are returned by pre_tool_call_modify; Hermes merges both.)"""
        args = args if isinstance(args, dict) else {}
        try:
            verdict = self.decide_once(tool_name, args, session_id, tool_call_id)
            if verdict is None:
                return None
            if verdict.kind == BLOCK:
                return {"action": "block", "message": verdict.message}
            if verdict.kind == ASK:
                verdict.pre_seen = True
                return {"action": "approve", "message": verdict.message, "rule_key": verdict.rule_key}
            return None
        except Exception:
            logger.exception("seatbelt: error while evaluating %s", tool_name)
            if find_adapter(self.adapters, tool_name, args) is not None or self._is_trade_like(tool_name, args):
                return {"action": "block", "message": messages.error_message(tool_name)}
            return None

    def pre_tool_call_modify(self, tool_name: str = "", args: Any = None, session_id: str = "", tool_call_id: str = "", **_: Any) -> Optional[dict]:
        """Second pre_tool_call callback: carries the leverage clamp even when the order also needs approval."""
        args = args if isinstance(args, dict) else {}
        try:
            verdict = self.decide_once(tool_name, args, session_id, tool_call_id)
            if verdict is not None and verdict.kind != BLOCK and verdict.modified_args:
                return {"action": "modify", "args": verdict.modified_args}
        except Exception:
            logger.exception("seatbelt: error preparing clamp for %s", tool_name)   # pre_tool_call fails closed
        return None

    def post_tool_call(self, tool_name: str = "", args: Any = None, tool_call_id: str = "", status: Optional[str] = None, **_: Any) -> None:
        """An ask-approved order counts once it actually ran; a denied or failed one releases its reservation."""
        try:
            key = self._key(tool_name, args if isinstance(args, dict) else {}, tool_call_id)
            self._settle(key, status in (None, "ok", "success"))
        except Exception:
            logger.exception("seatbelt: post_tool_call bookkeeping failed")

    def tool_execution(self, tool_name: str = "", args: Any = None, next_call: Any = None,
                       session_id: str = "", tool_call_id: str = "", **_: Any) -> Any:
        """Paper mode lives here. It enforces the same verdict itself because, on the agent-loop path, this
        middleware runs BEFORE the pre_tool_call hook and a simulated order would otherwise skip every limit."""
        args = args if isinstance(args, dict) else {}
        try:
            verdict = self.decide_once(tool_name, args, session_id, tool_call_id)
        except Exception:
            logger.exception("seatbelt: middleware could not evaluate %s", tool_name)
            if find_adapter(self.adapters, tool_name, args) is not None or self._is_trade_like(tool_name, args):
                return json.dumps({"error": messages.error_message(tool_name)})
            return next_call(args)
        if verdict is None:
            return next_call(args)                                  # not a trading call: untouched
        if verdict.kind == BLOCK:
            return json.dumps({"error": verdict.message})
        if not verdict.paper:
            return next_call(args)                                  # live: the hook enforces ask / clamp

        # ---- paper mode: simulate, never call the real tool -------------------------------------------
        try:
            if verdict.kind == ASK:                                 # cannot happen in paper mode; fail closed anyway
                self._settle(verdict.key, False)
                return json.dumps({"error": "Seatbelt: this order needs approval and cannot be simulated."})
            if verdict.intent is None or verdict.adapter is None:
                return json.dumps({"error": "Seatbelt paper mode cannot simulate a tool it has no adapter for."})
            merged = {**args, **(verdict.modified_args or {})}
            intent = verdict.adapter.build_intent(tool_name, merged, self.policy().unit_prices_usd)
            return simulate(intent, self.state, adjusted=bool(verdict.modified_args))
        except Exception:
            logger.exception("seatbelt: paper simulation failed for %s", tool_name)
            return json.dumps({"error": "Seatbelt paper mode could not simulate this order, so it was not sent."})
