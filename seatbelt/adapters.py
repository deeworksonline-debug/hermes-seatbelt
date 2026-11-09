"""Venue adapters: small YAML files that turn any tool call into a TradeIntent.

An adapter says "tool names matching X are orders; the symbol lives in arg Y, the size in arg Z".
That is how Binance, Bybit, Polymarket, OpenSea or anything else is supported without code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .intent import TradeIntent, dig, to_float

logger = logging.getLogger(__name__)

_FIELDS = ("symbol", "side", "quantity", "price", "notional", "leverage", "currency", "order_type")


@dataclass
class Adapter:
    name: str
    tool_re: re.Pattern
    kind: str = "order"
    venue: str = "unknown"
    venue_from: Optional[str] = None            # arg that carries the venue/exchange name
    fields: dict = field(default_factory=dict)  # field -> [candidate arg keys]
    symbol_template: Optional[str] = None       # e.g. "{contract}:{token_id}"
    when: dict = field(default_factory=dict)    # arg -> regex; adapter applies only if ALL match
    default_price: Optional[float] = None       # upper-bound unit price when none is given (prediction: 1.0)
    notes: str = ""

    def matches(self, tool_name: str, args: Optional[dict] = None) -> bool:
        if not self.tool_re.search(tool_name or ""):
            return False
        for key, pattern in self.when.items():
            value = dig(args or {}, key)
            if value is None or not re.search(pattern, str(value)):
                return False
        return True

    def _first(self, args: dict, name: str) -> tuple[Any, Optional[str]]:
        for key in self.fields.get(name, []):
            value = dig(args, key)
            if value is not None:
                return value, key
        return None, None

    def _values(self, args: dict, name: str) -> list:
        """EVERY alias present, not just the first: the agent controls which keys exist."""
        out = []
        for key in self.fields.get(name, []):
            value = dig(args, key)
            if value is not None:
                out.append((value, key))
        return out

    def build_intent(self, tool_name: str, args: dict, unit_prices_usd: Optional[dict] = None) -> TradeIntent:
        """Worst case wins: when several aliases disagree the largest size/leverage is used, a price on a
        market order is not trusted, and a declared notional can only RAISE the checked size."""
        args = args if isinstance(args, dict) else {}
        venue = self.venue
        if self.venue_from:
            raw_venue = dig(args, self.venue_from)
            if isinstance(raw_venue, str) and raw_venue.strip():
                venue = raw_venue.strip().lower()

        if self.symbol_template:
            try:
                symbol = self.symbol_template.format_map(_SafeDict(args))
            except Exception:
                symbol = ""
        else:
            raw_symbol, _ = self._first(args, "symbol")
            symbol = str(raw_symbol).strip() if raw_symbol is not None else ""
        raw_side, _ = self._first(args, "side")
        side = str(raw_side).strip().lower() if raw_side is not None else ""

        # --- size ---------------------------------------------------------------------------------
        quantities = [n for n in (to_float(v) for v, _ in self._values(args, "quantity")) if n is not None]
        quantity = max(quantities) if quantities else None
        quantity_garbled = bool(self._values(args, "quantity")) and not quantities
        prices = [n for n in (to_float(v) for v, _ in self._values(args, "price")) if n is not None and n > 0]
        price = max(prices) if prices else None
        order_types = [str(v).lower() for v, _ in self._values(args, "order_type")]
        is_market = any("market" in t for t in order_types)
        price_trusted = price is not None and not is_market
        declared = [n for n in (to_float(v) for v, _ in self._values(args, "notional")) if n is not None and n >= 0]
        declared_garbled = bool(self._values(args, "notional")) and not declared
        currency_raw, _ = self._first(args, "currency")
        currency = str(currency_raw).strip().upper() if currency_raw is not None else "USD"

        estimates: list[float] = []
        unbounded = quantity_garbled or declared_garbled
        if quantity is not None:
            if price_trusted:
                estimates.append(quantity * price)
            elif self.default_price is not None:
                estimates.append(quantity * self.default_price)      # e.g. a prediction share never costs > $1
            else:
                unbounded = True                                      # market order / no usable price: size unknown
        elif self.kind == "nft_buy" and price is not None:
            estimates.append(price)                                   # an NFT's price is its full cost
        estimates.extend(declared)                                    # declared notional can raise, never lower
        notional = None if (unbounded or not estimates) else max(estimates)
        if notional is not None and currency not in ("USD", "USDC", "USDT", "DAI"):
            rate = to_float((unit_prices_usd or {}).get(currency))
            notional = notional * rate if rate else None             # no known rate -> unknown -> policy asks

        # --- leverage: every alias, plus any key anywhere that looks like "leverage" --------------------
        levs, lev_keys, lev_nested, lev_garbled = [], [], False, False
        for value, key in self._values(args, "leverage"):
            number = to_float(value)
            if number is None:
                lev_garbled = True
            else:
                levs.append(number)
            if "." in key:
                lev_nested = True
            else:
                lev_keys.append(key)
        for path, value in _walk_leverage_like(args):
            if path in lev_keys or any(path == k for k in self.fields.get("leverage", [])):
                continue
            number = to_float(value)
            if number is None:
                lev_garbled = True
            else:
                levs.append(number)
            lev_nested = True                  # found somewhere we cannot safely rewrite
        leverage = max(levs) if levs else None

        return TradeIntent(
            tool_name=tool_name, kind=self.kind, venue=venue, symbol=symbol, side=side,
            quantity=quantity, price=price, notional_usd=notional, leverage=leverage,
            leverage_keys=tuple(lev_keys), leverage_nested=lev_nested, leverage_unparseable=lev_garbled,
            args=dict(args),
        )


def _walk_leverage_like(value, path: str = "", depth: int = 0):
    """Yield (path, value) for any key whose name contains 'lever' (leverageRatio, marginLeverage, ...)."""
    if depth > 6 or not isinstance(value, dict):
        return
    for key, item in value.items():
        here = f"{path}.{key}" if path else str(key)
        if re.search(r"lever", str(key), re.I) and not isinstance(item, (dict, list)):
            yield here, item
        if isinstance(item, dict):
            yield from _walk_leverage_like(item, here, depth + 1)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def parse_adapter(data: dict, source: str = "") -> Optional[Adapter]:
    """Validate one adapter mapping; invalid ones are skipped with a warning (never crash the agent)."""
    try:
        name = str(data["name"])
        pattern = str(data["match"]["tool"])
        tool_re = re.compile(pattern, re.I)
        fields = {}
        for fname in _FIELDS:
            raw = (data.get("fields") or {}).get(fname)
            if raw is None:
                continue
            fields[fname] = [str(k) for k in (raw if isinstance(raw, list) else [raw])]
        return Adapter(
            name=name, tool_re=tool_re, kind=str(data.get("kind", "order")),
            venue=str(data.get("venue", "unknown")).lower(), venue_from=data.get("venue_from"),
            fields=fields, symbol_template=data.get("symbol_template"), notes=str(data.get("notes", "")),
            when={str(k): str(v) for k, v in (data.get("match", {}).get("when") or {}).items()},
            default_price=to_float(data.get("default_price")),
        )
    except Exception as err:
        logger.warning("seatbelt: skipping invalid adapter %s: %s", source or data, err)
        return None


def load_adapters(*directories: Path) -> list[Adapter]:
    """Load every *.yaml adapter from the given directories (later directories can add more)."""
    from ._yaml import load_all

    adapters: list[Adapter] = []
    for directory in directories:
        if not directory or not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).glob("*.yaml")):
            try:
                documents = load_all(path.read_text(encoding="utf-8"))
            except Exception as err:
                logger.warning("seatbelt: cannot read adapter file %s: %s", path, err)
                continue
            for doc in documents:
                items = doc.get("adapters") if isinstance(doc, dict) and "adapters" in doc else [doc]
                for item in items:
                    if isinstance(item, dict):
                        adapter = parse_adapter(item, str(path))
                        if adapter:
                            adapters.append(adapter)
    return adapters


def find_adapter(adapters: list[Adapter], tool_name: str, args: Optional[dict] = None) -> Optional[Adapter]:
    for adapter in adapters:
        if adapter.matches(tool_name, args):
            return adapter
    return None
