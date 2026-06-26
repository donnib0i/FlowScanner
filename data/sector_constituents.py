"""
sector_constituents.py — map individual stocks to their sector.

The sector cards show full, human-readable sector names (no ETF tickers). This
module turns the live S&P 500 / GICS sector map (data.unusual_flow) into
`sector -> [individual tickers]`, with a static fallback for when the live map is
unavailable (market closed / fetch failed).

ETFs never appear here — the source is index constituents, and `filter_etfs` is
applied as a backstop.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from data.unusual_flow import get_ticker_sector_map
from data.etf_filter import filter_etfs

# Canonical, human-readable sector names — the single source of truth for what the
# UI shows. Order is the display order on the SECTORS grid.
SECTORS: List[str] = [
    "Technology",
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Financials",
    "Health Care",
    "Energy",
    "Industrials",
    "Materials",
    "Utilities",
    "Real Estate",
]

# Maps both GICS sector strings (Wikipedia S&P 500 table) and yfinance sector
# strings (screener / Nasdaq-100 extras) onto the canonical names above. Keys are
# lowercased for case-insensitive matching.
GICS_TO_SECTOR: Dict[str, str] = {
    # Technology
    "information technology": "Technology",
    "technology": "Technology",
    # Communication Services
    "communication services": "Communication Services",
    # Consumer Discretionary
    "consumer discretionary": "Consumer Discretionary",
    "consumer cyclical": "Consumer Discretionary",
    # Consumer Staples
    "consumer staples": "Consumer Staples",
    "consumer defensive": "Consumer Staples",
    # Financials
    "financials": "Financials",
    "financial services": "Financials",
    "financial": "Financials",
    # Health Care
    "health care": "Health Care",
    "healthcare": "Health Care",
    # Energy
    "energy": "Energy",
    # Industrials
    "industrials": "Industrials",
    # Materials
    "materials": "Materials",
    "basic materials": "Materials",
    # Utilities
    "utilities": "Utilities",
    # Real Estate
    "real estate": "Real Estate",
}


def normalize_sector(raw: str) -> Optional[str]:
    """Map a raw GICS/yfinance sector string to a canonical name, or None."""
    return GICS_TO_SECTOR.get((raw or "").strip().lower())


def constituents_for(sector: str, fallback_map: Optional[Dict[str, str]] = None) -> List[str]:
    """
    Individual stock tickers belonging to `sector`.

    Primary source: the live, cached S&P 500 / GICS map. If that is empty (market
    closed, fetch failed) and `fallback_map` (ticker -> canonical sector) is given,
    fall back to it. ETFs are filtered out either way.
    """
    try:
        live = get_ticker_sector_map() or {}
    except Exception:
        live = {}

    out: List[str] = []
    for ticker, raw_sector in live.items():
        if normalize_sector(raw_sector) == sector:
            out.append(str(ticker).upper())

    if not out and fallback_map:
        out = [t.upper() for t, s in fallback_map.items() if s == sector]

    return filter_etfs(out)
