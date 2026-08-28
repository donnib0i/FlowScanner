#!/usr/bin/env python3
"""
entry_lag.py — Entry-Lag Decay Analysis
D — Quant Validation Engine (companion to core/backtest.py)

THE QUESTION THIS ANSWERS
  Tailing scanner plays hasn't worked. There are three candidate explanations and
  they need completely different fixes:

    (1) No edge          — the signals were never good.
    (2) Edge that decays — real at emit time, gone by the time a human reads the
                           alert, picks a contract, and gets filled.
    (3) Edge not sized to — a sizing/discipline problem, not a scanner problem.

  This module separates (1) from (2). It replays historical signals and computes
  the outcome not once but at a LADDER of entry lags. The shape of expectancy vs
  lag is the entire deliverable:

    flat-and-positive  → edge is durable; the leak is elsewhere → look at (3)
    high-then-collapse → edge is real but perishable → automate entry or stop tailing
    flat-and-zero      → there was never an edge → (1)

WHY A SEPARATE MODULE
  core/backtest.py works on DAILY bars — one signal per ticker per day, evaluated
  at the close. A daily bar cannot express "five minutes later", so the lag
  question is structurally unanswerable there. This module runs the same family of
  signal primitives on INTRADAY bars, which is also what the live scanner actually
  does during a session. It reuses backtest.py's math (bs_price, find_strike,
  calc_hv) and scanner.py's grading (trade_grade) rather than restating them, so
  grades here mean the same thing they mean on the live deck.

DATA CONSTRAINT — READ THIS BEFORE TRUSTING ANY NUMBER
  The only intraday history available to this project is Yahoo via yfinance:

    interval   usable history      what it can measure
    1m         ~5 trading sessions lags of 1,2,3… min, but a sample far too small
    5m         ~60 calendar days   lags of 0,5,10,15,30,60 min  ← the workable study
    15m        ~60 calendar days   lags of 0,15,30,45,60 min

  So the +1 and +2 minute rungs of the requested ladder ARE NOT MEASURABLE at any
  usable sample size. This module refuses to fabricate them: a lag that is not an
  exact multiple of the bar interval is rejected at config time, not interpolated.
  See `LagConfig.validate()`.

  It also means the finest honest resolution for "how fast does it die" is 5
  minutes. If the edge decays inside the first 5 minutes, this study will see it
  as already-dead at lag 0 and cannot resolve the shape of the fall. That
  limitation is printed in the report rather than hidden.

LOOK-AHEAD GUARANTEE
  yfinance stamps an intraday bar by its START time, so bar i covers
  [ts[i], ts[i] + interval). The signal is computed from bars 0..i inclusive, so
  the earliest moment the decision can exist is ts[i] + interval — call it the
  decision time. Every entry therefore happens at or after decision_time + lag,
  and the entry price is the OPEN of the first bar starting at or after that
  instant. An open is transactable at the instant the bar begins, so no
  information from inside or after that bar is used to set the entry price.
  All of this funnels through one function, `entry_index_for_lag()`, and is
  asserted there — there is exactly one place to audit.

  Exits look forward, which is correct: an exit is an outcome, not an input.

This is a research tool. It measures the underlying's move, which is
assumption-free. The options overlay is a MODEL (Black-Scholes on an HV-derived
IV) and is reported in its own section, clearly separated, because a 0DTE premium
simulation is fragile enough that no conclusion should rest on it.
"""

import os
import re
import sys
import json
import math
import warnings
import argparse
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

import numpy as np
import pandas as pd
import yfinance as yf
from tabulate import tabulate
from colorama import Fore, Style, init

warnings.filterwarnings("ignore")
init(autoreset=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.scanner import (
    UNIVERSE, trade_grade, calc_options_score, calc_iv_rank_proxy,
    bs_delta, _extract_ticker_hist,
)
from core.backtest import bs_price, calc_hv, find_strike, RESULTS_DIR

MARKET_TZ = "America/New_York"

# ── Sample-size floor ─────────────────────────────────────────────────────────
# Why 30: it is the conventional point at which the sampling distribution of a
# mean is near-normal, so a standard error is meaningful at all. It is NOT the
# point at which a conclusion is safe. Per-trade signed returns on a 60-minute
# 0DTE horizon have a standard deviation on the order of 0.8-1.5%, so at n=30 the
# standard error on expectancy is ~0.2% — wider than any effect size we care
# about. A cell at n=30 can rule out "enormous", nothing more.
#
# So the module does two things rather than one:
#   - below MIN_CELL_N it refuses to print a statistic at all (shows n and
#     "insufficient"), because a win rate off 3 trades is worse than no number
#   - between MIN_CELL_N and THIN_CELL_N it prints the statistic but tags it THIN
#     so it is never read as settled
MIN_CELL_N = 30
THIN_CELL_N = 100

# Time-of-day buckets, in minutes from the 09:30 ET open. Chosen to match how the
# session actually behaves rather than to split it evenly: the first hour is
# auction/discovery, midday is drift, and the last two hours are the 0DTE gamma
# window where decay should be fastest if it is anywhere.
TOD_BUCKETS = [
    ("open",   0,   60),    # 09:30-10:30
    ("midday", 60,  270),   # 10:30-14:00
    ("close",  270, 390),   # 14:00-16:00
]

INTERVAL_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60}

# Yahoo's hard caps. Asking beyond these silently returns an empty frame, which
# would otherwise look like "no signals found" instead of "no data".
INTERVAL_MAX_DAYS = {"1m": 7, "2m": 59, "5m": 59, "15m": 59, "30m": 59, "60m": 729}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    """trade_grade() returns a colorized letter for the terminal; we need the letter."""
    return _ANSI.sub("", s)


# ── Config ─────────────────────────────────────────────────────────────────────
class LagConfig:
    def __init__(
        self,
        tickers: List[str] = None,
        lookback_days: int = None,       # None = the most this interval allows
        interval: str = "5m",
        lags: List[int] = None,          # entry lags in minutes
        hold_minutes: int = 60,          # how long the trade is held from ITS OWN entry
        stop_pct: float = 0.40,          # underlying % adverse move that ends the trade
        target_pct: float = 0.60,        # underlying % favorable move that ends the trade
        min_rel_vol: float = 1.5,
        balanced: bool = True,           # only keep signals evaluable at EVERY lag
        min_cell_n: int = MIN_CELL_N,
        vrp_multiplier: float = 1.20,
        spread_sim: float = 0.05,
        delta_target: float = 0.35,
        opt_min_minutes_to_close: int = 60,
        null_control: bool = False,      # replace real signals with random ones
        control_per_session: int = 12,
    ):
        self.tickers = tickers or list(UNIVERSE[:40])
        self.interval = interval
        # Default to the provider's cap for THIS interval rather than a fixed
        # number: a 59-day default would make --interval 1m fail on arrival, and
        # a 7-day default would silently throw away 8 weeks of 5m history.
        self.lookback_days = (lookback_days if lookback_days is not None
                              else INTERVAL_MAX_DAYS.get(interval, 59))
        self.lags = list(lags) if lags is not None else [0, 5, 10, 15, 30, 60]
        self.hold_minutes = hold_minutes
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.min_rel_vol = min_rel_vol
        self.balanced = balanced
        self.min_cell_n = min_cell_n
        self.vrp_multiplier = vrp_multiplier
        self.spread_sim = spread_sim
        self.delta_target = delta_target
        self.opt_min_minutes_to_close = opt_min_minutes_to_close
        self.null_control = null_control
        self.control_per_session = control_per_session

    @property
    def bar_minutes(self) -> int:
        return INTERVAL_MINUTES[self.interval]

    def validate(self) -> None:
        """
        Reject anything the data cannot honestly answer, loudly and at startup.

        The important one is the lag ladder. A 1-minute lag on 5-minute bars is not
        a hard measurement — it is an interpolation, and interpolating a price
        between two bars invents the very thing being measured. So it is an error,
        not a warning.
        """
        if self.interval not in INTERVAL_MINUTES:
            raise ValueError(
                f"interval {self.interval!r} not supported; choose from "
                f"{sorted(INTERVAL_MINUTES)}"
            )

        bm = self.bar_minutes
        bad = [l for l in self.lags if l < 0 or l % bm != 0]
        if bad:
            measurable = [l for l in range(0, 61) if l % bm == 0]
            raise ValueError(
                f"lags {bad} are not measurable on {self.interval} bars — a lag must be a "
                f"whole number of bars or the entry price would have to be interpolated, "
                f"which fabricates the quantity under test. "
                f"Measurable lags at {self.interval}: {measurable}. "
                f"For finer lags use --interval 1m (only ~{INTERVAL_MAX_DAYS['1m']} days of history)."
            )

        if self.hold_minutes % bm != 0:
            raise ValueError(
                f"hold_minutes {self.hold_minutes} must be a whole number of "
                f"{self.interval} bars"
            )

        cap = INTERVAL_MAX_DAYS[self.interval]
        if self.lookback_days > cap:
            raise ValueError(
                f"{self.interval} history is capped at {cap} days by the data source; "
                f"asked for {self.lookback_days}. Beyond the cap the provider returns an "
                f"empty frame, which would read as 'no signals' rather than 'no data'."
            )

        if not self.lags:
            raise ValueError("lag ladder is empty — nothing to compare")


# ── Session slicing ────────────────────────────────────────────────────────────
def session_frames(bars: pd.DataFrame) -> List[Tuple[date, pd.DataFrame]]:
    """
    Split a tz-aware intraday frame into one frame per trading session, in order.

    Everything downstream is session-local. That is not a convenience — a 0DTE
    trade cannot be carried overnight, so an entry or exit that would fall in the
    next session is not a worse trade, it is not a trade. Working inside a session
    slice makes that constraint structural instead of a check someone can forget.
    """
    if bars is None or bars.empty:
        return []

    idx = bars.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    local = idx.tz_convert(MARKET_TZ)
    bars = bars.copy()
    bars.index = local

    out: List[Tuple[date, pd.DataFrame]] = []
    for d, grp in bars.groupby(bars.index.date):
        grp = grp.dropna(how="all").sort_index()
        if len(grp):
            out.append((d, grp))
    out.sort(key=lambda t: t[0])
    return out


def session_minute(ts: pd.Timestamp) -> int:
    """Minutes since the 09:30 ET open. Drives the time-of-day breakdown."""
    return (ts.hour - 9) * 60 + (ts.minute - 30)


def tod_bucket(minute: int) -> str:
    for name, lo, hi in TOD_BUCKETS:
        if lo <= minute < hi:
            return name
    return "close" if minute >= TOD_BUCKETS[-1][1] else "open"


# ── Look-ahead choke point ─────────────────────────────────────────────────────
def entry_index_for_lag(
    ts: pd.DatetimeIndex,
    signal_idx: int,
    lag_minutes: int,
    bar_minutes: int,
) -> Optional[int]:
    """
    THE look-ahead guarantee lives here and nowhere else.

    `ts` is one session's bar START times. Bar i spans [ts[i], ts[i] + bar).
    The signal is computed from bars 0..i, so it cannot exist until bar i has
    closed:

        decision_time = ts[signal_idx] + bar_minutes

    A trader who needs `lag_minutes` to read the alert, pick a contract and send
    the order transacts at:

        entry_time = decision_time + lag_minutes

    We return the first bar STARTING at or after entry_time. The caller uses that
    bar's Open, which is the price printed at the instant the bar begins — so the
    entry price is fixed by information available strictly at or before
    entry_time. Nothing from inside the entry bar, and nothing after it, can move
    it.

    Returns None when the lag pushes the entry past the session's last bar; there
    is no such trade and we must not silently substitute a nearby one.
    """
    if signal_idx < 0 or signal_idx >= len(ts):
        return None

    decision_time = ts[signal_idx] + pd.Timedelta(minutes=bar_minutes)
    entry_time = decision_time + pd.Timedelta(minutes=lag_minutes)

    pos = int(ts.searchsorted(entry_time, side="left"))
    if pos >= len(ts):
        return None

    # Belt and braces: this is the invariant the whole study rests on.
    assert ts[pos] >= entry_time, (
        f"look-ahead violation: entry bar {ts[pos]} precedes entry time {entry_time}"
    )
    assert pos > signal_idx, (
        f"look-ahead violation: entry bar {pos} is not after signal bar {signal_idx}"
    )
    return pos


# ── Intraday signal detection ──────────────────────────────────────────────────
def detect_intraday_signals(
    sessions: List[Tuple[date, pd.DataFrame]],
    config: LagConfig,
) -> List[Dict]:
    """
    Run the scanner's signal primitives on intraday bars, session by session.

    These mirror core/backtest.detect_signals — inside bars, relative volume,
    breakouts, VWAP reclaims, EMA-cloud alignment, and the A+ confluence count —
    but computed on the intraday series, because that is what the live deck sees
    during a session. A daily detector fires once at the close and can never be
    tailed by five minutes.

    Relative volume is measured against the SAME SLOT in prior sessions (bar 3 vs
    bar 3 of previous days), not against a trailing intraday window. A trailing
    window needs ~20 bars of session before it means anything, which would delete
    the entire opening hour — the exact period where decay is most likely and most
    worth measuring. The same-slot baseline uses only completed prior sessions, so
    it stays look-ahead free.
    """
    signals: List[Dict] = []

    # slot -> volumes observed at that slot in COMPLETED prior sessions
    slot_vols: Dict[int, List[float]] = defaultdict(list)
    prior_session_close: Optional[float] = None

    for sess_date, frame in sessions:
        o = frame["Open"].values.astype(float)
        h = frame["High"].values.astype(float)
        l = frame["Low"].values.astype(float)
        c = frame["Close"].values.astype(float)
        v = frame["Volume"].values.astype(float)
        ts = frame.index
        n = len(frame)

        close_pd = pd.Series(c)
        ema9 = close_pd.ewm(span=9, adjust=False).mean().values
        ema21 = close_pd.ewm(span=21, adjust=False).mean().values

        # Session VWAP, cumulative — the intraday analogue of the daily MVWAP the
        # backtest uses. Value at bar i uses bars 0..i only.
        typical = (h + l + c) / 3.0
        cum_pv = np.cumsum(typical * v)
        cum_v = np.cumsum(v)
        vwap = np.divide(cum_pv, cum_v, out=np.full(n, np.nan), where=cum_v > 0)

        sess_open = float(o[0]) if n else 0.0
        gap_pct = ((sess_open - prior_session_close) / prior_session_close * 100.0
                   if prior_session_close else 0.0)

        for i in range(2, n - 1):   # need prior bars, and at least one bar ahead
            price = float(c[i])
            if price <= 0:
                continue

            prior_h, prior_l = float(h[i - 1]), float(l[i - 1])

            base = slot_vols.get(i, [])
            # Fewer than 5 prior samples is not a baseline, it is noise. Signals
            # that need relative volume simply do not fire until it exists.
            rel_vol = (float(v[i]) / float(np.mean(base))
                       if len(base) >= 5 and np.mean(base) > 0 else None)

            types: List[str] = []
            dirs: List[str] = []
            setup_q = 0.0

            e9, e21 = float(ema9[i]), float(ema21[i])
            vw = float(vwap[i]) if not np.isnan(vwap[i]) else None
            vw_prev = float(vwap[i - 1]) if not np.isnan(vwap[i - 1]) else None

            ema_bull = price > e9 > e21
            ema_bear = price < e9 < e21

            # ── Inside bar (compression) ────────────────────────────────────
            is_inside = h[i] <= prior_h and l[i] >= prior_l
            if is_inside:
                mid = (prior_h + prior_l) / 2.0
                types.append("inside")
                dirs.append("up" if price > mid else "down")
                setup_q = max(setup_q, 0.60)

            # ── High relative volume ────────────────────────────────────────
            if rel_vol is not None and rel_vol >= config.min_rel_vol:
                types.append("highvol")
                dirs.append("up" if c[i] > o[i] else "down")
                setup_q = max(setup_q, min(1.0, (rel_vol - 1.5) / 3.0 + 0.40))

            # ── Breakout of the prior bar's range on volume ─────────────────
            if rel_vol is not None and rel_vol > 1.2 and not is_inside:
                if price > prior_h:
                    types.append("breakout")
                    dirs.append("up")
                    setup_q = max(setup_q, min(1.0, (rel_vol - 1.0) / 3.0 + 0.50))
                elif price < prior_l:
                    types.append("breakout")
                    dirs.append("down")
                    setup_q = max(setup_q, min(1.0, (rel_vol - 1.0) / 3.0 + 0.50))

            # ── VWAP reclaim ────────────────────────────────────────────────
            if vw is not None and vw_prev is not None and c[i - 1] < vw_prev and price >= vw:
                types.append("vwap_reclaim")
                dirs.append("up")
                setup_q = max(setup_q, 0.65)

            # ── Opening-range break ─────────────────────────────────────────
            # The first 30 minutes set the day's reference range; a break of it on
            # volume is the classic intraday continuation trigger.
            orb_bars = max(1, 30 // config.bar_minutes)
            if i >= orb_bars and rel_vol is not None and rel_vol > 1.2:
                or_hi = float(np.max(h[:orb_bars]))
                or_lo = float(np.min(l[:orb_bars]))
                if price > or_hi and float(c[i - 1]) <= or_hi:
                    types.append("orb_break")
                    dirs.append("up")
                    setup_q = max(setup_q, 0.70)
                elif price < or_lo and float(c[i - 1]) >= or_lo:
                    types.append("orb_break")
                    dirs.append("down")
                    setup_q = max(setup_q, 0.70)

            if not types:
                continue

            # ── A+ confluence, same counting rule as the daily engine ───────
            bull = bear = 0
            if is_inside:
                bull += 1
                bear += 1
            if rel_vol is not None and rel_vol >= 1.5:
                bull += 1
                bear += 1
            if ema_bull:
                bull += 1
            if ema_bear:
                bear += 1
            if vw is not None and price > vw:
                bull += 1
            if vw is not None and price < vw:
                bear += 1
            if price > prior_h:
                bull += 1
            if price < prior_l:
                bear += 1
            if gap_pct > 0.25:
                bull += 1
            elif gap_pct < -0.25:
                bear += 1

            aplus = max(bull, bear)
            if bull >= 4:
                types.append("a_plus")
                dirs.append("up")
                setup_q = max(setup_q, 0.80 + min(0.20, (bull - 4) * 0.10))
            elif bear >= 4:
                types.append("a_plus")
                dirs.append("down")
                setup_q = max(setup_q, 0.80 + min(0.20, (bear - 4) * 0.10))

            up_votes = dirs.count("up")
            dn_votes = dirs.count("down")
            direction = "up" if up_votes >= dn_votes else "down"

            primary = "a_plus" if "a_plus" in types else types[0]
            minute = session_minute(ts[i])

            signals.append({
                "session_date": sess_date.isoformat(),
                "bar_index": i,
                "ts": ts[i].isoformat(),
                "session_minute": minute,
                "tod": tod_bucket(minute),
                "signal": primary,
                "signal_types": types,
                "direction": direction,
                "signal_price": round(price, 4),
                "rel_vol": round(rel_vol, 2) if rel_vol is not None else None,
                "gap_pct": round(gap_pct, 3),
                "setup_q": round(setup_q, 3),
                "aplus_score": aplus,
                "ema_bull": bool(ema_bull),
            })

        # Only now, with the session complete, does it join the baseline.
        for i in range(n):
            slot_vols[i].append(float(v[i]))
        prior_session_close = float(c[-1])

    return signals


def random_control_signals(
    sessions: List[Tuple[date, pd.DataFrame]],
    per_session: int,
    seed: int = 0,
) -> List[Dict]:
    """
    Coin-flip signals at random times — the null control.

    A "no edge" verdict is only meaningful if the instrument could have found an
    edge had one been there. This generates signals with no informational content
    at all and pushes them through the identical evaluation path. Two readings
    matter:

      control ~= 0            the measurement pipeline is unbiased; a zero result
                              from real signals is a real zero, not an artifact of
                              slippage conventions or the bracket rule
      real signals ~= control the signals carry no information beyond picking a
                              random moment — the sharpest available statement of
                              explanation (1)

    Without this, a near-zero result is ambiguous between "no edge" and "my
    harness quietly subtracts the edge".
    """
    rng = np.random.default_rng(seed)
    out: List[Dict] = []
    for sess_date, frame in sessions:
        n = len(frame)
        if n < 10:
            continue
        for idx in rng.choice(np.arange(2, n - 1), size=min(per_session, n - 3),
                              replace=False):
            i = int(idx)
            minute = session_minute(frame.index[i])
            out.append({
                "session_date": sess_date.isoformat(),
                "bar_index": i,
                "ts": frame.index[i].isoformat(),
                "session_minute": minute,
                "tod": tod_bucket(minute),
                "signal": "control",
                "signal_types": ["control"],
                "direction": "up" if rng.random() < 0.5 else "down",
                "signal_price": round(float(frame["Close"].iloc[i]), 4),
                "rel_vol": None,
                "gap_pct": 0.0,
                "setup_q": 0.5,
                "aplus_score": 0,
                "ema_bull": False,
            })
    return out


# ── Lag evaluation ─────────────────────────────────────────────────────────────
def _bracketed_return(
    h: np.ndarray, l: np.ndarray, c: np.ndarray,
    entry_idx: int, exit_idx: int, entry_px: float,
    direction: str, stop_pct: float, target_pct: float,
) -> Tuple[float, str]:
    """
    Walk forward from the entry bar applying a stop and a target in underlying %.

    Path ambiguity: within a single bar we cannot know whether the high or the low
    printed first. When a bar touches both levels we record the STOP. That is the
    conservative resolution and it is applied identically at every lag, so it
    cannot manufacture or hide a decay slope — it only shifts the whole curve
    down. Stating it matters because the opposite convention would flatter every
    number here.
    """
    if direction == "up":
        stop_px = entry_px * (1 - stop_pct / 100.0)
        targ_px = entry_px * (1 + target_pct / 100.0)
    else:
        stop_px = entry_px * (1 + stop_pct / 100.0)
        targ_px = entry_px * (1 - target_pct / 100.0)

    for k in range(entry_idx, exit_idx + 1):
        hit_stop = (l[k] <= stop_px) if direction == "up" else (h[k] >= stop_px)
        hit_targ = (h[k] >= targ_px) if direction == "up" else (l[k] <= targ_px)
        if hit_stop:
            return -stop_pct, "stop"
        if hit_targ:
            return target_pct, "target"

    exit_px = float(c[exit_idx])
    raw = (exit_px - entry_px) / entry_px * 100.0
    return (raw if direction == "up" else -raw), "time"


def evaluate_entry_lags(
    ticker: str,
    signals: List[Dict],
    sessions: List[Tuple[date, pd.DataFrame]],
    config: LagConfig,
    ticker_meta: Dict[str, Any] = None,
) -> List[Dict]:
    """
    Expand each signal into one record per lag rung.

    Three outcome measures per rung, deliberately:

      signed_return_pct — enter at lag L, hold `hold_minutes` FROM THAT ENTRY,
                          exit at the close of the hold. This is the realistic
                          experience: a late entry still gets its full hold.
      bracketed_pct     — the same trade with the stop/target applied. This is
                          what he actually trades, so it is the expectancy number.
      clock_return_pct  — enter at lag L, exit at the FIXED clock time the lag-0
                          trade would have exited. Holding shrinks as lag grows.
                          This is the pure diagnostic: it isolates "how much of the
                          move did I already miss" from "I had less time in it".

      Reporting only the first would confound a shorter hold with a worse entry.
      Reporting only the third would understate what a real trader gets. Both are
      needed to read the curve honestly.
    """
    by_date = {d.isoformat(): f for d, f in sessions}
    meta = ticker_meta or {}
    hold_bars = config.hold_minutes // config.bar_minutes
    records: List[Dict] = []

    for sig in signals:
        frame = by_date.get(sig["session_date"])
        if frame is None:
            continue

        ts = frame.index
        o = frame["Open"].values.astype(float)
        h = frame["High"].values.astype(float)
        l = frame["Low"].values.astype(float)
        c = frame["Close"].values.astype(float)
        n = len(frame)
        i = sig["bar_index"]
        direction = sig["direction"]

        # ── Balanced panel ──────────────────────────────────────────────────
        # Late-session signals stop being evaluable as the lag grows: at +60 a
        # 15:20 signal has nowhere to go. If we let them drop out rung by rung,
        # the population changes down the ladder and we would be reading a
        # composition shift as decay. So by default a signal is admitted only if
        # every rung of the ladder — entry AND full hold — fits inside its
        # session. The count of signals rejected for this reason is reported, and
        # --unbalanced turns it off for anyone who wants the raw view.
        feasible = {}
        for lag in config.lags:
            ei = entry_index_for_lag(ts, i, lag, config.bar_minutes)
            if ei is None or ei + hold_bars >= n:
                feasible[lag] = None
            else:
                feasible[lag] = ei

        if config.balanced and any(v is None for v in feasible.values()):
            continue

        # Fixed clock exit = where the lag-0 trade would have exited. Same wall
        # clock instant for every rung, which is what makes clock_return_pct
        # comparable across the ladder.
        base_entry = feasible.get(config.lags[0])
        clock_exit_idx = (base_entry + hold_bars) if base_entry is not None else None

        # Grade comes from the scanner's own trade_grade(), not a local copy of
        # its thresholds, so an A here is the same A the live deck prints. The
        # options-quality half of the score is a per-ticker property (liquidity,
        # IV rank) computed once from daily bars; only setup_q varies per signal.
        grade = strip_ansi(trade_grade(
            sig["setup_q"], int(meta.get("opt_score", 0)), bool(meta.get("has_contract", True))
        ))

        # Options-overlay inputs are fixed AT SIGNAL TIME. The scanner picks the
        # contract when it fires; a tailer buys that same contract later, at a
        # worse price. Re-picking the strike at each lag would quietly repair the
        # entry and erase the very effect under test.
        sig_px = float(sig["signal_price"])
        hv = float(meta.get("hv20") or 0.0)
        iv = hv * config.vrp_multiplier if hv > 0 else None
        minutes_to_close = None
        strike = None
        if iv:
            close_ts = ts[-1] + pd.Timedelta(minutes=config.bar_minutes)
            decision_ts = ts[i] + pd.Timedelta(minutes=config.bar_minutes)
            minutes_to_close = max(0.0, (close_ts - decision_ts).total_seconds() / 60.0)
            if minutes_to_close >= config.opt_min_minutes_to_close:
                T0 = minutes_to_close / (365.0 * 24 * 60)
                strike = find_strike(sig_px, direction, config.delta_target, iv, T0)

        for lag in config.lags:
            ei = feasible.get(lag)
            if ei is None:
                continue

            entry_px = float(o[ei])
            if entry_px <= 0:
                continue

            exit_idx = ei + hold_bars
            exit_px = float(c[exit_idx])

            raw = (exit_px - entry_px) / entry_px * 100.0
            signed = raw if direction == "up" else -raw

            brk, exit_reason = _bracketed_return(
                h, l, c, ei, exit_idx, entry_px, direction,
                config.stop_pct, config.target_pct,
            )

            clock_signed = None
            if clock_exit_idx is not None and clock_exit_idx >= ei:
                cpx = float(c[clock_exit_idx])
                craw = (cpx - entry_px) / entry_px * 100.0
                clock_signed = craw if direction == "up" else -craw

            # Slippage from the signal print to the actual fill — the direct,
            # model-free measure of "how much did the move run away from me".
            slip = (entry_px - sig_px) / sig_px * 100.0
            adverse_slip = slip if direction == "up" else -slip

            opt_pnl_pct = None
            if strike is not None and iv:
                opt_type = "call" if direction == "up" else "put"
                t_entry = max(0.0, minutes_to_close - lag) / (365.0 * 24 * 60)
                t_exit = max(0.0, minutes_to_close - lag - config.hold_minutes) / (365.0 * 24 * 60)
                ent_mid = bs_price(entry_px, strike, t_entry, iv, opt_type)
                if ent_mid > 0.05:
                    ent = ent_mid * (1 + config.spread_sim)
                    ex_mid = bs_price(exit_px, strike, t_exit, iv, opt_type)
                    ex = ex_mid * (1 - config.spread_sim)
                    opt_pnl_pct = round((ex - ent) / ent * 100.0, 2)

            records.append({
                "ticker": ticker,
                "session_date": sig["session_date"],
                "ts": sig["ts"],
                "session_minute": sig["session_minute"],
                "tod": sig["tod"],
                "signal": sig["signal"],
                "signal_types": sig["signal_types"],
                "direction": direction,
                "setup_q": sig["setup_q"],
                "rel_vol": sig["rel_vol"],
                "grade": grade,
                "lag_min": lag,
                "signal_price": round(sig_px, 4),
                "entry_price": round(entry_px, 4),
                "exit_price": round(exit_px, 4),
                "adverse_slip_pct": round(adverse_slip, 4),
                "signed_return_pct": round(signed, 4),
                "bracketed_pct": round(brk, 4),
                "clock_return_pct": round(clock_signed, 4) if clock_signed is not None else None,
                "exit_reason": exit_reason,
                "opt_pnl_pct": opt_pnl_pct,
                "sim_strike": strike,
            })

    return records


# ── Aggregation ────────────────────────────────────────────────────────────────
def cell_stats(rows: List[Dict], min_n: int = MIN_CELL_N) -> Dict:
    """
    Summarize one cell of the grid, refusing to summarize a cell that is too small.

    Below min_n we return the count and nothing else. Not zeros, not nulls dressed
    up as numbers — `sufficient: False`, so a consumer of the JSON cannot
    accidentally chart a win rate computed off four trades.

    A standard error and a t-stat accompany every reported mean, because the whole
    point of the exercise is comparing means across lags. Without a t-stat, a
    2-basis-point difference between rungs reads as a trend.
    """
    n = len(rows)
    out: Dict[str, Any] = {"n": n, "sufficient": n >= min_n, "min_n": min_n}
    if n < min_n:
        return out

    signed = np.array([r["signed_return_pct"] for r in rows], dtype=float)
    brk = np.array([r["bracketed_pct"] for r in rows], dtype=float)
    slip = np.array([r["adverse_slip_pct"] for r in rows], dtype=float)
    clock = np.array([r["clock_return_pct"] for r in rows
                      if r.get("clock_return_pct") is not None], dtype=float)

    sd = float(signed.std(ddof=1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    mean = float(signed.mean())

    wins = brk[brk > 0]
    losses = brk[brk <= 0]

    out.update({
        "thin": n < THIN_CELL_N,
        "win_rate": round(float((signed > 0).mean()), 4),
        "avg_return_pct": round(mean, 4),
        "median_return_pct": round(float(np.median(signed)), 4),
        "std_pct": round(sd, 4),
        "stderr_pct": round(se, 4),
        "t_stat": round(mean / se, 3) if se > 0 else None,
        "expectancy_pct": round(float(brk.mean()), 4),
        "bracket_win_rate": round(float((brk > 0).mean()), 4),
        "profit_factor": (round(float(wins.sum() / abs(losses.sum())), 3)
                          if len(losses) and losses.sum() != 0 else None),
        "avg_adverse_slip_pct": round(float(slip.mean()), 4),
        "avg_clock_return_pct": round(float(clock.mean()), 4) if len(clock) else None,
    })
    return out


def aggregate(records: List[Dict], config: LagConfig,
              dims: Tuple[str, ...] = ()) -> Dict[str, Dict]:
    """
    Build the lag ladder, optionally split by extra dimensions (grade, tod).

    Keys are "|"-joined dimension values ending in the lag, so a consumer can
    split them back apart without a schema.
    """
    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for r in records:
        key = "|".join([str(r.get(d, "?")) for d in dims] + [str(r["lag_min"])])
        buckets[key].append(r)
    return {k: cell_stats(v, config.min_cell_n) for k, v in sorted(buckets.items())}


def decay_verdict(ladder: Dict[str, Dict], config: LagConfig) -> Dict:
    """
    Turn the lag-0 vs longest-lag comparison into a statement — or into an
    explicit refusal to make one.

    This is the part most likely to be over-read, so it is deliberately hard to
    get a strong answer out of. It requires:
      - both endpoint cells at or above the sample floor
      - lag-0 expectancy to be distinguishable from zero (|t| >= 2) before any
        claim that there was an edge to decay in the first place

    Without an edge at lag 0 there is nothing to decay, and the correct output is
    "no measurable edge at emit time" — which is explanation (1), not (2).
    """
    lags = sorted(config.lags)
    first, last = ladder.get(str(lags[0]), {}), ladder.get(str(lags[-1]), {})

    if not first.get("sufficient") or not last.get("sufficient"):
        return {
            "verdict": "INSUFFICIENT_DATA",
            "detail": (f"lag {lags[0]} n={first.get('n', 0)}, lag {lags[-1]} "
                       f"n={last.get('n', 0)}; need >= {config.min_cell_n} at both ends"),
        }

    t0 = first.get("t_stat")
    e0 = first["avg_return_pct"]
    e1 = last["avg_return_pct"]

    if t0 is None or abs(t0) < 2.0:
        # "Not significant" is a weak, easily-misread statement. The useful form is
        # the upper end of the confidence interval: the largest edge the data is
        # still consistent with. If even THAT is below trading costs, the finding
        # is decision-grade rather than merely inconclusive.
        hi = e0 + 1.96 * first["stderr_pct"]
        return {
            "verdict": "NO_EDGE_AT_EMIT",
            "detail": (f"lag-0 mean signed return {e0:+.4f}% (t={t0}) is not "
                       f"distinguishable from zero at n={first['n']}. Nothing decays "
                       f"because nothing was there — explanation (1), not (2). "
                       f"95% CI upper bound on the edge is {hi:+.4f}% per trade on the "
                       f"underlying; any real edge is smaller than that. Compare it to "
                       f"your round-trip cost before concluding anything is tradeable."),
            "lag0_return": e0, "lagN_return": e1,
            "edge_ci95_high": round(hi, 5),
            "edge_ci95_low": round(e0 - 1.96 * first["stderr_pct"], 5),
        }

    # Both endpoints are real; is the drop itself bigger than the noise?
    se_diff = math.sqrt(first["stderr_pct"] ** 2 + last["stderr_pct"] ** 2)
    drop = e0 - e1
    t_drop = drop / se_diff if se_diff > 0 else 0.0
    retained = (e1 / e0) if e0 != 0 else 0.0

    if abs(t_drop) < 2.0:
        verdict = "EDGE_DURABLE"
        detail = (f"lag-0 {e0:+.4f}% vs lag-{lags[-1]} {e1:+.4f}%; the difference "
                  f"(t={t_drop:.2f}) is inside the noise. The edge survives the delay, "
                  f"so delay is not the leak — look at sizing and execution, "
                  f"explanation (3).")
    elif drop > 0:
        verdict = "EDGE_DECAYS"
        detail = (f"lag-0 {e0:+.4f}% falls to {e1:+.4f}% by +{lags[-1]}min "
                  f"({retained:.0%} retained, t={t_drop:.2f}). Real at emit, gone on "
                  f"delay — explanation (2).")
    else:
        verdict = "EDGE_IMPROVES_WITH_DELAY"
        detail = (f"lag-0 {e0:+.4f}% rises to {e1:+.4f}% (t={t_drop:.2f}). Entering "
                  f"later was better, which usually means the signal fires into an "
                  f"overextended bar and a pullback entry is superior.")

    return {"verdict": verdict, "detail": detail,
            "lag0_return": e0, "lagN_return": e1,
            "retained_frac": round(retained, 3), "t_drop": round(t_drop, 3)}


# ── Reporting ──────────────────────────────────────────────────────────────────
def _fmt_cell(c: Dict, field: str, pct: bool = True) -> str:
    if not c.get("sufficient"):
        return f"{Fore.WHITE}n={c.get('n', 0)} —{Style.RESET_ALL}"
    v = c.get(field)
    if v is None:
        return "—"
    s = f"{v:+.3f}%" if pct else f"{v:.1%}"
    if c.get("thin"):
        s = f"{Fore.YELLOW}{s}*{Style.RESET_ALL}"
    return s


def print_lag_report(records: List[Dict], config: LagConfig, meta: Dict = None) -> None:
    """Terminal report, in the same visual language as backtest.print_report()."""
    meta = meta or {}
    sep = Fore.WHITE + "─" * 78 + Style.RESET_ALL
    lags = sorted(config.lags)

    print(f"\n{Fore.CYAN + Style.BRIGHT}  D — ENTRY-LAG DECAY ANALYSIS{Style.RESET_ALL}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
          f"{len(records)} signal-lag rows  |  "
          f"{len(set(r['ticker'] for r in records))} tickers  |  "
          f"{config.interval} bars  |  {config.lookback_days}d lookback")
    print(f"  Hold: {config.hold_minutes}min from entry  |  "
          f"Bracket: -{config.stop_pct}% / +{config.target_pct}% underlying  |  "
          f"Panel: {'balanced' if config.balanced else 'unbalanced'}")
    print(sep)

    # ── Data-honesty block. Printed first, on purpose. ────────────────────────
    print(f"\n  {Style.BRIGHT}WHAT THIS CAN AND CANNOT SEE{Style.RESET_ALL}")
    print(f"    Bar resolution is {config.bar_minutes}min, so the finest measurable lag is "
          f"{config.bar_minutes}min.")
    if config.bar_minutes > 1:
        print(f"    Decay occurring INSIDE the first {config.bar_minutes} minutes is invisible "
              f"here — it")
        print(f"    would show up as a weak lag-0 cell, indistinguishable from 'no edge'.")
    print(f"    Reporting floor: n >= {config.min_cell_n} per cell. Cells below it show "
          f"n only.")
    print(f"    Cells marked {Fore.YELLOW}*{Style.RESET_ALL} are thin (n < {THIN_CELL_N}) — "
          f"directional at best.")
    if meta.get("dropped_unbalanced"):
        print(f"    {meta['dropped_unbalanced']} signals excluded: their full hold did not fit "
              f"the session at")
        print(f"    every lag rung. Excluding them keeps the population identical across the "
              f"ladder.")

    # The balanced panel needs max(lag) + hold minutes of session left, so it
    # amputates the end of the day. If that silently deletes a whole time-of-day
    # bucket, the "by time of day" table below is not merely thin there — it is
    # structurally blind, and saying so is the difference between a limitation and
    # a misreading.
    horizon = max(config.lags) + config.hold_minutes
    latest_minute = 390 - horizon
    # `>=`, not `>`: a signal at exactly `latest_minute` needs one more bar after
    # its exit to exist at all, so a bucket starting on the boundary is already
    # out of reach.
    missing = [name for name, lo, _ in TOD_BUCKETS if lo >= latest_minute]
    if missing:
        print(f"\n    {Fore.YELLOW}BLIND SPOT{Style.RESET_ALL}: a balanced panel needs "
              f"{horizon}min of session after the signal,")
        print(f"    so nothing after {9 + (latest_minute + 30)//60:02d}:"
              f"{(latest_minute + 30) % 60:02d} ET can be evaluated. "
              f"Bucket(s) {', '.join(missing)} are absent")
        print(f"    entirely — not measured, NOT measured-and-flat. Re-run with a shorter "
              f"--hold or a")
        print(f"    shorter lag ladder to see the close.")

    # ── Headline ladder ───────────────────────────────────────────────────────
    ladder = aggregate(records, config)
    print(f"\n{sep}")
    print(f"  {Style.BRIGHT}DECAY LADDER — ALL SIGNALS{Style.RESET_ALL}")
    rows = []
    for lag in lags:
        c = ladder.get(str(lag), {"n": 0, "sufficient": False})
        if not c.get("sufficient"):
            rows.append([f"+{lag}m", c.get("n", 0), "insufficient", "", "", "", "", ""])
            continue
        rows.append([
            f"+{lag}m", c["n"],
            f"{c['win_rate']:.1%}" + ("*" if c["thin"] else ""),
            f"{c['avg_return_pct']:+.4f}%",
            f"±{c['stderr_pct']:.4f}",
            f"{c['t_stat']:+.2f}" if c["t_stat"] is not None else "—",
            f"{c['expectancy_pct']:+.4f}%",
            f"{c['avg_adverse_slip_pct']:+.4f}%",
        ])
    print(tabulate(rows, headers=["lag", "n", "win%", "avg ret", "SE", "t", "expectancy", "slip"],
                   tablefmt="simple"))
    print(f"\n  avg ret    = mean signed underlying move, entry at lag -> +{config.hold_minutes}min")
    print(f"  t          = avg ret / SE. |t| < 2 means not distinguishable from zero.")
    print(f"  expectancy = same trade with the -{config.stop_pct}%/+{config.target_pct}% bracket applied")
    print(f"  slip       = how far price had already moved against you by entry time")

    # ── Fixed-clock diagnostic ────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  {Style.BRIGHT}FIXED-CLOCK DIAGNOSTIC{Style.RESET_ALL}  "
          f"(all rungs exit at the SAME instant the +{lags[0]}m trade did)")
    print(f"  Isolates 'the move already happened' from 'I had less time in the trade'.")
    crows = []
    for lag in lags:
        c = ladder.get(str(lag), {})
        crows.append([f"+{lag}m", c.get("n", 0),
                      f"{c['avg_clock_return_pct']:+.4f}%"
                      if c.get("sufficient") and c.get("avg_clock_return_pct") is not None
                      else "insufficient"])
    print(tabulate(crows, headers=["lag", "n", "avg ret to fixed exit"], tablefmt="simple"))

    # ── By grade ──────────────────────────────────────────────────────────────
    for dim, title in (("grade", "BY SIGNAL GRADE"), ("tod", "BY TIME OF DAY")):
        grid = aggregate(records, config, dims=(dim,))
        keys = sorted({k.rsplit("|", 1)[0] for k in grid})
        if not keys:
            continue
        print(f"\n{sep}")
        print(f"  {Style.BRIGHT}{title}{Style.RESET_ALL}  (expectancy %, n in parens)")
        grows = []
        for kv in keys:
            row = [kv]
            for lag in lags:
                c = grid.get(f"{kv}|{lag}", {"n": 0, "sufficient": False})
                if not c.get("sufficient"):
                    row.append(f"— ({c.get('n', 0)})")
                else:
                    star = "*" if c["thin"] else ""
                    row.append(f"{c['expectancy_pct']:+.3f}{star} ({c['n']})")
            grows.append(row)
        print(tabulate(grows, headers=[dim] + [f"+{l}m" for l in lags], tablefmt="simple"))

    suppressed = sum(1 for g in (aggregate(records, config, dims=("grade",)),
                                 aggregate(records, config, dims=("tod",)))
                     for c in g.values() if not c.get("sufficient"))
    if suppressed:
        print(f"\n  {suppressed} breakdown cells suppressed for n < {config.min_cell_n}.")

    # ── Options overlay, quarantined ──────────────────────────────────────────
    opt_rows = [r for r in records if r.get("opt_pnl_pct") is not None]
    print(f"\n{sep}")
    print(f"  {Style.BRIGHT}OPTIONS OVERLAY{Style.RESET_ALL}  "
          f"{Fore.YELLOW}(MODEL — do not base a decision on this){Style.RESET_ALL}")
    if len(opt_rows) < config.min_cell_n:
        print(f"    Only {len(opt_rows)} rows priced (needs >= {config.opt_min_minutes_to_close}min "
              f"to the close). Not reported.")
    else:
        print(f"    Black-Scholes on IV = HV20 x {config.vrp_multiplier}, strike fixed at signal "
              f"time,")
        print(f"    {config.spread_sim:.0%} spread. Real 0DTE premia are driven by IV moves this "
              f"model has no view of.")
        orows = []
        for lag in lags:
            sub = [r for r in opt_rows if r["lag_min"] == lag]
            if len(sub) < config.min_cell_n:
                orows.append([f"+{lag}m", len(sub), "insufficient", ""])
                continue
            p = np.array([r["opt_pnl_pct"] for r in sub], dtype=float)
            orows.append([f"+{lag}m", len(sub), f"{float((p > 0).mean()):.1%}",
                          f"{float(p.mean()):+.1f}%"])
        print(tabulate(orows, headers=["lag", "n", "win%", "avg contract P&L"], tablefmt="simple"))

    # ── Verdict ───────────────────────────────────────────────────────────────
    v = decay_verdict(ladder, config)
    print(f"\n{sep}")
    colour = {"EDGE_DECAYS": Fore.YELLOW, "EDGE_DURABLE": Fore.GREEN,
              "NO_EDGE_AT_EMIT": Fore.RED}.get(v["verdict"], Fore.WHITE)
    print(f"  {Style.BRIGHT}VERDICT: {colour}{v['verdict']}{Style.RESET_ALL}")
    print(f"  {v['detail']}")
    print(f"\n  {Fore.WHITE}This distinguishes (1) no edge from (2) decaying edge. It cannot")
    print(f"  speak to (3) sizing — that lives in the journal, not in price data.{Style.RESET_ALL}")
    print(f"{sep}\n")


def save_lag_results(records: List[Dict], config: LagConfig, meta: Dict = None) -> str:
    """Persist CSV + JSON into results/, matching backtest.save_results() naming."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"entrylag_{config.lookback_days}d_{config.interval}_{ts}"

    csv_name = f"{stem}.csv"
    if records:
        df = pd.DataFrame(records)
        df["signal_types"] = df["signal_types"].apply(
            lambda x: "|".join(x) if isinstance(x, list) else x)
        df.to_csv(RESULTS_DIR / csv_name, index=False)

    # The JSON carries the ANALYSIS; the CSV carries the rows. backtest.py embeds
    # its records in both because it produces a few thousand — this module
    # produces a few hundred thousand (signals x lag rungs), where embedding them
    # means a ~136MB JSON that duplicates the CSV byte for byte. Since results/
    # is a committed directory here, that is a live footgun, not just waste. The
    # JSON points at the CSV instead.
    ladder = aggregate(records, config)
    payload = {
        "config": {k: v for k, v in vars(config).items() if not callable(v)},
        "run_at": datetime.now().isoformat(),
        "meta": meta or {},
        "n_records": len(records),
        "records_csv": csv_name if records else None,
        "ladder": ladder,
        "by_grade": aggregate(records, config, dims=("grade",)),
        "by_tod": aggregate(records, config, dims=("tod",)),
        "by_grade_tod": aggregate(records, config, dims=("grade", "tod")),
        "verdict": decay_verdict(ladder, config),
    }
    json_path = RESULTS_DIR / f"{stem}.json"
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    return str(json_path)


# ── Runner ─────────────────────────────────────────────────────────────────────
def fetch_ticker_meta(tickers: List[str], lookback_days: int) -> Dict[str, Dict]:
    """
    Per-ticker daily-bar context: HV20 for the IV proxy, average volume and IV rank
    for the options score that feeds trade_grade.

    Daily bars are the right source for these even though the study is intraday —
    HV20 and IV rank are day-scale quantities, and reusing the scanner's own
    calc_options_score / calc_iv_rank_proxy keeps grades consistent with the deck.
    """
    out: Dict[str, Dict] = {}
    try:
        raw = yf.download(
            tickers, period="1y", interval="1d", group_by="ticker",
            progress=False, auto_adjust=True, threads=True, timeout=30,
        )
    except Exception:
        return {t: {} for t in tickers}

    for t in tickers:
        try:
            hist = _extract_ticker_hist(raw, t) if isinstance(raw.columns, pd.MultiIndex) \
                else raw.dropna(how="all")
            if hist is None or hist.empty or len(hist) < 30:
                out[t] = {}
                continue
            ivr = calc_iv_rank_proxy(hist)
            avg_vol = float(hist["Volume"].tail(20).mean())
            out[t] = {
                "hv20": calc_hv(hist["Close"]),
                "avg_vol": avg_vol,
                "opt_score": calc_options_score(avg_vol, ivr.get("ivr_score", 50) / 100.0),
                "has_contract": True,
            }
        except Exception:
            out[t] = {}
    return out


def run_lag_analysis(config: LagConfig) -> Tuple[List[Dict], Dict]:
    """
    Fetch intraday bars, detect signals, expand across the lag ladder.

    Returns (records, meta). An empty records list with a populated meta is a
    legitimate outcome and the caller must report it as such rather than as a
    failure — "the data does not support this analysis" is a finding.
    """
    config.validate()

    print(f"\n{Fore.CYAN + Style.BRIGHT}  D — ENTRY-LAG DECAY ANALYSIS{Style.RESET_ALL}")
    print(f"  {len(config.tickers)} tickers  |  {config.interval} bars  |  "
          f"{config.lookback_days}d  |  lags {config.lags}")
    print(f"  {Fore.CYAN}Downloading intraday bars...{Style.RESET_ALL}", end="", flush=True)

    try:
        raw = yf.download(
            config.tickers, period=f"{config.lookback_days}d", interval=config.interval,
            group_by="ticker", progress=False, auto_adjust=True, threads=True, timeout=60,
        )
    except Exception as e:
        print(f" ERROR: {e}")
        return [], {"error": str(e), "fatal": "intraday download failed"}

    if raw is None or raw.empty:
        print(" EMPTY")
        return [], {"fatal": (
            f"the data source returned no {config.interval} bars for "
            f"{config.lookback_days}d. Without intraday history a lag analysis is "
            f"impossible — daily bars cannot express a 5-minute delay."
        )}
    print(" done")

    meta_by_ticker = fetch_ticker_meta(config.tickers, config.lookback_days)

    all_records: List[Dict] = []
    total_signals = 0
    dropped = 0
    sessions_seen: set = set()
    failed = 0

    for n_i, ticker in enumerate(config.tickers, 1):
        sys.stdout.write(f"\r  Processing [{n_i}/{len(config.tickers)}] "
                         f"{Fore.CYAN}{ticker:<6}{Style.RESET_ALL}  ")
        sys.stdout.flush()
        try:
            bars = (_extract_ticker_hist(raw, ticker)
                    if isinstance(raw.columns, pd.MultiIndex) else raw.dropna(how="all"))
            if bars is None or bars.empty:
                failed += 1
                continue

            sessions = session_frames(bars)
            if len(sessions) < 5:
                failed += 1
                continue
            sessions_seen.update(d for d, _ in sessions)

            signals = (random_control_signals(sessions, config.control_per_session)
                       if config.null_control
                       else detect_intraday_signals(sessions, config))
            total_signals += len(signals)
            if not signals:
                continue

            recs = evaluate_entry_lags(ticker, signals, sessions, config,
                                       meta_by_ticker.get(ticker, {}))
            # A signal admitted to the balanced panel contributes exactly one row
            # per rung; anything short of that was dropped for session fit.
            dropped += len(signals) - (len(recs) // max(1, len(config.lags)))
            all_records.extend(recs)
        except Exception:
            failed += 1
            continue

    sys.stdout.write("\r" + " " * 78 + "\r")
    meta = {
        "tickers_ok": len(config.tickers) - failed,
        "tickers_failed": failed,
        "sessions": len(sessions_seen),
        "signals_detected": total_signals,
        "signals_evaluated": len(all_records) // max(1, len(config.lags)),
        "dropped_unbalanced": max(0, dropped),
        "bar_minutes": config.bar_minutes,
    }
    print(f"  Sessions: {meta['sessions']}  |  Signals: {total_signals}  |  "
          f"Evaluated: {meta['signals_evaluated']}  |  "
          f"Dropped (session fit): {meta['dropped_unbalanced']}  |  Failed: {failed}")
    return all_records, meta


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="D — Entry-Lag Decay Analysis (does the scanner's edge survive delay?)",
        epilog=("Lags must be whole multiples of the bar interval; fractional lags are "
                "rejected rather than interpolated."),
    )
    parser.add_argument("--days", type=int, default=None,
                        help="Lookback days (default: the provider cap for the chosen "
                             "interval — 59 for 5m/15m, 7 for 1m)")
    parser.add_argument("--interval", default="5m",
                        choices=sorted(INTERVAL_MINUTES),
                        help="Bar interval (default: 5m). 1m gives finer lags but only ~7 days.")
    parser.add_argument("--lags", nargs="+", type=int, metavar="MIN",
                        help="Entry-lag ladder in minutes (default: 0 5 10 15 30 60)")
    parser.add_argument("--hold", type=int, default=60,
                        help="Hold minutes from entry (default: 60)")
    parser.add_argument("--tickers", nargs="+", metavar="TICKER",
                        help="Specific tickers (default: UNIVERSE[:40])")
    parser.add_argument("--all", action="store_true",
                        help="Use full UNIVERSE (slow — intraday payloads are large)")
    parser.add_argument("--stop", type=float, default=0.40,
                        help="Bracket stop, underlying %% (default: 0.40)")
    parser.add_argument("--target", type=float, default=0.60,
                        help="Bracket target, underlying %% (default: 0.60)")
    parser.add_argument("--min-n", type=int, default=MIN_CELL_N,
                        help=f"Minimum n to report a cell (default: {MIN_CELL_N})")
    parser.add_argument("--unbalanced", action="store_true",
                        help="Keep signals not evaluable at every lag (changes population "
                             "down the ladder — read with care)")
    parser.add_argument("--null-control", action="store_true",
                        help="Replace real signals with random-time, random-direction ones. "
                             "Run this alongside the real study: it shows whether a flat "
                             "result means 'no edge' or 'broken harness'.")
    parser.add_argument("--no-save", action="store_true",
                        help="Don't write results to disk")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif args.all:
        tickers = list(UNIVERSE)
    else:
        tickers = list(UNIVERSE[:40])

    config = LagConfig(
        tickers=tickers, lookback_days=args.days, interval=args.interval,
        lags=args.lags, hold_minutes=args.hold, stop_pct=args.stop,
        target_pct=args.target, min_cell_n=args.min_n, balanced=not args.unbalanced,
        null_control=args.null_control,
    )
    if args.null_control:
        print(f"\n  {Fore.YELLOW}NULL CONTROL MODE — signals are random, not scanner "
              f"output.{Style.RESET_ALL}")

    try:
        config.validate()
    except ValueError as e:
        print(f"\n  {Fore.RED}CONFIG REJECTED{Style.RESET_ALL}\n  {e}\n")
        sys.exit(2)

    records, meta = run_lag_analysis(config)

    if meta.get("fatal"):
        print(f"\n  {Fore.RED}CANNOT RUN THIS ANALYSIS{Style.RESET_ALL}")
        print(f"  {meta['fatal']}")
        print(f"\n  What would be needed: intraday OHLCV at {config.interval} or finer,")
        print(f"  covering enough sessions to put >= {config.min_cell_n} signals in every")
        print(f"  grade x lag cell. Nothing here will synthesize it.\n")
        sys.exit(1)

    if not records:
        print(f"\n  {Fore.YELLOW}NO EVALUABLE SIGNALS{Style.RESET_ALL}")
        print(f"  {meta.get('signals_detected', 0)} signals detected but none survived "
              f"the balanced-panel filter.")
        print(f"  Try a shorter --hold, a shorter lag ladder, or --unbalanced.\n")
        sys.exit(1)

    print_lag_report(records, config, meta)

    if not args.no_save:
        path = save_lag_results(records, config, meta)
        print(f"  Saved: {path}\n")


if __name__ == "__main__":
    main()
