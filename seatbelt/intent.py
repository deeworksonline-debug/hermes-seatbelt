"""Normalized trade intent: one common shape for every venue (perps, prediction markets, NFTs, swaps)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Argument names whose values must never reach the audit log.
_SECRET_KEY_RE = re.compile(
    r"(secret|private|passw|pass_?phrase|mnemonic|seed|signature|api[_-]?key|token|auth|credential|cookie|bearer|jwt"
    r"|keystore|xprv|wif|^pk$|_pk$|^key$|_key$|privkey)", re.I)
# Secrets embedded in free text (shell commands, code): bearer tokens, 0x private-key-sized hex, key=value pairs.
_SECRET_TEXT_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._\-]{12,}|0x[0-9a-f]{64}|(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key)\s*[=:]\s*\S+)")

# Hermes built-in tools that are never treated as trading tools by the name heuristic.
BUILTIN_NON_TRADE_TOOLS = frozenset({
    "terminal", "read_file", "write_file", "patch", "search_files", "execute_code", "browser_navigate",
    "web_search", "web_extract", "delegate_task", "todo", "memory", "skill_manage", "skill_view",
    "clarify", "cronjob", "send_message", "process", "vision_analyze",
})

# A tool whose name matches this *might* move money; unknown ones are escalated, never silently allowed.
_TRADE_NAME_RE = re.compile(
    r"(order|trade|swap|buy|sell|bid|offer|perp|future|leverage|margin|position|withdraw|transfer|bridge"
    r"|purchase|mint|fulfil|accept_offer|place_bet|bet|stake|unstake|sign_tx|deposit|lend|borrow|redeem|liquidat"
    r"|(open|go|take)_(long|short)|^(long|short)$|(long|short)_(position|order|trade|perp)|send_(tx|transaction|token|tokens|funds|coin|crypto|payment|usdc|usdt|eth|btc|sol)"
    r"|(token|coin|crypto|currency)_?(convert|exchange)|convert_(token|coin|crypto)|execute_(swap|trade|order)|close_(all|position))",
    re.I,
)
# A read-only prefix ("get_", "quote_"...) exempts a name from the weak words above (get_order_book),
# but never from these strong money-moving verbs (get_and_buy, quote_and_swap).
_STRONG_VERB_RE = re.compile(r"(buy|sell|swap|create|place|submit|send_|transfer|withdraw|execute|fulfil|mint|bridge|liquidat|redeem)", re.I)
_READONLY_PREFIX_RE = re.compile(r"^(?:mcp_[a-z0-9]+_)?(get|list|fetch|search|read|quote|estimate|simulate|preview|show|describe|check|query)[_-]", re.I)


@dataclass(frozen=True)
class TradeIntent:
    tool_name: str
    kind: str = "order"                 # order | prediction | nft_buy | transfer | swap
    venue: str = "unknown"
    symbol: str = ""
    side: str = ""
    quantity: Optional[float] = None
    price: Optional[float] = None
    notional_usd: Optional[float] = None   # None = could not be determined -> policy escalates to "ask"
    leverage: Optional[float] = None
    leverage_keys: tuple = ()              # top-level arg names holding leverage (rewritten when clamping)
    leverage_nested: bool = False          # leverage found inside a nested object: cannot be rewritten safely
    leverage_unparseable: bool = False     # a leverage-looking field was present but not a number
    args: dict = field(default_factory=dict)


def looks_like_trade(tool_name: str) -> bool:
    """Name heuristic for a state-changing money tool with no adapter."""
    name = (tool_name or "").strip()
    if not name or name in BUILTIN_NON_TRADE_TOOLS:
        return False
    prefix = _READONLY_PREFIX_RE.match(name)
    if prefix:
        return bool(_STRONG_VERB_RE.search(name[prefix.end():]))
    return bool(_TRADE_NAME_RE.search(name))


def to_float(value: Any) -> Optional[float]:
    """Parse numbers loosely ('1,250.5', '$40', 3); anything else is None (never guessed)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").lstrip("$").lower().rstrip("x")
        try:
            number = float(cleaned)
        except ValueError:
            return None
        return number if number == number and abs(number) != float("inf") else None
    return None


def dig(args: dict, dotted: str) -> Any:
    """Read args['a']['b'] with the key 'a.b'; missing -> None."""
    current: Any = args
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def redact_args(args: Any, limit: int = 300) -> str:
    """Short, secret-free preview of tool arguments for the audit log."""
    def scrub(value: Any, key: str = "") -> Any:
        if key and _SECRET_KEY_RE.search(key):
            return "[redacted]"
        if isinstance(value, dict):
            return {k: scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [scrub(v, key) for v in value[:10]]
        if isinstance(value, str):
            value = _SECRET_TEXT_RE.sub("[redacted]", value)
            return value[:120] + "..." if len(value) > 120 else value
        return value

    text = repr(scrub(args if isinstance(args, dict) else {"args": args}))
    return text if len(text) <= limit else text[: limit - 3] + "..."


# Text of a shell command / code snippet that would place an order or move funds. Best effort, not a sandbox:
# it catches the obvious bypass (an agent calling an exchange SDK or API from execute_code or the shell).
_CODE_TRADE_RE = re.compile(
    r"(create_?order|place_?order|submit_?order|pmxt_order|pmxt_submit_order|send_?transaction|send_?raw_?transaction"
    r"|\.swap\(|\.withdraw\(|\.transfer\(|market_?(buy|sell)|limit_?(buy|sell)|set_?leverage)",
    re.I,
)
_EXCHANGE_HOST_RE = re.compile(
    r"(api\.binance\.|fapi\.binance\.|api\.bybit\.|api\.hyperliquid\.|clob\.polymarket\.|api\.opensea\.|api\.kraken\.|api\.coinbase\.)",
    re.I,
)
_HTTP_WRITE_RE = re.compile(r"(-X\s*POST|--data|\s-d\s|requests\.post|\.post\(|httpx\.post|fetch\(.*POST)", re.I)


def code_mentions_trading(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return False
    if _CODE_TRADE_RE.search(text):
        return True
    return bool(_EXCHANGE_HOST_RE.search(text) and _HTTP_WRITE_RE.search(text))
