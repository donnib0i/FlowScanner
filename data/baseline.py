"""
baseline.py — self-collected OI / option-volume history.

The scanner records a snapshot each run. Over days this lets us answer
"is today's OI/volume unusual for THIS name?" — the core signal. No history
is fabricated: derived ratios return None until prior observations exist.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "baselines", "baseline.db")


class BaselineStore:
    def __init__(self, db_path: str = _DEFAULT_DB):
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
