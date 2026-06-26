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


# Built-in, liquid optionable constituents per sector. This is the reliable base:
# the live Wikipedia/yfinance sector map is frequently IP-blocked on cloud hosts
# (Railway/Render), so we always union this in to guarantee real coverage. Names
# are classified by GICS; approximate is fine for a heatmap.
STATIC_CONSTITUENTS: Dict[str, List[str]] = {
    "Technology": [
        "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "MU", "ARM", "SMCI", "INTC", "QCOM",
        "TXN", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "SWKS", "MPWR", "WOLF", "NXPI",
        "ADI", "ASML", "TSM", "MCHP", "GFS", "ANET", "DELL", "HPQ", "HPE", "CSCO",
        "ORCL", "CRM", "NOW", "WDAY", "INTU", "ADBE", "IBM", "PLTR", "SNOW", "CRWD",
        "PANW", "DDOG", "NET", "ZS", "TWLO", "OKTA", "MDB", "TTD", "HUBS", "BILL",
        "ESTC", "CFLT", "IOT", "PATH", "DT", "BBAI", "APP", "U", "GTLB", "DOCN",
        "MSTR", "SOUN", "IONQ", "QUBT", "RGTI", "QBTS", "CIEN", "COHR", "STX", "WDC",
        "NTAP", "ZM", "DOCU", "TEAM", "ENPH", "FSLR", "SEDG", "ONDS", "AI",
    ],
    "Communication Services": [
        "META", "GOOGL", "GOOG", "NFLX", "DIS", "WBD", "SPOT", "T", "VZ", "TMUS",
        "CMCSA", "CHTR", "SNAP", "PINS", "RBLX", "TTWO", "EA", "MTCH", "BIDU", "TME",
        "ROKU", "LYV", "WMG", "PARA", "FOXA", "OMC", "IPG", "NYT",
    ],
    "Consumer Discretionary": [
        "AMZN", "TSLA", "HD", "LOW", "NKE", "LULU", "MCD", "SBUX", "BKNG", "ABNB",
        "CMG", "ORLY", "AZO", "ROST", "TJX", "MAR", "HLT", "GM", "F", "RIVN",
        "LCID", "NIO", "LI", "XPEV", "DKNG", "RCL", "CCL", "NCLH", "EXPE", "ETSY",
        "EBAY", "W", "CHWY", "DASH", "ULTA", "YUM", "DHI", "LEN", "GRMN", "BBY",
        "GME", "AMC", "BYND", "PTON", "CVNA", "CROX", "DECK", "RL", "TPR", "WYNN",
        "LVS", "MGM", "APTV", "TGT", "SHOP",
    ],
    "Consumer Staples": [
        "WMT", "COST", "PG", "KO", "PEP", "MDLZ", "MO", "PM", "CL", "KMB",
        "GIS", "KHC", "STZ", "MNST", "KDP", "HSY", "K", "SYY", "ADM", "KR",
        "DG", "DLTR", "EL", "CLX", "CHD", "TSN", "HRL", "CAG", "SJM",
    ],
    "Financials": [
        "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "BX", "KKR",
        "AXP", "V", "MA", "PYPL", "SQ", "COF", "USB", "PNC", "TFC", "BK",
        "STT", "CB", "PGR", "TRV", "AIG", "MET", "PRU", "AFL", "ALL", "MMC",
        "AON", "ICE", "CME", "SPGI", "MCO", "COIN", "HOOD", "SOFI", "AFRM", "UPST",
        "NU", "IBKR", "ALLY", "SYF", "DFS", "KEY", "RF", "CFG", "FITB", "HBAN",
        "MTB", "NDAQ", "BULL",
    ],
    "Health Care": [
        "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "DHR", "PFE", "AMGN",
        "ISRG", "MDT", "BMY", "GILD", "VRTX", "CVS", "CI", "ELV", "HUM", "REGN",
        "ZTS", "BSX", "SYK", "BDX", "HCA", "MCK", "MRNA", "BNTX", "NVO", "HIMS",
        "RXRX", "NVAX", "BIIB", "IDXX", "DXCM", "ALNY", "SRPT", "EW", "GEHC", "IQV",
        "CNC", "MOH", "NBIX", "HALO", "TGTX", "APLS", "FOLD", "RARE", "IOVA", "BEAM",
        "EDIT", "NTLA", "CRSP", "VKTX", "CYTK",
    ],
    "Energy": [
        "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "OXY", "WMB",
        "KMI", "OKE", "HAL", "DVN", "FANG", "HES", "BKR", "TRGP", "CTRA", "MRO",
        "APA", "EQT", "LNG", "SU", "CNQ", "AR", "RRC", "SM", "MTDR", "CHRD",
        "PR", "OVV", "NOV", "FTI",
    ],
    "Industrials": [
        "CAT", "DE", "BA", "HON", "GE", "UNP", "UPS", "RTX", "LMT", "NOC",
        "GD", "ETN", "EMR", "ITW", "CSX", "NSC", "MMM", "FDX", "WM", "PH",
        "TDG", "GEV", "CARR", "OTIS", "PCAR", "CMI", "JCI", "ROK", "AME", "FAST",
        "PWR", "URI", "LHX", "TXT", "HWM", "AXON", "VRT", "IR", "DOV", "XYL",
        "ODFL", "WAB", "BLDR", "HII", "LDOS", "CACI", "KTOS", "AVAV", "JOBY", "ACHR",
        "RKLB", "ASTS", "LUNR", "PLUG", "BLNK", "CHPT",
    ],
    "Materials": [
        "LIN", "SHW", "APD", "FCX", "NEM", "ECL", "DOW", "DD", "NUE", "CTVA",
        "VMC", "MLM", "PPG", "ALB", "CF", "MOS", "STLD", "CLF", "X", "AA",
        "MP", "VALE", "SCCO", "GOLD", "FMC", "IFF", "CE", "EMN", "RPM", "AMCR",
    ],
    "Utilities": [
        "NEE", "DUK", "SO", "D", "AEP", "SRE", "EXC", "XEL", "ED", "PEG",
        "WEC", "ES", "AEE", "DTE", "PPL", "FE", "ETR", "CNP", "CMS", "NI",
        "LNT", "EVRG", "AES", "ATO", "PCG", "EIX", "NRG", "VST", "CEG", "PNW",
    ],
    "Real Estate": [
        "PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "CCI", "DLR", "CBRE",
        "VICI", "EXR", "AVB", "EQR", "SBAC", "INVH", "IRM", "WY", "ARE", "MAA",
        "ESS", "KIM", "UDR", "REG", "HST", "BXP", "DOC", "CPT", "FRT",
    ],
}


def constituents_for(sector: str, fallback_map: Optional[Dict[str, str]] = None) -> List[str]:
    """
    Individual stock tickers belonging to `sector`.

    Always unions the built-in STATIC_CONSTITUENTS (reliable on cloud hosts where
    the live fetch is IP-blocked) with the live S&P 500 / GICS map when available,
    so coverage is as broad as possible. `fallback_map` is a last resort if both
    are empty. ETFs are filtered out.
    """
    try:
        live = get_ticker_sector_map() or {}
    except Exception:
        live = {}

    out: set = set(STATIC_CONSTITUENTS.get(sector, []))
    for ticker, raw_sector in live.items():
        if normalize_sector(raw_sector) == sector:
            out.add(str(ticker).upper())

    if not out and fallback_map:
        out = {t.upper() for t, s in fallback_map.items() if s == sector}

    return filter_etfs(sorted(out))
