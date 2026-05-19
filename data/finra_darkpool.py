#!/usr/bin/env python3
"""
finra_darkpool.py — Dark Pool Intelligence Module

Primary: yfinance-based dark pool proxy (real-time, no credentials)
  Dark pool signature: high relative volume + suppressed price movement
  = institutional accumulation/distribution happening off-exchange.
  Formula: dp_score = (vol_ratio / max(abs(daily_return), 0.002)) * scaling

Fallback: FINRA OTC Transparency API (limited to ~2023 data)
  Used as supplementary signal when available.

No API key required.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, date
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ─── Tuning ───────────────────────────────────────────────────────────────────

# Days of history to compute avg volume baseline
LOOKBACK_DAYS = 20

# Minimum relative volume to flag (1.3× avg — dark pool doesn't need extreme volume)
MIN_VOL_RATIO = 1.3

# Min score to emit a directional label (vs NEUTRAL)
DIRECTIONAL_THRESHOLD = 15.0

# Min score to include in results at all
MIN_SCORE = 10.0

# Cache TTL: yfinance data is intraday-cached by Yahoo anyway, 15-min TTL
DP_CACHE_TTL = 900  # 15 minutes

_dp_cache: Dict[str, object] = {}
_dp_cache_ts: float = 0.0


# ─── Core signal engine ───────────────────────────────────────────────────────

def _compute_signal(ticker: str) -> Optional[Dict]:
    """
    Compute dark pool proxy signal for a single ticker.

    Returns signal dict or None on data failure.
    """
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="25d", auto_adjust=True)
        if hist.empty or len(hist) < 5:
            return None

        hist = hist.dropna(subset=["Close", "Volume"])
        if len(hist) < 5:
            return None

        # Today's session (most recent bar)
        today = hist.iloc[-1]
        prior = hist.iloc[:-1]

        today_vol = float(today["Volume"])
        avg_vol = float(prior["Volume"].mean())

        if avg_vol <= 0 or today_vol <= 0:
            return None

        vol_ratio = today_vol / avg_vol

        if vol_ratio < MIN_VOL_RATIO:
            return None  # below threshold — skip

        # Price impact: abs daily return
        today_open = float(today["Open"]) if "Open" in today and today["Open"] > 0 else float(today["Close"])
        today_close = float(today["Close"])
        daily_return = (today_close - today_open) / today_open if today_open > 0 else 0.0

        # Dark pool proxy score: high volume + low price impact = stealth institutional flow
        # vol component:    (ratio - 1) × 50  → 1.3x=15, 1.5x=25, 2x=50, 3x=100
        # stealth bonus:    max(0, 2.0 - impact_pct) × 10  → rewards sub-2% impact (max +20)
        price_impact_pct = abs(daily_return) * 100
        vol_component = (vol_ratio - 1.0) * 50.0
        stealth_bonus = max(0.0, 2.0 - price_impact_pct) * 10.0
        score = min(100.0, max(0.0, vol_component + stealth_bonus))

        # Direction: use 5-day price trend
        if len(hist) >= 6:
            trend = (float(hist.iloc[-1]["Close"]) / float(hist.iloc[-6]["Close"])) - 1.0
        else:
            trend = daily_return

        if score >= DIRECTIONAL_THRESHOLD:
            signal = "ACCUMULATION" if trend >= 0 else "DISTRIBUTION"
        else:
            signal = "NEUTRAL"

        # Dollar volume proxy
        dollar_vol = today_vol * today_close

        return {
            "ticker": ticker,
            "dp_ratio": round(min(1.0, vol_ratio / 10.0), 4),   # normalised 0–1 for UI compat
            "dp_ratio_4wk_avg": 1.0,
            "dp_anomaly": round(vol_ratio - 1.0, 4),
            "dp_dollar_vol": round(dollar_vol, 2),
            "dp_trades": 0,  # not available from yfinance
            "signal": signal,
            "score": round(score, 2),
            "vol_ratio": round(vol_ratio, 2),
            "price_impact_pct": round(abs(daily_return) * 100, 3),
            "week": date.today().isoformat(),
            "source": "yfinance-proxy",
        }

    except Exception as exc:
        logger.debug("Signal compute failed for %s: %s", ticker, exc)
        return None


def get_darkpool_signals(tickers: List[str]) -> List[Dict]:
    """
    For a list of tickers, return dark pool proxy signals.

    Each signal dict:
    {
        "ticker":            str,
        "dp_ratio":          float,  # normalised vol anomaly (0–1)
        "dp_ratio_4wk_avg":  float,  # always 1.0 (baseline)
        "dp_anomaly":        float,  # vol_ratio - 1 (how many × above avg)
        "dp_dollar_vol":     float,  # estimated dollar volume today
        "dp_trades":         int,    # 0 (not available)
        "signal":            str,    # ACCUMULATION | DISTRIBUTION | NEUTRAL
        "score":             float,  # 0–100
        "vol_ratio":         float,  # today vol / 20-day avg vol
        "price_impact_pct":  float,  # abs(daily return) %
        "week":              str,    # today's date
        "source":            str,    # "yfinance-proxy"
    }
    """
    if not tickers:
        return []

    signals: List[Dict] = []
    for ticker in tickers:
        sig = _compute_signal(ticker.upper())
        if sig is not None and sig["score"] >= MIN_SCORE:
            signals.append(sig)

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def get_darkpool_signals_cached(tickers: List[str]) -> List[Dict]:
    """TTL-cached version of get_darkpool_signals (15-min TTL)."""
    global _dp_cache, _dp_cache_ts

    cache_key = ",".join(sorted(t.upper() for t in tickers))
    now = time.time()

    if cache_key in _dp_cache and (now - _dp_cache_ts) < DP_CACHE_TTL:
        return _dp_cache[cache_key]

    signals = get_darkpool_signals(tickers)
    _dp_cache[cache_key] = signals
    _dp_cache_ts = now
    return signals


# ─── Display formatting ───────────────────────────────────────────────────────

def format_darkpool_table(signals: List[Dict]) -> str:
    if not signals:
        return "No dark pool signals found."

    try:
        from tabulate import tabulate
    except ImportError:
        lines = ["Ticker | Signal | Score | VolRatio | PriceImpact% | $Vol | Date"]
        for s in signals:
            lines.append(
                f"{s['ticker']:6s} | {s['signal']:12s} | {s['score']:5.1f} "
                f"| {s.get('vol_ratio', 0):5.1f}× | {s.get('price_impact_pct', 0):.3f}% "
                f"| ${s['dp_dollar_vol']:>14,.0f} | {s['week']}"
            )
        return "\n".join(lines)

    rows = []
    for s in signals:
        rows.append([
            s["ticker"],
            s["signal"],
            f"{s['score']:.1f}",
            f"{s.get('vol_ratio', 0):.1f}×",
            f"{s.get('price_impact_pct', 0):.3f}%",
            f"${s['dp_dollar_vol']:>14,.0f}",
            s["week"],
        ])

    headers = ["Ticker", "Signal", "Score", "Vol Ratio", "Price Impact", "Est $ Vol", "Date"]
    return tabulate(rows, headers=headers, tablefmt="rounded_outline", stralign="left")


# ─── CLI entrypoint ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Dark Pool Proxy Signal Scanner")
    parser.add_argument("tickers", nargs="+", help="Ticker symbols to scan")
    parser.add_argument("--min-score", type=float, default=0, help="Min score filter")
    args = parser.parse_args()

    signals = get_darkpool_signals(args.tickers)
    if args.min_score:
        signals = [s for s in signals if s["score"] >= args.min_score]

    print(format_darkpool_table(signals))

    if not signals:
        print("\nNo anomalies detected. Vol ratio must be ≥2× avg to flag.")
