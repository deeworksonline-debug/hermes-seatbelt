"""YAML loading that works with whichever parser is installed (Hermes ships ruamel.yaml; PyYAML is optional)."""

from __future__ import annotations

from typing import Any


def load_all(text: str) -> list[Any]:
    try:
        import yaml                                    # PyYAML, if present
        return list(yaml.safe_load_all(text))
    except ImportError:
        from ruamel.yaml import YAML                   # always available inside Hermes

        return list(YAML(typ="safe").load_all(text))


def load(text: str) -> Any:
    docs = load_all(text)
    return docs[0] if docs else None
