"""
baseline.py — self-collected OI / option-volume history.

The scanner records a snapshot each run. Over days this lets us answer
"is today's OI/volume unusual for THIS name?" — the core signal. No history
is fabricated: derived ratios return None until prior observations exist.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from typing import Optional

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "baselines", "baseline.db")

# How far back observations stay useful. A repeat-hit read is about the last
# couple of weeks of accumulation and an OI average is a 20-session window;
# nothing consults a print from four months ago, so nothing keeps one.
RETENTION_DAYS = 120

# A second, absolute ceiling. Retention alone is only a bound if the number of
# contracts observed per session is bounded, and it is not — a wide universe on
# a heavy expiry can write far more rows than a normal day.
MAX_CONTRACT_OBS = 250_000
MAX_TICKER_OBS = 50_000


def default_db_path() -> str:
    """Where the store lives unless a caller says otherwise.

    Env-overridable because the container's filesystem is ephemeral (history
    that vanishes on redeploy can never reach DAY 2) and because a test run
    must not accumulate sessions in the developer's real store.
    """
    return os.environ.get("SCANNER_BASELINE_DB") or _DEFAULT_DB


class BaselineStore:
    def __init__(self, db_path: Optional[str] = None):
        db_path = db_path or default_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contract_obs (
                obs_date TEXT, ticker TEXT, opt_type TEXT,
                strike REAL, expiry TEXT, oi INTEGER, volume INTEGER,
                PRIMARY KEY (obs_date, ticker, opt_type, strike, expiry)
            );
            CREATE TABLE IF NOT EXISTS ticker_obs (
                obs_date TEXT, ticker TEXT,
                total_opt_vol INTEGER, total_oi INTEGER, equity_vol INTEGER,
                PRIMARY KEY (obs_date, ticker)
            );
            """
        )
        self.conn.commit()

    # ── writes (upsert, last-write-wins per day) ──
    def record_contract(self, obs_date, ticker, opt_type, strike, expiry, oi, volume) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO contract_obs "
            "(obs_date,ticker,opt_type,strike,expiry,oi,volume) VALUES (?,?,?,?,?,?,?)",
            (obs_date, ticker.upper(), opt_type, float(strike), expiry, int(oi), int(volume)),
        )
        self.conn.commit()

    def record_ticker(self, obs_date, ticker, total_opt_vol, total_oi, equity_vol) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ticker_obs "
            "(obs_date,ticker,total_opt_vol,total_oi,equity_vol) VALUES (?,?,?,?,?)",
            (obs_date, ticker.upper(), int(total_opt_vol), int(total_oi), int(equity_vol)),
        )
        self.conn.commit()

    # ── derived signals ──
    def contract_oi_vs_avg(self, ticker, opt_type, strike, expiry, today_oi) -> Optional[float]:
        row = self.conn.execute(
            "SELECT AVG(oi) FROM contract_obs "
            "WHERE ticker=? AND opt_type=? AND strike=? AND expiry=?",
            (ticker.upper(), opt_type, float(strike), expiry),
        ).fetchone()
        avg = row[0] if row else None
        if not avg or avg <= 0:
            return None
        return round(today_oi / avg, 2)

    def contract_sessions(self, ticker, opt_type, strike, expiry) -> int:
        """How many distinct sessions this exact contract has been observed in.

        Sessions, not observations: the scanner may run forty times on a
        Tuesday, and forty prints of the same strike inside one day is one
        decision, not forty. Position building is what the count is for, and
        that only shows up across days — hence COUNT(DISTINCT obs_date), which
        the per-day primary key already makes cheap.
        """
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT obs_date) FROM contract_obs "
            "WHERE ticker=? AND opt_type=? AND strike=? AND expiry=?",
            (ticker.upper(), opt_type, float(strike), expiry),
        ).fetchone()
        return int(row[0]) if row and row[0] else 0

    def ticker_optvol_rvol(self, ticker, today_opt_vol, window: int = 20) -> Optional[float]:
        rows = self.conn.execute(
            "SELECT total_opt_vol FROM ticker_obs WHERE ticker=? "
            "ORDER BY obs_date DESC LIMIT ?",
            (ticker.upper(), window),
        ).fetchall()
        vols = [r[0] for r in rows if r[0] and r[0] > 0]
        if not vols:
            return None
        avg = sum(vols) / len(vols)
        if avg <= 0:
            return None
        return round(today_opt_vol / avg, 2)

    # ── bounded storage ──
    def count_contract_obs(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM contract_obs").fetchone()[0])

    def count_ticker_obs(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM ticker_obs").fetchone()[0])

    def prune(self, today: Optional[_dt.date] = None,
              max_age_days: int = RETENTION_DAYS,
              max_rows: int = MAX_CONTRACT_OBS,
              max_ticker_rows: int = MAX_TICKER_OBS) -> int:
        """Drop history nothing reads any more. Returns the rows removed.

        The store is written once per contract per scan and never read further
        back than a few weeks, so left alone it is a file that only grows.
        Two bounds, because either one alone leaks: age, and a hard row ceiling
        for the days a wide universe writes far more rows than usual. The
        ceiling evicts oldest-first — the recent sessions are the ones a
        repeat-hit read is about.
        """
        cutoff = ((today or _dt.date.today()) - _dt.timedelta(days=max_age_days)).isoformat()
        removed = 0
        cur = self.conn.execute("DELETE FROM contract_obs WHERE obs_date < ?", (cutoff,))
        removed += cur.rowcount or 0
        cur = self.conn.execute("DELETE FROM ticker_obs WHERE obs_date < ?", (cutoff,))
        removed += cur.rowcount or 0

        for table, count, cap in (("contract_obs", self.count_contract_obs(), max_rows),
                                  ("ticker_obs", self.count_ticker_obs(), max_ticker_rows)):
            if count > cap:
                cur = self.conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} ORDER BY obs_date ASC LIMIT ?)",
                    (count - cap,),
                )
                removed += cur.rowcount or 0

        self.conn.commit()
        return removed

    def close(self) -> None:
        self.conn.close()
