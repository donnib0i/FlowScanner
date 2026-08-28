"""
signal_journal.py — durable record of every signal the scanner emitted.

The scanner scores and ranks setups live, prints them, and then forgets them.
That makes the only question that actually matters unanswerable: "we tailed
these plays — did they work?" You cannot attribute a P&L to a signal you never
wrote down. This module writes them down.

One row per emitted signal. A row is a full snapshot of what the scanner
believed at emit time — every score it computed, the contract it picked, the
underlying price it saw, and the run that produced it — so that weeks later an
attribution pass can join the row against what price actually did afterwards
without needing to re-derive anything from a chain that no longer exists.

Nothing here fabricates history. A field the scanner did not compute is stored
as NULL, never as a zero that a later analysis would read as a real reading.

NATURAL KEY (idempotency)
  (run_id, symbol, direction, contract_key)

  contract_key is "EXPIRY|STRIKE|TYPE" for a signal that named a contract, and
  the empty string for one that did not. The reasoning: within a single scan a
  symbol has one forward direction, and at most one best contract per direction.
  A re-emit inside the same run is therefore the *same* signal being re-printed
  — an interactive re-render, a re-sort, a repeated flow merge — not a new call
  by the scanner, and it upserts rather than appending. Across runs the run_id
  differs, so a genuinely new scan of the same ticker is always a new row and
  the history of how the scanner's opinion changed over the day is preserved.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "signals.db")

# Grade letters the scanner can emit, ordered best-first. Used to validate the
# `grade` filter so a typo returns an error instead of a silently empty result.
GRADES = ("A", "B", "C", "D")


def utc_now_iso() -> str:
    """Emit timestamps are UTC and timezone-explicit.

    The scanner runs on a laptop in Pacific time, but an attribution pass joins
    these rows against market data stamped in UTC. Storing a naive local
    timestamp would put every row off by 7 or 8 hours depending on the season,
    which is exactly the kind of error that looks like real signal decay.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    """A run id sorts chronologically as a string and is unique per process.

    The timestamp prefix means `ORDER BY run_id` is `ORDER BY time` without a
    join, and the random suffix keeps two scans launched in the same second
    from colliding into one another's rows.
    """
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def contract_key(contract: Optional[Dict]) -> str:
    """The contract half of the natural key. Empty string when no contract."""
    if not contract:
        return ""
    return f"{contract.get('exp', '')}|{contract.get('strike', '')}|{contract.get('type', '')}"


def _f(value: Any) -> Optional[float]:
    """Float or None — never a substituted zero. See the module docstring."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SignalJournal:
    """SQLite-backed store of emitted signals. Upsert on the natural key."""

    def __init__(self, db_path: str = _DEFAULT_DB):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id      TEXT PRIMARY KEY,
                started_at  TEXT NOT NULL,
                scan_kind   TEXT NOT NULL,
                params      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                run_id            TEXT NOT NULL,
                symbol            TEXT NOT NULL,
                direction         TEXT NOT NULL,
                contract_key      TEXT NOT NULL,
                emitted_at        TEXT NOT NULL,

                -- every score the scanner computed for this signal
                grade             TEXT,
                setup_q           REAL,
                opt_score         INTEGER,
                whale_score       INTEGER,

                underlying_px     REAL,

                -- the chosen contract, quoted as of emit time
                contract_type     TEXT,
                contract_strike   REAL,
                contract_expiry   TEXT,
                contract_dte      INTEGER,
                contract_bid      REAL,
                contract_ask      REAL,
                contract_mid      REAL,
                contract_delta    REAL,
                contract_iv       REAL,
                contract_oi       INTEGER,
                contract_vol      INTEGER,

                -- context that made the scanner rank it where it did
                signal_combo      TEXT,
                rel_vol           REAL,
                change_pct        REAL,
                gap_pct           REAL,
                extra             TEXT,

                PRIMARY KEY (run_id, symbol, direction, contract_key)
            );

            -- Attribution reads by day and by name far more than by run.
            CREATE INDEX IF NOT EXISTS idx_signals_emitted ON signals (emitted_at);
            CREATE INDEX IF NOT EXISTS idx_signals_symbol  ON signals (symbol, emitted_at);
            CREATE INDEX IF NOT EXISTS idx_signals_grade   ON signals (grade, emitted_at);
            """
        )
        self.conn.commit()

    # ── writes ───────────────────────────────────────────────────────────────
    def start_run(self, run_id: str, scan_kind: str, params: Dict) -> str:
        """Record the run and the parameters that produced it.

        The parameters matter as much as the signals: a grade-A at
        --enrich-top 5 was chosen from a different candidate pool than a
        grade-A at --enrich-top 50, and an attribution pass that ignores that
        is comparing two different scanners.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, scan_kind, params) VALUES (?,?,?,?)",
            (run_id, utc_now_iso(), scan_kind, json.dumps(params, default=str, sort_keys=True)),
        )
        self.conn.commit()
        return run_id

    def record_signal(self, run_id: str, result: Dict,
                      grade: Optional[str] = None,
                      emitted_at: Optional[str] = None) -> None:
        """Write one signal, keyed naturally so a re-emit updates in place.

        `result` is a scanner result dict as built by _process_ticker() and
        enriched by enrich_contracts(); this reads it defensively because the
        same shape is assembled by several call sites.
        """
        c = result.get("contract") or {}
        row = (
            run_id,
            str(result.get("ticker", "")).upper(),
            result.get("direction") or "",
            contract_key(result.get("contract")),
            emitted_at or utc_now_iso(),
            grade,
            _f(result.get("setup_q")),
            _i(result.get("opt_score")),
            _i(result.get("whale_score")),
            _f(result.get("price")),
            c.get("type"),
            _f(c.get("strike")),
            c.get("exp"),
            _i(c.get("dte")),
            _f(c.get("bid")),
            _f(c.get("ask")),
            _f(c.get("mid")),
            _f(c.get("delta")),
            _f(c.get("iv")),
            _i(c.get("oi")),
            _i(c.get("vol")),
            result.get("signal_combo"),
            _f(result.get("rel_vol")),
            _f(result.get("change_pct")),
            _f(result.get("gap_pct")),
            json.dumps({k: result[k] for k in ("is_laggard", "breakout", "inside_day",
                                               "gap_flag", "high_vol", "rsi14", "hv_regime")
                        if k in result}, default=str, sort_keys=True),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO signals ("
            "run_id,symbol,direction,contract_key,emitted_at,"
            "grade,setup_q,opt_score,whale_score,underlying_px,"
            "contract_type,contract_strike,contract_expiry,contract_dte,"
            "contract_bid,contract_ask,contract_mid,contract_delta,contract_iv,"
            "contract_oi,contract_vol,"
            "signal_combo,rel_vol,change_pct,gap_pct,extra"
            ") VALUES (" + ",".join("?" * 26) + ")",
            row,
        )
        self.conn.commit()

    def record_signals(self, run_id: str, results: List[Dict],
                       grade_fn=None, emitted_at: Optional[str] = None) -> int:
        """Write a whole scan's worth of signals in one transaction.

        Returns the number of rows written. `grade_fn(result) -> str` supplies
        the plain letter grade; the scanner's own trade_grade() returns an
        ANSI-coloured string, which must never reach the database.
        """
        stamp = emitted_at or utc_now_iso()
        n = 0
        for r in results:
            self.record_signal(run_id, r,
                               grade=grade_fn(r) if grade_fn else None,
                               emitted_at=stamp)
            n += 1
        return n

    # ── reads ────────────────────────────────────────────────────────────────
    def query(self, start: Optional[str] = None, end: Optional[str] = None,
              symbol: Optional[str] = None, grade: Optional[str] = None,
              run_id: Optional[str] = None, min_setup_q: Optional[float] = None,
              limit: int = 500) -> List[Dict]:
        """Fetch signals, newest first, joined to the run that produced them.

        `start` / `end` are compared as ISO-8601 strings, which sorts correctly
        because every stored timestamp is UTC with the same precision. A bare
        date ("2026-08-28") therefore works as a day filter: it is <= every
        timestamp on that day and > every timestamp before it. `end` is
        inclusive of the whole day when given as a bare date.
        """
        where, params = [], []
        if start:
            where.append("s.emitted_at >= ?")
            params.append(start)
        if end:
            # A bare date must include the day it names, not stop at midnight.
            where.append("s.emitted_at <= ?")
            params.append(end if len(end) > 10 else end + "T99")
        if symbol:
            where.append("s.symbol = ?")
            params.append(symbol.upper())
        if grade:
            where.append("s.grade = ?")
            params.append(grade.upper())
        if run_id:
            where.append("s.run_id = ?")
            params.append(run_id)
        if min_setup_q is not None:
            where.append("s.setup_q >= ?")
            params.append(float(min_setup_q))

        sql = ("SELECT s.*, r.scan_kind, r.params AS run_params FROM signals s "
               "LEFT JOIN runs r ON r.run_id = s.run_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY s.emitted_at DESC, s.symbol ASC LIMIT ?"
        params.append(int(limit))
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def by_date_range(self, start: str, end: str, limit: int = 500) -> List[Dict]:
        return self.query(start=start, end=end, limit=limit)

    def by_symbol(self, symbol: str, limit: int = 500) -> List[Dict]:
        return self.query(symbol=symbol, limit=limit)

    def by_grade(self, grade: str, limit: int = 500) -> List[Dict]:
        return self.query(grade=grade, limit=limit)

    def runs(self, limit: int = 50) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT r.*, COUNT(s.run_id) AS n_signals FROM runs r "
            "LEFT JOIN signals s ON s.run_id = r.run_id "
            "GROUP BY r.run_id ORDER BY r.started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0])

    def close(self) -> None:
        self.conn.close()
