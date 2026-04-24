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
        hold_candles: int = 1,            # how many daily bars to hold (1 = next day close)
        delta_target: float = 0.30,       # target delta for simulated contract
        stop_pct: float = 0.50,           # stop at 50% loss on contract
        target_pct: float = 1.00,         # take profit at 100% gain on contract
        min_rel_vol: float = 1.5,         # min relative volume to qualify as "high vol" signal
        gap_threshold: float = 0.005,     # min gap % to flag (0.5%)
        vrp_multiplier: float = 1.20,     # IV = HV20 × this (vol risk premium proxy)
        spread_sim: float = 0.05,         # simulate 5% bid/ask spread on mid
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

    Each record contains:
      date, signal_type, direction, price (open of signal day),
      prior_close, rel_vol, gap_pct, setup_quality
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

    # Rolling 20-day avg volume for relative volume
    vol_series = pd.Series(volumes)
    avg_vol_20 = vol_series.rolling(20).mean().values

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

        # ── Gap signal ──────────────────────────────────────────────────────
        if abs(gap_pct) >= config.gap_threshold:
            sig = "gap_up" if gap_pct > 0 else "gap_down"
            # Gap fill bias: fade the gap → gap up = expect down fill, gap down = expect up fill
            direction = "down" if gap_pct > 0 else "up"
            signal_types.append(sig)
            directions.append(direction)
            setup_q = max(setup_q, min(1.0, abs(gap_pct) / 0.03))

        # ── Inside bar ─────────────────────────────────────────────────────
        prev_high = highs[i - 1]
        prev_low  = lows[i - 1]
        is_inside = highs[i] <= prev_high and lows[i] >= prev_low
        if is_inside:
            # Inside day: direction = whichever way today closed relative to midpoint
            mid = (prev_high + prev_low) / 2
            direction = "up" if closes[i] > mid else "down"
            signal_types.append("inside")
            directions.append(direction)
            setup_q = max(setup_q, 0.6)

        # ── Double inside bar ───────────────────────────────────────────────
        if i >= 2:
            prev2_high = highs[i - 2]
            prev2_low  = lows[i - 2]
            prev_inside = highs[i-1] <= prev2_high and lows[i-1] >= prev2_low
            if is_inside and prev_inside:
                signal_types.append("double_inside")
                direction = "up" if closes[i] > (prev2_high + prev2_low) / 2 else "down"
                directions.append(direction)
                setup_q = max(setup_q, 0.75)

        # ── High relative volume ────────────────────────────────────────────
        if rel_vol >= config.min_rel_vol:
            direction = "up" if closes[i] > open_p else "down"
            signal_types.append("highvol")
            directions.append(direction)
            setup_q = max(setup_q, min(1.0, (rel_vol - 1.5) / 3.0 + 0.4))

        # ── Breakout: price closed above prior day high (bull) or below prior day low (bear) ──
        if not is_inside and rel_vol > 1.2:
            if closes[i] > prev_high:
                signal_types.append("breakout")
                directions.append("up")
                setup_q = max(setup_q, min(1.0, (rel_vol - 1.0) / 3.0 + 0.5))
            elif closes[i] < prev_low:
                signal_types.append("breakout")
                directions.append("down")
                setup_q = max(setup_q, min(1.0, (rel_vol - 1.0) / 3.0 + 0.5))

        # ── Trend day (strong directional close) ───────────────────────────
        body_pct = abs(closes[i] - open_p) / open_p if open_p > 0 else 0
        range_pct = (highs[i] - lows[i]) / lows[i] if lows[i] > 0 else 0
        if body_pct > 0.015 and body_pct / max(range_pct, 0.001) > 0.65:
            direction = "up" if closes[i] > open_p else "down"
            signal_types.append("trend")
            directions.append(direction)
            setup_q = max(setup_q, min(1.0, body_pct / 0.04))

        if not signal_types:
            continue

        # Filter by signal type if requested
        if config.signal_filter != "all":
            if config.signal_filter not in signal_types:
                continue

        # Use consensus direction (majority vote)
        up_votes = directions.count("up")
        dn_votes = directions.count("down")
        direction = "up" if up_votes >= dn_votes else "down"

        # Historical vol for IV proxy
        hv = calc_hv(hist["Close"].iloc[:i+1])

        signals.append({
            "date":         dates[i].strftime("%Y-%m-%d"),
            "signal_types": signal_types,
            "direction":    direction,
            "price":        round(price, 4),
            "open_p":       round(open_p, 4),
            "prior_close":  round(prior, 4),
            "rel_vol":      round(rel_vol, 2),
            "gap_pct":      round(gap_pct * 100, 3),
            "hv20":         round(hv, 4),
            "setup_q":      round(setup_q, 3),
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
            T0  = config.hold_candles / 365.0
            T1  = max(0.0, T0 - 1 / 365.0)    # time remaining at exit

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

                # Exit price at forward date
                if T1 > 0:
                    exit_mid = bs_price(next_close, K, T1, iv, opt_type)
                else:
                    # At expiry: intrinsic only
                    if opt_type == "call":
                        exit_mid = max(0.0, next_close - K)
                    else:
                        exit_mid = max(0.0, K - next_close)

                exit_price = exit_mid * (1 - config.spread_sim)  # receive the bid
                opt_exit   = round(exit_price, 4)

                raw_pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0

                # Apply stop/target rules
                # Check if stop would have been hit (intraday adverse move)
                if dirn == "up":
                    adverse_price = next_low  # worst intraday price for a long call
                else:
                    adverse_price = next_high  # worst intraday price for a long put

                adverse_mid = bs_price(adverse_price, K, T0 * 0.5, iv, opt_type)
                adverse_pnl = (adverse_mid - entry_price) / entry_price if entry_price > 0 else 0.0

                if adverse_pnl <= -config.stop_pct:
                    # Stopped out
                    raw_pnl_pct = -config.stop_pct
                    opt_exit    = round(entry_price * (1 - config.stop_pct), 4)
                elif raw_pnl_pct >= config.target_pct:
                    # Target hit
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
                        "direction_correct": int(correct),
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

    # ── Options simulation summary ─────────────────────────────────────────
    opt_records = [r for r in all_records if r.get("opt_pnl_pct") is not None]
    if opt_records:
        print(f"\n{sep}\n{Fore.YELLOW}  OPTIONS SIMULATION SUMMARY{Style.RESET_ALL}\n")
        m = calc_metrics(opt_records, "Options")
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

  REMINDER: These are BS model estimates using HV×1.2 as IV proxy.
  Real IV, spreads, and slippage will differ. Add ~20% to entry cost
  and ~20% to exit spread for a more conservative real-world estimate.
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
def run_backtest(config: BacktestConfig) -> List[Dict]:
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

    return str(json_path)


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
                                 "double_inside", "highvol", "trend", "laggard"],
                        help="Filter to one signal type")
    parser.add_argument("--hold",     type=int,  default=1,
                        help="Hold period in days (default: 1 = next close)")
    parser.add_argument("--delta",    type=float, default=0.30,
                        help="Target delta for simulated contracts (default: 0.30)")
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
        all_records = run_backtest(config)
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
        all_records = run_backtest(config)
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
        all_records = run_backtest(config)
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
        base_recs = run_backtest(config)
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
            recs2 = run_backtest(cfg2)
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
        all_records = run_backtest(config)
        signal_types = ["gap_up", "gap_down", "inside", "double_inside", "highvol", "trend", "breakout"]
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
    records = run_backtest(config)
    if not records:
        print(f"\n  {Fore.RED}No signals found.{Style.RESET_ALL}")
        return

    print_report(records, config)

    if not args.no_save:
        path = save_results(records, config)
        print(f"  Saved: {path}\n")


if __name__ == "__main__":
    main()
