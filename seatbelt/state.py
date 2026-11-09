"""Durable state: halt flag, paper/live mode, limit overrides, daily counters, audit log, paper fills.

Backed by SQLite (Hermes' plugin_db in production, any sqlite3 connection in tests). All access is
serialized with one lock because tool calls can run on parallel worker threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (day TEXT PRIMARY KEY, orders INTEGER NOT NULL DEFAULT 0, notional REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT, tool TEXT NOT NULL,
    venue TEXT, symbol TEXT, side TEXT, notional REAL, leverage REAL, mode TEXT,
    decision TEXT NOT NULL, reason TEXT, args_preview TEXT);
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, venue TEXT, symbol TEXT, side TEXT,
    quantity REAL, price REAL, notional REAL, leverage REAL);
CREATE TABLE IF NOT EXISTS learn (tool TEXT PRIMARY KEY, first_seen REAL NOT NULL, count INTEGER NOT NULL DEFAULT 1, arg_keys TEXT);
"""

MODE_PAPER = "paper"
MODE_LIVE = "live"


def today_utc(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


class State:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.lock = threading.RLock()
        with self.lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # --- key/value -----------------------------------------------------------------------------
    def _get(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key: str, value: str) -> None:
        self._conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self._conn.commit()

    def get_flag(self, name: str) -> bool:
        with self.lock:
            return self._get(f"flag:{name}", "0") == "1"

    def set_flag(self, name: str, value: bool = True) -> None:
        with self.lock:
            self._set(f"flag:{name}", "1" if value else "0")

    def is_halted(self) -> bool:
        with self.lock:
            return self._get("halted", "0") == "1"

    def set_halted(self, halted: bool) -> None:
        with self.lock:
            self._set("halted", "1" if halted else "0")

    def mode(self) -> str:
        with self.lock:
            return MODE_LIVE if self._get("mode", MODE_PAPER) == MODE_LIVE else MODE_PAPER   # anything odd -> paper

    def set_mode(self, mode: str) -> None:
        with self.lock:
            self._set("mode", MODE_LIVE if mode == MODE_LIVE else MODE_PAPER)

    def overrides(self) -> dict:
        with self.lock:
            try:
                data = json.loads(self._get("overrides", "{}"))
                return data if isinstance(data, dict) else {}
            except ValueError:
                return {}

    def set_override(self, key: str, value: Any) -> None:
        with self.lock:
            data = self.overrides()
            data[key] = value
            self._set("overrides", json.dumps(data))

    def clear_overrides(self) -> None:
        with self.lock:
            self._set("overrides", "{}")

    # --- daily counters --------------------------------------------------------------------------
    def totals(self, day: Optional[str] = None) -> tuple[int, float]:
        with self.lock:
            row = self._conn.execute("SELECT orders, notional FROM counters WHERE day=?", (day or today_utc(),)).fetchone()
            return (int(row[0]), float(row[1])) if row else (0, 0.0)

    def add_totals(self, notional: Optional[float], day: Optional[str] = None) -> None:
        """Count one order. Unknown notional counts the order but adds no money (the order was escalated)."""
        with self.lock:
            self._conn.execute(
                "INSERT INTO counters(day,orders,notional) VALUES(?,1,?) "
                "ON CONFLICT(day) DO UPDATE SET orders=orders+1, notional=notional+excluded.notional",
                (day or today_utc(), float(notional or 0.0)))
            self._conn.commit()

    # --- audit -----------------------------------------------------------------------------------
    def log(self, *, tool: str, decision: str, reason: str = "", session_id: str = "", venue: str = "",
            symbol: str = "", side: str = "", notional: Optional[float] = None, leverage: Optional[float] = None,
            mode: str = "", args_preview: str = "", now: Optional[float] = None) -> None:
        with self.lock:
            self._conn.execute(
                "INSERT INTO audit(ts,session_id,tool,venue,symbol,side,notional,leverage,mode,decision,reason,args_preview) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (now if now is not None else time.time(), session_id, tool, venue, symbol, side, notional, leverage,
                 mode, decision, reason, args_preview))
            self._conn.commit()

    def recent(self, limit: int = 15) -> list[dict]:
        with self.lock:
            cur = self._conn.execute("SELECT ts,tool,venue,symbol,side,notional,leverage,mode,decision,reason FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # --- paper ledger ----------------------------------------------------------------------------
    def add_paper_fill(self, *, venue: str, symbol: str, side: str, quantity: Optional[float], price: Optional[float],
                       notional: Optional[float], leverage: Optional[float]) -> None:
        with self.lock:
            self._conn.execute(
                "INSERT INTO paper_fills(ts,venue,symbol,side,quantity,price,notional,leverage) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), venue, symbol, side, quantity, price, notional, leverage))
            self._conn.commit()

    def paper_summary(self) -> dict:
        with self.lock:
            row = self._conn.execute("SELECT COUNT(*), COALESCE(SUM(notional),0) FROM paper_fills").fetchone()
            return {"fills": int(row[0]), "notional": float(row[1])}

    # --- learn mode ------------------------------------------------------------------------------
    def learn_add(self, tool: str, arg_keys: list[str]) -> None:
        with self.lock:
            self._conn.execute(
                "INSERT INTO learn(tool,first_seen,count,arg_keys) VALUES(?,?,1,?) "
                "ON CONFLICT(tool) DO UPDATE SET count=count+1",
                (tool, time.time(), ",".join(sorted(arg_keys))[:300]))
            self._conn.commit()

    def learn_list(self) -> list[dict]:
        with self.lock:
            cur = self._conn.execute("SELECT tool,count,arg_keys FROM learn ORDER BY count DESC, tool LIMIT 50")
            return [{"tool": r[0], "count": r[1], "arg_keys": r[2]} for r in cur.fetchall()]
