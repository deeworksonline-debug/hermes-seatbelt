"""Seatbelt - your limits on what an AI agent can trade, enforced in code.

A checkpoint in front of every trading-looking tool call: allow, block, ask the human, or clamp.
Paper mode simulates trades. /seatbelt halt stops everything. Everything is logged locally.

Not a sandbox: see README "Honest limits".
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .adapters import load_adapters
from .commands import make_halt_handler, make_handler
from .guard_self import SelfGuard
from .runtime import Runtime
from .state import State

logger = logging.getLogger(__name__)

PLUGIN_NAME = "seatbelt"
_HERE = Path(__file__).resolve().parent


def _hermes_config_files() -> list[Path]:
    """Hermes' own config.yaml, where plugin enablement lives: the agent must not edit it."""
    try:
        from hermes_constants import get_hermes_home

        return [get_hermes_home() / "config.yaml"]
    except Exception:
        return []


def _open_state() -> tuple[State, list[Path]]:
    """Durable state in Hermes' per-plugin data dir; in-memory (defaults, no persistence) if that fails."""
    protected = [_HERE]
    try:
        from plugins.plugin_storage import plugin_data_dir, plugin_db

        protected.append(plugin_data_dir(PLUGIN_NAME))
        return State(plugin_db(PLUGIN_NAME)), protected
    except Exception:
        logger.warning("seatbelt: durable storage unavailable; using in-memory state (limits reset on restart)", exc_info=True)
        return State(sqlite3.connect(":memory:", check_same_thread=False)), protected


def register(ctx) -> None:
    state, protected = _open_state()
    user_adapters = None
    try:
        user_adapters = protected[1] / "adapters" if len(protected) > 1 else None
    except Exception:
        pass
    adapters = load_adapters(_HERE / "venues", user_adapters) if user_adapters else load_adapters(_HERE / "venues")
    runtime = Runtime(state, adapters, SelfGuard(protected, _hermes_config_files()))

    ctx.register_hook("pre_tool_call", runtime.pre_tool_call_modify)   # leverage clamp (merged with the next one)
    ctx.register_hook("pre_tool_call", runtime.pre_tool_call)          # block / ask
    ctx.register_hook("post_tool_call", runtime.post_tool_call)
    ctx.register_middleware("tool_execution", runtime.tool_execution)
    ctx.register_command("seatbelt", make_handler(state), description="Trading limits: status, log, halt, resume, paper, live, limits, learn", args_hint="<status|log|halt|resume|paper|live|limits|learn>")
    ctx.register_command("halt", make_halt_handler(state), description="Seatbelt: block all trading actions now")
    logger.info("seatbelt: loaded %d adapters; mode=%s halted=%s", len(adapters), state.mode(), state.is_halted())
