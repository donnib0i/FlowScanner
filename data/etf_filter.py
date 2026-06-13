"""
etf_filter.py — single source of truth for "is this an ETF?".

Two tiers:
  1. KNOWN_ETFS — the symbols previously hardcoded as anchors across the scanner.
     These are exactly the ETFs that used to pollute results. Offline, instant.
  2. yfinance quoteType lookup for unknown screener tickers, cached to disk so
     each name is queried at most once.
"""
from __future__ import annotations

import json
import os
from typing import Iterable, List

# Seeded from the old _ETF_ANCHORS (unusual_flow.py) + ANCHOR ETFs (universe.py).
# Mega-cap stocks that lived in ANCHOR (AAPL/MSFT/NVDA/META/AMZN/GOOGL/TSLA) are
# deliberately NOT here — they are stocks.
KNOWN_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "MDY",
    "VXX", "UVXY", "SVXY",
    "TQQQ", "SOXL", "UPRO", "SPXL", "TNA", "LABU", "TECL", "FNGU",
    "SQQQ", "SOXS", "SPXS", "TZA", "LABD", "TECS", "FNGD",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLC", "XLRE",
    "GLD", "SLV", "USO", "CPER", "KWEB", "FXI", "XBI", "ARKK",
}

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "baselines", "etf_cache.json")
_cache: dict | None = None


def _set_cache_path(path: str) -> None:
    global _CACHE_PATH
    _CACHE_PATH = path


def _reset_cache() -> None:
    global _cache
    _cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_CACHE_PATH) as f:
            _cache = json.load(f)
    except Exception:
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(_cache, f)
    except Exception:
        pass


def _lookup_quote_type(ticker: str) -> str:
    """Query yfinance for the security type. Returns 'ETF', 'EQUITY', or '' on failure."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).get_info()
        return str(info.get("quoteType", "")).upper()
    except Exception:
        return ""


def is_etf(ticker: str, network: bool = False) -> bool:
    """
    True if `ticker` is an ETF.

    Fast path (default): known-ETF blocklist + cached answers only — no network,
    safe to call across the whole universe. Unknown names are treated as stocks.
    Pass network=True to confirm an unknown name via a (slow) yfinance quoteType
    lookup; the result is cached so it is paid at most once per name.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return False
    if t in KNOWN_ETFS:
        return True
    cache = _load_cache()
    if t in cache:
        return cache[t] == "ETF"
    if not network:
        return False
    qt = _lookup_quote_type(t)
    if qt:                      # only cache confident answers
        cache[t] = qt
        _save_cache()
    return qt == "ETF"


def filter_etfs(tickers: Iterable[str], network: bool = False) -> List[str]:
    """Drop ETFs, preserve order, dedupe. network=True confirms unknowns via yfinance."""
    out, seen = [], set()
    for t in tickers:
        u = (t or "").upper().strip()
        if not u or u in seen or is_etf(u, network=network):
            continue
        seen.add(u)
        out.append(u)
    return out
