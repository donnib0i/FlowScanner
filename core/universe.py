"""
universe.py — Live universe builder.

Finds tickers by where money is actually flowing right now, not a static list.
Refreshes every 15 minutes. Falls back to ANCHOR on any failure.

Sources (merged in priority order):
  1. Yahoo Finance most-active screener  — highest dollar volume today
  2. Yahoo Finance day gainers/losers     — where price is moving
  3. S&P 500 constituents                — Wikipedia, full index coverage
  4. Nasdaq-100 constituents             — Wikipedia
  5. High relative volume filter         — unusual activity vs prior day
  6. ANCHOR ETFs                         — always included (index/sector/leveraged)
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import warnings
from typing import List, Tuple

import pandas as pd
import requests
import yfinance as yf

from data.unusual_flow import get_scan_pool as _get_sector_pool

warnings.filterwarnings("ignore")

# ── Always-on anchors ──────────────────────────────────────────────────────────
# Small permanent set: broad market, sector, leveraged, vol ETFs.
# These are always included regardless of screener results.
ANCHOR: List[str] = [
    # Broad
    "SPY", "QQQ", "IWM", "DIA", "MDY",
    # Vol
    "VXX", "UVXY", "SVXY",
    # Bull leveraged
    "TQQQ", "SOXL", "UPRO", "SPXL", "TNA", "LABU", "TECL", "FNGU",
    # Bear leveraged
    "SQQQ", "SOXS", "SPXS", "TZA", "LABD", "TECS", "FNGD",
    # Sector ETFs
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLC", "XLRE",
    # Commodities
    "GLD", "SLV", "USO", "CPER",
    # Mega cap — always worth watching
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "TSLA",
]

_CACHE: dict = {"tickers": [], "ts": 0.0}
_TTL = 900  # 15 min


# ── Data fetchers ──────────────────────────────────────────────────────────────

def _yf_screener(scr_id: str, n: int = 100) -> List[str]:
    """Hit Yahoo Finance predefined screener. Returns list of symbols."""
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        r = requests.get(
            url,
            params={"formatted": "false", "scrIds": scr_id, "count": n},
            headers={"User-Agent": "Mozilla/5.0 (compatible)"},
            timeout=8,
        )
        r.raise_for_status()
        quotes = r.json()["finance"]["result"][0]["quotes"]
        return [q["symbol"] for q in quotes if "symbol" in q]
    except Exception:
        return []


def fetch_most_active(n: int = 100) -> List[str]:
    """Top stocks by dollar volume right now."""
    return _yf_screener("most_actives", n)


def fetch_day_gainers(n: int = 75) -> List[str]:
    """Top % gainers today — where upward momentum money is flowing."""
    return _yf_screener("day_gainers", n)


def fetch_day_losers(n: int = 75) -> List[str]:
    """Top % losers today — where bearish flow / puts may be active."""
    return _yf_screener("day_losers", n)


def fetch_sp500() -> List[str]:
    """S&P 500 constituents from Wikipedia. ~503 tickers."""
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            storage_options={"User-Agent": "Mozilla/5.0"},
        )
        syms = tables[0]["Symbol"].tolist()
        return [str(s).replace(".", "-") for s in syms if isinstance(s, str)]
    except Exception:
        return []


def fetch_nasdaq100() -> List[str]:
    """Nasdaq-100 constituents from Wikipedia. ~100 tickers."""
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            storage_options={"User-Agent": "Mozilla/5.0"},
        )
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            for i, c in enumerate(cols):
                if "ticker" in c or "symbol" in c:
                    syms = t.iloc[:, i].dropna().tolist()
                    return [
                        s for s in syms
                        if isinstance(s, str) and s.replace("-", "").isalpha() and len(s) <= 5
                    ]
        return []
    except Exception:
        return []


def fetch_high_relvol(
    candidates: List[str],
    min_relvol: float = 1.5,
    top_n: int = 100,
) -> List[Tuple[str, float]]:
    """
    Screen candidates for unusually high relative volume (today vs prior day).
    relvol > 1.5 = money moving in abnormally. Returns (ticker, relvol) sorted desc.
    """
    if not candidates:
        return []
    try:
        batch = yf.download(
            candidates,
            period="2d",
            interval="1d",
            group_by="ticker",
            progress=False,
            threads=True,
            timeout=20,
            auto_adjust=True,
        )
        scored: List[Tuple[str, float]] = []
        for t in candidates:
            try:
                if isinstance(batch.columns, pd.MultiIndex):
                    hist = (
                        batch[t].dropna(how="all")
                        if t in batch.columns.get_level_values(0)
                        else pd.DataFrame()
                    )
                else:
                    hist = batch.dropna(how="all")
                if len(hist) < 2:
                    continue
                prev_vol = float(hist["Volume"].iloc[-2])
                today_vol = float(hist["Volume"].iloc[-1])
                prev_close = float(hist["Close"].iloc[-2])
                if prev_vol <= 0 or prev_close < 1.0:
                    continue
                relvol = today_vol / prev_vol
                if relvol >= min_relvol:
                    scored.append((t, relvol))
            except Exception:
                continue
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]
    except Exception:
        return []


# ── Clean symbol filter ────────────────────────────────────────────────────────

def _clean(syms: List[str]) -> List[str]:
    """Remove obviously invalid symbols."""
    out = []
    seen = set()
    for s in syms:
        if not isinstance(s, str):
            continue
        s = s.upper().strip()
        # Allow only letters and hyphen (BRK-B style), 1-5 chars
        core = s.replace("-", "")
        if not (1 <= len(s) <= 5 and core.isalpha()):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ── Main builder ───────────────────────────────────────────────────────────────

def build_universe() -> List[str]:
    """
    Assemble a live, data-driven universe.

    Tier 1 (money flow NOW):   most active + day movers
    Tier 2 (index coverage):   S&P 500 + Nasdaq-100
    Tier 3 (unusual activity): high relative volume within T2
    Tier 4 (always on):        ANCHOR ETFs

    Returns deduplicated list with highest-signal tickers first.
    """
    # Tier 1 — live money flow signals (fast API calls)
    most_active  = fetch_most_active(n=100)
    day_gainers  = fetch_day_gainers(n=75)
    day_losers   = fetch_day_losers(n=75)

    # Tier 2 — index coverage (slower, full index memberships)
    sp500 = fetch_sp500()
    ndx   = fetch_nasdaq100()

    # Tier 3 — relative volume anomalies within the index pool
    # Batch the index tickers in chunks to keep it fast
    index_pool = _clean(list(dict.fromkeys(sp500 + ndx)))[:400]
    relvol_movers = [t for t, _ in fetch_high_relvol(index_pool, min_relvol=1.5, top_n=100)]

    # Merge: signal tickers first, then broad coverage
    ordered = (
        _clean(most_active)       # highest signal — money is HERE right now
        + _clean(day_gainers)     # price is moving up with volume
        + _clean(day_losers)      # bearish flow / put activity
        + _clean(relvol_movers)   # unusual vs prior day baseline
        + _clean(sp500)           # full S&P 500
        + _clean(ndx)             # Nasdaq-100 overlap/extras
        + _clean(_get_sector_pool())  # comprehensive sector coverage (S&P500 + screeners, live)
        + list(ANCHOR)
    )

    # Single-stocks only: strip ETFs (mega-cap stocks in ANCHOR survive the filter).
    return finalize_universe(ordered, exclude_etfs=True)


def finalize_universe(raw: List[str], exclude_etfs: bool = True) -> List[str]:
    """Clean + dedupe a raw ticker list, optionally dropping ETFs (keep stocks)."""
    cleaned = _clean(raw)
    if exclude_etfs:
        from data.etf_filter import filter_etfs
        return filter_etfs(cleaned)
    return cleaned


def get_universe(force: bool = False) -> List[str]:
    """
    Cached universe. Refreshes every 15 min.
    Pass force=True to rebuild immediately.
    Returns ANCHOR list as fallback if all sources fail.
    """
    now = time.time()
    if not force and now - _CACHE["ts"] < _TTL and _CACHE["tickers"]:
        return list(_CACHE["tickers"])

    tickers = build_universe()
    if tickers:
        _CACHE["tickers"] = tickers
        _CACHE["ts"] = now
        return list(tickers)

    return list(ANCHOR)


def universe_summary() -> str:
    """One-line summary of current universe size and sources."""
    u = get_universe()
    return f"{len(u)} tickers (live: most-active + gainers/losers + high-relvol + S&P500 + NDX)"
