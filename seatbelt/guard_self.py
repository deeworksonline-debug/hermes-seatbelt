"""Tamper protection: stop the agent from editing or disabling Seatbelt itself.

Best effort, not a sandbox. It covers the obvious routes: file-write tools (including multi-file patches),
shell/code text that names Seatbelt's files or Hermes' config, and `hermes plugins/config` commands that
could switch the plugin off. The limits are documented in the README.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional

_WRITE_TOOLS = {"write_file": "path", "patch": "path"}
_TEXT_TOOLS = {"terminal": "command", "execute_code": "code"}

# `patch` can edit many files from one V4A body: *** Update File: path / *** Add File: ... / *** Delete File: ...
_V4A_PATH_RE = re.compile(r"^\*\*\*\s*(?:update|add|delete|move to)(?:\s+file)?:\s*(.+?)\s*$", re.I | re.M)

# A path whose segment is exactly plugins/seatbelt or plugin-data/seatbelt (not merely a name containing it).
_SEGMENT_RE = re.compile(r"(?:^|/)(?:plugin-data|plugins)/seatbelt(?:/|$|\s|[;&|])")
_SENSITIVE_NEAR_RE = re.compile(r"(?<![\w-])seatbelt(?=/|\s|$|[;&|]).{0,60}(?:plugin-data|plugins|data\.db|\.hermes)|(?:plugin-data|plugins|data\.db|\.hermes).{0,60}(?<![\w-])seatbelt(?=/|\s|$|[;&|])")
# Commands that can switch plugins off or rewrite Hermes configuration.
_PLUGIN_CLI_RE = re.compile(r"hermes\b[^\n;|&]*\bplugins?\b[^\n;|&]*\b(?:disable|remove|uninstall|update)\b", re.I)
_CONFIG_SET_RE = re.compile(r"hermes\b[^\n;|&]*\bconfig\b[^\n;|&]*\bset\b", re.I)
# The agent must not drive Seatbelt's human-only controls (e.g. `hermes chat -q "/seatbelt live ..."` from a shell).
_SLASH_CMD_RE = re.compile(r"(?<![\w/.-])/(?:seatbelt|halt)\b", re.I)


def _norm(text: str) -> str:
    text = text.replace("\\", "/").lower()
    text = re.sub(r"[\"'`{}]", "", text)
    return re.sub(r"/{2,}", "/", text)


class SelfGuard:
    def __init__(self, protected_dirs: Iterable[Path], protected_files: Optional[Iterable[Path]] = None) -> None:
        resolved = lambda p: _norm(str(Path(p).resolve())).rstrip("/")
        self._dirs = [resolved(p) for p in protected_dirs if p]
        self._files = [resolved(p) for p in (protected_files or []) if p]
        self._home = _norm(str(Path.home()))
        hermes_home = os.environ.get("HERMES_HOME", "")
        self._hermes_home = _norm(hermes_home) if hermes_home else self._home + "/.hermes"

    # -- path checks ---------------------------------------------------------------------------------
    def _path_is_protected(self, raw: str) -> bool:
        try:
            resolved = _norm(str(Path(os.path.expanduser(raw)).resolve()))
        except Exception:
            resolved = _norm(raw)
        if any(resolved == d or resolved.startswith(d + "/") for d in self._dirs):
            return True
        if resolved in self._files:
            return True
        return bool(_SEGMENT_RE.search(resolved + "/"))

    def _expand(self, lowered: str) -> str:
        for token in ("$home", "${home}", "~", "%userprofile%"):
            lowered = lowered.replace(token, self._home)
        for token in ("$hermes_home", "%hermes_home%"):
            lowered = lowered.replace(token, self._hermes_home)
        return lowered

    # -- main check ----------------------------------------------------------------------------------
    def violates(self, tool_name: str, args: dict) -> bool:
        if not isinstance(args, dict):
            return False
        key = _WRITE_TOOLS.get(tool_name)
        if key:
            paths = []
            value = args.get(key)
            if isinstance(value, str):
                paths.append(value)
            body = args.get("patch")
            if tool_name == "patch" and isinstance(body, str):
                paths.extend(_V4A_PATH_RE.findall(body))
            return any(self._path_is_protected(p) for p in paths)
        key = _TEXT_TOOLS.get(tool_name)
        if key:
            text = args.get(key)
            if not isinstance(text, str):
                return False
            if _PLUGIN_CLI_RE.search(text) or _CONFIG_SET_RE.search(text) or _SLASH_CMD_RE.search(text):
                return True
            lowered = self._expand(_norm(text))
            if _SEGMENT_RE.search(lowered) or _SENSITIVE_NEAR_RE.search(lowered):
                return True
            return any(d and d in lowered for d in self._dirs + self._files)
        return False
