"""Read-only look at how Hermes is configured (never writes anything)."""

from __future__ import annotations

from pathlib import Path


def approval_mode() -> str:
    """Hermes' `approvals.mode`: "manual" (a person decides), "smart" (an AI guardian may approve for you),
    "off" (everything auto-approved). Hermes' own default, when unset, is "smart". Anything unreadable is
    "unknown", which Seatbelt treats as not-a-human."""
    try:
        from hermes_constants import get_hermes_home

        from ._yaml import load

        path = Path(get_hermes_home()) / "config.yaml"
        data = load(path.read_text(encoding="utf-8")) if path.exists() else {}
        approvals = data.get("approvals") if isinstance(data, dict) else None
        mode = approvals.get("mode") if isinstance(approvals, dict) else None
        if mode is False:
            return "off"                # YAML 1.1 reads an unquoted `off` as the boolean false
        if mode is True:
            return "unknown"
        return str(mode).strip().lower() if mode else "smart"
    except Exception:
        return "unknown"


def humans_decide(mode: str) -> bool:
    return mode == "manual"


FIX_HINT = "hermes config set approvals.mode manual"
