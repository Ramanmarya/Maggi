"""
On-disk bar cache (SQLite, stdlib).

Historical option data is the expensive part of a backtest: even batched,
reconstructing a two-year QQQ chain is thousands of network round trips. A
run you cannot repeat cheaply is a run you will not iterate on, so every bar
is written to disk on first fetch and read from disk forever after.

Two tables. `bars` holds the data. `ranges` records which (symbol, start, end)
windows have already been requested, so a contract that legitimately has no
bars on a date — an expiry that never listed, a strike that never traded — is
remembered as empty instead of being re-requested on every run.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable, NamedTuple

CACHE_PATH = Path(__file__).resolve().parent / "cache" / "bars.sqlite"


class Bar(NamedTuple):
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarCache:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else CACHE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS bars (
                symbol TEXT NOT NULL,
                day    TEXT NOT NULL,
                open   REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, day)
            );
            CREATE TABLE IF NOT EXISTS ranges (
                symbol TEXT NOT NULL,
                start  TEXT NOT NULL,
                end    TEXT NOT NULL,
                PRIMARY KEY (symbol, start, end)
            );
            CREATE INDEX IF NOT EXISTS bars_symbol_day ON bars (symbol, day);
            """
        )
        self._db.commit()

    # ---- reads -----------------------------------------------------------
    def have_range(self, symbol: str, start: date, end: date) -> bool:
        """True if this exact window was already fetched, empty result included."""
        row = self._db.execute(
            "SELECT 1 FROM ranges WHERE symbol=? AND start<=? AND end>=? LIMIT 1",
            (symbol, start.isoformat(), end.isoformat()),
        ).fetchone()
        return row is not None

    def bars(self, symbol: str, start: date | None = None, end: date | None = None) -> list[Bar]:
        sql = "SELECT day,open,high,low,close,volume FROM bars WHERE symbol=?"
        params: list = [symbol]
        if start:
            sql += " AND day>=?"; params.append(start.isoformat())
        if end:
            sql += " AND day<=?"; params.append(end.isoformat())
        sql += " ORDER BY day"
        return [
            Bar(date.fromisoformat(d), o, h, l, c, v)
            for d, o, h, l, c, v in self._db.execute(sql, params)
        ]

    def bar_on(self, symbol: str, day: date) -> Bar | None:
        row = self._db.execute(
            "SELECT day,open,high,low,close,volume FROM bars WHERE symbol=? AND day=?",
            (symbol, day.isoformat()),
        ).fetchone()
        if not row:
            return None
        d, o, h, l, c, v = row
        return Bar(date.fromisoformat(d), o, h, l, c, v)

    def symbols_with_data(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT DISTINCT symbol FROM bars")}

    # ---- writes ----------------------------------------------------------
    def put_bars(self, symbol: str, bars: Iterable[Bar]) -> int:
        rows = [(symbol, b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars]
        if rows:
            self._db.executemany(
                "INSERT OR REPLACE INTO bars (symbol,day,open,high,low,close,volume) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def mark_range(self, symbol: str, start: date, end: date) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO ranges (symbol,start,end) VALUES (?,?,?)",
            (symbol, start.isoformat(), end.isoformat()),
        )

    def commit(self) -> None:
        self._db.commit()

    def stats(self) -> dict:
        bars = self._db.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        syms = self._db.execute("SELECT COUNT(DISTINCT symbol) FROM bars").fetchone()[0]
        rngs = self._db.execute("SELECT COUNT(*) FROM ranges").fetchone()[0]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"bars": bars, "symbols": syms, "ranges": rngs, "bytes": size}

    def close(self) -> None:
        self._db.commit()
        self._db.close()
