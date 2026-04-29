#!/usr/bin/env python3
"""
backtest.py — Scanner Signal Backtester
D — Quant Validation Engine

Tests the scanner's signals against historical data.
Answers the real question: are these setups actually profitable?

Usage:
  python3 backtest.py                        # full backtest, last 60 days
  python3 backtest.py --days 90              # last 90 days
  python3 backtest.py --tickers NVDA AMD     # specific tickers only
  python3 backtest.py --signal gap           # one signal type only
  python3 backtest.py --report               # load saved results, print report
  python3 backtest.py --compare              # compare signal types head to head

WHAT THIS TESTS:
  1. Setup quality  — do gap fills fill? do inside bars break out? does high relVol follow through?
  2. Directional accuracy — when the scanner says "up", does price go up?
  3. Options P&L simulation — using Black-Scholes + historical vol to approximate what contracts
     the scanner would have suggested and whether they would have been profitable
  4. Sector laggard plays — does the laggard actually catch up to its sector?

LIMITATIONS (be real about this):
  - Uses end-of-day prices, not intraday. Real entries/exits happen mid-session.
  - IV estimated from historical realized vol (HV20) × 1.2 vol-risk-premium proxy.
    Real IV varies. This is directionally correct but not exact.
  - No real bid/ask spread history — simulates 5% spread on mid.
  - Scanner was built for current conditions; past signals ≠ future results.

This is a research tool, not a guarantee. Use it to understand which signals work,
in what market conditions, and at what win rates.
"""

import os
import sys
import json
import math
import warnings
import argparse
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

import numpy as np
import pandas as pd
import yfinance as yf
from tabulate import tabulate
from colorama import Fore, Style, init

warnings.filterwarnings("ignore")
init(autoreset=True)

# ── Import scanner logic ───────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from scanner import (
    UNIVERSE, TICKER_SECTOR, SECTOR_ETFS,
    bs_delta, norm_cdf, vix_delta_target, fetch_vix,
)

RESULTS_DIR = Path(__file__).parent / "backtest_results"
RESULTS_DIR.mkdir(exist_ok=True)


# ── Config ─────────────────────────────────────────────────────────────────────
class BacktestConfig:
    def __init__(
        self,
        tickers: List[str] = None,
        lookback_days: int = 60,
        signal_filter: str = "all",       # all | gap | inside | highvol | laggard
        hold_candles: int = 3,            # how many daily bars to hold (3 = 3-day hold, calibrated for 1-5 DTE swing)
        delta_target: float = 0.35,       # target delta for simulated contract (0.35 = swing-friendly, slightly OTM)
        stop_pct: float = 0.50,           # stop at 50% loss on contract
        target_pct: float = 1.50,         # take profit at 150% gain on contract (swing gives more room)
        min_rel_vol: float = 1.5,         # min relative volume to qualify as "high vol" signal
        gap_threshold: float = 0.005,     # min gap % to flag (0.5%)
        vrp_multiplier: float = 1.20,     # IV = HV20 × this (vol risk premium proxy)
        spread_sim: float = 0.05,         # simulate 5% bid/ask spread on mid
        min_dte: int = 3,                 # minimum days-to-expiry for simulated contracts (1-5 DTE swing)
    ):
        self.tickers = tickers or list(UNIVERSE[:80])  # default: first 80 for speed
        self.lookback_days = lookback_days
        self.signal_filter = signal_filter
        self.hold_candles = hold_candles
        self.delta_target = delta_target
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.min_rel_vol = min_rel_vol
        self.gap_threshold = gap_threshold
        self.vrp_multiplier = vrp_multiplier
        self.spread_sim = spread_sim
        self.min_dte = min_dte


# ── Math helpers ───────────────────────────────────────────────────────────────
def bs_price(S: float, K: float, T: float, sigma: float, opt_type: str = "call") -> float:
    """Black-Scholes option price."""
    T = max(T, 1 / (365 * 1440))
    sigma = max(sigma, 0.05)
    if S <= 0 or K <= 0:
        return max(0.0, (S - K) if opt_type == "call" else (K - S))
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == "call":
        return S * norm_cdf(d1) - K * norm_cdf(d2)
    else:
        return K * norm_cdf(-d2) - S * norm_cdf(-d1)


def calc_hv(close_series: pd.Series, window: int = 20) -> float:
    """Historical volatility (annualized) from log returns."""
    if len(close_series) < window + 1:
        return 0.30
    log_returns = np.log(close_series / close_series.shift(1)).dropna()
    if len(log_returns) < window:
        return 0.30
    hv = float(log_returns.tail(window).std() * math.sqrt(252))
    return max(hv, 0.05)


def find_strike(price: float, direction: str, delta_target: float,
                iv: float, T: float, step: float = None) -> float:
    """Find the strike closest to target delta."""
    if step is None:
        step = max(0.5, round(price * 0.01, 0))

    # Search OTM strikes in delta-target range
    opt_type = "call" if direction == "up" else "put"
    best_strike = price
    best_delta_diff = 1.0

    mult = 1.0 if direction == "up" else -1.0
    for i in range(1, 30):
        K = round(price + mult * step * i, 2)
        if K <= 0:
            continue
        d = abs(bs_delta(price, K, T, iv, opt_type))
        diff = abs(d - delta_target)
        if diff < best_delta_diff:
            best_delta_diff = diff
            best_strike = K

    return best_strike


# ── Signal Detection (mirrors scanner logic on historical bars) ────────────────
def detect_signals(hist: pd.DataFrame, config: BacktestConfig) -> List[Dict]:
    """
    Run signal detection on a historical OHLCV dataframe.
    Returns one record per day where a signal fired.

    Signals detected:
      gap_up / gap_down   — momentum follow (data-confirmed)
      inside / double_inside — compression setups
      highvol             — unusual volume (relative to 20-day avg)
      breakout            — close above prior high / below prior low with volume
      trend               — strong-bodied directional day (tightened thresholds)
      unfilled_gap        — price approaching an open gap zone (gap magnet)
      vwap_reclaim        — price reclaims rolling MVWAP after closing below it
      a_plus              — Dante's A+ confluence: inside + gap target + vol + EMA cloud + breakout
    """
    if len(hist) < 30:
        return []

    signals = []
    closes  = hist["Close"].values.astype(float)
    opens   = hist["Open"].values.astype(float)
    highs   = hist["High"].values.astype(float)
    lows    = hist["Low"].values.astype(float)
    volumes = hist["Volume"].values.astype(float)
    dates   = hist.index

    close_pd = pd.Series(closes)
    high_pd  = pd.Series(highs)
    low_pd   = pd.Series(lows)
    vol_pd   = pd.Series(volumes)

    # Rolling 20-day avg volume for relative volume
    avg_vol_20 = vol_pd.rolling(20).mean().values

    # ── Ripster EMA Clouds (daily) ──────────────────────────────────────────
    # Fast cloud: EMA(9)  — short-term momentum
    # Medium cloud: EMA(34) — intermediate trend
    # Trend filter: EMA(200) — macro direction
    ema9_v   = close_pd.ewm(span=9,   adjust=False).mean().values
    ema34_v  = close_pd.ewm(span=34,  adjust=False).mean().values
    ema200_v = close_pd.ewm(span=200, adjust=False).mean().values

    # Rolling MVWAP: volume-weighted average price, 20-bar window
    # Proxy for daily VWAP drift — "above MVWAP" = institutional support
    typical  = (high_pd + low_pd + close_pd) / 3
    rvwap_v  = ((typical * vol_pd).rolling(20).sum() / vol_pd.rolling(20).sum()).values

    for i in range(20, len(hist) - 1):  # need at least 1 bar ahead for forward return
        price      = closes[i]
        prior      = closes[i - 1]
        open_p     = opens[i]
        avg_vol    = avg_vol_20[i - 1] or 1.0
        today_vol  = volumes[i]
        rel_vol    = today_vol / avg_vol if avg_vol > 0 else 1.0
        gap_pct    = (open_p - prior) / prior if prior > 0 else 0.0

        signal_types = []
        directions   = []
        setup_q      = 0.0

        # ── EMA Cloud + VWAP state ──────────────────────────────────────────
        e9   = float(ema9_v[i])
        e34  = float(ema34_v[i])
        e200 = float(ema200_v[i])
        e34_prev3 = float(ema34_v[max(0, i - 3)])

        vwap      = float(rvwap_v[i]) if not np.isnan(rvwap_v[i]) else None
        vwap_prev = float(rvwap_v[i - 1]) if i > 0 and not np.isnan(rvwap_v[i - 1]) else None

        # Ripster cloud bullish: price > EMA9 > EMA34, EMA34 sloping up
        ema_bull = bool(price > e9 and e9 > e34 and e34 > e34_prev3)
        ema_bear = bool(price < e9 and e9 < e34 and e34 < e34_prev3)
        above_vwap = bool(price > vwap) if vwap is not None else None

        # VWAP reclaim: yesterday's close was below MVWAP, today closed above
        vwap_reclaim_flag = bool(
            vwap is not None and vwap_prev is not None and
            closes[i - 1] < vwap_prev and price >= vwap
        )

        # ── Gap signal ──────────────────────────────────────────────────────
        if abs(gap_pct) >= config.gap_threshold:
            sig = "gap_up" if gap_pct > 0 else "gap_down"
            # Momentum follow: 57.6% of gap_ups continue up, 50.6% of gap_downs continue down
            direction = "up" if gap_pct > 0 else "down"
            signal_types.append(sig)
            directions.append(direction)
            setup_q = max(setup_q, min(1.0, abs(gap_pct) / 0.03))

        # ── Inside bar ─────────────────────────────────────────────────────
        prev_high = highs[i - 1]
        prev_low  = lows[i - 1]
        is_inside = highs[i] <= prev_high and lows[i] >= prev_low
        if is_inside:
            mid = (prev_high + prev_low) / 2
            direction = "up" if closes[i] > mid else "down"
            signal_types.append("inside")
            directions.append(direction)
            setup_q = max(setup_q, 0.6)

        # ── Double inside bar ───────────────────────────────────────────────
        is_double_inside = False
        if i >= 2:
            prev2_high  = highs[i - 2]
            prev2_low   = lows[i - 2]
            prev_inside = highs[i - 1] <= prev2_high and lows[i - 1] >= prev2_low
            if is_inside and prev_inside:
                is_double_inside = True
                signal_types.append("double_inside")
                direction = "up" if closes[i] > (prev2_high + prev2_low) / 2 else "down"
                directions.append(direction)
                setup_q = max(setup_q, 0.80)

        # ── High relative volume ────────────────────────────────────────────
        if rel_vol >= config.min_rel_vol:
            direction = "up" if closes[i] > open_p else "down"
            signal_types.append("highvol")
            directions.append(direction)
            setup_q = max(setup_q, min(1.0, (rel_vol - 1.5) / 3.0 + 0.4))

        # ── Breakout: close above prior high or below prior low with volume ─
        if not is_inside and rel_vol > 1.2:
            if closes[i] > prev_high:
                signal_types.append("breakout")
                directions.append("up")
                setup_q = max(setup_q, min(1.0, (rel_vol - 1.0) / 3.0 + 0.5))
            elif closes[i] < prev_low:
                signal_types.append("breakout")
                directions.append("down")
                setup_q = max(setup_q, min(1.0, (rel_vol - 1.0) / 3.0 + 0.5))

        # ── Trend day (strong bodied close, tightened thresholds) ──────────
        body_pct  = abs(closes[i] - open_p) / open_p if open_p > 0 else 0
        range_pct = (highs[i] - lows[i]) / lows[i] if lows[i] > 0 else 0
        if body_pct > 0.025 and body_pct / max(range_pct, 0.001) > 0.70 and rel_vol >= 1.3:
            direction = "up" if closes[i] > open_p else "down"
            signal_types.append("trend")
            directions.append(direction)
            setup_q = max(setup_q, min(1.0, body_pct / 0.04))

        # ── Unfilled gap approach (gap as magnet) ──────────────────────────
        gap_targets = []
        for j in range(1, i):
            pc  = closes[j - 1]
            op  = opens[j]
            if pc <= 0:
                continue
            gpct = (op - pc) / pc * 100
            if abs(gpct) < 0.25 or abs(gpct) > 8.0:
                continue
            if gpct > 0:
                gap_top, gap_bot = float(op), float(pc)
                filled = any(float(lows[k]) <= pc for k in range(j + 1, i + 1))
            else:
                gap_top, gap_bot = float(pc), float(op)
                filled = any(float(highs[k]) >= pc for k in range(j + 1, i + 1))
            if not filled:
                gap_mid  = (gap_top + gap_bot) / 2
                dist_pct = (gap_mid - price) / price * 100 if price > 0 else 999
                if abs(dist_pct) <= 3.0:
                    dtf = "up" if gap_mid > price else "down"
                    gap_targets.append({"mid": gap_mid, "dist_pct": dist_pct, "dtf": dtf, "bars_ago": i - j})

        if gap_targets:
            nearest = min(gap_targets, key=lambda g: abs(g["dist_pct"]))
            signal_types.append("unfilled_gap")
            directions.append(nearest["dtf"])
            adist = abs(nearest["dist_pct"])
            sq_gap = 0.80 if adist <= 0.5 else 0.70 if adist <= 1.5 else 0.60
            setup_q = max(setup_q, sq_gap)

        # ── VWAP reclaim signal ─────────────────────────────────────────────
        if vwap_reclaim_flag and "vwap_reclaim" not in signal_types:
            signal_types.append("vwap_reclaim")
            directions.append("up")
            setup_q = max(setup_q, 0.65)

        # ── A+ Confluence Scorer ────────────────────────────────────────────
        # Dante's setup: inside/double-inside (compression) + unfilled gap target above
        # + volume/flow confirmation + EMA cloud aligned + breakout of key level
        aplus_bull = 0
        aplus_bear = 0

        # 1. Compression — inside day or double inside (flag/pennant coiling)
        if is_inside or is_double_inside:
            aplus_bull += 1
            aplus_bear += 1

        # 2. Gap magnet target in direction (unfilled gap above = target for longs)
        gap_above = [g for g in gap_targets if g["dtf"] == "up"]
        gap_below = [g for g in gap_targets if g["dtf"] == "down"]
        if gap_above:
            aplus_bull += 1
        if gap_below:
            aplus_bear += 1

        # 3. Volume / flow confirmation (institutional money coming in)
        if rel_vol >= 1.5:
            aplus_bull += 1
            aplus_bear += 1

        # 4. Ripster EMA cloud aligned in direction
        if ema_bull:
            aplus_bull += 1
        if ema_bear:
            aplus_bear += 1

        # 5. Breakout of key level: prior day high/low OR VWAP cross
        vwap_cross_up   = bool(vwap is not None and vwap_prev is not None
                               and closes[i - 1] <= vwap_prev and price > vwap)
        vwap_cross_down = bool(vwap is not None and vwap_prev is not None
                               and closes[i - 1] >= vwap_prev and price < vwap)
        if closes[i] > prev_high or vwap_cross_up:
            aplus_bull += 1
        if closes[i] < prev_low or vwap_cross_down:
            aplus_bear += 1

        aplus_score = max(aplus_bull, aplus_bear)

        if aplus_bull >= 4 and "a_plus" not in signal_types:
            signal_types.append("a_plus")
            directions.append("up")
            setup_q = max(setup_q, 0.80 + min(0.20, (aplus_bull - 4) * 0.10))
        elif aplus_bear >= 4 and "a_plus" not in signal_types:
            signal_types.append("a_plus")
            directions.append("down")
            setup_q = max(setup_q, 0.80 + min(0.20, (aplus_bear - 4) * 0.10))

        if not signal_types:
            continue

        # Filter by signal type if requested
        if config.signal_filter != "all":
            mapped = config.signal_filter
            if mapped == "gap":
                if not any(s in ("gap_up", "gap_down", "unfilled_gap") for s in signal_types):
                    continue
            elif mapped == "a_plus":
                if "a_plus" not in signal_types:
                    continue
            elif mapped not in signal_types:
                continue

        # Use consensus direction (majority vote)
        up_votes = directions.count("up")
        dn_votes = directions.count("down")
        direction = "up" if up_votes >= dn_votes else "down"

        # Historical vol for IV proxy
        hv = calc_hv(hist["Close"].iloc[:i + 1])

        # Primary signal label: A+ > unfilled_gap > others
        primary_signal = signal_types[0]
        if "a_plus" in signal_types:
            primary_signal = "a_plus"
        elif "unfilled_gap" in signal_types:
            primary_signal = "unfilled_gap"

        signals.append({
            "date":         dates[i].strftime("%Y-%m-%d"),
            "signal":       primary_signal,
            "signal_types": signal_types,
            "direction":    direction,
            "price":        round(price, 4),
            "open_p":       round(open_p, 4),
            "prior_close":  round(prior, 4),
            "rel_vol":      round(rel_vol, 2),
            "gap_pct":      round(gap_pct * 100, 3),
            "hv20":         round(hv, 4),
            "setup_q":      round(setup_q, 3),
            "ema_bull":     ema_bull,
            "above_vwap":   above_vwap,
            "aplus_score":  aplus_score,
            "vwap":         round(vwap, 2) if vwap is not None else None,
            "bar_index":    i,
        })

    return signals


# ── Forward Return Evaluation ──────────────────────────────────────────────────
def evaluate_forward_returns(
    ticker: str,
    signals: List[Dict],
    hist: pd.DataFrame,
    config: BacktestConfig,
) -> List[Dict]:
    """
    For each signal, calculate:
      - 1-day forward return (next close vs signal close)
      - N-day forward return (based on config.hold_candles)
      - Intraday move next day (high - low)
      - Direction correct? (1 = yes, 0 = no)
      - Simulated options P&L
    """
    closes = hist["Close"].values.astype(float)
    highs  = hist["High"].values.astype(float)
    lows   = hist["Low"].values.astype(float)
    opens  = hist["Open"].values.astype(float)
    n      = len(closes)

    results = []

    for sig in signals:
        i     = sig["bar_index"]
        price = sig["price"]
        dirn  = sig["direction"]
        hv    = sig["hv20"]

        if i + config.hold_candles >= n:
            continue  # not enough forward bars

        # Forward prices
        next_open  = opens[i + 1]
        next_close = closes[i + config.hold_candles]
        next_high  = max(highs[i+1 : i + config.hold_candles + 1])
        next_low   = min(lows[i+1  : i + config.hold_candles + 1])

        # Raw price return from signal close
        fwd_return = (next_close - price) / price if price > 0 else 0.0
        # Entry at next open (realistic — can't enter at prior close in practice)
        entry_return = (next_close - next_open) / next_open if next_open > 0 else fwd_return

        correct = (dirn == "up" and fwd_return > 0) or (dirn == "down" and fwd_return < 0)

        # Signed return (positive = direction was right)
        signed_return = abs(fwd_return) if correct else -abs(fwd_return)

        # ── Options P&L simulation ─────────────────────────────────────────
        opt_pnl_pct  = None
        opt_pnl_usd  = None
        opt_entry    = None
        opt_exit     = None
        sim_strike   = None
        sim_delta    = None
        sim_iv       = None

        if hv > 0 and price > 1.0:
            iv  = hv * config.vrp_multiplier   # IV = HV × VRP proxy
            # Use min_dte so we simulate buying options with at least min_dte days left.
            # This avoids the 1-DTE problem where any adverse intraday tick makes the
            # option nearly worthless — 2-DTE options retain meaningful time value at exit.
            T0  = max(config.hold_candles, config.min_dte) / 365.0
            T1  = max(0.0, T0 - config.hold_candles / 365.0)  # time remaining at exit

            # Find strike near target delta
            K = find_strike(price, dirn, config.delta_target, iv, T0)
            sim_strike = K
            sim_iv     = round(iv, 4)

            # Entry price (BS mid + spread)
            opt_type   = "call" if dirn == "up" else "put"
            entry_mid  = bs_price(price, K, T0, iv, opt_type)
            if entry_mid > 0.05:
                entry_price = entry_mid * (1 + config.spread_sim)  # pay the ask
                opt_entry   = round(entry_price, 4)

                # Exit price at EOD (T1 = T0 - hold period; option retains time value)
                if T1 > 0:
                    exit_mid = bs_price(next_close, K, T1, iv, opt_type)
                else:
                    if opt_type == "call":
                        exit_mid = max(0.0, next_close - K)
                    else:
                        exit_mid = max(0.0, K - next_close)

                exit_price = exit_mid * (1 - config.spread_sim)  # receive the bid
                opt_exit   = round(exit_price, 4)

                raw_pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0

                # Apply EOD stop/target rules only — no intraday adverse check.
                # The old T0*0.5 adverse check was firing on 79.7% of trades because
                # any small intraday move against a 1-DTE OTM option made it ~worthless.
                # EOD-only evaluation is honest and consistent with our price data.
                if raw_pnl_pct <= -config.stop_pct:
                    raw_pnl_pct = -config.stop_pct
                    opt_exit    = round(entry_price * (1 - config.stop_pct), 4)
                elif raw_pnl_pct >= config.target_pct:
                    raw_pnl_pct = config.target_pct

                opt_pnl_pct = round(raw_pnl_pct * 100, 2)
                opt_pnl_usd = round(raw_pnl_pct * entry_price * 100, 2)  # per 1 contract

                sim_delta = round(abs(bs_delta(price, K, T0, iv, opt_type)), 3)

        results.append({
            **sig,
            "ticker":        ticker,
            "fwd_return_pct":  round(fwd_return * 100, 3),
            "entry_return_pct": round(entry_return * 100, 3),
            "signed_return_pct": round(signed_return * 100, 3),
            "direction_correct": int(correct),
            "next_open":    round(next_open, 4),
            "next_close":   round(next_close, 4),
            "intraday_range_pct": round((next_high - next_low) / next_low * 100, 3) if next_low > 0 else 0,
            "opt_pnl_pct":  opt_pnl_pct,
            "opt_pnl_usd":  opt_pnl_usd,
            "opt_entry":    opt_entry,
            "opt_exit":     opt_exit,
            "sim_strike":   sim_strike,
            "sim_delta":    sim_delta,
            "sim_iv":       sim_iv,
        })

    return results


# ── Sector Laggard Backtest ────────────────────────────────────────────────────
def backtest_laggards(
    config: BacktestConfig,
    lookback_days: int = None,
) -> List[Dict]:
    """
    Tests sector laggard signals:
    When a sector ETF is up >1% on the day and one of its components
    has lagged (e.g., sector up 2% but stock up 0%), does the stock
    catch up the next day?
    """
    lookback = lookback_days or config.lookback_days
    end = datetime.today()
    start = end - timedelta(days=lookback + 30)

    # Map sector ETFs to their member tickers
    sector_members: Dict[str, List[str]] = defaultdict(list)
    for ticker, sector in TICKER_SECTOR.items():
        if ticker in config.tickers:
            sector_members[sector].append(ticker)

    results = []

    for sector_name, etf in SECTOR_ETFS.items():
        members = sector_members.get(sector_name, [])
        if not members:
            continue

        tickers_to_fetch = [etf] + members
        print(f"  Laggard: {sector_name} ({etf}) [{len(members)} members]...", end="", flush=True)

        try:
            raw = yf.download(
                tickers_to_fetch, start=start, end=end,
                interval="1d", group_by="ticker",
                progress=False, auto_adjust=True, timeout=20,
            )
        except Exception:
            print(" skip")
            continue

        def get_hist(t: str) -> Optional[pd.DataFrame]:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if t not in raw.columns.get_level_values(0):
                        return None
                    h = raw[t].dropna(how="all")
                else:
                    h = raw.dropna(how="all")
                return h if len(h) >= 20 else None
            except Exception:
                return None

        etf_hist = get_hist(etf)
        if etf_hist is None:
            print(" no ETF data")
            continue

        etf_closes = etf_hist["Close"].values.astype(float)
        etf_dates  = etf_hist.index

        for i in range(1, len(etf_closes) - 1):
            etf_return = (etf_closes[i] - etf_closes[i-1]) / etf_closes[i-1]
            if etf_return < 0.01:   # only fire when sector is up >1%
                continue

            for member in members:
                m_hist = get_hist(member)
                if m_hist is None:
                    continue

                # Align dates
                try:
                    sig_date = etf_dates[i]
                    if sig_date not in m_hist.index:
                        continue
                    m_idx = m_hist.index.get_loc(sig_date)
                    if m_idx < 1 or m_idx >= len(m_hist) - 1:
                        continue

                    m_closes = m_hist["Close"].values.astype(float)
                    m_return = (m_closes[m_idx] - m_closes[m_idx-1]) / m_closes[m_idx-1]
                    lag = etf_return - m_return   # how far the member lagged

                    if lag < 0.005:   # at least 0.5% behind sector
                        continue

                    # Forward: did the laggard catch up next day?
                    fwd_return = (m_closes[m_idx + 1] - m_closes[m_idx]) / m_closes[m_idx]
                    correct = fwd_return > 0

                    results.append({
                        "date":         sig_date.strftime("%Y-%m-%d"),
                        "sector":       sector_name,
                        "etf":          etf,
                        "ticker":       member,
                        "sector_return_pct": round(etf_return * 100, 3),
                        "stock_return_pct":  round(m_return * 100, 3),
                        "lag_pct":      round(lag * 100, 3),
                        "fwd_return_pct": round(fwd_return * 100, 3),
                        "signed_return_pct": round(abs(fwd_return) * 100 * (1 if correct else -1), 3),
                        "direction_correct": int(correct),
                        "signal":       "laggard",
                        "signal_types": ["laggard"],
                    })
                except Exception:
                    continue

        print(f" {len([r for r in results if r.get('sector') == sector_name])} signals")

    return results


# ── Metrics Engine ─────────────────────────────────────────────────────────────
def calc_metrics(records: List[Dict], label: str = "ALL") -> Dict:
    """
    Calculate full quant metrics for a set of backtest records.
    Returns a dict of metrics with explanations embedded.
    """
    if not records:
        return {"label": label, "count": 0}

    n          = len(records)
    correct    = [r["direction_correct"] for r in records]
    signed_ret = [r["signed_return_pct"] for r in records]
    fwd_ret    = [r["fwd_return_pct"]    for r in records]

    win_rate   = sum(correct) / n
    avg_return = np.mean(signed_ret)
    avg_fwd    = np.mean(fwd_ret)
    std_ret    = np.std(signed_ret) if n > 1 else 0

    # Sharpe (using daily returns, annualized with 252 trading days)
    # Formula: (avg return - 0) / std × sqrt(252)
    sharpe = (avg_return / std_ret * math.sqrt(252)) if std_ret > 0 else 0

    # Profit factor: sum of winners / sum of abs(losers)
    wins   = [r for r in signed_ret if r > 0]
    losses = [r for r in signed_ret if r <= 0]
    pf     = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")

    # Max drawdown simulation
    equity = [100.0]
    for r in signed_ret:
        equity.append(equity[-1] * (1 + r / 100))
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak
        if dd > max_dd:
            max_dd = dd

    # Expected value per trade
    ev = win_rate * np.mean([r for r in signed_ret if r > 0] or [0]) + \
         (1 - win_rate) * np.mean([r for r in signed_ret if r <= 0] or [0])

    # Options metrics (if available)
    opt_records = [r for r in records if r.get("opt_pnl_pct") is not None]
    opt_metrics = {}
    if opt_records:
        opt_pnls     = [r["opt_pnl_pct"] for r in opt_records]
        opt_wins     = [p for p in opt_pnls if p > 0]
        opt_losses   = [p for p in opt_pnls if p <= 0]
        opt_pf       = (sum(opt_wins) / abs(sum(opt_losses))) if opt_losses and sum(opt_losses) != 0 else float("inf")
        opt_metrics  = {
            "opt_count":    len(opt_records),
            "opt_win_rate": round(len(opt_wins) / len(opt_records), 4),
            "opt_avg_pnl":  round(np.mean(opt_pnls), 2),
            "opt_avg_win":  round(np.mean(opt_wins), 2) if opt_wins else 0,
            "opt_avg_loss": round(np.mean(opt_losses), 2) if opt_losses else 0,
            "opt_pf":       round(opt_pf, 3),
            "opt_avg_usd":  round(np.mean([r["opt_pnl_usd"] for r in opt_records if r.get("opt_pnl_usd")]), 2),
            "opt_avg_delta": round(np.mean([r["sim_delta"] for r in opt_records if r.get("sim_delta")]), 3),
            "opt_avg_iv":   round(np.mean([r["sim_iv"] for r in opt_records if r.get("sim_iv")]), 4),
        }

    return {
        "label":      label,
        "count":      n,
        "win_rate":   round(win_rate, 4),
        "avg_return": round(avg_return, 3),
        "avg_fwd":    round(avg_fwd, 3),
        "std_ret":    round(std_ret, 3),
        "sharpe":     round(sharpe, 3),
        "profit_factor": round(pf, 3),
        "max_drawdown":  round(max_dd, 4),
        "expected_value": round(ev, 3),
        "final_equity":   round(equity[-1], 2),
        **opt_metrics,
    }


# ── Report Printer ─────────────────────────────────────────────────────────────
def print_report(all_records: List[Dict], config: BacktestConfig) -> None:
    """Print a full quant report to the terminal."""

    sep = Fore.WHITE + "─" * 72 + Style.RESET_ALL

    print(f"\n{Fore.CYAN + Style.BRIGHT}  D — SCANNER BACKTEST REPORT{Style.RESET_ALL}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
          f"{len(all_records)} signals  |  {len(set(r['ticker'] for r in all_records))} tickers  |  "
          f"{config.lookback_days}-day lookback")
    print(sep)

    # ── Explanation block ──────────────────────────────────────────────────
    print(f"\n{Fore.YELLOW}  WHAT YOU'RE LOOKING AT{Style.RESET_ALL}")
    print("""
  Win Rate       — % of signals where price moved in predicted direction
                   next day. 50% = coin flip. Want >55% to be meaningful.

  Profit Factor  — gross wins / gross losses. >1.5 = edge. <1.0 = losing.

  Sharpe Ratio   — return per unit of risk, annualized. >1.0 = decent.
                   >2.0 = strong. Negative = losing more than you're winning.

  Expected Value — avg $ move per signal (signed by direction). Positive
                   means the setup has an edge over random.

  Options P&L    — simulated contract P&L using Black-Scholes + historical
                   vol × 1.2 (vol risk premium). NOT real historical prices.
                   Treats your delta target (0.30), your stop (50%), and
                   your target (100%) as the trade management rules.
                   Use this as a directional estimate, not exact truth.
    """)
    print(sep)

    # ── Overall metrics ────────────────────────────────────────────────────
    overall = calc_metrics(all_records, "OVERALL")
    print(f"\n{Fore.GREEN + Style.BRIGHT}  OVERALL RESULTS{Style.RESET_ALL}")
    _print_metrics_block(overall)

    # ── By signal type ─────────────────────────────────────────────────────
    signal_types = set()
    for r in all_records:
        for s in r.get("signal_types", []):
            signal_types.add(s)

    if len(signal_types) > 1:
        print(f"\n{sep}\n{Fore.YELLOW}  BREAKDOWN BY SIGNAL TYPE{Style.RESET_ALL}\n")
        sig_rows = []
        for sig in sorted(signal_types):
            recs = [r for r in all_records if sig in r.get("signal_types", [])]
            m    = calc_metrics(recs, sig)
            wr_c = Fore.GREEN if m.get("win_rate", 0) > 0.55 else (Fore.YELLOW if m.get("win_rate", 0) > 0.50 else Fore.RED)
            pf_c = Fore.GREEN if m.get("profit_factor", 0) > 1.5 else (Fore.YELLOW if m.get("profit_factor", 0) > 1.0 else Fore.RED)
            sig_rows.append([
                Fore.CYAN + sig + Style.RESET_ALL,
                m["count"],
                f"{wr_c}{m.get('win_rate', 0)*100:.1f}%{Style.RESET_ALL}",
                f"{m.get('avg_return', 0):+.2f}%",
                f"{pf_c}{m.get('profit_factor', 0):.2f}{Style.RESET_ALL}",
                f"{m.get('sharpe', 0):.2f}",
                f"{m.get('expected_value', 0):+.3f}%",
                f"{m.get('opt_win_rate', 0)*100:.1f}%" if m.get("opt_count") else "—",
                f"{m.get('opt_avg_pnl', 0):+.1f}%" if m.get("opt_count") else "—",
            ])
        headers = ["Signal", "N", "Win%", "Avg Ret", "Prof.Factor", "Sharpe", "EV/trade", "Opt Win%", "Opt Avg%"]
        print("  " + tabulate(sig_rows, headers=headers, tablefmt="simple").replace("\n", "\n  "))

    # ── Top 10 setups by signed return ─────────────────────────────────────
    print(f"\n{sep}\n{Fore.GREEN}  TOP 10 SETUPS (by signed return){Style.RESET_ALL}\n")
    top10 = sorted(all_records, key=lambda r: r.get("signed_return_pct", 0), reverse=True)[:10]
    top_rows = []
    for r in top10:
        c = Fore.GREEN if r["direction_correct"] else Fore.RED
        top_rows.append([
            r["ticker"],
            r["date"],
            ", ".join(r.get("signal_types", [])),
            r["direction"],
            f"{c}{r['fwd_return_pct']:+.2f}%{Style.RESET_ALL}",
            f"{r['opt_pnl_pct']:+.1f}%" if r.get("opt_pnl_pct") is not None else "—",
            f"{r.get('rel_vol', 0):.1f}x",
        ])
    print("  " + tabulate(
        top_rows,
        headers=["Ticker", "Date", "Signal", "Dir", "Fwd Ret", "Opt P&L", "RelVol"],
        tablefmt="simple"
    ).replace("\n", "\n  "))

    # ── Worst setups ───────────────────────────────────────────────────────
    print(f"\n{sep}\n{Fore.RED}  WORST 10 SETUPS{Style.RESET_ALL}\n")
    worst10 = sorted(all_records, key=lambda r: r.get("signed_return_pct", 0))[:10]
    bad_rows = []
    for r in worst10:
        bad_rows.append([
            r["ticker"],
            r["date"],
            ", ".join(r.get("signal_types", [])),
            r["direction"],
            f"{Fore.RED}{r['fwd_return_pct']:+.2f}%{Style.RESET_ALL}",
            f"{r['opt_pnl_pct']:+.1f}%" if r.get("opt_pnl_pct") is not None else "—",
        ])
    print("  " + tabulate(
        bad_rows,
        headers=["Ticker", "Date", "Signal", "Dir", "Fwd Ret", "Opt P&L"],
        tablefmt="simple"
    ).replace("\n", "\n  "))

    # ── Equity curve (ASCII) ───────────────────────────────────────────────
    print(f"\n{sep}\n{Fore.CYAN}  EQUITY CURVE (simulated $100 start, directional returns){Style.RESET_ALL}\n")
    _print_equity_curve(all_records)

    # ── VIX regime analysis ────────────────────────────────────────────────
    if any(r.get("vix") for r in all_records):
        _print_vix_breakdown(all_records)

    # ── Signal combo breakdown ─────────────────────────────────────────────
    print(f"\n{sep}\n{Fore.CYAN}  SIGNAL COMBO BREAKDOWN (sorted by direction accuracy, n≥8){Style.RESET_ALL}\n")
    from collections import defaultdict as _dd
    combo_map: Dict = _dd(lambda: {"wins": 0, "total": 0, "pnl": 0.0})
    for r in all_records:
        stypes = r.get("signal_types", [])
        combo  = "+".join(sorted(stypes)) if stypes else r.get("signal", "?")
        dc     = r.get("direction_correct", 0)
        pnl    = r.get("opt_pnl_pct") or 0.0
        combo_map[combo]["total"] += 1
        combo_map[combo]["pnl"]   += pnl
        if dc: combo_map[combo]["wins"] += 1
    combo_rows = []
    for combo, s in sorted(combo_map.items(),
                            key=lambda x: (x[1]["wins"] / x[1]["total"] if x[1]["total"] else 0),
                            reverse=True):
        if s["total"] < 8:
            continue
        wr  = s["wins"] / s["total"]
        avg = s["pnl"] / s["total"]
        wr_c  = Fore.GREEN if wr >= 0.70 else (Fore.YELLOW if wr >= 0.55 else Fore.RED)
        avg_c = Fore.GREEN if avg > 0 else Fore.RED
        combo_rows.append([
            combo[:44],
            s["total"],
            f"{wr_c}{wr*100:.1f}%{Style.RESET_ALL}",
            f"{avg_c}{avg:+.1f}%{Style.RESET_ALL}",
        ])
    print("  " + tabulate(combo_rows,
                           headers=["Signal Combo", "N", "Dir WR%", "Avg Opt P&L"],
                           tablefmt="simple").replace("\n", "\n  "))

    # ── Options simulation summary ─────────────────────────────────────────
    opt_records = [r for r in all_records if r.get("opt_pnl_pct") is not None]
    if opt_records:
        print(f"\n{sep}\n{Fore.YELLOW}  OPTIONS SIMULATION SUMMARY{Style.RESET_ALL}\n")
        m = calc_metrics(opt_records, "Options")

        # Real-world adjusted P&L (add 20% to entry cost, 20% to exit spread)
        rw_pnls = []
        for r in opt_records:
            entry = r.get("opt_entry", 0) or 0
            pnl_pct = r.get("opt_pnl_pct", 0) or 0
            if entry > 0:
                exit_price  = entry * (1 + pnl_pct / 100)
                real_entry  = entry * 1.20
                real_exit   = exit_price * 0.80
                rw_pnls.append((real_exit - real_entry) / real_entry * 100)
        rw_avg   = sum(rw_pnls) / len(rw_pnls) if rw_pnls else 0.0
        rw_wins  = sum(1 for p in rw_pnls if p > 0)
        rw_wr    = rw_wins / len(rw_pnls) if rw_pnls else 0.0

        print(f"""
  Contracts simulated:  {m.get('opt_count', 0)}
  Options win rate:     {m.get('opt_win_rate', 0)*100:.1f}%
  Avg P&L per trade:    {m.get('opt_avg_pnl', 0):+.1f}% (of contract premium)
  Avg win:              +{m.get('opt_avg_win', 0):.1f}%
  Avg loss:             {m.get('opt_avg_loss', 0):.1f}%
  Profit factor:        {m.get('opt_pf', 0):.2f}
  Avg P&L in USD:       ${m.get('opt_avg_usd', 0):+.2f} per contract (1 lot = 100 shares)
  Avg entry delta:      {m.get('opt_avg_delta', 0):.3f}
  Avg IV used:          {m.get('opt_avg_iv', 0)*100:.1f}%

  REAL-WORLD ADJUSTED (entry ×1.20, exit ×0.80 — simulates spread + slippage):
  {Fore.YELLOW}  Adjusted win rate:    {rw_wr*100:.1f}%  (vs {m.get('opt_win_rate',0)*100:.1f}% raw){Style.RESET_ALL}
  {Fore.RED}  Adjusted avg P&L:     {rw_avg:+.1f}%  (vs {m.get('opt_avg_pnl',0):+.1f}% raw){Style.RESET_ALL}

  ⚠  These are BS model estimates using HV×1.2 as IV proxy.
     Real IV, spreads, and slippage will differ.
     Focus on DIRECTION ACCURACY (dir WR%) — that's the real edge.
     Only trade S/A-tier combos (70%+ dir WR) to overcome spread costs.
        """)

    print(sep)
    print(f"\n  Results saved to: {RESULTS_DIR}/")
    print()


def _print_metrics_block(m: Dict) -> None:
    """Print core metrics with color coding."""
    if m.get("count", 0) == 0:
        print("  No data.")
        return

    wr  = m.get("win_rate", 0)
    pf  = m.get("profit_factor", 0)
    sh  = m.get("sharpe", 0)
    ev  = m.get("expected_value", 0)
    dd  = m.get("max_drawdown", 0)
    eq  = m.get("final_equity", 100)

    wr_c  = Fore.GREEN if wr > 0.55 else (Fore.YELLOW if wr > 0.50 else Fore.RED)
    pf_c  = Fore.GREEN if pf > 1.5  else (Fore.YELLOW if pf > 1.0  else Fore.RED)
    sh_c  = Fore.GREEN if sh > 1.0  else (Fore.YELLOW if sh > 0    else Fore.RED)
    ev_c  = Fore.GREEN if ev > 0    else Fore.RED
    eq_c  = Fore.GREEN if eq > 100  else Fore.RED

    print(f"""
  Signals:          {m['count']}
  Win Rate:         {wr_c}{wr*100:.1f}%{Style.RESET_ALL}  (>55% meaningful, >60% strong)
  Profit Factor:    {pf_c}{pf:.2f}{Style.RESET_ALL}   (>1.5 = edge)
  Sharpe (ann.):    {sh_c}{sh:.2f}{Style.RESET_ALL}   (>1.0 = decent, >2.0 = strong)
  Expected Value:   {ev_c}{ev:+.3f}%{Style.RESET_ALL} per trade
  Max Drawdown:     {Fore.RED}{dd*100:.1f}%{Style.RESET_ALL}
  Avg Return:       {m.get('avg_return', 0):+.3f}% (signed by direction)
  Final Equity:     {eq_c}${eq:.2f}{Style.RESET_ALL} (from $100 start)
    """)


def _print_equity_curve(records: List[Dict], width: int = 60) -> None:
    """ASCII equity curve."""
    eq = [100.0]
    for r in records:
        eq.append(eq[-1] * (1 + r.get("signed_return_pct", 0) / 100))

    if len(eq) < 2:
        return

    hi = max(eq)
    lo = min(eq)
    rng = hi - lo or 1.0
    height = 10
    cols = min(width, len(eq))
    step = max(1, len(eq) // cols)
    sampled = eq[::step][-cols:]

    chart = [[" "] * len(sampled) for _ in range(height)]
    for col, val in enumerate(sampled):
        row = height - 1 - int((val - lo) / rng * (height - 1))
        row = max(0, min(height - 1, row))
        chart[row][col] = "█"

    for row in chart:
        c = Fore.GREEN if "█" in row else ""
        print("  " + c + "".join(row) + Style.RESET_ALL)

    print(f"\n  Start: $100.00  |  End: ${eq[-1]:.2f}  |  "
          f"Peak: ${hi:.2f}  |  Trough: ${lo:.2f}")


def _print_vix_breakdown(records: List[Dict]) -> None:
    """Break down win rates by VIX regime."""
    regimes = {"low (<16)": [], "normal (16–24)": [], "elevated (24–35)": [], "fear (>35)": []}
    for r in records:
        v = r.get("vix", 0)
        if v < 16:      regimes["low (<16)"].append(r)
        elif v < 24:    regimes["normal (16–24)"].append(r)
        elif v < 35:    regimes["elevated (24–35)"].append(r)
        else:           regimes["fear (>35)"].append(r)

    rows = []
    for label, recs in regimes.items():
        if not recs:
            continue
        wr = sum(r["direction_correct"] for r in recs) / len(recs)
        rows.append([label, len(recs), f"{wr*100:.1f}%"])

    if rows:
        print(f"\n  VIX REGIME BREAKDOWN:\n")
        print("  " + tabulate(rows, headers=["VIX Regime", "N", "Win%"], tablefmt="simple").replace("\n", "\n  "))


# ── Main Runner ────────────────────────────────────────────────────────────────
def run_backtest_cli(config: BacktestConfig) -> List[Dict]:
    """
    Main backtest loop.
    Downloads historical data for all tickers, detects signals, evaluates forward returns.
    """
    print(f"\n{Fore.CYAN + Style.BRIGHT}  D — SCANNER BACKTEST{Style.RESET_ALL}")
    print(f"  Tickers: {len(config.tickers)}  |  Lookback: {config.lookback_days} days  "
          f"|  Signal: {config.signal_filter}  |  Hold: {config.hold_candles}D")
    print(f"  Delta target: {config.delta_target}  |  Stop: -{config.stop_pct*100:.0f}%  "
          f"|  Target: +{config.target_pct*100:.0f}%")
    print()

    end   = datetime.today()
    start = end - timedelta(days=config.lookback_days + 60)  # extra buffer for vol calc

    # Batch download
    print(f"  {Fore.CYAN}Downloading {len(config.tickers)} tickers...{Style.RESET_ALL}", end="", flush=True)
    try:
        raw = yf.download(
            config.tickers,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            group_by="ticker",
            progress=False,
            auto_adjust=True,
            threads=True,
            timeout=30,
        )
        print(f" done ({len(config.tickers)} tickers)")
    except Exception as e:
        print(f" ERROR: {e}")
        return []

    all_records = []
    failed      = 0

    for i, ticker in enumerate(config.tickers, 1):
        sys.stdout.write(f"\r  Processing [{i}/{len(config.tickers)}] {Fore.CYAN}{ticker:<6}{Style.RESET_ALL}  ")
        sys.stdout.flush()

        try:
            if isinstance(raw.columns, pd.MultiIndex):
                if ticker not in raw.columns.get_level_values(0):
                    failed += 1
                    continue
                hist = raw[ticker].dropna(how="all")
            else:
                hist = raw.dropna(how="all")

            if len(hist) < 30:
                failed += 1
                continue

            # Trim to lookback window (keep extra for HV calc)
            cutoff = end - timedelta(days=config.lookback_days)
            hist_full = hist
            hist = hist[hist.index >= pd.Timestamp(cutoff)]
            if len(hist) < 5:
                continue

            # Need full history for HV calc — pass full hist, signal detection uses bar indices
            signals = detect_signals(hist_full, config)
            # Filter to lookback period
            cutoff_str = cutoff.strftime("%Y-%m-%d")
            signals = [s for s in signals if s["date"] >= cutoff_str]

            if not signals:
                continue

            records = evaluate_forward_returns(ticker, signals, hist_full, config)
            all_records.extend(records)

        except Exception:
            failed += 1
            continue

    sys.stdout.write("\r" + " " * 72 + "\r")
    print(f"  Processed: {len(config.tickers) - failed} tickers  |  "
          f"Signals found: {len(all_records)}  |  Failed: {failed}")

    return all_records


def save_results(records: List[Dict], config: BacktestConfig) -> str:
    """Save backtest results to JSON and CSV."""
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"backtest_{config.lookback_days}d_{config.signal_filter}_{ts}"

    # JSON (full data)
    json_path = RESULTS_DIR / f"{stem}.json"
    with open(json_path, "w") as f:
        json.dump({
            "config": {k: v for k, v in vars(config).items() if not callable(v)},
            "records": records,
            "metrics": calc_metrics(records),
            "run_at":  datetime.now().isoformat(),
        }, f, indent=2)

    # CSV (flat)
    if records:
        df = pd.DataFrame(records)
        df["signal_types"] = df["signal_types"].apply(lambda x: "|".join(x) if isinstance(x, list) else x)
        df.to_csv(RESULTS_DIR / f"{stem}.csv", index=False)

    # HTML dashboard
    viz_path = generate_html_report(records, config)
    print(f"  Viz: {viz_path}")

    return str(json_path)


def generate_html_report(records: List[Dict], config: BacktestConfig) -> str:
    """
    Generate a live HTML dashboard from backtest results.
    Overwrites backtest_viz.html with current run data.
    """
    SIG_ORDER = ["a_plus", "breakout", "highvol", "trend", "gap_up", "gap_down",
                 "inside", "double_inside", "vwap_reclaim", "unfilled_gap"]

    sig_data = []
    for sig in SIG_ORDER:
        recs = [r for r in records if sig in r.get("signal_types", [])]
        if len(recs) < 3:
            continue
        m = calc_metrics(recs, sig)
        sig_data.append({
            "name":   sig,
            "n":      m["count"],
            "wr":     round(m.get("win_rate", 0) * 100, 1),
            "sharpe": round(m.get("sharpe", 0), 2),
            "pf":     round(m.get("profit_factor", 0), 2),
            "ev":     round(m.get("expected_value", 0), 3),
            "opt_wr": round(m.get("opt_win_rate", 0) * 100, 1) if m.get("opt_count") else None,
            "opt_avg": round(m.get("opt_avg_pnl", 0), 1) if m.get("opt_count") else None,
        })

    # Equity curve
    eq = [100.0]
    for r in records:
        eq.append(eq[-1] * (1 + r.get("signed_return_pct", 0) / 100))
    # Downsample equity curve to max 200 points
    step = max(1, len(eq) // 200)
    eq_sampled = eq[::step]

    # A+ vs non-A+ comparison
    aplus   = [r for r in records if "a_plus" in r.get("signal_types", [])]
    non_ap  = [r for r in records if "a_plus" not in r.get("signal_types", [])]
    m_ap    = calc_metrics(aplus,  "A+")
    m_nonap = calc_metrics(non_ap, "Non-A+")

    run_at   = datetime.now().strftime("%Y-%m-%d %H:%M")
    lookback = config.lookback_days
    n_tickers = len(config.tickers)

    sig_js   = json.dumps(sig_data)
    eq_js    = json.dumps([round(v, 2) for v in eq_sampled])
    aplus_js = json.dumps({
        "aplus":  {"wr": round(m_ap.get("win_rate", 0) * 100, 1),
                   "sharpe": round(m_ap.get("sharpe", 0), 2),
                   "pf": round(m_ap.get("profit_factor", 0), 2),
                   "n": m_ap.get("count", 0)},
        "rest":   {"wr": round(m_nonap.get("win_rate", 0) * 100, 1),
                   "sharpe": round(m_nonap.get("sharpe", 0), 2),
                   "pf": round(m_nonap.get("profit_factor", 0), 2),
                   "n": m_nonap.get("count", 0)},
    })

    # Top 10 setups
    top10 = sorted(records, key=lambda r: r.get("signed_return_pct", 0), reverse=True)[:10]
    top10_rows = "".join(f"""
      <tr>
        <td style="color:#22d3ee">{r['ticker']}</td>
        <td style="color:#64748b">{r['date']}</td>
        <td><span class="sig-badge">{', '.join(r.get('signal_types', []))}</span></td>
        <td style="color:{'#22d3ee' if r['direction']=='up' else '#f43f5e'}">{r['direction']}</td>
        <td style="color:{'#22d3ee' if r['direction_correct'] else '#f43f5e'}">{r['fwd_return_pct']:+.2f}%</td>
        <td style="color:{'#22d3ee' if (r.get('opt_pnl_pct') or -1) > 0 else '#94a3b8'}">{f"{r['opt_pnl_pct']:+.1f}%" if r.get('opt_pnl_pct') is not None else "—"}</td>
        <td style="color:#94a3b8">{r.get('rel_vol', 0):.1f}x</td>
      </tr>""" for r in top10)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>D — Quant Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #0a0e1a; --surface: #111827; --surface2: #1a2235;
      --border: #1e2d45; --text: #e2e8f0; --muted: #64748b;
      --green: #22d3ee; --red: #f43f5e; --yellow: #fbbf24; --blue: #60a5fa; --purple: #a78bfa;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; padding: 24px; }}
    .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
    .header h1 {{ font-size: 20px; font-weight: 700; letter-spacing: 0.05em; }}
    .header .meta {{ color: var(--muted); font-size: 11px; text-align: right; line-height: 1.8; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }}
    .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }}
    .card h3 {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); margin-bottom: 14px; }}
    .stat {{ font-size: 28px; font-weight: 700; }}
    .stat-label {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}
    .green {{ color: var(--green); }} .red {{ color: var(--red); }} .yellow {{ color: var(--yellow); }}
    .blue {{ color: var(--blue); }} .muted {{ color: var(--muted); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th {{ color: var(--muted); text-align: left; padding: 8px 10px; font-weight: 400; font-size: 11px; border-bottom: 1px solid var(--border); }}
    td {{ padding: 8px 10px; border-bottom: 1px solid rgba(30,45,69,0.5); }}
    .sig-badge {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 3px; padding: 2px 6px; font-size: 10px; color: var(--muted); }}
    .aplus-badge {{ background: rgba(167,139,250,0.15); border: 1px solid rgba(167,139,250,0.4); border-radius: 3px; padding: 2px 6px; font-size: 10px; color: var(--purple); }}
    .chart-wrap {{ position: relative; height: 220px; }}
    .chart-wrap-lg {{ position: relative; height: 280px; }}
    .section-title {{ font-size: 12px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin: 24px 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
    .aplus-compare {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .aplus-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }}
    .aplus-card.highlight {{ border-color: rgba(167,139,250,0.5); background: rgba(167,139,250,0.05); }}
    .metric-row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(30,45,69,0.5); }}
    .metric-row:last-child {{ border-bottom: none; }}
    @media (max-width: 768px) {{ .grid-2 {{ grid-template-columns: 1fr; }} .aplus-compare {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>D — QUANT DASHBOARD</h1>
      <div style="color:var(--muted);font-size:11px;margin-top:4px">{lookback}d lookback · {n_tickers} tickers · {len(records)} signals</div>
    </div>
    <div class="meta">
      <div>Updated: {run_at}</div>
      <div>delta={config.delta_target} · stop={int(config.stop_pct*100)}% · target={int(config.target_pct*100)}% · min_dte={config.min_dte}d</div>
    </div>
  </div>

  <p class="section-title">A+ Setup Performance</p>
  <div class="aplus-compare" id="apluscmp"></div>

  <p class="section-title">Signal Comparison</p>
  <div class="grid-2">
    <div class="card">
      <h3>Sharpe by Signal</h3>
      <div class="chart-wrap-lg"><canvas id="sharpeChart"></canvas></div>
    </div>
    <div class="card">
      <h3>Win Rate by Signal</h3>
      <div class="chart-wrap-lg"><canvas id="wrChart"></canvas></div>
    </div>
  </div>

  <p class="section-title">Equity Curve</p>
  <div class="card" style="margin-bottom:24px">
    <h3>Simulated $100 (directional returns, all signals)</h3>
    <div class="chart-wrap-lg"><canvas id="eqChart"></canvas></div>
  </div>

  <p class="section-title">Top 10 Setups</p>
  <div class="card" style="margin-bottom:24px">
    <table>
      <thead><tr><th>Ticker</th><th>Date</th><th>Signal</th><th>Dir</th><th>Fwd Ret</th><th>Opt P&L</th><th>RelVol</th></tr></thead>
      <tbody>{top10_rows}</tbody>
    </table>
  </div>

  <p class="section-title">Options P&L by Signal</p>
  <div class="card">
    <h3>Options Win Rate vs Directional Win Rate</h3>
    <div class="chart-wrap-lg"><canvas id="optChart"></canvas></div>
  </div>

<script>
  const SIGS = {sig_js};
  const EQ   = {eq_js};
  const AP   = {aplus_js};

  const gridOpts = {{ color: 'rgba(30,45,69,0.8)', lineWidth: 0.5 }};
  const tickOpts = {{ color: '#94a3b8', font: {{ size: 11, family: 'SF Mono, monospace' }} }};

  // ── A+ compare cards
  const cmp = document.getElementById('apluscmp');
  [['A+ Setup', AP.aplus, true], ['All Other Signals', AP.rest, false]].forEach(([label, d, hl]) => {{
    const wr_c  = d.wr  > 55 ? '#22d3ee' : d.wr  > 50 ? '#fbbf24' : '#f43f5e';
    const sh_c  = d.sharpe > 1.5 ? '#22d3ee' : d.sharpe > 0 ? '#fbbf24' : '#f43f5e';
    const pf_c  = d.pf > 1.5 ? '#22d3ee' : d.pf > 1.0 ? '#fbbf24' : '#f43f5e';
    cmp.innerHTML += `
      <div class="aplus-card ${{hl ? 'highlight' : ''}}">
        <div style="font-size:14px;font-weight:700;margin-bottom:14px;color:${{hl ? '#a78bfa' : '#94a3b8'}}">${{label}} <span style="font-size:11px;font-weight:400;color:var(--muted)">N=${{d.n}}</span></div>
        <div class="metric-row"><span style="color:var(--muted)">Win Rate</span><span style="color:${{wr_c}};font-weight:600">${{d.wr}}%</span></div>
        <div class="metric-row"><span style="color:var(--muted)">Sharpe</span><span style="color:${{sh_c}};font-weight:600">${{d.sharpe}}</span></div>
        <div class="metric-row"><span style="color:var(--muted)">Profit Factor</span><span style="color:${{pf_c}};font-weight:600">${{d.pf}}</span></div>
      </div>`;
  }});

  // ── Sharpe bar chart
  new Chart(document.getElementById('sharpeChart'), {{
    type: 'bar',
    data: {{
      labels: SIGS.map(s => s.name),
      datasets: [{{ label: 'Sharpe', data: SIGS.map(s => s.sharpe),
        backgroundColor: SIGS.map(s => s.name === 'a_plus' ? 'rgba(167,139,250,0.45)' : s.sharpe >= 1.5 ? 'rgba(34,211,238,0.35)' : s.sharpe >= 0 ? 'rgba(34,211,238,0.15)' : 'rgba(244,63,94,0.25)'),
        borderColor:     SIGS.map(s => s.name === 'a_plus' ? '#a78bfa' : s.sharpe >= 0 ? '#22d3ee' : '#f43f5e'),
        borderWidth: 1.5, borderRadius: 4 }}]
    }},
    options: {{ indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: ctx => ` Sharpe ${{ctx.parsed.x.toFixed(2)}}` }} }} }},
      scales: {{ x: {{ grid: gridOpts, ticks: tickOpts }}, y: {{ grid: {{ display: false }}, ticks: {{ ...tickOpts, color: '#94a3b8' }} }} }}
    }}
  }});

  // ── Win Rate bar chart
  new Chart(document.getElementById('wrChart'), {{
    type: 'bar',
    data: {{
      labels: SIGS.map(s => s.name),
      datasets: [{{ label: 'Win %', data: SIGS.map(s => s.wr),
        backgroundColor: SIGS.map(s => s.name === 'a_plus' ? 'rgba(167,139,250,0.45)' : s.wr >= 55 ? 'rgba(34,211,238,0.35)' : s.wr >= 50 ? 'rgba(251,191,36,0.25)' : 'rgba(244,63,94,0.25)'),
        borderColor:     SIGS.map(s => s.name === 'a_plus' ? '#a78bfa' : s.wr >= 55 ? '#22d3ee' : s.wr >= 50 ? '#fbbf24' : '#f43f5e'),
        borderWidth: 1.5, borderRadius: 4 }}]
    }},
    options: {{ indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: ctx => ` ${{ctx.parsed.x.toFixed(1)}}%` }} }} }},
      scales: {{
        x: {{ min: 45, max: 75, grid: gridOpts, ticks: {{ ...tickOpts, callback: v => v + '%' }} }},
        y: {{ grid: {{ display: false }}, ticks: {{ ...tickOpts, color: '#94a3b8' }} }}
      }}
    }}
  }});

  // ── Equity curve
  new Chart(document.getElementById('eqChart'), {{
    type: 'line',
    data: {{
      labels: EQ.map((_, i) => i),
      datasets: [{{ label: 'Equity', data: EQ,
        borderColor: EQ[EQ.length-1] > 100 ? '#22d3ee' : '#f43f5e',
        backgroundColor: EQ[EQ.length-1] > 100 ? 'rgba(34,211,238,0.06)' : 'rgba(244,63,94,0.06)',
        borderWidth: 2, pointRadius: 0, fill: true, tension: 0.2 }}]
    }},
    options: {{ responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: ctx => ` $$${{ctx.parsed.y.toFixed(2)}}` }} }} }},
      scales: {{
        y: {{ grid: gridOpts, ticks: {{ ...tickOpts, callback: v => '$' + v.toFixed(0) }} }},
        x: {{ display: false }}
      }}
    }}
  }});

  // ── Options: directional win% vs options win%
  const hasopts = SIGS.filter(s => s.opt_wr !== null);
  new Chart(document.getElementById('optChart'), {{
    type: 'bar',
    data: {{
      labels: hasopts.map(s => s.name),
      datasets: [
        {{ label: 'Dir Win%', data: hasopts.map(s => s.wr),
           backgroundColor: 'rgba(34,211,238,0.30)', borderColor: '#22d3ee', borderWidth: 1.5, borderRadius: 3 }},
        {{ label: 'Opt Win%', data: hasopts.map(s => s.opt_wr),
           backgroundColor: 'rgba(96,165,250,0.30)', borderColor: '#60a5fa', borderWidth: 1.5, borderRadius: 3 }},
      ]
    }},
    options: {{ responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: true, labels: {{ color: '#94a3b8', boxWidth: 12, font: {{ size: 11 }} }} }},
        tooltip: {{ callbacks: {{ label: ctx => ` ${{ctx.parsed.y.toFixed(1)}}%` }} }}
      }},
      scales: {{
        y: {{ min: 0, max: 80, grid: gridOpts, ticks: {{ ...tickOpts, callback: v => v + '%' }} }},
        x: {{ grid: {{ display: false }}, ticks: tickOpts }}
      }}
    }}
  }});
</script>
</body>
</html>"""

    viz_path = Path(__file__).parent / "backtest_viz.html"
    with open(viz_path, "w") as f:
        f.write(html)
    return str(viz_path)


def load_latest_results() -> Optional[Dict]:
    """Load the most recent backtest JSON."""
    files = sorted(RESULTS_DIR.glob("backtest_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    with open(files[0]) as f:
        return json.load(f)


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="D — Scanner Backtest Engine"
    )
    parser.add_argument("--days",     type=int,  default=60,
                        help="Lookback days (default: 60)")
    parser.add_argument("--tickers",  nargs="+", metavar="TICKER",
                        help="Specific tickers (default: UNIVERSE[:80])")
    parser.add_argument("--all",      action="store_true",
                        help="Use full UNIVERSE (~230 tickers, slower)")
    parser.add_argument("--signal",   default="all",
                        choices=["all", "gap", "gap_up", "gap_down", "inside",
                                 "double_inside", "highvol", "trend", "laggard",
                                 "breakout", "unfilled_gap", "vwap_reclaim", "a_plus"],
                        help="Filter to one signal type")
    parser.add_argument("--hold",     type=int,  default=1,
                        help="Hold period in days (default: 1 = next close)")
    parser.add_argument("--delta",    type=float, default=0.40,
                        help="Target delta for simulated contracts (default: 0.40)")
    parser.add_argument("--min-dte",  type=int,   default=2,
                        help="Minimum days-to-expiry for simulated contracts (default: 2)")
    parser.add_argument("--stop",     type=float, default=0.50,
                        help="Contract stop loss %% (default: 0.50 = 50%%)")
    parser.add_argument("--target",   type=float, default=1.00,
                        help="Contract take profit %% (default: 1.00 = 100%%)")
    parser.add_argument("--report",   action="store_true",
                        help="Load and print the last saved backtest result")
    parser.add_argument("--compare",  action="store_true",
                        help="Run all signal types and compare side by side")
    parser.add_argument("--combos",   action="store_true",
                        help="Compare signal combo performance (hv_only, hv+bk, etc.)")
    parser.add_argument("--ticker-rank", action="store_true",
                        help="Rank tickers by signal performance")
    parser.add_argument("--regime",   action="store_true",
                        help="Break down signal performance by HV20 regime (calm/normal/volatile)")
    parser.add_argument("--multi-hold", action="store_true",
                        help="Test multiple hold periods (1D, 2D, 3D) side by side")
    parser.add_argument("--laggard",  action="store_true",
                        help="Run sector laggard backtest only")
    parser.add_argument("--no-save",  action="store_true",
                        help="Don't save results to disk")
    args = parser.parse_args()

    # Load last result
    if args.report:
        data = load_latest_results()
        if not data:
            print("  No saved results found. Run a backtest first.")
            return
        records = data["records"]
        cfg = BacktestConfig(**{k: v for k, v in data["config"].items()
                                if k in BacktestConfig.__init__.__code__.co_varnames})
        print_report(records, cfg)
        return

    # Build config
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif args.all:
        tickers = list(UNIVERSE)
    else:
        tickers = list(UNIVERSE[:80])

    config = BacktestConfig(
        tickers=tickers,
        lookback_days=args.days,
        signal_filter="all" if args.compare else args.signal,
        hold_candles=args.hold,
        delta_target=args.delta,
        stop_pct=args.stop,
        target_pct=args.target,
        min_dte=args.min_dte,
    )

    # Sector laggard mode
    if args.laggard:
        print(f"\n{Fore.CYAN + Style.BRIGHT}  D — SECTOR LAGGARD BACKTEST{Style.RESET_ALL}")
        print(f"  {args.days}-day lookback\n")
        records = backtest_laggards(config)
        if records:
            print_report(records, config)
            if not args.no_save:
                path = save_results(records, config)
                print(f"  Saved: {path}")
        return

    # ── COMBO mode ────────────────────────────────────────────────────────────
    if args.combos:
        print(f"\n{Fore.CYAN + Style.BRIGHT}  D — SIGNAL COMBO ANALYSIS{Style.RESET_ALL}\n")
        all_records = run_backtest_cli(config)
        combos = [
            ("HV_ONLY (volatile)",  lambda r: "highvol" in r["signal_types"] and "breakout" not in r["signal_types"] and "inside" not in r["signal_types"] and "gap_up" not in r["signal_types"] and "gap_down" not in r["signal_types"] and r.get("hv20", 0) >= 0.35),
            ("HV_ONLY (calm/norm)", lambda r: "highvol" in r["signal_types"] and "breakout" not in r["signal_types"] and "inside" not in r["signal_types"] and r.get("hv20", 0) < 0.35),
            ("HV + INSIDE",         lambda r: "highvol" in r["signal_types"] and "inside" in r["signal_types"]),
            ("HV + GAP_DOWN",       lambda r: "highvol" in r["signal_types"] and "gap_down" in r["signal_types"]),
            ("HV + GAP_UP (fade)",  lambda r: "highvol" in r["signal_types"] and "gap_up" in r["signal_types"]),
            ("HV + BREAKOUT",       lambda r: "highvol" in r["signal_types"] and "breakout" in r["signal_types"]),
            ("BREAKOUT only",       lambda r: "breakout" in r["signal_types"] and "highvol" not in r["signal_types"]),
            ("GAP_DOWN only",       lambda r: "gap_down" in r["signal_types"] and "highvol" not in r["signal_types"]),
            ("GAP_UP only (fade)",  lambda r: "gap_up" in r["signal_types"] and "highvol" not in r["signal_types"]),
            ("INSIDE only",         lambda r: "inside" in r["signal_types"] and "highvol" not in r["signal_types"]),
        ]
        rows = []
        for label, fn in combos:
            subset = [r for r in all_records if fn(r)]
            if len(subset) < 5:
                continue
            m = calc_metrics(subset, label)
            wr_c = Fore.GREEN if m["win_rate"] > 0.55 else (Fore.YELLOW if m["win_rate"] > 0.50 else Fore.RED)
            pf_c = Fore.GREEN if m["profit_factor"] > 1.5 else (Fore.YELLOW if m["profit_factor"] > 1.0 else Fore.RED)
            sh_c = Fore.GREEN if m["sharpe"] > 2.0 else (Fore.YELLOW if m["sharpe"] > 0.5 else Fore.RED)
            rows.append([
                Fore.CYAN + label + Style.RESET_ALL,
                m["count"],
                f"{wr_c}{m['win_rate']*100:.1f}%{Style.RESET_ALL}",
                f"{pf_c}{m['profit_factor']:.2f}{Style.RESET_ALL}",
                f"{sh_c}{m['sharpe']:.2f}{Style.RESET_ALL}",
                f"{m['expected_value']:+.3f}%",
            ])
        headers = ["Combo", "N", "Win%", "PF", "Sharpe", "EV%"]
        print("  " + tabulate(rows, headers=headers, tablefmt="simple").replace("\n", "\n  "))
        if not args.no_save:
            save_results(all_records, config)
        return

    # ── REGIME mode ───────────────────────────────────────────────────────────
    if args.regime:
        print(f"\n{Fore.CYAN + Style.BRIGHT}  D — HV20 REGIME ANALYSIS{Style.RESET_ALL}\n")
        all_records = run_backtest_cli(config)
        signals_of_interest = ["highvol", "breakout", "inside", "gap_down"]
        for sig in signals_of_interest:
            base = [r for r in all_records if sig in r["signal_types"]]
            if not base:
                continue
            print(f"  {Fore.WHITE + Style.BRIGHT}{sig.upper()}{Style.RESET_ALL}  (N={len(base)} total)")
            rows = []
            for label, lo, hi in [("calm     HV20<0.20", 0.0, 0.20), ("normal   0.20-0.35", 0.20, 0.35), ("volatile HV20>0.35", 0.35, 9.99)]:
                subset = [r for r in base if lo <= r.get("hv20", 0) < hi]
                if len(subset) < 3:
                    continue
                m = calc_metrics(subset, label)
                wr_c = Fore.GREEN if m["win_rate"] > 0.54 else (Fore.YELLOW if m["win_rate"] > 0.50 else Fore.RED)
                pf_c = Fore.GREEN if m["profit_factor"] > 1.3 else (Fore.YELLOW if m["profit_factor"] > 1.0 else Fore.RED)
                rows.append([
                    label, m["count"],
                    f"{wr_c}{m['win_rate']*100:.1f}%{Style.RESET_ALL}",
                    f"{pf_c}{m['profit_factor']:.2f}{Style.RESET_ALL}",
                    f"{m['sharpe']:.2f}",
                    f"{m['expected_value']:+.3f}%",
                ])
            if rows:
                print("  " + tabulate(rows, headers=["Regime", "N", "Win%", "PF", "Sharpe", "EV%"], tablefmt="simple").replace("\n", "\n  "))
            print()
        return

    # ── TICKER RANK mode ──────────────────────────────────────────────────────
    if args.ticker_rank:
        print(f"\n{Fore.CYAN + Style.BRIGHT}  D — TICKER RANKING (highvol + breakout signals){Style.RESET_ALL}\n")
        all_records = run_backtest_cli(config)
        by_ticker = defaultdict(list)
        for r in all_records:
            if "highvol" in r["signal_types"] or "breakout" in r["signal_types"]:
                by_ticker[r["ticker"]].append(r)
        ranked = []
        for t, recs in by_ticker.items():
            m = calc_metrics(recs, t)
            if m["count"] >= 3:
                ranked.append((t, m))
        ranked.sort(key=lambda x: x[1]["sharpe"], reverse=True)
        rows = []
        for t, m in ranked[:25]:
            wr_c = Fore.GREEN if m["win_rate"] > 0.60 else (Fore.YELLOW if m["win_rate"] > 0.50 else Fore.RED)
            pf_c = Fore.GREEN if m["profit_factor"] > 2.0 else (Fore.YELLOW if m["profit_factor"] > 1.0 else Fore.RED)
            rows.append([
                Fore.WHITE + Style.BRIGHT + t + Style.RESET_ALL,
                m["count"],
                f"{wr_c}{m['win_rate']*100:.0f}%{Style.RESET_ALL}",
                f"{pf_c}{m['profit_factor']:.2f}{Style.RESET_ALL}",
                f"{m['sharpe']:.2f}",
                f"{m['expected_value']:+.3f}%",
                f"{m.get('opt_win_rate',0)*100:.0f}%" if m.get("opt_count") else "—",
            ])
        print("  " + tabulate(rows, headers=["Ticker", "N", "Win%", "PF", "Sharpe", "EV%", "Opt Win%"], tablefmt="simple").replace("\n", "\n  "))
        print()
        if not args.no_save:
            save_results(all_records, config)
        return

    # ── MULTI-HOLD mode ───────────────────────────────────────────────────────
    if args.multi_hold:
        print(f"\n{Fore.CYAN + Style.BRIGHT}  D — MULTI-HOLD PERIOD ANALYSIS{Style.RESET_ALL}\n")
        base_recs = run_backtest_cli(config)
        # For multi-hold, re-run with different hold_candles if records have fwd data
        # We'll use the entry_return_pct (open-to-close) vs fwd_return_pct (close-to-next-close)
        # as our 0.5D vs 1D proxies; for 2D+ we need a re-run
        signals_to_test = ["highvol", "breakout"]
        rows = []
        for sig in signals_to_test:
            base = [r for r in base_recs if sig in r["signal_types"] and r.get("hv20", 0) >= 0.35]
            if len(base) < 5:
                continue
            # Intraday (open→close)
            intra = [r.get("entry_return_pct", 0) * (1 if r["direction"] == "up" else -1) for r in base]
            # Overnight (close→next close = signed_return_pct for 1D hold)
            overn = [r.get("signed_return_pct", 0) for r in base]
            def row_stats(rets, label):
                wins = sum(1 for x in rets if x > 0)
                avg  = sum(rets) / len(rets) if rets else 0
                std  = (sum((x-avg)**2 for x in rets)/len(rets))**0.5 if len(rets) > 1 else 1
                sharpe = (avg/std)*math.sqrt(252) if std > 0 else 0
                pf_g = sum(x for x in rets if x > 0)
                pf_l = abs(sum(x for x in rets if x < 0))
                pf   = pf_g/pf_l if pf_l > 0 else 999
                wr_c = Fore.GREEN if wins/len(rets) > 0.54 else (Fore.YELLOW if wins/len(rets) > 0.50 else Fore.RED)
                return [f"{sig}  {label}", len(rets),
                        f"{wr_c}{wins/len(rets)*100:.1f}%{Style.RESET_ALL}",
                        f"{pf:.2f}", f"{sharpe:.2f}", f"{avg:+.3f}%"]
            rows.append(row_stats(intra, "intraday (open→close)"))
            rows.append(row_stats(overn, "overnight (close→next)"))
        # Re-run with 2D and 3D hold
        for hold in [2, 3]:
            cfg2 = BacktestConfig(tickers=config.tickers, lookback_days=config.lookback_days,
                                  hold_candles=hold, delta_target=config.delta_target,
                                  stop_pct=config.stop_pct, target_pct=config.target_pct)
            recs2 = run_backtest_cli(cfg2)
            for sig in signals_to_test:
                subset = [r for r in recs2 if sig in r["signal_types"] and r.get("hv20", 0) >= 0.35]
                if len(subset) < 5:
                    continue
                rets = [r.get("signed_return_pct", 0) for r in subset]
                wins = sum(1 for x in rets if x > 0)
                avg  = sum(rets) / len(rets) if rets else 0
                std  = (sum((x-avg)**2 for x in rets)/len(rets))**0.5 if len(rets) > 1 else 1
                sharpe = (avg/std)*math.sqrt(252) if std > 0 else 0
                pf_g = sum(x for x in rets if x > 0)
                pf_l = abs(sum(x for x in rets if x < 0))
                pf   = pf_g/pf_l if pf_l > 0 else 999
                wr_c = Fore.GREEN if wins/len(rets) > 0.54 else (Fore.YELLOW if wins/len(rets) > 0.50 else Fore.RED)
                rows.append([f"{sig}  {hold}D hold", len(rets),
                              f"{wr_c}{wins/len(rets)*100:.1f}%{Style.RESET_ALL}",
                              f"{pf:.2f}", f"{sharpe:.2f}", f"{avg:+.3f}%"])
        print("  (volatile regime only: HV20 >= 0.35)\n")
        print("  " + tabulate(rows, headers=["Signal / Hold", "N", "Win%", "PF", "Sharpe", "Avg Ret"], tablefmt="simple").replace("\n", "\n  "))
        print()
        return

    # Compare mode: run all signal types
    if args.compare:
        print(f"\n{Fore.CYAN + Style.BRIGHT}  D — SIGNAL COMPARISON{Style.RESET_ALL}\n")
        all_records = run_backtest_cli(config)
        signal_types = ["gap_up", "gap_down", "inside", "double_inside", "highvol", "trend", "breakout", "unfilled_gap", "vwap_reclaim", "a_plus"]
        rows = []
        for sig in signal_types:
            recs = [r for r in all_records if sig in r.get("signal_types", [])]
            if not recs:
                continue
            m = calc_metrics(recs, sig)
            wr_c  = Fore.GREEN if m.get("win_rate", 0) > 0.55 else (Fore.YELLOW if m.get("win_rate", 0) > 0.50 else Fore.RED)
            pf_c  = Fore.GREEN if m.get("profit_factor", 0) > 1.5 else (Fore.YELLOW if m.get("profit_factor", 0) > 1.0 else Fore.RED)
            rows.append([
                Fore.CYAN + sig + Style.RESET_ALL,
                m["count"],
                f"{wr_c}{m.get('win_rate', 0)*100:.1f}%{Style.RESET_ALL}",
                f"{m.get('avg_return', 0):+.2f}%",
                f"{pf_c}{m.get('profit_factor', 0):.2f}{Style.RESET_ALL}",
                f"{m.get('sharpe', 0):.2f}",
                f"{m.get('expected_value', 0):+.3f}%",
                f"{m.get('opt_win_rate', 0)*100:.1f}%" if m.get("opt_count") else "—",
                f"{m.get('opt_avg_pnl', 0):+.1f}%" if m.get("opt_count") else "—",
                f"${m.get('opt_avg_usd', 0):+.2f}" if m.get("opt_count") else "—",
            ])
        headers = ["Signal", "N", "Win%", "Avg Ret", "PF", "Sharpe", "EV", "Opt Win%", "Opt Avg%", "$/contract"]
        print("\n  " + tabulate(rows, headers=headers, tablefmt="simple").replace("\n", "\n  "))
        if not args.no_save:
            save_results(all_records, config)
        return

    # Standard backtest
    records = run_backtest_cli(config)
    if not records:
        print(f"\n  {Fore.RED}No signals found.{Style.RESET_ALL}")
        return

    print_report(records, config)

    if not args.no_save:
        path = save_results(records, config)
        print(f"  Saved: {path}\n")


if __name__ == "__main__":
    main()
