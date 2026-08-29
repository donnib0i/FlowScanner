"""
core/constants.py -- Static tables and tunables: the universe, sector map, and every
threshold the scan reads. Change a number here, not in the logic.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from colorama import Fore, Style
from typing import Dict


# ─── Sector ETFs & Ticker→Sector Map ─────────────────────────────────────────
# Full, human-readable sector names — never show the ETF ticker to the user. The
# ETF is only an internal proxy for the sector's headline % move.
SECTOR_ETFS: Dict[str, str] = {
    "Technology":             "XLK",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples":       "XLP",
    "Financials":             "XLF",
    "Health Care":            "XLV",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Materials":              "XLB",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
}


# Sector relative-strength breakout tunables
RS_THRESH = 0.5   # sector must out/under-perform SPY by >= this many points


RS_VOL    = 1.1   # elevated relative-volume gate


TICKER_SECTOR: Dict[str, str] = {
    # Tech / semis / software
    "AAPL":"Technology","MSFT":"Technology","NVDA":"Technology","AMD":"Technology","AVGO":"Technology",
    "INTC":"Technology","QCOM":"Technology","TXN":"Technology","AMAT":"Technology","LRCX":"Technology",
    "MU":"Technology","ARM":"Technology","SMCI":"Technology","PLTR":"Technology","SNOW":"Technology",
    "CRWD":"Technology","PANW":"Technology","DDOG":"Technology","NET":"Technology","ZS":"Technology",
    "TWLO":"Technology","OKTA":"Technology","NFLX":"Technology","AMZN":"Technology","TSLA":"Technology",
    "SHOP":"Technology","RIVN":"Technology","LCID":"Technology","NIO":"Technology","LI":"Technology","XPEV":"Technology",
    "MSTR":"Technology","MARA":"Technology","RIOT":"Technology","CLSK":"Technology","BITF":"Technology","HUT":"Technology",
    "TQQQ":"Technology","SOXL":"Technology","SQQQ":"Technology","SOXS":"Technology","TECL":"Technology","TECS":"Technology",
    "FNGU":"Technology","FNGD":"Technology","QQQ":"Technology",
    # Financials
    "GS":"Financials","MS":"Financials","JPM":"Financials","BAC":"Financials",
    "C":"Financials","WFC":"Financials","V":"Financials","MA":"Financials",
    "SCHW":"Financials","IBKR":"Financials","SQ":"Financials","PYPL":"Financials",
    "AFRM":"Financials","UPST":"Financials","HOOD":"Financials","COIN":"Financials",
    "SOFI":"Financials",
    # Energy
    "XOM":"Energy","CVX":"Energy","USO":"Energy","CPER":"Materials",
    # Health / Biotech
    "HIMS":"Health Care","MRNA":"Health Care","PFE":"Health Care","BNTX":"Health Care",
    "LABU":"Health Care","LABD":"Health Care",
    # Drones / Defense Tech
    "ONDS":"Technology",
    # CommSvcs / consumer
    "META":"Communication Services","GOOGL":"Communication Services","GOOG":"Communication Services","SNAP":"Communication Services",
    "PINS":"Communication Services","RBLX":"Communication Services","ABNB":"Communication Services","BKNG":"Communication Services",
    "EBAY":"Communication Services","ETSY":"Communication Services","UBER":"Communication Services","LYFT":"Communication Services",
    "DASH":"Communication Services","GME":"Communication Services","AMC":"Communication Services",
    "BABA":"Communication Services","JD":"Communication Services","PDD":"Communication Services","KWEB":"Communication Services","FXI":"Communication Services",
    # Industrials
    "F":"Industrials","GM":"Industrials",
    # Materials / metals
    "GLD":"Materials","SLV":"Materials","CPER":"Materials",
    # Broad index (no sector)
    "SPX":"Index","SPY":"Index","IWM":"Index","DIA":"Index","MDY":"Index",
    "UPRO":"Index","SPXL":"Index","SPXS":"Index","SDOW":"Index",
    "TNA":"Index","TZA":"Index","VXX":"Vol","UVXY":"Vol","SVXY":"Vol",
}


# ─── Universe ─────────────────────────────────────────────────────────────────
UNIVERSE = [
    # Index ETFs
    "SPY","QQQ","IWM","DIA","MDY",
    # Volatility
    "VXX","UVXY","SVXY",
    # Leveraged bull
    "TQQQ","SOXL","UPRO","SPXL","TNA","LABU","TECL","FNGU",
    # Leveraged bear
    "SQQQ","SOXS","SPXS","TZA","LABD","TECS","FNGD","SDOW",
    # Mega caps
    "AAPL","MSFT","NVDA","META","AMZN","GOOGL","TSLA",
    # Semis
    "AMD","AVGO","MU","ARM","SMCI","INTC","QCOM","TXN","AMAT","LRCX",
    "KLAC","MRVL","ON","SWKS","MPWR","WOLF","NXPI","ADI",
    # High retail options vol
    "COIN","PLTR","MSTR","HOOD","SOFI","MARA","RIOT","CLSK","BITF","HUT",
    "RBLX","SNAP","UBER","LYFT","DASH","AFRM","UPST","GME","AMC",
    "BULL","CORZ","IREN","CIFR",
    # Growth tech / software
    "NFLX","CRWD","PANW","DDOG","NET","ZS","SNOW","TWLO",
    "SHOP","PYPL","ABNB","BKNG","EBAY","ETSY",
    "APP","RDDT","GTLB","MDB","TTD","HUBS","BILL","DOCN","U",
    "BOX","ESTC","CFLT","IOT","GTLB","PATH","DT","AI","BBAI",
    # AI / cloud
    "ORCL","CRM","NOW","WDAY","INTU","ADBE","IBM",
    # EV / transport
    "RIVN","LCID","NIO","LI","XPEV","F","GM","BLNK","CHPT",
    # Financials / fintech
    "GS","MS","JPM","BAC","C","WFC","V","MA","SCHW","IBKR",
    "NU","AFRM","OPEN","LMND","ROOT",
    # Energy / commodities
    "XOM","CVX","GLD","SLV","CPER","USO","OXY","SLB","HAL","DVN",
    "FCX","CLF","MP","VALE","AA",
    # Healthcare / biotech / GLP-1
    "HIMS","MRNA","PFE","BNTX","ONDS","NVO","LLY","RXRX","APLS",
    "NVAX","ACMR","RARE","FOLD","TGTX","KROS","RCUS",
    # Defense / aerospace
    "RTX","LMT","NOC","BA","GD","HII","LDOS","CACI","KTOS","AVAV",
    # Consumer / retail
    "AMZN","WMT","TGT","COST","HD","LOW","NKE","LULU","PTON","BYND",
    # Media / streaming
    "DIS","WBD","SPOT","TTWO","EA",
    # China
    "BABA","JD","PDD","KWEB","FXI","BIDU","TME",
    # Small/mid cap sleepers (high vol, unusual flow candidates)
    "PINS","OKTA","GOOG","IONQ","QUBT","RGTI","QBTS","ARQQ",
    "SOUN","BBAI","GFAI","AITX","AGEN","IOVA","FATE","EDIT","BEAM",
    "ACHR","JOBY","WKHS",
    "ASTS","LUNR","RDW","RKLB","MNTS",
]


# ─── Ladder (calls vs puts) row selection ─────────────────────────────────────
# Floors that keep untradeable strikes out of the ladder. A strike with OI=1 or
# 3 contracts of volume is noise, not flow.
LADDER_MIN_OI    = 50


LADDER_MIN_VOL   = 25


LADDER_DELTA_MIN = 0.10   # below: lottery tickets


LADDER_DELTA_MAX = 0.90   # above: deep ITM, priced like stock


_GRADE_COLORS = {
    "A": Fore.GREEN  + Style.BRIGHT,
    "B": Fore.YELLOW,
    "C": Fore.WHITE,
    "D": Fore.RED,
}


DEEP_ITM_PCT   = 15.0    # beyond this far in the money...


DEEP_ITM_VOL   = 1_000   # ...volume must justify the depth


MIN_OI         = 50


MIN_VOL        = 250


MIN_MID        = 0.05


WIDE_SPREAD_PCT = 12.0   # flagged, never rejected


# ─── Filter / Sort ────────────────────────────────────────────────────────────
FILTER_LABELS = {
    "all":      "All",
    "gap":      "Gap Fills",
    "inside":   "Inside Day",
    "highvol":  "High Vol",
    "breakout": "Breakouts",
    "options":  "Options >=60",
    "any":      "Any Setup",
    "a_grade":  "Grade A",
    "laggard":  "Sector Laggards",
}


SORT_LABELS = {
    "setup":   "Setup Quality",
    "options": "Options Score",
    "relvol":  "Rel Vol",
    "gap":     "Gap %",
    "change":  "Change %",
    "lag":     "Lag Score",
    "ev":      "Expected Value",
}


FILTER_MAP = {"1": "all", "2": "gap", "3": "inside", "4": "highvol",
              "5": "breakout", "6": "options", "7": "any", "8": "a_grade", "9": "laggard"}


SORT_MAP   = {"s1": "setup", "s2": "options", "s3": "relvol",
              "s4": "gap",   "s5": "change",  "s6": "lag", "s7": "ev"}


# ─── CSV Export ───────────────────────────────────────────────────────────────
_BASE_FIELDS = [
    "ticker", "price", "change_pct", "gap_pct", "gap_flag", "inside_day",
    "rel_vol", "high_vol", "today_vol", "avg_vol", "ivr_proxy",
    "spread_label", "opt_score", "setup_q", "direction",
    "is_laggard", "lag_pct", "lag_score", "lag_direction",
]


_TIER_COLORS = {
    "whale":         Fore.RED   + Style.BRIGHT,
    "block":         Fore.YELLOW + Style.BRIGHT,
    "institutional": Fore.CYAN,
    "retail":        Fore.WHITE,
}
