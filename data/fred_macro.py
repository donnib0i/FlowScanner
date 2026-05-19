#!/usr/bin/env python3
"""
fred_macro.py — FRED Macro Regime Context Module

Pulls key macro indicators from the St. Louis Fed FRED API.
Free API key: https://fred.stlouisfed.org/docs/api/api_key.html (instant signup)

Set via env var:  export FRED_API_KEY=your_key_here
Or in a file:     echo '{"api_key":"..."}' > ~/.fred_key.json

Without a key, returns a UNAVAILABLE regime so the UI degrades gracefully.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
REQUEST_TIMEOUT = 10
CACHE_TTL = 3600  # 1 hour — macro data doesn't change by the minute

_cache: Dict = {}
_cache_ts: float = 0.0

# ── Key macro series ───────────────────────────────────────────────────────────

SERIES = {
    "fedfunds":  {"id": "FEDFUNDS",  "label": "Fed Funds Rate",       "unit": "%"},
    "dgs2":      {"id": "DGS2",      "label": "2Y Treasury",           "unit": "%"},
    "dgs10":     {"id": "DGS10",     "label": "10Y Treasury",          "unit": "%"},
    "t10y2y":    {"id": "T10Y2Y",    "label": "Yield Spread (10Y-2Y)", "unit": "%"},
    "vix":       {"id": "VIXCLS",    "label": "VIX",                   "unit": ""},
    "unrate":    {"id": "UNRATE",    "label": "Unemployment Rate",     "unit": "%"},
    "cpiaucsl":  {"id": "CPIAUCSL",  "label": "CPI (YoY proxy)",       "unit": ""},
}


# ── Credentials ───────────────────────────────────────────────────────────────

def load_api_key() -> str:
    key = os.environ.get("FRED_API_KEY", "")
    if key:
        return key
    for path in [os.path.expanduser("~/.fred_key.json"), os.path.join(os.path.dirname(__file__), ".fred_key.json")]:
        if os.path.exists(path):
            try:
                return json.loads(open(path).read()).get("api_key", "")
            except Exception:
                pass
    return ""


# ── Fetcher ───────────────────────────────────────────────────────────────────

def _fetch_series(series_id: str, api_key: str, limit: int = 3) -> Optional[float]:
    """Fetch most recent value for a FRED series. Returns float or None."""
    try:
        r = requests.get(FRED_BASE, params={
            "series_id": series_id,
            "api_key": api_key,
            "sort_order": "desc",
            "limit": limit,
            "file_type": "json",
        }, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.debug("FRED %s → HTTP %s", series_id, r.status_code)
            return None
        obs = r.json().get("observations", [])
        for ob in obs:
            val = ob.get("value", ".")
            if val != ".":
                return float(val)
        return None
    except Exception as exc:
        logger.debug("FRED fetch failed for %s: %s", series_id, exc)
        return None


# ── Regime engine ─────────────────────────────────────────────────────────────

def _compute_regime(data: Dict[str, Optional[float]]) -> Dict:
    """
    Classify macro regime based on key indicators.

    Returns:
        regime:     "RISK-ON" | "RISK-OFF" | "NEUTRAL" | "UNAVAILABLE"
        score:      -100 (full risk-off) to +100 (full risk-on)
        signals:    list of contributing signal strings
    """
    signals = []
    score = 0

    vix = data.get("vix")
    t10y2y = data.get("t10y2y")
    fedfunds = data.get("fedfunds")
    dgs10 = data.get("dgs10")
    unrate = data.get("unrate")

    if all(v is None for v in [vix, t10y2y, fedfunds]):
        return {"regime": "UNAVAILABLE", "score": 0, "signals": ["No FRED data — add API key"]}

    # VIX: risk signal (high VIX = risk-off)
    if vix is not None:
        if vix < 15:
            score += 30
            signals.append(f"VIX {vix:.1f} (complacent — risk-on)")
        elif vix < 20:
            score += 10
            signals.append(f"VIX {vix:.1f} (calm — mild risk-on)")
        elif vix < 25:
            score -= 10
            signals.append(f"VIX {vix:.1f} (elevated — caution)")
        elif vix < 35:
            score -= 30
            signals.append(f"VIX {vix:.1f} (high — risk-off)")
        else:
            score -= 50
            signals.append(f"VIX {vix:.1f} (extreme — full risk-off)")

    # Yield curve: inversion = recession signal (risk-off)
    if t10y2y is not None:
        if t10y2y > 0.5:
            score += 20
            signals.append(f"Yield curve +{t10y2y:.2f}% (normal — risk-on)")
        elif t10y2y > 0:
            score += 5
            signals.append(f"Yield curve +{t10y2y:.2f}% (flat — neutral)")
        elif t10y2y > -0.5:
            score -= 15
            signals.append(f"Yield curve {t10y2y:.2f}% (inverted — caution)")
        else:
            score -= 25
            signals.append(f"Yield curve {t10y2y:.2f}% (deeply inverted — risk-off)")

    # Fed rate: high rates = headwind
    if fedfunds is not None:
        if fedfunds < 3.0:
            score += 15
            signals.append(f"Fed Funds {fedfunds:.2f}% (easy — risk-on)")
        elif fedfunds < 4.5:
            score += 0
            signals.append(f"Fed Funds {fedfunds:.2f}% (neutral)")
        elif fedfunds < 5.5:
            score -= 10
            signals.append(f"Fed Funds {fedfunds:.2f}% (restrictive — mild risk-off)")
        else:
            score -= 20
            signals.append(f"Fed Funds {fedfunds:.2f}% (very restrictive — risk-off)")

    # Unemployment: low = risk-on (economy strong)
    if unrate is not None:
        if unrate < 4.5:
            score += 10
            signals.append(f"Unemployment {unrate:.1f}% (low — risk-on)")
        elif unrate < 6.0:
            score -= 5
            signals.append(f"Unemployment {unrate:.1f}% (rising — caution)")
        else:
            score -= 15
            signals.append(f"Unemployment {unrate:.1f}% (high — risk-off)")

    # Regime classification
    if score >= 30:
        regime = "RISK-ON"
    elif score <= -20:
        regime = "RISK-OFF"
    else:
        regime = "NEUTRAL"

    return {"regime": regime, "score": score, "signals": signals}


# ── Public API ────────────────────────────────────────────────────────────────

def get_macro_context() -> Dict:
    """
    Returns macro regime context dict:
    {
        "regime":      str,     # RISK-ON | RISK-OFF | NEUTRAL | UNAVAILABLE
        "score":       int,     # -100 to +100
        "signals":     list,    # human-readable signal strings
        "data":        dict,    # raw values {fedfunds, dgs2, dgs10, t10y2y, vix, ...}
        "source":      str,
        "last_updated": str,
    }
    """
    global _cache, _cache_ts

    now = time.time()
    if _cache and (now - _cache_ts) < CACHE_TTL:
        return _cache

    api_key = load_api_key()
    if not api_key:
        result = {
            "regime": "UNAVAILABLE",
            "score": 0,
            "signals": ["FRED API key not set — get a free key at fred.stlouisfed.org"],
            "data": {},
            "source": "FRED (no key)",
            "last_updated": datetime.now().strftime("%H:%M:%S"),
        }
        return result

    raw: Dict[str, Optional[float]] = {}
    for key, info in SERIES.items():
        raw[key] = _fetch_series(info["id"], api_key)

    regime_info = _compute_regime(raw)

    result = {
        "regime": regime_info["regime"],
        "score": regime_info["score"],
        "signals": regime_info["signals"],
        "data": {
            key: {
                "label": SERIES[key]["label"],
                "value": raw[key],
                "unit": SERIES[key]["unit"],
            }
            for key in SERIES
            if raw.get(key) is not None
        },
        "source": "FRED (St. Louis Fed)",
        "last_updated": datetime.now().strftime("%H:%M:%S"),
    }

    _cache = result
    _cache_ts = now
    return result


def get_macro_context_cached() -> Dict:
    """Alias for get_macro_context (caching is built-in)."""
    return get_macro_context()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    ctx = get_macro_context()
    print(f"\n  MACRO REGIME: {ctx['regime']}  (score: {ctx['score']:+d})")
    print()
    for sig in ctx["signals"]:
        print(f"  • {sig}")
    print()
    if ctx["data"]:
        print("  Raw data:")
        for k, v in ctx["data"].items():
            print(f"    {v['label']:30s}  {v['value']}{v['unit']}")
    if ctx["regime"] == "UNAVAILABLE":
        print("\n  To enable: export FRED_API_KEY=your_key")
        print("  Free signup: https://fred.stlouisfed.org/docs/api/api_key.html")
