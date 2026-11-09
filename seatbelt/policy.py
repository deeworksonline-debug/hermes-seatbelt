"""Policy: the user's limits. Defaults ship with the plugin; overrides live in the state DB.

Overrides are set only through /seatbelt commands (human typed), never by editing a file the
agent could also edit.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, fields as dc_fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULTS_FILE = Path(__file__).parent / "defaults" / "policy.yaml"


@dataclass
class Policy:
    max_order_usd: float = 100.0            # above this: block
    ask_above_usd: float = 50.0             # above this (and <= max): ask the human
    max_leverage: float = 3.0
    leverage_action: str = "clamp"          # clamp | block
    symbols_allow: list = field(default_factory=lambda: ["BTC", "ETH"])
    venues_allow: list = field(default_factory=list)   # empty = any venue
    on_unlisted: str = "ask"                # ask | block, for symbols/venues outside the allow-lists
    daily_max_orders: int = 10
    daily_max_notional_usd: float = 500.0
    unknown_trade_tools: str = "ask"        # ask | block (live mode); always blocked in paper mode
    unit_prices_usd: dict = field(default_factory=dict)  # e.g. {"ETH": 2500} for native-priced NFTs
    admin_user_ids: list = field(default_factory=list)   # who may use sensitive commands from a gateway

    # Keys a human may change with `/seatbelt limits <key> <value>`.
    EDITABLE = (
        "max_order_usd", "ask_above_usd", "max_leverage", "leverage_action", "on_unlisted",
        "daily_max_orders", "daily_max_notional_usd", "unknown_trade_tools",
    )


_TYPES = {f.name: f.type for f in dc_fields(Policy)}


def _coerce(name: str, value: Any) -> Any:
    expected = _TYPES.get(name)
    if expected in (float, "float"):
        number = float(value)
        if not math.isfinite(number) or number < 0 or number > 1e9:
            raise ValueError(f"{name} must be a finite number between 0 and 1,000,000,000")
        return number
    if expected in (int, "int"):
        number = int(value)
        if number < 0 or number > 1_000_000:
            raise ValueError(f"{name} must be an integer between 0 and 1,000,000")
        return number
    if expected in (bool, "bool"):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name} must be true or false")
    if name in ("leverage_action",) and value not in ("clamp", "block"):
        raise ValueError("leverage_action must be 'clamp' or 'block'")
    if name in ("on_unlisted", "unknown_trade_tools") and value not in ("ask", "block"):
        raise ValueError(f"{name} must be 'ask' or 'block'")
    return value


def coerce_override(key: str, value: Any) -> Any:
    """Strict validation for human-typed changes: raises ValueError instead of falling back."""
    if key not in Policy.EDITABLE:
        raise ValueError(f"'{key}' is not an editable limit")
    return _coerce(key, value)


def load_policy(overrides: dict | None = None) -> Policy:
    """Defaults file -> overrides. Any bad value falls back to the safe default (and is logged)."""
    policy = Policy()
    sources: list[dict] = []
    try:
        from ._yaml import load

        loaded = load(DEFAULTS_FILE.read_text(encoding="utf-8")) if DEFAULTS_FILE.exists() else {}
        if isinstance(loaded, dict):
            sources.append(loaded)
    except Exception as err:
        logger.warning("seatbelt: cannot read default policy: %s", err)
    if overrides:
        sources.append(overrides)
    for source in sources:
        for key, value in source.items():
            if key not in _TYPES or key == "EDITABLE":
                continue
            try:
                setattr(policy, key, _coerce(key, value))
            except (TypeError, ValueError) as err:
                logger.warning("seatbelt: ignoring invalid policy value %s=%r: %s", key, value, err)
    return policy
