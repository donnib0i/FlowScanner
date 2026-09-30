"""
data/cboe_chain.py -- CBOE's delayed options snapshot, as an open-interest source.

Why this exists: outside market hours yfinance reports zero open interest (and
an IV of 1e-5) across the whole chain, so the gamma surface could not be built
pre-market or after the close. dxFeed fixes that, but only where a TastyTrade
session can authenticate without an SMS -- which rules out the Railway
container. CBOE publishes the same settled open interest, plus IV and the
closing bid/ask, on a public CDN with no key: it works everywhere.

Returns {} on any failure. The caller's surface is degraded without it, never
broken by it.
"""
import logging
import re
import threading
import time
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
_TTL_SECS = 600          # the snapshot is ~15m delayed; refetching sooner buys nothing
_TIMEOUT = 30            # SPX is ~13MB
_INDEXES = {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX", "OEX", "XEO", "MRUT"}

# OCC symbol: root (SPX / SPXW / QQQ ...), YYMMDD, C|P, strike x 1000 (8 digits)
_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

_cache: Dict[str, tuple] = {}
_lock = threading.Lock()


def _cboe_symbol(symbol: str) -> str:
    s = symbol.strip().upper().lstrip("^")
    return f"_{s}" if s in _INDEXES else s


def _fetch_raw(symbol: str) -> List[Dict]:
    sym = _cboe_symbol(symbol)
    with _lock:
        hit = _cache.get(sym)
        if hit and time.monotonic() - hit[0] < _TTL_SECS:
            return hit[1]
    r = requests.get(CBOE_URL.format(sym=sym), timeout=_TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    opts = (r.json().get("data") or {}).get("options") or []
    with _lock:
        _cache[sym] = (time.monotonic(), opts)
    return opts


def fetch_chain_stats(symbol: str, expiries: List[str]) -> Dict[tuple, Dict[str, float]]:
    """
    {(expiry, strike, type): {"oi", "day_volume", "iv", "bid", "ask"}}.

    SPX and SPXW list the same (expiry, strike) on monthly dates; both are open
    contracts dealers hedge, so their open interest and volume are summed.
    """
    want = set(expiries or [])
    if not want:
        return {}
    try:
        opts = _fetch_raw(symbol)
    except Exception as e:
        logger.debug("CBOE fetch failed for %s: %s", symbol, type(e).__name__)
        return {}

    out: Dict[tuple, Dict[str, float]] = {}
    for o in opts:
        m = _OCC.match(str(o.get("option", "")))
        if not m:
            continue
        _, ymd, cp, k = m.groups()
        expiry = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}"
        if expiry not in want:
            continue
        key = (expiry, round(int(k) / 1000.0, 4), "call" if cp == "C" else "put")
        rec = out.setdefault(key, {"oi": 0.0, "day_volume": 0.0,
                                   "iv": 0.0, "bid": 0.0, "ask": 0.0})
        rec["oi"] += float(o.get("open_interest") or 0)
        rec["day_volume"] += float(o.get("volume") or 0)
        # Quotes are per-contract, not additive: keep the first real one.
        for f in ("iv", "bid", "ask"):
            if rec[f] <= 0:
                rec[f] = float(o.get(f) or 0)
    return out
