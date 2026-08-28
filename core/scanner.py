#!/usr/bin/env python3
# scanner.py — Elite Market Scanner v2

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.universe import get_universe, ANCHOR, universe_summary
from core.tv_universe import (
    DEFAULT_CAP as TV_DEFAULT_CAP,
    drop_report as tv_drop_report,
    load_tradingview_csv,
)
from core.market_calendar import is_market_open, minutes_to_close
from data.sector_constituents import constituents_for, SECTORS

import yfinance as yf
import colorama
from colorama import Fore, Style
from tabulate import tabulate
import argparse, time, sys, csv, os, math, json, warnings
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Any
import pandas as pd

warnings.filterwarnings("ignore")
colorama.init(autoreset=True)

# ─── TastyTrade real flow (drop-in replacement for yfinance scan) ─────────────
try:
    from data.tt_flow import (
        scan_options_flow_tt,
        load_credentials as _tt_load_creds,
        last_error as _tt_last_error,
    )
    _TT_USER, _TT_PASS = _tt_load_creds()
    # Credentials being *present* says nothing about the session authenticating.
    _TT_AVAILABLE = bool(_TT_USER and _TT_PASS)
except Exception:
    _TT_AVAILABLE = False
    scan_options_flow_tt = None
    def _tt_last_error() -> str:
        return "tt_flow module unavailable"

# ─── Flow provenance ─────────────────────────────────────────────────────────
# Which feed actually produced the flow the user is looking at. Recorded by the
# scan that ran, never inferred from configuration: TastyTrade can be fully
# configured and still fail every login, in which case the served flow is the
# 15-minute-delayed yfinance fallback and must not be labelled live.
_FLOW_SOURCE: Dict[str, Any] = {}


def reset_flow_source() -> None:
    _FLOW_SOURCE.clear()
    _FLOW_SOURCE.update(source=None, reason="no flow scan has run yet", ts=None)


reset_flow_source()


def _set_flow_source(source: str, reason: str) -> None:
    _FLOW_SOURCE.update(source=source, reason=reason, ts=time.time())


def get_flow_source() -> Dict[str, Any]:
    """Provenance of the most recent flow scan. `live` describes the data."""
    src = dict(_FLOW_SOURCE)
    src["live"] = src.get("source") == "tastytrade-live"
    ts = src.get("ts")
    src["age_s"] = round(time.time() - ts, 1) if ts else None
    return src

# ─── yfinance session (curl_cffi if available, else None) ─────────────────────
try:
    from curl_cffi import requests as _cffi_requests
    _YF_SESSION: Optional[object] = _cffi_requests.Session(impersonate="chrome")
except Exception:
    _YF_SESSION = None

# ─── yfinance ticker normalization ───────────────────────────────────────────
_YF_TICKER_MAP: Dict[str, str] = {
    # ^SPX, not ^GSPC: both quote the index, but ^GSPC exposes no option chain.
    "SPX": "^SPX", "VIX": "^VIX", "RUT": "^RUT", "NDX": "^NDX",
}

def _yf_ticker(sym: str) -> str:
    return _YF_TICKER_MAP.get(sym.upper(), sym)

def _yf(sym: str) -> yf.Ticker:
    """Let yfinance 1.2+ manage its own curl_cffi session internally."""
    return yf.Ticker(_yf_ticker(sym))


# ─── Option chain cache ──────────────────────────────────────────────────────
# One scan pass fetches the same chain more than once: scan_options_flow walks
# the near expiries for its ticker list, then enrich_contracts fetches the same
# expiries again for the overlapping top-N, and the IV-vs-HV adjustment right
# after it fetches the front expiry a third time. Same URL, seconds apart.
#
# TTL is deliberately far below the 45s live refresh default, so a refresh
# always re-hits the network and nothing on screen can go stale between passes.
# This collapses duplicates *within* a pass only — it is not a data cache.
_CHAIN_TTL_S = 20.0
_CHAIN_CACHE_MAX = 512
_chain_cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}


def _option_chain(t: yf.Ticker, symbol: str, exp: str,
                  ttl: float = _CHAIN_TTL_S) -> Any:
    """Fetch one expiry's chain, reusing a fetch made moments ago in this pass."""
    key = (_yf_ticker(symbol), exp)
    now = time.monotonic()
    hit = _chain_cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    chain = t.option_chain(exp)
    if len(_chain_cache) >= _CHAIN_CACHE_MAX:
        # Cheap bound: drop everything already past its TTL, and if that frees
        # nothing, drop the oldest. An unbounded dict in the live loop is a leak.
        stale = [k for k, (ts, _) in _chain_cache.items() if now - ts >= ttl]
        for k in stale:
            del _chain_cache[k]
        if not stale:
            del _chain_cache[min(_chain_cache, key=lambda k: _chain_cache[k][0])]
    _chain_cache[key] = (now, chain)
    return chain


def clear_chain_cache() -> None:
    _chain_cache.clear()

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

def classify_breakout(change_pct: float, spy_chg: float, rel_vol: float) -> tuple:
    """RS vs SPY + breakout label. When spy_chg == 0.0, rs falls back to change_pct
    (same convention as the per-ticker RS calc). Returns (rs_vs_spy, breakout)."""
    rs = round(change_pct - spy_chg, 2) if spy_chg != 0.0 else round(change_pct, 2)
    if rs >= RS_THRESH and change_pct > 0 and rel_vol >= RS_VOL:
        return rs, "up"
    if rs <= -RS_THRESH and change_pct < 0 and rel_vol >= RS_VOL:
        return rs, "down"
    return rs, "none"

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
UNIVERSE = list(dict.fromkeys(UNIVERSE))  # dedupe


def fetch_dynamic_universe(top_n: int = 50) -> List[str]:
    """
    Pull today's top movers from a broad watchlist to surface sleepers.
    Returns tickers with high relative volume or big % moves not in core UNIVERSE.
    Combines with UNIVERSE for full coverage.
    """
    # Extended watchlist beyond core — scanned for movers only
    extended = [
        "CELH","MNST","KO","PEP","MCD","SBUX","CMG","YUM",
        "TSCO","DKS","FIVE","OLLI","BOOT","CAVA","BROS",
        "AXON","TDY","PODD","ISRG","SYK","BSX","EW",
        "CLX","PG","JNJ","ABT","MDT","TMO","DHR","A",
        "ALLY","SFM","CTAS","PAYX","ADP","VRSK","MSCI",
        "ENPH","FSLR","RUN","SPWR","ARRY","NEE","CEG",
        "DUOL","COUR","UDMY","CHGG","LRN",
        "CELH","SFIX","W","ETSY","WISH","OZON",
        "SMTC","ACLS","ONTO","MKSI","UCTT","FORM","ICHR",
        "GENI","DKNG","PENN","MGM","CZR","LVS","WYNN",
        "CCL","RCL","NCLH","DAL","UAL","AAL","LUV","JBLU",
    ]
    try:
        all_tickers = list(dict.fromkeys(extended))
        batch = yf.download(
            all_tickers, period="2d", interval="1d",
            group_by="ticker", progress=False, threads=True, timeout=15,
        )
        movers = []
        for t in all_tickers:
            try:
                if isinstance(batch.columns, pd.MultiIndex):
                    hist = batch[t].dropna(how="all") if t in batch.columns.get_level_values(0) else pd.DataFrame()
                else:
                    hist = batch.dropna(how="all")
                if len(hist) < 2:
                    continue
                prev_close = float(hist["Close"].iloc[-2])
                today_close = float(hist["Close"].iloc[-1])
                prev_vol = float(hist["Volume"].iloc[-2])
                today_vol = float(hist["Volume"].iloc[-1])
                if prev_close <= 0:
                    continue
                chg = abs((today_close - prev_close) / prev_close)
                relvol = today_vol / max(prev_vol, 1)
                # Surface if >3% move OR >2x relative volume
                if chg > 0.03 or relvol > 2.0:
                    movers.append((t, chg + relvol * 0.1))
            except Exception:
                continue
        movers.sort(key=lambda x: x[1], reverse=True)

        # FIX 10: filter out penny stocks and invalid tickers before returning
        def _is_valid_ticker(sym: str, min_price: float = 2.0) -> bool:
            try:
                _ti = _yf(sym)
                _info = _ti.fast_info
                _price = getattr(_info, 'last_price', 0) or 0
                return float(_price) >= min_price
            except Exception:
                return False

        top_movers = [t for t, _ in movers[:top_n * 2]]  # oversample to account for filtered-out tickers
        filtered = [s for s in top_movers if len(s) <= 5 and s.isalpha() and _is_valid_ticker(s)]
        return filtered[:top_n]
    except Exception:
        return []

# _YF_TICKER_MAP and _yf_ticker defined above near _yf()

# ─── Math helpers ─────────────────────────────────────────────────────────────
def fetch_vix() -> float:
    """Return current VIX level, or -1.0 on failure. Retries 3× with backoff."""
    for attempt in range(3):
        try:
            v = _yf("VIX")
            h = v.history(period="5d")
            if not h.empty:
                return round(float(h["Close"].dropna().iloc[-1]), 2)
        except Exception:
            pass
        if attempt < 2:
            time.sleep(1.5)
    return -1.0


def vix_delta_target(vix: float) -> float:
    """
    Adjust the ideal contract delta based on VIX regime.
    Target range: 0.25–0.40. High VIX → further OTM, Low VIX → closer ATM.
    """
    if vix <= 0:   return 0.35          # unknown, use default
    if vix < 13:   return 0.40          # low/complacent → near ATM but not ITM
    if vix < 16:   return 0.37          # calm
    if vix < 20:   return 0.33          # normal
    if vix < 24:   return 0.30          # cautious
    if vix < 30:   return 0.27          # elevated
    if vix < 40:   return 0.25          # fear
    return 0.22                         # extreme fear → further OTM


def norm_cdf(x: float) -> float:
    """Abramowitz & Stegun approximation (max error 7.5e-8)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    p = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    c = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * p
    return c if x >= 0 else 1.0 - c


def bs_delta(S: float, K: float, T: float, sigma: float, opt_type: str = "call") -> float:
    """Black-Scholes delta without scipy."""
    T     = max(T, 1.0 / (365 * 1440))   # floor: 1 minute
    sigma = max(sigma, 0.05)
    if S <= 0 or K <= 0:
        return (1.0 if opt_type == "call" else -1.0) if S > K else 0.0
    # Near expiry: d1 → ±∞ and delta collapses to 0/1. Use limit directly.
    if T < 0.0001:   # < ~52 minutes — digital payoff regime
        if opt_type == "call":
            return 1.0 if S >= K else 0.0
        else:
            return -1.0 if S <= K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    d  = norm_cdf(d1)
    return d if opt_type == "call" else d - 1.0

# ─── Ladder (calls vs puts) row selection ─────────────────────────────────────
# Floors that keep untradeable strikes out of the ladder. A strike with OI=1 or
# 3 contracts of volume is noise, not flow.
LADDER_MIN_OI    = 50
LADDER_MIN_VOL   = 25
LADDER_DELTA_MIN = 0.10   # below: lottery tickets
LADDER_DELTA_MAX = 0.90   # above: deep ITM, priced like stock


def _num(v, default=0.0) -> float:
    """float() that survives None/NaN/strings."""
    try:
        f = float(v)
        return default if f != f else f   # NaN != NaN
    except (TypeError, ValueError):
        return default


def ladder_rows(raw, opt_type: str, price: float, dte: int,
                top_n: int = 8) -> List[Dict]:
    """
    Clean, score and rank one side of an option chain for the calls-vs-puts ladder.

    `raw` is an iterable of chain rows (dicts or a DataFrame's records) carrying
    strike / volume / openInterest / bid / ask / lastPrice / impliedVolatility.

    Ranking is by dollar flow, but only across strikes that are actually
    tradeable. Ranking the whole chain by dollar flow alone surfaces deep-ITM
    contracts — they cost 10x more, so vol x mid puts them on top even when the
    real positioning is at the money. Delta comes from Black-Scholes on the
    chain's own IV, not a moneyness approximation.
    """
    rows: List[Dict] = []
    T = max(dte, 0) / 365.0

    for r in raw:
        strike = _num(r.get("strike"))
        if strike <= 0:
            continue
        vol = int(_num(r.get("volume")))
        oi  = int(_num(r.get("openInterest")))
        if vol < LADDER_MIN_VOL or oi < LADDER_MIN_OI:
            continue

        bid, ask = _num(r.get("bid")), _num(r.get("ask"))
        # Outside RTH yfinance zeroes bid/ask — fall back to the last print.
        mid = (bid + ask) / 2 if bid + ask > 0 else _num(r.get("lastPrice"))
        if mid <= 0:
            continue

        iv = _num(r.get("impliedVolatility"))
        delta = abs(bs_delta(price, strike, T, iv, opt_type))
        if not (LADDER_DELTA_MIN <= delta <= LADDER_DELTA_MAX):
            continue

        rows.append({
            "strike": strike,
            "type": opt_type,
            "vol": vol,
            "oi": oi,
            "mid": round(mid, 2),
            "iv": round(iv * 100, 1),
            "dollar_flow": round(vol * mid * 100, 0),
            "ddoi": round(delta * oi, 0),
            "delta": round(delta, 3),
        })

    rows.sort(key=lambda x: x["dollar_flow"], reverse=True)
    return rows[:top_n]


# ─── Display helpers ──────────────────────────────────────────────────────────
def fmt_vol(v: int) -> str:
    if v >= 1_000_000_000: return f"{v/1e9:.1f}B"
    if v >= 1_000_000:     return f"{v/1e6:.1f}M"
    if v >= 1_000:         return f"{v/1e3:.0f}K"
    return str(v)


def fmt_num(v: int) -> str:
    if v >= 1_000_000: return f"{v/1e6:.1f}M"
    if v >= 1_000:     return f"{v/1e3:.0f}k"
    return str(v)


def score_bar(score: int, width: int = 10) -> str:
    filled = int(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def score_colored(score: int) -> str:
    c = Fore.GREEN if score >= 75 else (Fore.YELLOW if score >= 50 else Fore.RED)
    return f"{c}{score_bar(score)}{Style.RESET_ALL} {c}{score:3d}{Style.RESET_ALL}"


def color_change(val: float) -> str:
    if val > 0: return Fore.GREEN + f"+{val:.2f}%" + Style.RESET_ALL
    if val < 0: return Fore.RED   + f"{val:.2f}%"  + Style.RESET_ALL
    return f"{val:.2f}%"


def color_gap(gap_pct: float, flag: Optional[str]) -> str:
    if flag == "gap_up":    return Fore.CYAN   + f"+{gap_pct:.2f}%^" + Style.RESET_ALL
    if flag == "gap_down":  return Fore.YELLOW + f"{gap_pct:.2f}%v"  + Style.RESET_ALL
    if abs(gap_pct) > 0.5:  return Fore.WHITE  + f"{gap_pct:+.2f}%✓" + Style.RESET_ALL
    return f"{gap_pct:+.2f}%"


# ─── Trade Side / Whale Analytics ────────────────────────────────────────────
def classify_trade_side(bid: float, ask: float, last: float) -> str:
    """
    Lee-Ready heuristic: classify whether a trade hit the ask (buyer aggression)
    or the bid (seller aggression).
    Top 25% of spread → 'ask', bottom 25% → 'bid', else → 'mid'.
    """
    if bid <= 0 or ask <= 0 or last <= 0:
        return "mid"
    spread = ask - bid
    if spread <= 0:
        return "mid"
    if last >= ask - spread * 0.25:
        return "ask"   # buyer hitting ask = bullish
    if last <= bid + spread * 0.25:
        return "bid"   # seller hitting bid = bearish
    return "mid"


def calc_iv_skew(calls_df: pd.DataFrame, puts_df: pd.DataFrame, price: float) -> float:
    """
    IV skew = avg call IV − avg put IV (ATM ±5% strikes).
    Positive = calls pricier = bullish positioning.
    Negative = puts pricier = fear / hedging.
    """
    try:
        lo, hi = price * 0.95, price * 1.05
        c_iv = pd.to_numeric(
            calls_df.loc[(calls_df["strike"] >= lo) & (calls_df["strike"] <= hi),
                         "impliedVolatility"],
            errors="coerce",
        ).dropna()
        p_iv = pd.to_numeric(
            puts_df.loc[(puts_df["strike"] >= lo) & (puts_df["strike"] <= hi),
                        "impliedVolatility"],
            errors="coerce",
        ).dropna()
        if c_iv.empty or p_iv.empty:
            return 0.0
        return round(float(c_iv.mean()) - float(p_iv.mean()), 4)
    except Exception:
        return 0.0


def calc_whale_score(signal: Dict) -> int:
    """
    Composite 0–100 institutional signal score. Log-scaled dollar flow base
    prevents a $1M print from being treated the same as $10M.
      Base (log-scaled flow): 0–60
      Side multiplier:        ask=1.0x, bid/mid=0.75x
      Expiry bonus:           0DTE-7DTE +15, <=30DTE +5
      Unusual vol/OI:         vol_oi >= 10 → +10
    """
    flow = signal.get("total_flow", 0)
    if flow <= 0:
        return 0
    base = min(60, max(0, int(math.log10(max(flow, 1000) / 1000) / 3 * 60)))
    side_mult = 1.0 if signal.get("trade_side") == "ask" else 0.75
    expiry_bonus = 15 if signal.get("dte", 99) <= 7 else 5 if signal.get("dte", 99) <= 30 else 0
    unusual_bonus = 10 if signal.get("vol_oi_ratio", 0) >= 10 else 0
    return min(100, int(base * side_mult) + expiry_bonus + unusual_bonus)


def fmt_whale_score(score: int) -> str:
    bar = score_bar(score, width=8)
    if score >= 80:   c = Fore.RED + Style.BRIGHT
    elif score >= 60: c = Fore.YELLOW + Style.BRIGHT
    elif score >= 40: c = Fore.YELLOW
    else:             c = Fore.WHITE
    return f"{c}{bar} {score:3d}{Style.RESET_ALL}"


def grade_score(setup_q: float, opt_score: int, has_contract: bool) -> float:
    """The composite behind the letter. Same weighting the a_grade filter uses."""
    return setup_q * 50 + opt_score * 0.30 + (20 if has_contract else 0)


def grade_letter(setup_q: float, opt_score: int, has_contract: bool) -> str:
    """Plain A/B/C/D. The signal journal stores this — never the coloured form,
    because ANSI escapes in a database column make every later filter wrong."""
    score = grade_score(setup_q, opt_score, has_contract)
    if score >= 75: return "A"
    if score >= 55: return "B"
    if score >= 35: return "C"
    return "D"


_GRADE_COLORS = {
    "A": Fore.GREEN  + Style.BRIGHT,
    "B": Fore.YELLOW,
    "C": Fore.WHITE,
    "D": Fore.RED,
}


def trade_grade(setup_q: float, opt_score: int, has_contract: bool) -> str:
    letter = grade_letter(setup_q, opt_score, has_contract)
    return _GRADE_COLORS[letter] + letter + Style.RESET_ALL

# ─── Options score ────────────────────────────────────────────────────────────
def get_spread_tier(avg_vol: float) -> Tuple[str, int, float]:
    if avg_vol > 50_000_000: return "~$0.01", 40, 0.30
    if avg_vol > 10_000_000: return "~$0.05", 32, 0.50
    if avg_vol >  1_000_000: return "~$0.15", 18, 1.00
    return "~$0.40+", 5, 2.00


def calc_options_score(avg_vol: float, ivr_proxy: float) -> int:
    _, sp, tm = get_spread_tier(avg_vol)
    vs = max(0.0, min(30.0, (math.log10(max(avg_vol, 1)) - 4) / (math.log10(1e8) - 4) * 30))
    iv = ivr_proxy * 20.0
    oe = max(avg_vol * tm, 1)
    os_ = max(0.0, min(10.0, (math.log10(oe) - 3) / (math.log10(1e7) - 3) * 10))
    return min(100, max(0, round(sp + vs + iv + os_)))

# ─── IV Rank Proxy (HV20/HV60 ratio) ────────────────────────────────────────
def calc_iv_rank_proxy(hist: pd.DataFrame) -> Dict:
    """
    Uses HV20/HV60 ratio as an IV rank signal (no live options chain needed).
    Returns dict with keys: hv20, hv60, ratio, ivr_score (0-100), label.
      ratio > 1.4  → elevated IV (IVR > 70) — options expensive
      ratio > 1.1  → normal
      ratio < 0.8  → compressed (IVR < 30) — options cheap
    """
    result = {"hv20": 0.0, "hv60": 0.0, "ratio": 1.0, "ivr_score": 50, "label": "NORMAL"}
    try:
        closes = hist["Close"].dropna()
        if len(closes) < 65:
            return result

        def _hv(n: int) -> float:
            c = closes.tail(n + 1)
            if len(c) < n:
                return 0.0
            lr = [math.log(float(c.iloc[i]) / float(c.iloc[i - 1])) for i in range(1, len(c))]
            mean = sum(lr) / len(lr)
            var = sum((x - mean) ** 2 for x in lr) / len(lr)
            return round(math.sqrt(var * 252), 4)

        hv20 = _hv(20)
        hv60 = _hv(60)
        if hv60 <= 0:
            return result

        ratio = round(hv20 / hv60, 3)
        if ratio > 1.4:
            ivr_score = min(100, int(50 + (ratio - 1.0) * 50))
            label = "ELEVATED"
        elif ratio > 1.1:
            ivr_score = int(40 + (ratio - 1.0) * 33)
            label = "NORMAL"
        elif ratio < 0.8:
            ivr_score = min(100, max(0, int(30 * ratio / 0.8)))
            label = "COMPRESSED"
        else:
            ivr_score = int(30 + (ratio - 0.8) * 50)
            label = "NORMAL"

        return {"hv20": hv20, "hv60": hv60, "ratio": ratio,
                "ivr_score": ivr_score, "label": label}
    except Exception:
        return result


def get_contract_display(c: Optional[Dict], market_open: bool = True) -> Dict:
    """
    Return a clean structured dict for web UI display — no ANSI codes.
    Fields: label, exp_short, strike_str, type_char, delta_str, price_str,
            dte_str, stale, ivr_display, vol_oi_str, confidence.
    """
    if not c:
        return {"label": "—", "stale": False, "empty": True}

    exp_short  = c["exp"][5:]  # MM-DD
    type_char  = "C" if c["type"] == "call" else "P"
    strike_str = f"${c['strike']:.0f}" if c["strike"] == int(c["strike"]) else f"${c['strike']:.2f}"
    delta_str  = f"δ{c['delta']:+.2f}"
    dte_str    = "0DTE" if c["dte"] == 0 else f"{c['dte']}DTE"
    stale      = c.get("stale", False) or (c["bid"] == 0 and c["ask"] == 0)

    if stale or not market_open:
        price_str = f"Last: ${c['mid']:.2f}"
    else:
        price_str = f"${c['bid']:.2f}/${c['ask']:.2f}"

    mid_str    = f"${c['mid']:.2f}"
    voi        = c["vol"] / max(c["oi"], 1)
    vol_oi_str = f"x{voi:.1f}" if voi >= 1 else "—"
    label      = f"{exp_short} {strike_str}{type_char} · {delta_str} · {mid_str} · {dte_str}"

    if stale and not market_open:
        status_tag = "AFTER HOURS"
    elif stale:
        status_tag = "STALE"
    else:
        status_tag = ""

    return {
        "label":      label,
        "exp":        c["exp"],
        "exp_short":  exp_short,
        "strike":     c["strike"],
        "strike_str": strike_str,
        "type":       c["type"],
        "type_char":  type_char,
        "delta":      round(c["delta"], 3),
        "delta_str":  delta_str,
        "bid":        c["bid"],
        "ask":        c["ask"],
        "mid":        c["mid"],
        "mid_str":    mid_str,
        "price_str":  price_str,
        "dte":        c["dte"],
        "dte_str":    dte_str,
        "stale":      stale,
        "status_tag": status_tag,
        "vol":        c["vol"],
        "oi":         c["oi"],
        "vol_oi_str": vol_oi_str,
        "iv":           round(c.get("iv", 0), 3),
        "roi":          c.get("roi", 0),
        "score":        c.get("score", 0),
        "target_price": c.get("target_price"),
        "empty":        False,
    }


# ─── Key Level Detection ──────────────────────────────────────────────────────
def find_key_levels(hist: pd.DataFrame, price: float) -> List[Dict]:
    """
    Detects support/resistance from:
      - Swing highs/lows (5-bar pivot)
      - Yesterday's high/low (key intraday reference)
      - Round numbers (±8%)
    Returns up to 8 levels sorted by proximity, filtered to ±6%.
    """
    if len(hist) < 10:
        return []

    highs    = hist["High"].values.astype(float)
    lows     = hist["Low"].values.astype(float)
    vols     = hist["Volume"].values.astype(float)
    n        = len(hist)
    mean_vol = max(vols.mean(), 1)
    raw: List[Tuple[float, str, float, int]] = []  # (price, type, vol_ratio, bars_ago)

    lb = min(5, n // 4)
    for i in range(lb, n - lb):
        wh = highs[max(0, i - lb) : i + lb + 1]
        wl = lows[max(0, i - lb)  : i + lb + 1]
        if highs[i] >= max(wh) - 0.001:
            raw.append((highs[i], "resistance", vols[i] / mean_vol, n - 1 - i))
        if lows[i] <= min(wl) + 0.001:
            raw.append((lows[i],  "support",    vols[i] / mean_vol, n - 1 - i))

    # Yesterday's extremes — reliable intraday reference
    if n >= 2:
        raw.append((highs[-2], "resistance", vols[-2] / mean_vol * 1.5, 1))
        raw.append((lows[-2],  "support",    vols[-2] / mean_vol * 1.5, 1))

    # Round numbers ±8%
    lo, hi = price * 0.92, price * 1.08
    step = (1.0 if price < 30 else 2.5 if price < 100 else
            5.0 if price < 300 else 10.0 if price < 1000 else
            25.0 if price < 3000 else 50.0)
    rp = math.floor(lo / step) * step
    while rp <= hi:
        if lo <= rp <= hi and abs(rp - price) / price > 0.002:
            raw.append((rp, "resistance" if rp > price else "support", 0.5, 0))
        rp += step

    if not raw:
        return []

    # Cluster within 1% buckets
    raw.sort(key=lambda x: x[0])
    clusters: List[List] = []
    grp = [raw[0]]
    for item in raw[1:]:
        if item[0] / grp[-1][0] - 1 < 0.010:
            grp.append(item)
        else:
            clusters.append(grp)
            grp = [item]
    clusters.append(grp)

    levels = []
    for g in clusters:
        rep   = sum(x[0] for x in g) / len(g)
        dist  = (rep - price) / price * 100
        if abs(dist) < 0.10:       # filter levels too close to current price
            continue

        tot_vol_r  = sum(x[2] for x in g)
        min_bars   = min(x[3] for x in g)
        touches    = len(g)

        # Strength 0–10
        strength = (
            min(4, touches)
            + min(3, int(tot_vol_r))
            + (2 if min_bars <= 3 else 1 if min_bars <= 10 else 0)
            + (1 if abs(rep % step) < step * 0.05 or abs(rep % step - step) < step * 0.05 else 0)
        )
        strength = min(10, strength)

        levels.append({
            "price":    round(rep, 2),
            "type":     "support" if rep < price else "resistance",
            "strength": strength,
            "touches":  touches,
            "dist_pct": dist,
        })

    levels = [l for l in levels if 0.10 < abs(l["dist_pct"]) < 6.0]
    levels.sort(key=lambda x: abs(x["dist_pct"]))
    return levels[:8]


def level_str(level: Optional[Dict]) -> str:
    if not level:
        return "—"
    stars = "*" * min(4, level["strength"] // 2 + 1)
    ltype = "S" if level["type"] == "support" else "R"
    c     = Fore.GREEN if ltype == "S" else Fore.RED
    return f"{c}{ltype}${level['price']:.1f}{Style.RESET_ALL} {Fore.YELLOW}{stars}{Style.RESET_ALL}"

# ─── Unfilled Gap Detection ───────────────────────────────────────────────────

def find_unfilled_gaps(
    hist: pd.DataFrame,
    current_price: float,
    lookback: int = 60,
    min_gap_pct: float = 0.20,
    max_gap_pct: float = 8.0,
) -> List[Dict]:
    """
    Scans historical OHLCV for gaps that have NOT been filled yet.

    Gap definition (Dante's model):
      - Gap UP:   today's open > prior close → leaves a void zone = [prior_close, open]
                  Unfilled if price has NEVER traded back down to prior_close since then.
      - Gap DOWN: today's open < prior close → leaves a void zone = [open, prior_close]
                  Unfilled if price has NEVER traded back up to prior_close since then.

    These open gap zones are price magnets / trade targets, not signals themselves.

    Returns list of unfilled gaps sorted by distance from current price (closest first),
    capped at 5. Each dict includes zone bounds, midpoint, distance, and direction
    relative to current price (above or below) — which tells you the trade direction.
    """
    if hist is None or len(hist) < 10:
        return []

    df = hist.tail(lookback + 5).copy().reset_index(drop=True)
    if len(df) < 5:
        return []

    opens  = df["Open"].values.astype(float)
    highs  = df["High"].values.astype(float)
    lows   = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    n      = len(df)

    unfilled: List[Dict] = []

    for i in range(1, n - 1):  # leave at least 1 bar after to test fill
        prior_close = closes[i - 1]
        this_open   = opens[i]

        if prior_close <= 0:
            continue

        gap_pct = (this_open - prior_close) / prior_close * 100

        if abs(gap_pct) < min_gap_pct or abs(gap_pct) > max_gap_pct:
            continue

        if gap_pct > 0:
            # Gap UP: void zone is [prior_close, this_open] — BELOW this_open
            # Filled if any subsequent LOW came back down to prior_close
            gap_top    = round(float(this_open),   2)
            gap_bottom = round(float(prior_close), 2)
            gap_type   = "gap_up"
            filled = any(float(lows[j]) <= prior_close for j in range(i + 1, n))
        else:
            # Gap DOWN: void zone is [this_open, prior_close] — ABOVE this_open
            # Filled if any subsequent HIGH came back up to prior_close
            gap_top    = round(float(prior_close), 2)
            gap_bottom = round(float(this_open),   2)
            gap_type   = "gap_down"
            filled = any(float(highs[j]) >= prior_close for j in range(i + 1, n))

        if filled:
            continue  # gap was filled at some point — not a target

        # Still open — record it
        gap_mid  = round((gap_top + gap_bottom) / 2, 2)
        dist_pct = round((gap_mid - current_price) / current_price * 100, 2)

        # Direction to trade: gap above = go UP to fill it (calls), gap below = go DOWN (puts)
        direction_to_fill = "up" if gap_mid > current_price else "down"

        bars_ago = n - 1 - i

        unfilled.append({
            "type":              gap_type,           # "gap_up" or "gap_down"
            "top":               gap_top,
            "bottom":            gap_bottom,
            "mid":               gap_mid,
            "gap_pct":           round(abs(gap_pct), 2),
            "dist_pct":          dist_pct,           # + = gap is above price, - = below
            "direction_to_fill": direction_to_fill,  # which way price needs to move
            "bars_ago":          bars_ago,
        })

    if not unfilled:
        return []

    # Deduplicate nearby gaps (within 0.5% of each other — cluster them)
    unfilled.sort(key=lambda g: g["mid"])
    deduped: List[Dict] = [unfilled[0]]
    for g in unfilled[1:]:
        prev = deduped[-1]
        if abs(g["mid"] - prev["mid"]) / prev["mid"] < 0.005:
            # Keep the closer one
            if abs(g["dist_pct"]) < abs(prev["dist_pct"]):
                deduped[-1] = g
        else:
            deduped.append(g)

    # Sort by distance, return up to 5 nearest
    deduped.sort(key=lambda g: abs(g["dist_pct"]))
    return deduped[:5]


# ─── Options Contract Finder ──────────────────────────────────────────────────
def _nan0(v):
    """Convert a value to float, treating None/NaN as 0."""
    try:
        f = float(v)
        return 0.0 if f != f else f   # f != f is True only for NaN
    except Exception:
        return 0.0


def _score_contract(row: pd.Series, S: float, T: float, direction: str,
                    target_delta: float = 0.45, dte_mode: str = "all",
                    target_price: float = 0.0) -> float:
    """Score a contract row. When target_price is set, strike proximity to that
    target and OI/volume at that strike become the dominant ranking factors —
    matching how a trader picks the strike at their gap/level/ATH target."""
    try:
        K     = float(row["strike"])
        iv    = _nan0(row.get("impliedVolatility", 0.3)) or 0.3
        oi    = int(_nan0(row.get("openInterest", 0)))
        cvol  = int(_nan0(row.get("volume", 0)))
        bid   = _nan0(row.get("bid", 0))
        ask   = _nan0(row.get("ask", 0))
        mid   = (bid + ask) / 2 if (bid + ask) > 0 else float(row.get("lastPrice", 0) or 0)
        stale = bid == 0 and ask == 0   # market closed / no quote

        if mid <= 0.05:
            return -1.0

        # Hard reject illiquid contracts — no point scoring zero-OI junk
        if oi < 50 and cvol < 25:
            return -1.0

        # Penalize stale quotes (market closed / no live bid-ask) — still score but discount
        stale_penalty = 0.60 if stale else 1.0

        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        # Index options (SPX/MDY) have tighter natural spreads relative to premium;
        # high-priced instruments: use 10% spread limit vs 40% for stocks
        spread_limit = 0.10 if S > 500 else 0.40
        if spread_pct > spread_limit:
            return -1.0

        otype = "call" if direction == "up" else "put"
        delta = abs(bs_delta(S, K, T, iv, otype))

        # Hard reject deep ITM contracts — delta > 0.65 means the contract moves
        # like the stock. You're paying for intrinsic value, not leverage.
        if delta > 0.65:
            return -1.0

        # Expected 1-sigma move (annualised IV → daily move for this DTE window)
        sigma_move  = S * max(iv, 0.05) * math.sqrt(max(T, 1.0 / 365))
        if otype == "call":
            ev_1sigma = max(0.0, (S + sigma_move) - K)
        else:
            ev_1sigma = max(0.0, K - (S - sigma_move))
        # ROI at 1-sigma target, capped to prevent micro-priced outliers distorting rank
        roi_score = min(1.0, max(0.0, (ev_1sigma - mid) / (mid + 0.01)) / 5.0)

        # Liquidity: OI + volume (log-scaled). Heavy OI at a level = institutional agreement.
        vol_oi_ratio = cvol / max(oi, 1)
        voi_score    = min(1.0, math.log10(max(vol_oi_ratio, 1.0)) / math.log10(50))
        delta_score  = max(0.0, 1.0 - abs(delta - target_delta) / 0.15)
        liq_score    = min(1.0, math.log10(max(oi + cvol + 1, 1)) / 5.5)
        # Soft OI penalty — thin contracts still score, just lower
        if oi < 200 and cvol < 100:
            liq_score *= 0.75
        elif oi < 500 and cvol < 200:
            liq_score *= 0.88
        spread_score = max(0.0, 1.0 - spread_pct * 2.5)

        # Soft penalty for expensive contracts (>$10 mid) — still ranked, just lower priority
        price_penalty = 1.0 if mid <= 10.0 else max(0.6, 1.0 - (mid - 10.0) / 40.0)

        # Strike gravity — round number strikes attract institutional OI (gamma levels)
        if K % 50 == 0:
            strike_gravity = 1.08
        elif K % 25 == 0:
            strike_gravity = 1.05
        elif K % 10 == 0:
            strike_gravity = 1.03
        elif K % 5 == 0:
            strike_gravity = 1.01
        else:
            strike_gravity = 1.0

        # ── Target-price mode ─────────────────────────────────────────────────
        # When caller provides a target (unfilled gap, heavy level, ATH), the
        # strike closest to that target with the most OI/volume wins.
        # Target score decays with distance as a % of the underlying price.
        if target_price > 0:
            dist_pct   = abs(K - target_price) / max(target_price, 1.0)
            # Full score within 1% of target; drops to 0 at 8% away
            target_score = max(0.0, 1.0 - dist_pct / 0.08)
            # Heavy OI at the target strike = market sees the same level
            heavy_oi_score = min(1.0, math.log10(max(oi + 1, 1)) / 4.5)
            # Weights: target proximity 40%, OI at target 30%, spread 15%, liq 15%
            return (target_score * 40 + heavy_oi_score * 30 +
                    spread_score * 15 + liq_score * 15) * stale_penalty * price_penalty * strike_gravity

        return (roi_score * 20 + delta_score * 15 + liq_score * 35 + spread_score * 15 + voi_score * 15) * stale_penalty * price_penalty * strike_gravity
    except Exception:
        return -1.0


def get_best_contract(ticker: str, direction: str, price: float,
                      vix: float = -1.0, top_n: int = 1,
                      dte_mode: str = "all",
                      target_price: float = 0.0) -> Optional[Dict]:
    """
    direction: "up" → calls, "down" → puts.
    dte_mode: "0dte" (same-day only), "weekly" (2–7 DTE), "monthly" (8–45 DTE), "all" (no constraint)
    VIX is used to set the ideal delta target (high VIX → further OTM).
    Returns the contract with the best composite score or None.
    """
    try:
        t = _yf(ticker)

        # Guard against stale/missing price — always fetch live.
        try:
            live = float(t.fast_info.last_price or 0)
            if live > 0 and (price <= 0 or abs(live - price) / price > 0.02):
                price = live
        except Exception:
            pass

        exps = t.options
        if not exps:
            return None

        today = datetime.now().date()

        def dte(e: str) -> int:
            return (datetime.strptime(e, "%Y-%m-%d").date() - today).days

        # Allow 0DTE during market hours (9:30–16:00 ET); exclude outside hours
        # Holiday- and early-close-aware; weekday() < 5 called Thanksgiving a session.
        _market_open = is_market_open()
        min_dte = 0 if _market_open else 1
        future = [e for e in exps if dte(e) >= min_dte]

        # Filter candidates by dte_mode
        if dte_mode == "0dte":
            cands = [e for e in future if dte(e) == 0]
            if not cands and not _market_open:
                cands = [e for e in future if dte(e) <= 1]  # after hours: next day
        elif dte_mode == "weekly":
            cands = (
                [e for e in future if 2 <= dte(e) <= 7]
                or [e for e in future if dte(e) <= 14]
            )
        elif dte_mode == "monthly":
            cands = [e for e in future if 8 <= dte(e) <= 45]
            if not cands:
                cands = [e for e in future if dte(e) <= 90]
        else:  # "all" — no constraint
            cands = list(future)
        if not cands:
            cands = list(future[:2])
        if not cands:
            cands = list(exps[:2])

        # Base delta target varies by DTE mode; VIX further adjusts within each mode
        _mode_delta = {"0dte": 0.45, "weekly": 0.38, "monthly": 0.30, "all": 0.38}
        target_delta = _mode_delta.get(dte_mode, 0.45)
        # Apply VIX overlay only if VIX is known and moves the target further OTM
        vix_dt = vix_delta_target(vix)
        if vix > 0:
            target_delta = min(target_delta, vix_dt)
        scored: List[tuple] = []   # (score, contract_dict)

        for exp in cands[:3]:
            d = dte(exp)
            # For 0DTE: real minutes left in the session, not an arbitrary floor.
            # minutes_to_close honours the 13:00 ET half sessions — assuming a
            # 16:00 close on those days overstates remaining time by 3 hours,
            # which inflates every 0DTE time-value estimate.
            if d == 0:
                mins_left = max(1.0, minutes_to_close() or 60.0)
                T = (mins_left / 1440.0) / 365.0
            else:
                T = d / 365.0
            T = max(T, 1.0 / (1440 * 365))   # absolute floor: 1 minute
            try:
                chain = _option_chain(t, ticker, exp)
            except Exception:
                continue
            df = chain.calls if direction == "up" else chain.puts
            if df.empty:
                continue

            # Narrow to ±15% moneyness; tight for high-price instruments (SPX ±4% = ±$220)
            # SPX ~$5500 at ±15% = ±$825 — way too wide; use ±4% for price > $500
            # MDY ~$643 at ±15% = ±$96 — still wide; ±4% gives ±$26 which is reasonable
            if price > 500:
                moneyness_band = 0.05 if vix > 30 else 0.04
            else:
                moneyness_band = 0.20 if vix > 30 else 0.15
            df = df[
                (df["strike"] >= price * (1 - moneyness_band)) &
                (df["strike"] <= price * (1 + moneyness_band))
            ]
            if df.empty:
                continue

            for _, row in df.iterrows():
                sc = _score_contract(row, price, T, direction, target_delta, dte_mode, target_price)
                if sc <= 0:
                    continue
                K     = float(row["strike"])
                iv    = _nan0(row.get("impliedVolatility", 0.3)) or 0.3
                oi    = int(_nan0(row.get("openInterest", 0)))
                cvol  = int(_nan0(row.get("volume", 0)))
                bid   = _nan0(row.get("bid", 0))
                ask   = _nan0(row.get("ask", 0))
                mid   = (bid + ask) / 2 if (bid + ask) > 0 else _nan0(row.get("lastPrice", 0))
                stale = bid == 0 and ask == 0
                otype = "call" if direction == "up" else "put"
                delta = bs_delta(price, K, T, iv, otype)
                # Expected ROI at 1-sigma move (for display)
                sigma_move_raw = price * max(iv, 0.05) * math.sqrt(max(T, 1.0 / 365))
                if otype == "call":
                    ev_raw = max(0.0, (price + sigma_move_raw) - K)
                else:
                    ev_raw = max(0.0, K - (price - sigma_move_raw))
                roi_pct = round((ev_raw - mid) / (mid + 0.01) * 100, 1) if mid > 0 else 0.0

                scored.append((sc, {
                    "exp":          exp,
                    "dte":          d,
                    "strike":       K,
                    "type":         otype,
                    "delta":        delta,
                    "iv":           iv,
                    "oi":           oi,
                    "vol":          cvol,
                    "bid":          bid,
                    "ask":          ask,
                    "mid":          mid,
                    "stale":        stale,
                    "score":        round(sc, 1),
                    "roi":          roi_pct,
                    "target_price": round(target_price, 2) if target_price else None,
                }))

        # Sort by score descending; tiebreak by OI descending (contracts within 3 pts prefer higher OI)
        scored.sort(key=lambda x: (-x[0], -x[1].get("oi", 0)))
        top = [c for _, c in scored[:top_n]]
        if not top:
            return None
        return top[0] if top_n == 1 else top
    except Exception:
        return None


def fmt_contract(c: Optional[Dict]) -> str:
    if not c:
        return "—"
    exp_s  = c["exp"][5:]                         # MM-DD
    ctype  = "C" if c["type"] == "call" else "P"
    cc     = Fore.CYAN if ctype == "C" else Fore.YELLOW
    dc     = Fore.GREEN if c["type"] == "call" else Fore.YELLOW
    delta  = f"{dc}δ{c['delta']:+.2f}{Style.RESET_ALL}"
    vol_s  = fmt_num(c["vol"])
    oi_s   = fmt_num(c["oi"])
    dte_s  = f"{c['dte']}DTE" if c["dte"] > 0 else "0DTE"
    stale_tag = f" {Fore.RED}[STALE]{Style.RESET_ALL}" if c.get("stale") else ""
    if c.get("stale") or (c["bid"] == 0 and c["ask"] == 0):
        price_s = f"last${c['mid']:.2f}"
    else:
        price_s = f"${c['bid']:.2f}/${c['ask']:.2f}"
    # VOI — the headline signal (vol/OI ratio)
    voi = c["vol"] / max(c["oi"], 1)
    if voi >= 20:   vc = Fore.RED + Style.BRIGHT
    elif voi >= 10: vc = Fore.RED
    elif voi >= 5:  vc = Fore.YELLOW + Style.BRIGHT
    elif voi >= 2:  vc = Fore.YELLOW
    else:           vc = Fore.WHITE
    voi_s = f"{vc}x{voi:.1f}{Style.RESET_ALL}"
    return (
        f"{cc}{exp_s} ${c['strike']:.0f}{ctype}{Style.RESET_ALL}"
        f"  {voi_s}  {delta}  {price_s}  V:{vol_s}/OI:{oi_s}  {Fore.WHITE}{dte_s}{Style.RESET_ALL}"
        f"{stale_tag}"
    )

# ─── Sector Analysis ──────────────────────────────────────────────────────────
def scan_sectors() -> Dict[str, Dict]:
    """Fetch sector ETF data. Returns sector_name → metrics dict."""
    sector_data: Dict[str, Dict] = {}
    etfs = list(SECTOR_ETFS.values())
    fetch_list = etfs + ["SPY"]

    print(f"  {Fore.CYAN}Scanning sectors (batch)...{Style.RESET_ALL}", end="", flush=True)
    batch = _fetch_batch_history(fetch_list, period="30d")
    sys.stdout.write("\r" + " " * 50 + "\r")
    sys.stdout.flush()

    # If batch download failed (cloud IP block, timeout, etc.), fall back to
    # individual _yf() calls which use the curl_cffi session and work on Render.
    use_batch = not batch.empty
    if not use_batch:
        print(f"  {Fore.YELLOW}Batch failed — falling back to individual fetches{Style.RESET_ALL}", end="", flush=True)

    spy_chg = _get_spy_change(batch) if use_batch else 0.0

    for name, etf in SECTOR_ETFS.items():
        try:
            if use_batch:
                hist = _extract_ticker_hist(batch, etf)
            else:
                t = _yf(etf)
                hist = t.history(period="5d")
            if hist.empty or len(hist) < 3:
                continue

            hist = hist.dropna(subset=["Close", "High", "Low", "Volume"])
            if len(hist) < 3:
                continue

            today     = hist.iloc[-1]
            yesterday = hist.iloc[-2]

            price       = float(today["Close"])
            prior_close = float(yesterday["Close"])
            today_vol   = int(today["Volume"])
            today_high  = float(today["High"])
            today_low   = float(today["Low"])

            avg_vol    = float(hist["Volume"].iloc[:-1].tail(10).mean())
            change_pct = (price - prior_close) / prior_close * 100
            rel_vol    = today_vol / avg_vol if avg_vol > 0 else 1.0

            day_range = today_high - today_low
            price_loc = (price - today_low) / day_range if day_range > 0 else 0.5

            base   = hist.iloc[-4] if len(hist) >= 4 else hist.iloc[0]
            mom_3d = (price - float(base["Close"])) / float(base["Close"]) * 100

            strength = change_pct * 2.0 + (rel_vol - 1.0) * 0.5 + mom_3d * 0.3

            rs_vs_spy, breakout = classify_breakout(change_pct, spy_chg, rel_vol)

            sector_data[name] = {
                "etf":        etf,
                "price":      price,
                "change_pct": change_pct,
                "rel_vol":    rel_vol,
                "price_loc":  price_loc,
                "mom_3d":     mom_3d,
                "strength":   strength,
                "bias":       "up" if strength >= 0 else "down",
                "rs_vs_spy":  rs_vs_spy,
                "breakout":   breakout,
            }
        except Exception:
            pass

    return sector_data


# ─── Sector Laggard Detection ─────────────────────────────────────────────────
def find_sector_laggards(results: List[Dict], sector_data: Dict[str, Dict]) -> List[Dict]:
    """
    Find tickers lagging their sector's move.
    Strong sector (|strength| > 1.5) + ticker underperforming = catch-up play.
    Tags each result with is_laggard / lag_pct / lag_score. Returns sorted laggard list.
    """
    for r in results:
        r.setdefault("is_laggard", False)
        r.setdefault("lag_pct", 0.0)
        r.setdefault("lag_score", 0.0)
        r.setdefault("lag_direction", None)

    strong_sectors = {n: d for n, d in sector_data.items() if abs(d["strength"]) > 1.5}
    laggards: List[Dict] = []

    for r in results:
        sname = TICKER_SECTOR.get(r["ticker"])
        if not sname or sname not in strong_sectors:
            continue

        sd         = strong_sectors[sname]
        sector_chg = sd["change_pct"]
        ticker_chg = r["change_pct"]
        lag        = sector_chg - ticker_chg   # + = sector leading, ticker behind

        if abs(sector_chg) < 0.5:
            continue

        if sector_chg > 0 and lag > 0.4:
            lag_score = lag * abs(sd["strength"])
            r.update({"is_laggard": True, "lag_pct": round(lag, 3),
                       "lag_score": round(lag_score, 3), "lag_direction": "up"})
            laggards.append(r)

        elif sector_chg < 0 and lag < -0.4:
            lag_score = abs(lag) * abs(sd["strength"])
            r.update({"is_laggard": True, "lag_pct": round(lag, 3),
                       "lag_score": round(lag_score, 3), "lag_direction": "down"})
            laggards.append(r)

    laggards.sort(key=lambda x: x["lag_score"], reverse=True)
    return laggards[:12]


def rank_breakout_constituents(sector_chg: float, breakout: str,
                               quotes: Dict[str, Dict],
                               n_laggards: int = 3, n_leaders: int = 2) -> List[tuple]:
    """Order a breakout sector's constituents: catch-up laggards first, then momentum
    leaders. lag = sector_chg - ticker_chg. Returns [(ticker, role, change, lag)]."""
    if breakout == "none" or not quotes:
        return []

    rows = [(tk, q["change_pct"], sector_chg - q["change_pct"]) for tk, q in quotes.items()]

    if breakout == "up":
        laggards = sorted(rows, key=lambda r: r[2], reverse=True)   # hasn't risen yet: biggest +lag
        leaders  = sorted(rows, key=lambda r: r[1], reverse=True)   # strongest up move
    else:  # down
        laggards = sorted(rows, key=lambda r: r[2])                 # hasn't fallen yet: most -lag
        leaders  = sorted(rows, key=lambda r: r[1])                 # strongest down move

    picked: List[tuple] = []
    used = set()
    for tk, ch, lag in laggards[:n_laggards]:
        picked.append((tk, "laggard", round(ch, 2), round(lag, 2)))
        used.add(tk)
    for tk, ch, lag in leaders:
        if len([p for p in picked if p[1] == "leader"]) >= n_leaders:
            break
        if tk in used:
            continue
        picked.append((tk, "leader", round(ch, 2), round(lag, 2)))
        used.add(tk)
    return picked


# ─── Sector Heatmap (individual constituents) ────────────────────────────────
def _quotes_for(tickers: List[str], period: str = "5d") -> Dict[str, Dict]:
    """
    Batch-fetch OHLCV for `tickers` and return
    {ticker: {"change_pct", "dollar_vol", "price"}}.

    Same fetch path as scan_sectors (single yf.download with per-ticker fallback),
    so it works behind cloud IP blocks. Network — call off the event loop.
    """
    out: Dict[str, Dict] = {}
    if not tickers:
        return out

    batch     = _fetch_batch_history(tickers, period=period)
    use_batch = not batch.empty

    for tk in tickers:
        try:
            if use_batch:
                hist = _extract_ticker_hist(batch, tk)
            else:
                hist = _yf(tk).history(period=period)
            if hist.empty:
                continue
            hist = hist.dropna(subset=["Close", "Volume"])
            if len(hist) < 2:
                continue

            today       = hist.iloc[-1]
            prior_close = float(hist.iloc[-2]["Close"])
            price       = float(today["Close"])
            if prior_close <= 0 or price <= 0:
                continue

            change_pct = (price - prior_close) / prior_close * 100
            dollar_vol = price * float(today["Volume"])
            out[tk] = {"change_pct": change_pct, "dollar_vol": dollar_vol, "price": price}
        except Exception:
            pass

    return out


# In-process cache so repeated taps on the same sector don't refetch.
_HEATMAP_CACHE: Dict[str, tuple] = {}   # sector -> (timestamp, payload)
_HEATMAP_TTL = 60.0


def sector_heatmap(sector: str, limit: int = 150) -> Dict:
    """
    Heatmap data for one sector: its individual constituents with today's % change
    and a size weight (dollar volume). Sorted by weight desc, capped to `limit`
    (default high enough to show essentially every liquid name in the sector).
    """
    now    = time.time()
    cached = _HEATMAP_CACHE.get(sector)
    if cached and now - cached[0] < _HEATMAP_TTL:
        return cached[1]

    tickers = constituents_for(sector, fallback_map=TICKER_SECTOR)
    quotes  = _quotes_for(tickers)

    stocks = [
        {"ticker": tk, "change": round(q["change_pct"], 2), "weight": round(q["dollar_vol"], 0)}
        for tk, q in quotes.items()
    ]
    stocks.sort(key=lambda s: s["weight"], reverse=True)

    payload = {"sector": sector, "stocks": stocks[:limit]}
    _HEATMAP_CACHE[sector] = (now, payload)
    return payload


_PLAYS_CACHE: Dict[str, tuple] = {}   # "sector:dte_mode" -> (timestamp, payload)
_PLAYS_TTL = 60.0


def sector_breakout_plays(sector: str, sector_data: Dict[str, Dict],
                          vix: float = -1.0, dte_mode: str = "all",
                          n_laggards: int = 3, n_leaders: int = 2) -> Dict:
    """For an RS-breakout sector, build constituent contract plays (laggards first,
    then leaders). Returns {sector, breakout, plays:[{ticker,role,change,lag,contract}]}.
    Network — call off the event loop."""
    sd = sector_data.get(sector)
    breakout = sd.get("breakout", "none") if sd else "none"
    if not sd or breakout == "none":
        return {"sector": sector, "breakout": "none", "plays": []}

    now    = time.time()
    key    = f"{sector}:{dte_mode}"
    cached = _PLAYS_CACHE.get(key)
    if cached and now - cached[0] < _PLAYS_TTL:
        return cached[1]

    direction = "up" if breakout == "up" else "down"
    quotes    = _quotes_for(constituents_for(sector, fallback_map=TICKER_SECTOR))
    ranked    = rank_breakout_constituents(sd["change_pct"], breakout, quotes,
                                           n_laggards, n_leaders)

    plays = []
    for ticker, role, change, lag in ranked:
        contract = get_best_contract(ticker, direction, 0, vix, top_n=1, dte_mode=dte_mode)
        if isinstance(contract, list):
            contract = contract[0] if contract else None
        if not contract:
            continue
        plays.append({"ticker": ticker, "role": role, "change": change,
                      "lag": lag, "contract": contract})

    payload = {"sector": sector, "breakout": breakout, "plays": plays}
    _PLAYS_CACHE[key] = (now, payload)
    return payload


def top_individual_laggard(sector_data: Dict[str, Dict], scan_n: int = 2) -> Optional[Dict]:
    """
    The single individual stock that most diverges against its sector — the red name
    in a green sector (or the green name in a red sector). Looks only at the
    strongest-moving sectors (|change| >= 0.5%) to bound network cost.
    """
    moved = [(n, d) for n, d in sector_data.items() if abs(d.get("change_pct", 0)) >= 0.5]
    if not moved:
        return None
    moved.sort(key=lambda x: abs(x[1]["change_pct"]), reverse=True)

    best: Optional[Dict] = None
    for name, d in moved[:scan_n]:
        sector_chg = d["change_pct"]
        quotes     = _quotes_for(constituents_for(name, fallback_map=TICKER_SECTOR))
        for tk, q in quotes.items():
            stock_chg = q["change_pct"]
            # divergence against the sector's direction; >0 means it's bucking the move
            div = (sector_chg - stock_chg) if sector_chg > 0 else (stock_chg - sector_chg)
            if div <= 0:
                continue
            if best is None or div > best["divergence"]:
                best = {
                    "ticker":        tk,
                    "sector":        name,
                    "sector_change": round(sector_chg, 2),
                    "stock_change":  round(stock_chg, 2),
                    "divergence":    round(div, 2),
                }
    return best


# ─── Contract selection quality ──────────────────────────────────────────────
# Premium alone ranks contracts by what they *cost*, so a deep-ITM contract with
# no volume outranks genuine flow. These two helpers are pure so they can be
# tested against real chain data rather than mocks.

DEEP_ITM_PCT   = 15.0    # beyond this far in the money...
DEEP_ITM_VOL   = 1_000   # ...volume must justify the depth
MIN_OI         = 50
MIN_VOL        = 250
MIN_MID        = 0.05
WIDE_SPREAD_PCT = 12.0   # flagged, never rejected


def _itm_pct(strike: float, opt_type: str, spot: float) -> float:
    """How far in the money, as a percentage of spot. 0.0 when out of the money."""
    if spot <= 0:
        return 0.0
    intrinsic = (spot - strike) if opt_type == "call" else (strike - spot)
    return max(0.0, intrinsic / spot * 100.0)


def contract_quality(c: Dict, spot: float, market_open: bool = True) -> Tuple[bool, str]:
    """
    (ok, reason) — whether a contract represents tradeable directional flow.

    Deliberately NOT an extrinsic-value test. Extrinsic ratio rejects the AMD
    575P correctly and the TSLA 355P incorrectly: a stale mid can sit below
    intrinsic on the most heavily traded contract of the day. Distance from
    spot plus liquidity is the rule that holds up against real data.
    """
    if spot <= 0:
        return True, ""   # nothing to judge against; dropping would hide real flow

    dte = c.get("dte", -1)
    if dte == 0 and not market_open:
        return False, "expired — 0DTE after the close"

    mid = float(c.get("mid", 0) or 0)
    if mid <= MIN_MID:
        return False, "no premium"

    vol = int(c.get("vol", 0) or 0)
    oi  = int(c.get("oi", 0) or 0)

    # Depth is checked before liquidity: a junk contract usually trips both, and
    # "deep ITM (26%) on 160 lots" explains why its premium looked large, which
    # bare "illiquid" does not.
    itm = _itm_pct(float(c.get("strike", 0) or 0), c.get("type", "call"), spot)
    if itm > DEEP_ITM_PCT and vol < DEEP_ITM_VOL:
        return False, f"deep ITM ({itm:.0f}%) on {vol} lots"

    if oi < MIN_OI and vol < MIN_VOL:
        return False, "illiquid"

    return True, ""


def contract_economics(c: Dict, spot: float) -> Dict:
    """
    What the contract needs in order to pay: breakeven, the move to reach it,
    where the strike sits against spot, and how much of the price is spread.

    pct_to_breakeven is signed against the *direction of the trade*: negative
    means spot is already through breakeven, positive means it still has to get
    there. A put's breakeven is below its strike, so the arithmetic differs.
    """
    strike = float(c.get("strike", 0) or 0)
    mid    = float(c.get("mid", 0) or 0)
    otype  = c.get("type", "call")
    bid    = float(c.get("bid", 0) or 0)
    ask    = float(c.get("ask", 0) or 0)

    breakeven = strike + mid if otype == "call" else strike - mid

    if spot > 0:
        raw = (breakeven - spot) / spot * 100.0
        # A call needs spot to rise to breakeven; a put needs it to fall.
        pct_to_be = raw if otype == "call" else -raw
        moneyness = (strike - spot) / spot * 100.0
    else:
        pct_to_be = None
        moneyness = None

    spread_pct = None
    if bid > 0 and ask > 0 and mid > 0:
        spread_pct = (ask - bid) / mid * 100.0

    return {
        "breakeven":        round(breakeven, 2),
        "pct_to_breakeven": round(pct_to_be, 2) if pct_to_be is not None else None,
        "moneyness_pct":    round(moneyness, 2) if moneyness is not None else None,
        "spread_pct":       round(spread_pct, 2) if spread_pct is not None else None,
        "wide_spread":      bool(spread_pct is not None and spread_pct >= WIDE_SPREAD_PCT),
    }


# ─── Options Flow Scanner (Enhanced) ─────────────────────────────────────────
def scan_options_flow(tickers: List[str], show_progress: bool = True,
                      on_signal=None, on_progress=None) -> List[Dict]:
    """
    Detect unusual options activity per ticker.

    Tries the TastyTrade OPRA stream first and falls back to yfinance. Whichever
    one produced the returned signals is recorded via get_flow_source() so the
    UI can label the data honestly instead of trusting that credentials exist.
    """
    if _TT_AVAILABLE and scan_options_flow_tt is not None:
        try:
            if show_progress:
                sys.stdout.write(f"\r  {Fore.GREEN}[TT LIVE]{Style.RESET_ALL} Streaming real options flow...\n")
                sys.stdout.flush()
            tt_signals = scan_options_flow_tt(
                tickers, _TT_USER, _TT_PASS,
                window_secs=90, max_dte=14,
                show_progress=show_progress,
            )
            if tt_signals:
                _set_flow_source("tastytrade-live", "live OPRA trade prints")
                return tt_signals
            err = _tt_last_error()
            if err:
                reason = f"TastyTrade unavailable ({err})"
            elif not is_market_open():
                reason = "market closed — no live prints to collect"
            else:
                reason = "TastyTrade returned no prints"
            if show_progress:
                sys.stdout.write(f"  [TT] {reason} — falling back to yfinance\n")
        except Exception as e:
            reason = f"TastyTrade error: {e}"
            if show_progress:
                sys.stdout.write(f"  [TT] {reason} — falling back to yfinance\n")
    else:
        reason = "TastyTrade credentials not configured"

    signals = _scan_options_flow_yf(
        tickers, show_progress=show_progress,
        on_signal=on_signal, on_progress=on_progress,
    )
    _set_flow_source("yfinance-delayed", reason)
    return signals


def _scan_options_flow_yf(tickers: List[str], show_progress: bool = True,
                          on_signal=None, on_progress=None) -> List[Dict]:
    """
    yfinance flow path: a 15-minute-delayed daily snapshot, not trade prints.
    Enhanced vs CheddarFlow/Unusual Whales:
      - Trade side classification (bid/ask aggression)
      - Premium tiers: retail / institutional ($100K+) / block ($500K+) / whale ($1M+)
      - Golden sweep: vol > OI×10, at ask, flow > $100K
      - Stacked flow: 3+ unique unusual strikes = institutional position building
      - IV skew: call vs put implied vol differential
      - DTE breakdown: 0DTE / 1-7DTE / 8+DTE flow buckets
      - Whale score: composite 0-100 signal strength
    Sorted by whale_score descending.
    """
    flow_signals: List[Dict] = []
    today = datetime.now().date()

    def dte_of(e: str) -> int:
        return (datetime.strptime(e, "%Y-%m-%d").date() - today).days

    # Detect market hours — after close, bid/ask are stale zeros; use lastPrice instead
    try:
        import zoneinfo
        _et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except ImportError:
        import pytz
        _et = datetime.now(pytz.timezone("America/New_York"))
    _market_open = is_market_open()

    for i, ticker in enumerate(tickers, 1):
        if show_progress:
            sys.stdout.write(
                f"\r  Flow [{i}/{len(tickers)}] {Fore.CYAN}{ticker:<6}{Style.RESET_ALL}  "
            )
            sys.stdout.flush()
        if on_progress:
            on_progress({"ticker": ticker, "i": i, "n": len(tickers)})
        try:
            t    = _yf(ticker)
            exps = t.options
            if not exps:
                continue

            near_exps = [e for e in exps if 0 <= dte_of(e) <= 14] or list(exps[:2])

            call_flow = put_flow = 0.0
            dte0_flow = dte1_7_flow = dte8p_flow = 0.0
            filtered_n = 0
            filtered_premium = 0.0
            filtered_reasons: List[str] = []
            top_call = top_put = None
            call_contracts: List[Dict] = []
            put_contracts:  List[Dict] = []
            all_unusual_strikes: List[float] = []
            all_calls_dfs: List[pd.DataFrame] = []
            all_puts_dfs:  List[pd.DataFrame] = []

            # Live price for IV skew calculation
            try:
                cur_price = float(t.fast_info.last_price or 0)
            except Exception:
                cur_price = 0.0

            for exp in near_exps[:3]:
                d = dte_of(exp)
                try:
                    chain = _option_chain(t, ticker, exp)
                except Exception:
                    continue

                all_calls_dfs.append(chain.calls)
                all_puts_dfs.append(chain.puts)

                for df, otype in [(chain.calls, "call"), (chain.puts, "put")]:
                    if df.empty:
                        continue
                    df = df.copy()
                    df["vol_n"]  = pd.to_numeric(df["volume"],       errors="coerce").fillna(0).clip(lower=0)
                    df["oi_n"]   = pd.to_numeric(df["openInterest"], errors="coerce").fillna(0).clip(lower=0)
                    df["bid_n"]  = pd.to_numeric(df["bid"],          errors="coerce").fillna(0).clip(lower=0)
                    df["ask_n"]  = pd.to_numeric(df["ask"],          errors="coerce").fillna(0).clip(lower=0)
                    df["last_n"] = pd.to_numeric(
                        df.get("lastPrice", pd.Series(dtype=float)), errors="coerce"
                    ).fillna(0).clip(lower=0)
                    mid_base     = (df["bid_n"] + df["ask_n"]) / 2
                    df["mid_n"]  = mid_base.where(mid_base > 0, df["last_n"])
                    df["vol_oi"] = df["vol_n"] / df["oi_n"].clip(lower=1)
                    df["flow"]   = df["vol_n"] * df["mid_n"] * 100
                    df["has_mkt"]= (df["bid_n"] > 0) & (df["ask_n"] > 0)

                    # After hours: bid/ask are 0 but volume/OI data is still valid.
                    # Use lastPrice as mid and skip live-market-quote requirement.
                    mkt_filter = df["has_mkt"] if _market_open else (df["mid_n"] > 0.05)
                    unusual = df[
                        (df["vol_n"]    >= 50) &
                        (df["oi_n"]     >= 10) &
                        (df["vol_oi"]   >= 2.0) &
                        (df["flow"]     >= 5_000) &
                        (df["mid_n"]    >  0.05) &
                        mkt_filter
                    ]

                    for _, row in unusual.iterrows():
                        bid_v  = float(row["bid_n"])
                        ask_v  = float(row["ask_n"])
                        last_v = float(row["last_n"])
                        mid_v  = float(row["mid_n"])
                        vol_n  = int(row["vol_n"])
                        oi_n   = int(row["oi_n"])
                        flow_v = float(row["flow"])
                        strike = float(row["strike"])

                        trade_side = classify_trade_side(bid_v, ask_v, last_v)
                        is_sweep   = bool(vol_n > oi_n * 5)
                        is_golden  = bool(
                            vol_n > oi_n * 10 and
                            trade_side == "ask" and
                            flow_v >= 100_000
                        )

                        if flow_v >= 1_000_000:   tier = "whale"
                        elif flow_v >= 500_000:   tier = "block"
                        elif flow_v >= 100_000:   tier = "institutional"
                        else:                     tier = "retail"

                        entry = {
                            "ticker":       ticker,
                            "exp":          exp,
                            "dte":          d,
                            "strike":       strike,
                            "type":         otype,
                            "vol":          vol_n,
                            "oi":           oi_n,
                            "vol_oi":       round(float(row["vol_oi"]), 1),
                            "mid":          round(mid_v, 2),
                            "bid":          round(bid_v, 2),
                            "ask":          round(ask_v, 2),
                            "flow":         round(flow_v, 0),
                            "sweep":        is_sweep,
                            "golden_sweep": is_golden,
                            "trade_side":   trade_side,
                            "premium_tier": tier,
                        }
                        entry.update(contract_economics(entry, cur_price))

                        # Premium measures conviction only if the contract could
                        # carry any. A deep-ITM contract nobody traded books
                        # millions on its price alone and drags the bias with it,
                        # so junk is excluded from the totals — but counted, and
                        # reported, rather than silently dropped.
                        ok, why = contract_quality(entry, cur_price, _market_open)
                        if not ok:
                            filtered_n += 1
                            filtered_premium += flow_v
                            if why not in filtered_reasons:
                                filtered_reasons.append(why)
                            continue

                        all_unusual_strikes.append(strike)

                        # DTE bucket
                        if d == 0:      dte0_flow    += flow_v
                        elif d <= 7:    dte1_7_flow  += flow_v
                        else:           dte8p_flow   += flow_v

                        if otype == "call":
                            call_flow += flow_v
                            call_contracts.append(entry)
                            if top_call is None or flow_v > top_call["flow"]:
                                top_call = entry
                        else:
                            put_flow += flow_v
                            put_contracts.append(entry)
                            if top_put is None or flow_v > top_put["flow"]:
                                top_put = entry

            total_flow = call_flow + put_flow
            if total_flow < 5_000:
                continue

            # IV skew — use only the closest expiry (mixing multiple DTEs distorts skew)
            iv_skew = 0.0
            if all_calls_dfs and all_puts_dfs and cur_price > 0:
                try:
                    iv_skew = calc_iv_skew(all_calls_dfs[0], all_puts_dfs[0], cur_price)
                except Exception:
                    pass

            bias           = "call" if call_flow >= put_flow else "put"
            top_contract   = top_call if bias == "call" else top_put
            unique_strikes = len(set(round(s, 0) for s in all_unusual_strikes))
            stacked_flow   = unique_strikes >= 3
            golden_sweep   = any(c.get("golden_sweep") for c in call_contracts + put_contracts)
            dom_side       = top_contract.get("trade_side", "mid") if top_contract else "mid"
            top_tier       = top_contract.get("premium_tier", "retail") if top_contract else "retail"

            signal = {
                # Original fields (backward-compatible)
                "ticker":         ticker,
                "call_flow":      call_flow,
                "put_flow":       put_flow,
                "total_flow":     total_flow,
                "flow_bias":      bias,
                "pc_ratio":       put_flow / call_flow if call_flow > 0 else 999.0,
                "top_call":       top_call,
                "top_put":        top_put,
                "top_contract":   top_contract,
                "call_contracts": call_contracts,
                "put_contracts":  put_contracts,
                # Enhanced fields
                "trade_side":     dom_side,
                "iv_skew":        iv_skew,
                "stacked_flow":   stacked_flow,
                "unique_strikes": unique_strikes,
                "golden_sweep":   golden_sweep,
                "premium_tier":   top_tier,
                "dte0_flow":      dte0_flow,
                "dte1_7_flow":    dte1_7_flow,
                "dte8p_flow":     dte8p_flow,
                "whale_score":    0,  # computed below
                # Spot travels with the signal so downstream consumers can place
                # strikes against price without re-fetching a quote.
                "spot":             round(cur_price, 2),
                "filtered_n":       filtered_n,
                "filtered_premium": round(filtered_premium, 0),
                "filtered_reasons": filtered_reasons,
            }
            signal["whale_score"] = calc_whale_score(signal)

            # FIX 6: IV skew adjustment — apply to flow-level options confidence
            # iv_skew = avg_call_IV - avg_put_IV (from calc_iv_skew)
            # Negative skew = puts pricier = fear/hedging → reduce call edge
            # Positive skew = calls pricier = bullish positioning → boost call edge
            _skew_adj = 0.0
            if iv_skew < -0.05 and signal["flow_bias"] == "call":
                _skew_adj = -0.10   # bearish fear positioning — penalize call recommendations
            elif iv_skew > 0.05 and signal["flow_bias"] == "call":
                _skew_adj = 0.05    # bullish skew — slight call edge boost
            signal["iv_skew_options_adj"] = round(_skew_adj, 2)

            flow_signals.append(signal)
            if on_signal:
                on_signal(signal)
        except Exception:
            pass
        time.sleep(0.15)

    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    # Sort by whale score (strength of institutional signal) then dollar flow
    flow_signals.sort(key=lambda x: (x["whale_score"], x["total_flow"]), reverse=True)
    return flow_signals


# ─── Dark Pool Approximation ──────────────────────────────────────────────────
def scan_dark_pool_prints(tickers: List[str], show_progress: bool = True) -> List[Dict]:
    """
    Detect potential dark pool / off-exchange block prints in equity data.
    Heuristic: today's volume ≥ 2.5× 20-day avg AND intraday price move < 0.5%.
    Price-insensitive volume = characteristic of institutional off-exchange execution.
    Returns list sorted by relative volume descending.
    """
    prints: List[Dict] = []

    if show_progress:
        sys.stdout.write(
            f"  {Fore.CYAN}Dark pool scan ({len(tickers)} tickers, batch)...{Style.RESET_ALL}"
        )
        sys.stdout.flush()
    batch = _fetch_batch_history(tickers, period="30d")
    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    for ticker in tickers:
        try:
            hist = _extract_ticker_hist(batch, ticker)
            if hist.empty or len(hist) < 5:
                continue

            hist = hist.dropna(subset=["Close", "High", "Low", "Volume"]).copy()
            if len(hist) < 5:
                continue

            today_row  = hist.iloc[-1]
            vol_today  = int(today_row["Volume"])
            open_p     = float(today_row["Open"])
            close_p    = float(today_row["Close"])
            high_p     = float(today_row["High"])
            low_p      = float(today_row["Low"])
            avg_vol_20 = float(hist["Volume"].iloc[:-1].tail(20).mean())

            if avg_vol_20 <= 0:
                continue

            rel_vol    = vol_today / avg_vol_20
            intra_move = abs(close_p - open_p) / open_p * 100
            day_range  = (high_p - low_p) / max(low_p, 0.01) * 100

            if rel_vol < 2.5 or intra_move >= 0.5:
                continue

            prints.append({
                "ticker":     ticker,
                "price":      round(close_p, 2),
                "rel_vol":    round(rel_vol, 2),
                "intra_move": round(intra_move, 3),
                "day_range":  round(day_range, 3),
                "vol_today":  vol_today,
                "avg_vol_20": int(avg_vol_20),
                "dollar_vol": round(vol_today * close_p, 0),
                "dp_bias":    "accumulation" if close_p >= open_p else "distribution",
            })
        except Exception:
            pass

    prints.sort(key=lambda x: x["rel_vol"], reverse=True)
    return prints


def fmt_flow(f: float) -> str:
    """Format dollar flow value."""
    if f >= 1_000_000: return f"${f/1e6:.1f}M"
    if f >= 1_000:     return f"${f/1e3:.0f}K"
    return f"${f:.0f}"


def fmt_flow_contract(c: Optional[Dict]) -> str:
    if not c:
        return "—"
    ctype = "C" if c["type"] == "call" else "P"
    cc    = Fore.CYAN if ctype == "C" else Fore.YELLOW
    sweep = f" {Fore.RED}[SWEEP]{Style.RESET_ALL}" if c.get("sweep") else ""
    dte_s = f"{c['dte']}DTE" if c.get("dte", 1) > 0 else "0DTE"
    price_s = f"${c['mid']:.2f}"
    return (
        f"{cc}{c['exp'][5:]} ${c['strike']:.0f}{ctype}{Style.RESET_ALL}"
        f"  {Fore.WHITE}{dte_s}{Style.RESET_ALL}"
        f"  vol:{fmt_num(c['vol'])}  OI:{fmt_num(c['oi'])}"
        f"  x{c['vol_oi']:.1f}  {price_s}  {fmt_flow(c['flow'])}{sweep}"
    )


def print_sector_laggards(laggards: List[Dict], sector_data: Dict[str, Dict]) -> None:
    if not laggards:
        print(f"  {Fore.YELLOW}No sector laggards found (sector strength threshold not met).{Style.RESET_ALL}")
        return
    sep = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL
    print(sep)
    print(Fore.WHITE + Style.BRIGHT + "  SECTOR LAGGARDS  (catch-up plays)" + Style.RESET_ALL)
    rows = []
    for r in laggards:
        sname = TICKER_SECTOR.get(r["ticker"], "?")
        sd    = sector_data.get(sname, {})
        sec_chg = sd.get("change_pct", 0)
        lag_dir = "^CALLS" if r["lag_direction"] == "up" else "vPUTS"
        c = Fore.CYAN if r["lag_direction"] == "up" else Fore.YELLOW
        rows.append([
            Fore.WHITE + Style.BRIGHT + r["ticker"] + Style.RESET_ALL,
            f"${r['price']:.2f}",
            color_change(r["change_pct"]),
            f"{Fore.GREEN}+{sec_chg:.2f}%{Style.RESET_ALL}" if sec_chg > 0 else f"{Fore.RED}{sec_chg:.2f}%{Style.RESET_ALL}",
            sname,
            f"{r['lag_pct']:+.2f}%",
            c + lag_dir + Style.RESET_ALL,
            fmt_contract(r.get("contract")),
        ])
    headers = ["TICKER", "PRICE", "TICKER CHG", "SECTOR CHG", "SECTOR", "LAG", "PLAY", "CONTRACT"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))
    print(sep)


def print_options_flow(flow_signals: List[Dict]) -> None:
    if not flow_signals:
        print(f"  {Fore.YELLOW}No unusual options flow detected.{Style.RESET_ALL}")
        return
    sep = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL
    print(sep)
    print(Fore.WHITE + Style.BRIGHT + "  OPTIONS FLOW  (unusual activity)" + Style.RESET_ALL)
    rows = []
    for f in flow_signals:
        bias_c = Fore.CYAN if f["flow_bias"] == "call" else Fore.YELLOW
        bias_s = bias_c + f["flow_bias"].upper() + Style.RESET_ALL
        tc = f.get("top_contract")
        contract_str = fmt_flow_contract(tc) if tc else "—"
        rows.append([
            Fore.WHITE + Style.BRIGHT + f["ticker"] + Style.RESET_ALL,
            fmt_flow(f["total_flow"]),
            f"{Fore.CYAN}{fmt_flow(f['call_flow'])}{Style.RESET_ALL}",
            f"{Fore.YELLOW}{fmt_flow(f['put_flow'])}{Style.RESET_ALL}",
            bias_s,
            contract_str,
        ])
    headers = ["TICKER", "TOTAL $FLOW", "CALL FLOW", "PUT FLOW", "BIAS", "TOP CONTRACT"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))
    print(sep)


def print_whale_alerts(flow_signals: List[Dict]) -> None:
    """Print whale-scored institutional flow table."""
    whales = [f for f in flow_signals if f.get("whale_score", 0) >= 40]
    if not whales:
        print(f"  {Fore.YELLOW}No whale-level flow detected (score ≥ 40).{Style.RESET_ALL}")
        return
    sep = Fore.WHITE + Style.BRIGHT + "─" * 110 + Style.RESET_ALL
    print(sep)
    print(Fore.WHITE + Style.BRIGHT + "  WHALE ALERTS  (institutional signal score ≥ 40)" + Style.RESET_ALL)
    rows = []
    tier_colors = {
        "whale":         Fore.RED + Style.BRIGHT,
        "block":         Fore.YELLOW + Style.BRIGHT,
        "institutional": Fore.CYAN,
        "retail":        Fore.WHITE,
    }
    for f in whales:
        side   = f.get("trade_side", "mid")
        side_c = Fore.GREEN if side == "ask" else (Fore.RED if side == "bid" else Fore.WHITE)
        tier_c = tier_colors.get(f.get("premium_tier", "retail"), Fore.WHITE)
        skew   = f.get("iv_skew", 0.0)
        skew_s = (Fore.GREEN if skew > 0.01 else (Fore.RED if skew < -0.01 else Fore.WHITE)) \
                 + f"{skew:+.3f}" + Style.RESET_ALL
        dte_s  = (
            f"0D:{fmt_flow(f.get('dte0_flow',0))} "
            f"1-7:{fmt_flow(f.get('dte1_7_flow',0))} "
            f"8+:{fmt_flow(f.get('dte8p_flow',0))}"
        )
        rows.append([
            fmt_whale_score(f["whale_score"]),
            Fore.WHITE + Style.BRIGHT + f["ticker"] + Style.RESET_ALL,
            fmt_flow(f["total_flow"]),
            tier_c + f.get("premium_tier", "retail").upper() + Style.RESET_ALL,
            side_c + side.upper() + Style.RESET_ALL,
            f"{Fore.RED}GOLDEN{Style.RESET_ALL}" if f.get("golden_sweep") else "—",
            f"{Fore.CYAN}x{f['unique_strikes']}{Style.RESET_ALL}" if f.get("stacked_flow") else "—",
            skew_s,
            dte_s,
        ])
    headers = ["SCORE", "TICKER", "TOTAL $", "TIER", "SIDE", "GOLDEN", "STACKED", "IV SKEW", "DTE BREAKDOWN"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))
    print(sep)


def print_sector_heatmap(sector_data: Dict[str, Dict]) -> None:
    """Print sector strength sorted strongest→weakest."""
    if not sector_data:
        return

    sep = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL
    print(sep)
    print(Fore.WHITE + Style.BRIGHT + "  SECTOR HEATMAP  (sorted by strength)" + Style.RESET_ALL)

    ranked = sorted(sector_data.items(), key=lambda x: x[1]["strength"], reverse=True)
    line   = "  "
    for name, d in ranked:
        chg = d["change_pct"]
        loc = d["price_loc"]

        if chg > 0.5:   c = Fore.GREEN + Style.BRIGHT
        elif chg > 0:   c = Fore.GREEN
        elif chg > -0.5:c = Fore.YELLOW
        else:           c = Fore.RED

        loc_ind = "▲" if loc > 0.6 else ("▼" if loc < 0.4 else "─")
        line += f"{c}{name}{loc_ind}{chg:+.2f}%{Style.RESET_ALL}  "

    print(line)
    print(sep)


def get_forward_direction(r: Dict, sector_data: Dict[str, Dict]) -> str:
    """
    Forward-looking direction: what is most likely to happen next.
    Weights: sector bias > gap direction > price location in range > level proximity > vol.
    NOT derived solely from what price already did.
    """
    score_up = 0.0
    score_dn = 0.0

    # 1. Sector bias — the macro tailwind/headwind (highest weight)
    sname = TICKER_SECTOR.get(r["ticker"])
    if sname and sname in sector_data:
        sd  = sector_data[sname]
        mag = abs(sd["strength"])
        if sd["bias"] == "up":
            score_up += mag * 0.6
        else:
            score_dn += mag * 0.6

    # 2. Gap direction — backtest-calibrated (60-day, 2800+ signals)
    # gap_down: fill bias = price recovers UP → bullish → calls
    # gap_up standalone: slight continuation (50.9% up vs 49.1% fade) → mild bullish
    # gap_up + highvol: FADE wins 56.2% → bearish → puts
    if r["gap_flag"] == "gap_down":
        score_up += 2.5   # gap down → fill up → bullish
    elif r["gap_flag"] == "gap_up":
        if r.get("high_vol"):
            score_dn += 3.0   # gap_up + HV = strong fade (Sharpe 2.42 fading)
        else:
            score_up += 1.2   # gap_up alone: slight continuation lean (50.9%)

    # 3. Price location in today's range (upper = buyers in control → calls)
    loc = r.get("price_loc", 0.5)
    if loc > 0.65:
        score_up += 1.5
    elif loc < 0.35:
        score_dn += 1.5

    # 4. Nearest key level — where is price heading?
    nl = r.get("near_level")
    if nl:
        dist     = nl["dist_pct"]   # + = above price (resistance), - = below (support)
        strength = nl["strength"]
        if nl["type"] == "support" and abs(dist) < 2.0 and strength >= 3:
            # Sitting just above support → bounce candidate → calls
            score_up += 0.8
        elif nl["type"] == "resistance" and dist < 2.0 and strength >= 3:
            # Pressing into resistance → could break out (follow sector) or reject
            if score_up > score_dn:
                score_up += 0.5   # sector is up, bet on breakout
            else:
                score_dn += 0.8   # sector weak, bet on rejection

    # 5. High volume confirms the move direction
    if r["high_vol"]:
        if r["change_pct"] > 0:
            score_up += 1.0
        else:
            score_dn += 1.0

    return "up" if score_up >= score_dn else "down"


def apply_forward_directions(results: List[Dict], sector_data: Dict[str, Dict]) -> None:
    """Update direction field for all results using forward-looking logic."""
    for r in results:
        r["direction"] = get_forward_direction(r, sector_data)


# ─── Batch Data Fetching ──────────────────────────────────────────────────────
def _fetch_batch_history(tickers: List[str], period: str = "1y") -> pd.DataFrame:
    """
    Download OHLCV for all tickers in a SINGLE HTTP request using yf.download().
    Eliminates the per-ticker rate-limiting that killed the old approach.
    Returns a MultiIndex DataFrame (metric, ticker) or flat DataFrame for one ticker.
    """
    try:
        _dl_kwargs = dict(tickers=tickers, period=period,
                         auto_adjust=True, progress=False, threads=True)
        if _YF_SESSION is not None:
            try: data = yf.download(**_dl_kwargs, session=_YF_SESSION)
            except TypeError: data = yf.download(**_dl_kwargs)
        else:
            data = yf.download(**_dl_kwargs)
        return data
    except Exception:
        return pd.DataFrame()


def _fetch_live_prices(tickers: List[str]) -> Dict[str, float]:
    """
    Batch-fetch 1-minute intraday for all tickers and return {ticker: last_price}.
    Used during market hours when the daily bar's Close is still NaN.
    """
    if not tickers:
        return {}
    try:
        _dl_kwargs = dict(tickers=tickers, period="1d", interval="1m",
                         auto_adjust=True, progress=False, threads=True)
        if _YF_SESSION is not None:
            try: data = yf.download(**_dl_kwargs, session=_YF_SESSION)
            except TypeError: data = yf.download(**_dl_kwargs)
        else:
            data = yf.download(**_dl_kwargs)
        if data.empty:
            return {}
        prices: Dict[str, float] = {}
        for ticker in tickers:
            try:
                hist = _extract_ticker_hist(data, ticker)
                if hist.empty:
                    continue
                closes = hist["Close"].dropna()
                if closes.empty:
                    continue
                prices[ticker] = float(closes.iloc[-1])
            except Exception:
                pass
        return prices
    except Exception:
        return {}


def _extract_ticker_hist(batch: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """
    Pull one ticker's OHLCV out of a batch download result.
    Handles both multi-ticker (MultiIndex columns) and single-ticker (flat) cases.
    """
    if batch.empty:
        return pd.DataFrame()
    try:
        if not isinstance(batch.columns, pd.MultiIndex):
            # Single-ticker download: batch IS the ticker's history
            return batch.dropna(how="all").copy()
        # Multi-ticker: columns = (metric, ticker) — try both axis orderings
        try:
            h = batch.xs(ticker, level=1, axis=1)
        except KeyError:
            h = batch.xs(ticker, level=0, axis=1)
        return h.dropna(how="all").copy()
    except Exception:
        return pd.DataFrame()


def _process_ticker(ticker: str, hist: pd.DataFrame, live_price: float = 0.0, spy_chg: float = 0.0) -> Optional[Dict]:
    """
    Convert pre-fetched OHLCV history into a result dict. Zero network calls.
    `hist` should be 1y of data; last 60 rows used for level detection,
    full range used for IVR proxy (replaces fast_info.year_high/year_low).
    `live_price` is the latest intraday price; used when today's daily Close is NaN.
    """
    try:
        if hist.empty or len(hist) < 5:
            return None

        # Last 60 trading days for gap/inside-day/level detection
        hist60 = hist.tail(60).copy()

        # Handle intraday: if today's Close is NaN (market open), use last complete row
        last = hist60.iloc[-1]
        intraday_open: Optional[float] = None
        if pd.isna(last.get("Close", float("nan"))):
            # Capture today's open for gap detection before dropping the NaN row
            try:
                intraday_open = float(last["Open"]) if not pd.isna(last["Open"]) else None
            except Exception:
                intraday_open = None
            hist60 = hist60.dropna(subset=["Close", "High", "Low"]).copy()
            if len(hist60) < 2:
                return None

        today     = hist60.iloc[-1]
        yesterday = hist60.iloc[-2]

        prior_close = float(yesterday["Close"])

        # Price: use live intraday price if we dropped today's NaN row, else today's close
        if intraday_open is not None and live_price > 0:
            price  = live_price
            open_p = intraday_open
        elif intraday_open is not None:
            price  = float(today["Close"])   # last complete close as fallback
            open_p = intraday_open
        else:
            price  = float(today["Close"])
            open_p = float(today["Open"])
        today_high  = float(today["High"])
        today_low   = float(today["Low"])
        yest_high   = float(yesterday["High"])
        yest_low    = float(yesterday["Low"])
        today_vol   = int(today["Volume"])

        vol_s   = hist60["Volume"].iloc[:-1]
        avg_vol = float(vol_s.tail(20).mean()) if len(vol_s) >= 1 else float(today_vol)

        change_pct = (price - prior_close) / prior_close * 100
        gap_pct    = (open_p - prior_close) / prior_close * 100

        gap_flag: Optional[str] = None
        if abs(gap_pct) > 0.5:
            if gap_pct > 0:
                gap_flag = None if today_low <= prior_close else "gap_up"
            else:
                gap_flag = None if today_high >= prior_close else "gap_down"

        inside_day = today_high < yest_high and today_low > yest_low
        rel_vol    = today_vol / avg_vol if avg_vol > 0 else 0.0
        high_vol   = rel_vol > 1.4

        # Double inside day check
        double_inside_day = False
        if inside_day and len(hist60) >= 3:
            day2 = hist60.iloc[-3]
            double_inside_day = (float(yesterday["High"]) < float(day2["High"]) and
                                 float(yesterday["Low"])  > float(day2["Low"]))

        # Breakout signal: price closed above prior day high (bull) or below prior day low (bear)
        # Requires vol confirmation (>1.2x avg) to filter noise
        breakout: Optional[str] = None
        if not inside_day:
            if price > yest_high and rel_vol > 1.2:
                breakout = "bull"
            elif price < yest_low and rel_vol > 1.2:
                breakout = "bear"

        # RS vs SPY: how much the ticker is outperforming or lagging the market today
        rs_vs_spy = round(change_pct - spy_chg, 2) if spy_chg != 0.0 else 0.0

        # Price location in today's range (0 = at low, 1 = at high)
        day_range = today_high - today_low
        price_loc = (price - today_low) / day_range if day_range > 0 else 0.5

        # HV20: annualized 20-day historical volatility of log returns
        try:
            closes_20 = hist60["Close"].dropna().tail(21)
            if len(closes_20) >= 10:
                log_rets = [math.log(float(closes_20.iloc[i]) / float(closes_20.iloc[i-1]))
                            for i in range(1, len(closes_20))]
                mean_lr = sum(log_rets) / len(log_rets)
                var_lr  = sum((x - mean_lr) ** 2 for x in log_rets) / len(log_rets)
                hv20    = round(math.sqrt(var_lr * 252), 4)
            else:
                hv20 = 0.0
        except Exception:
            hv20 = 0.0

        hv_regime = "volatile" if hv20 >= 0.35 else ("normal" if hv20 >= 0.20 else "calm")

        # ── Ripster EMA Clouds ──────────────────────────────────────────────────
        # EMA9 = short momentum, EMA34 = trend spine, EMA200 = macro direction
        try:
            c_series = hist60["Close"].dropna()
            ema9_val   = float(c_series.ewm(span=9,   adjust=False).mean().iloc[-1])
            ema34_val  = float(c_series.ewm(span=34,  adjust=False).mean().iloc[-1])
            ema200_val = float(c_series.ewm(span=200, adjust=False).mean().iloc[-1])
            # BUG 2: EWM NaN guard — use current price as fallback if EWM returns NaN
            if pd.isna(ema9_val):   ema9_val   = price
            if pd.isna(ema34_val):  ema34_val  = price
            if pd.isna(ema200_val): ema200_val = price
            # EMA34 slope: compare to 3 bars ago
            ema34_prev = float(c_series.ewm(span=34, adjust=False).mean().iloc[-4]) if len(c_series) >= 4 else ema34_val
            if pd.isna(ema34_prev): ema34_prev = ema34_val
            ema_cloud_bull = (price > ema9_val and ema9_val > ema34_val and ema34_val > ema34_prev)
            ema_cloud_bear = (price < ema9_val and ema9_val < ema34_val and ema34_val < ema34_prev)
            above_ema200   = price > ema200_val

            # FIX 8: RSI-14 — computed here alongside EMAs using the same c_series
            delta_close = c_series.diff()
            gain  = delta_close.clip(lower=0).rolling(14).mean()
            loss  = (-delta_close.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, float('nan'))
            rsi14 = float(100 - 100 / (1 + rs.iloc[-1]))
            if pd.isna(rsi14): rsi14 = 50.0
        except Exception:
            ema9_val = ema34_val = ema200_val = 0.0
            ema_cloud_bull = ema_cloud_bear = False
            above_ema200 = True
            rsi14 = 50.0

        # ── Rolling MVWAP (20-bar volume-weighted avg price) ─────────────────
        try:
            h60 = hist60.dropna(subset=["Close", "High", "Low", "Volume"])
            typical_60  = (h60["High"] + h60["Low"] + h60["Close"]) / 3
            mvwap_series = (typical_60 * h60["Volume"]).rolling(20).sum() / h60["Volume"].rolling(20).sum()
            mvwap        = float(mvwap_series.iloc[-1]) if not pd.isna(mvwap_series.iloc[-1]) else None
            mvwap_prev   = float(mvwap_series.iloc[-2]) if len(mvwap_series) >= 2 and not pd.isna(mvwap_series.iloc[-2]) else mvwap
            above_vwap   = bool(price > mvwap) if mvwap else None
            vwap_reclaim = bool(mvwap is not None and mvwap_prev is not None
                                and float(yesterday["Close"]) < mvwap_prev and price >= mvwap)
        except Exception:
            mvwap = mvwap_prev = None
            above_vwap = None
            vwap_reclaim = False

        # IVR proxy: 52-week high/low from downloaded history — no fast_info needed
        # Kept for backward-compat (stored in result dict) but NOT used for options scoring.
        try:
            yr_high   = float(hist["High"].max())
            yr_low    = float(hist["Low"].min())
            ivr_proxy = (price - yr_low) / (yr_high - yr_low) if yr_high > yr_low else 0.5
            ivr_proxy = max(0.0, min(1.0, ivr_proxy))
        except Exception:
            ivr_proxy = 0.5

        # Full IV rank proxy (HV20/HV60 ratio) — authoritative IVR (BUG 3 fix: computed first)
        iv_rank_data = calc_iv_rank_proxy(hist)

        spread_label, _, _ = get_spread_tier(avg_vol)
        # BUG 3: use iv_rank_data["ivr_score"] / 100.0 as the authoritative IVR (0–1 scale)
        # instead of the 52-week scalar ivr_proxy — eliminates duplicate conflicting IVR signals
        opt_score = calc_options_score(avg_vol, iv_rank_data["ivr_score"] / 100.0)

        # Expected move (1-sigma, annualized HV20): ±% of current price
        expected_move_pct = round(hv20 / math.sqrt(252) * 100, 2) if hv20 > 0 else 0.0

        # Key levels (use 60d window — same as before)
        levels     = find_key_levels(hist60, price)
        near_level = next((l for l in levels if l["strength"] >= 2), levels[0] if levels else None)

        # Unfilled historical gaps — price targets/magnets (Dante's gap play model)
        # These are prior session gaps that have never been filled — use as trade targets
        unfilled_gaps   = find_unfilled_gaps(hist60, price)
        nearest_gap     = unfilled_gaps[0] if unfilled_gaps else None

        # ── Signal combo classification (backtest-derived) ──────────────────────
        # Key combos from 60-day backtest on 80 tickers, 2134 signals:
        #   BK+GU+HV+TR (breakout+gap_up+highvol+trend):  85.0% dir WR, +18.3% avg opt P&L ← S TIER
        #   BK+GD+HV+TR (breakout+gap_dn+highvol+trend):  90.0% dir WR  ← S TIER (n=10)
        #   BK+GU (breakout+gap_up):                       75.9% dir WR, +24.0% avg opt P&L ← A TIER
        #   hv_only (no gap/bk/inside) + volatile:         Sharpe 4.94, PF 2.38 ← A TIER
        #   hv + inside + volatile:                        Sharpe 3.07
        #   gap_down + hv + volatile:                      Sharpe 2.91
        #   gap_up + hv (FADE direction):                  Sharpe 2.42  ← note: fade, not continuation
        #   gap_up alone:                                  62.1% dir WR ← B TIER
        #   unfilled_gap alone:                            41.9% dir WR ← AVOID
        #   highvol in calm market (HV20 < 0.30):          PF 0.75 ← LOSING, suppress
        is_hv_only   = high_vol and not gap_flag and not inside_day and not breakout
        is_hv_inside = high_vol and inside_day
        is_hv_gapdn  = high_vol and gap_flag == "gap_down"
        is_hv_gapup  = high_vol and gap_flag == "gap_up"   # fade signal, direction is DOWN
        is_calm_hv   = high_vol and hv_regime == "calm"    # losing in backtest

        # Trend-strong: strong bodied candle (body > 70% of range), meaningful vol
        # Same definition as backtest.py detect_signals() — 85-90% WR when combined with BK+GU/GD
        _body_pct  = abs(price - open_p) / open_p if open_p > 0 else 0.0
        _range_pct = (today_high - today_low) / today_low if today_low > 0 else 0.0
        trend_strong = (_body_pct > 0.025 and
                        _body_pct / max(_range_pct, 0.001) > 0.70 and
                        rel_vol >= 1.3)

        is_bk_gu    = bool(breakout == "bull" and gap_flag == "gap_up")
        is_bk_gd    = bool(breakout == "bear" and gap_flag == "gap_down")

        if is_bk_gu and high_vol and trend_strong:
            signal_combo = "BK+GU+HV+TR"     # 85.0% dir WR — S tier
        elif is_bk_gd and high_vol and trend_strong:
            signal_combo = "BK+GD+HV+TR"     # 90.0% dir WR — S tier
        elif is_bk_gu:
            signal_combo = "BK+GU"            # 75.9% dir WR — A tier
        elif is_hv_only:
            signal_combo = "HV_PURE"
        elif is_hv_inside:
            signal_combo = "HV+ID"
        elif is_hv_gapdn:
            signal_combo = "HV+GD"
        elif is_hv_gapup:
            signal_combo = "HV+GU_FADE"
        elif high_vol and breakout:
            signal_combo = "HV+BK"
        elif breakout:
            signal_combo = "BK"
        elif inside_day:
            signal_combo = "ID"
        elif gap_flag == "gap_down":
            signal_combo = "GD"
        elif gap_flag == "gap_up":
            signal_combo = "GU"
        else:
            signal_combo = ""

        # Combo rank — tiered by backtest-validated direction accuracy
        # S: 80%+ dir WR  A: 70-79%  B: 55-69%  C: <55%  AVOID: <50% avg
        _combo_tier = {
            "BK+GU+HV+TR": "S",   # 85.0% WR
            "BK+GD+HV+TR": "S",   # 90.0% WR
            "BK+GU":        "A",   # 75.9% WR
            "HV_PURE":      "A",   # Sharpe 4.94, volatile only
            "HV+ID":        "A",   # Sharpe 3.07
            "HV+GD":        "B",   # Sharpe 2.91
            "HV+GU_FADE":   "B",   # Sharpe 2.42 (fade)
            "HV+BK":        "B",   # validated but mixed
            "BK":           "B",   # 66.7% WR
            "GU":           "B",   # 62.1% WR
            "GD":           "C",   # 53.4% WR
            "ID":           "C",   # 50.0% WR
        }
        combo_rank = _combo_tier.get(signal_combo, "C")

        # ── Setup quality 0.0–1.0 ────────────────────────────────────────────
        # Weights from backtest. Volatile regime (HV20 >= 0.35) required for full HV credit.
        sq = 0.0

        # Gap signals
        if gap_flag == "gap_down":   sq += 0.28   # PF 1.06 — slight fill edge
        if gap_flag == "gap_up":     sq += 0.10   # PF 0.95 — mostly noise; only useful as fade

        # Core signals
        if inside_day:               sq += 0.20   # PF 1.09

        # Highvol — regime-gated. Edge only exists in volatile stocks (HV20 >= 0.35).
        # Regime analysis (2800+ signals): volatile WR=55.3% PF=1.53 Sharpe=2.48
        #                                   normal   WR=41.4% PF=0.44 Sharpe=-3.58 ← LOSING
        #                                   calm     WR=41.7% PF=0.75 Sharpe=-1.65 ← LOSING
        if high_vol and hv_regime == "volatile":   sq += 0.35
        elif high_vol and hv_regime == "normal":   sq += 0.00   # no edge confirmed by backtest
        elif high_vol and hv_regime == "calm":     sq -= 0.05   # slight penalty — false signal

        # Breakout — regime-gated same way
        # volatile: WR=59.7% PF=1.33 Sharpe=1.65 | calm: WR=36% PF=0.59 Sharpe=-3.2
        if breakout and hv_regime == "volatile":   sq += 0.28
        elif breakout and hv_regime == "normal":   sq += 0.10
        elif breakout and hv_regime == "calm":     sq += 0.00

        # Inside day — INVERTED regime logic. Works in CALM stocks (coiling for breakout).
        # Backtest: calm inside WR=50% PF=2.94 Sharpe=5.44 | volatile inside PF=1.10 Sharpe=0.58
        # Override the base inside_day contribution above
        if inside_day:
            # Remove the flat +0.20 and replace with regime-aware score
            sq -= 0.20
            if hv_regime == "calm":     sq += 0.35   # best inside day setup — coiling in calm
            elif hv_regime == "normal": sq += 0.15
            else:                       sq += 0.12   # volatile inside: moderate

        # Combo bonuses — validated high-edge combinations
        if is_hv_gapdn and hv_regime == "volatile":   sq = min(1.0, sq + 0.12)  # Sharpe 3.1
        if is_hv_inside and hv_regime == "volatile":  sq = min(1.0, sq + 0.10)  # Sharpe 2.7
        if is_hv_gapup:                               sq = min(1.0, sq + 0.08)  # Sharpe 1.76 (fade)
        # New backtest-validated bonuses (60d, 2134 signals):
        if is_bk_gu:                                  sq = min(1.0, sq + 0.25)  # BK+GU 75.9% WR
        if is_bk_gu and high_vol and trend_strong:    sq = min(1.0, sq + 0.10)  # S-tier boost
        if is_bk_gd and high_vol and trend_strong:    sq = min(1.0, sq + 0.35)  # BK+GD+HV+TR 90% WR

        # Level bonuses
        if near_level and near_level["strength"] >= 5:           sq = min(1.0, sq + 0.25)
        elif near_level and near_level["strength"] >= 3:         sq = min(1.0, sq + 0.15)
        elif near_level and near_level["strength"] >= 1:         sq = min(1.0, sq + 0.05)
        if gap_flag and near_level and near_level["strength"] >= 4: sq = min(1.0, sq + 0.20)

        # Unfilled gap bonus: nearby open gap gives a clear target → better edge
        # Bonus scales by proximity: within 2% is strong edge, within 5% is moderate
        if nearest_gap:
            adist = abs(nearest_gap["dist_pct"])
            if adist <= 1.0:   sq = min(1.0, sq + 0.20)   # gap very close — high-conviction target
            elif adist <= 2.5: sq = min(1.0, sq + 0.14)
            elif adist <= 5.0: sq = min(1.0, sq + 0.07)

        # FIX 7: VWAP reclaim → setup quality boost (price reclaimed MVWAP = bullish shift)
        if vwap_reclaim:
            sq = min(1.0, sq + 0.10)

        # FIX 8: RSI-based momentum boost to setup quality
        if rsi14 < 30:
            sq = min(1.0, sq + 0.12)   # oversold — call edge
        elif rsi14 > 70:
            sq = min(1.0, sq + 0.08)   # overbought — put edge (general momentum)

        # Provisional direction — overwritten by apply_forward_directions()
        direction = "up" if change_pct >= 0 else "down"

        return {
            "ticker":        ticker,
            "price":         price,
            "change_pct":    change_pct,
            "gap_pct":       gap_pct,
            "gap_flag":      gap_flag,
            "inside_day":    inside_day,
            "breakout":      breakout,
            "rel_vol":       rel_vol,
            "high_vol":      high_vol,
            "hv20":          hv20,
            "hv_regime":     hv_regime,
            "signal_combo":  signal_combo,
            "combo_rank":    combo_rank,
            "rs_vs_spy":     rs_vs_spy,
            "today_vol":     today_vol,
            "avg_vol":       avg_vol,
            "ivr_proxy":     ivr_proxy,
            "spread_label":  spread_label,
            "opt_score":     opt_score,
            "levels":        levels,
            "near_level":    near_level,
            "unfilled_gaps": unfilled_gaps,
            "nearest_gap":   nearest_gap,
            "setup_q":       sq,
            "price_loc":     price_loc,
            "direction":     direction,
            "contract":      None,   # populated by enrich_contracts()
            "is_laggard":    False,
            "lag_pct":       0.0,
            "lag_score":     0.0,
            "lag_direction": None,
            "open_p":        open_p,
            "prior_close":   prior_close,
            "today_high":    today_high,
            "today_low":     today_low,
            "yest_high":     yest_high,
            "yest_low":      yest_low,
            "iv_rank_data":  iv_rank_data,
            "expected_move_pct": expected_move_pct,
            "rsi14":         rsi14,
            "vwap_reclaim":  vwap_reclaim,
            "above_vwap":    above_vwap,
            "mvwap":         mvwap,
        }
    except Exception:
        return None


def _get_spy_change(batch) -> float:
    """Extract SPY's day change % from the batch download. Returns 0.0 on failure."""
    try:
        spy_hist = _extract_ticker_hist(batch, "SPY")
        if spy_hist is None or len(spy_hist) < 2:
            return 0.0
        last = spy_hist.tail(2)
        prev = float(last.iloc[-2]["Close"])
        curr = float(last.iloc[-1]["Close"])
        if pd.isna(curr) and len(spy_hist) >= 3:
            # Market open — use prior two closes
            prev = float(spy_hist.iloc[-3]["Close"])
            curr = float(spy_hist.iloc[-2]["Close"])
        if prev <= 0:
            return 0.0
        return (curr - prev) / prev * 100
    except Exception:
        return 0.0


def scan_tickers(tickers: List[str], show_progress: bool = True) -> List[Dict]:
    # One batch download for all tickers — no per-ticker rate limiting
    # Always include SPY for RS vs SPY calculation
    download_tickers = tickers if "SPY" in tickers else tickers + ["SPY"]
    if show_progress:
        sys.stdout.write(
            f"  {Fore.CYAN}Downloading {len(tickers)} tickers (batch)...{Style.RESET_ALL}"
        )
        sys.stdout.flush()
    batch = _fetch_batch_history(download_tickers, period="1y")
    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    # Fetch SPY change once for RS calculation across all tickers
    spy_chg = _get_spy_change(batch)

    # Fetch live intraday prices in case today's daily bar is still incomplete (NaN Close)
    live_prices = _fetch_live_prices(tickers)

    results: List[Dict] = []
    total = len(tickers)
    for i, ticker in enumerate(tickers, 1):
        if show_progress:
            done = int(i / total * 24)
            bar  = "█" * done + "░" * (24 - done)
            sys.stdout.write(f"\r  [{bar}] {i}/{total}  {Fore.CYAN}{ticker:<6}{Style.RESET_ALL}  ")
            sys.stdout.flush()
        hist = _extract_ticker_hist(batch, ticker)
        r    = _process_ticker(ticker, hist, live_price=live_prices.get(ticker, 0.0), spy_chg=spy_chg)
        if r:
            results.append(r)
    if show_progress:
        sys.stdout.write("\r" + " " * 72 + "\r")
        sys.stdout.flush()
    return results


def enrich_contracts(results: List[Dict], top_n: int = 20, vix: float = -1.0,
                     dte_mode: str = "all") -> None:
    """
    Fetch options chains for the top-N tickers by setup quality.
    Populates result['contract'] in place. VIX adjusts delta target.
    dte_mode: "0dte" | "weekly" | "monthly" | "all"
    """
    ranked = sorted(
        [r for r in results if r["gap_flag"] or r["inside_day"] or r["high_vol"] or r.get("breakout")],
        key=lambda r: r["setup_q"],
        reverse=True,
    )[:top_n]

    if not ranked:
        print(f"  {Fore.YELLOW}No setups to enrich.{Style.RESET_ALL}")
        return

    print(f"  {Fore.CYAN}Fetching options chains for {len(ranked)} setups...{Style.RESET_ALL}")
    for i, r in enumerate(ranked, 1):
        sys.stdout.write(f"\r  Options [{i}/{len(ranked)}] {Fore.CYAN}{r['ticker']:<6}{Style.RESET_ALL}  ")
        sys.stdout.flush()
        # Derive target price: unfilled gap → heavy level → ATH proxy (none)
        # Unfilled gap: use mid if the gap is in the direction of the trade
        _tgt = 0.0
        ng = r.get("nearest_gap")
        nl = r.get("near_level")
        if ng:
            dtf = ng.get("direction_to_fill", "")
            if (r["direction"] == "up" and dtf == "up") or (r["direction"] == "down" and dtf == "down"):
                _tgt = float(ng.get("mid", 0) or 0)
        if not _tgt and nl and nl.get("strength", 0) >= 3:
            _tgt = float(nl.get("price", 0) or 0)
        r["contract"] = get_best_contract(r["ticker"], r["direction"], r["price"], vix=vix,
                                          dte_mode=dte_mode, target_price=_tgt)

        # FIX 9: IV vs HV comparison — fetch near-ATM call IVs to compute mean_iv
        # and compare against realized HV20. Adjusts opt_score in place.
        try:
            _t = _yf(r["ticker"])
            _exps = _t.options
            if _exps:
                _today = datetime.now().date()
                _near_exp = None
                for _e in _exps:
                    _d = (datetime.strptime(_e, "%Y-%m-%d").date() - _today).days
                    if _d >= 0:
                        _near_exp = _e
                        break
                if _near_exp:
                    _chain = _option_chain(_t, r["ticker"], _near_exp)
                    _calls = _chain.calls
                    _px = r["price"]
                    _atm = _calls[
                        (_calls["strike"] >= _px * 0.97) & (_calls["strike"] <= _px * 1.03)
                    ]
                    if not _atm.empty:
                        _ivs = pd.to_numeric(_atm["impliedVolatility"], errors="coerce").dropna()
                        if not _ivs.empty:
                            mean_iv = float(_ivs.mean())
                            hv20 = r.get("hv20", 0)
                            if hv20 > 0 and mean_iv > 0:
                                iv_vs_hv = mean_iv / hv20
                                _opt = r["opt_score"] / 100.0
                                if iv_vs_hv < 0.8:
                                    _opt = min(1.0, _opt + 0.15)   # options cheap vs realized vol
                                elif iv_vs_hv > 1.3:
                                    _opt = max(0.0, _opt - 0.10)   # options expensive
                                r["opt_score"] = int(round(_opt * 100))
                                r["iv_vs_hv"]  = round(iv_vs_hv, 3)
                                r["mean_iv"]   = round(mean_iv, 4)
        except Exception:
            pass

        time.sleep(0.15)
    sys.stdout.write("\r" + " " * 55 + "\r")
    sys.stdout.flush()

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


def apply_filter(results: List[Dict], f: str) -> List[Dict]:
    if f == "gap":      return [r for r in results if r["gap_flag"]]
    if f == "inside":   return [r for r in results if r["inside_day"]]
    if f == "highvol":  return [r for r in results if r["high_vol"]]
    if f == "breakout": return [r for r in results if r.get("breakout")]
    if f == "options":  return [r for r in results if r["opt_score"] >= 60]
    if f == "any":     return [r for r in results if (
        r["gap_flag"] or r["inside_day"] or r["high_vol"] or r.get("breakout") or
        r["opt_score"] >= 60 or
        (r.get("near_level") and r["near_level"]["strength"] >= 3)
    )]
    if f == "laggard": return [r for r in results if r.get("is_laggard")]
    if f == "a_grade":
        return [r for r in results
                if r["setup_q"] * 50 + r["opt_score"] * 0.30 + (20 if r["contract"] else 0) >= 75]
    return results


def apply_sort(results: List[Dict], s: str) -> List[Dict]:
    keys: Dict = {
        "setup":   lambda r: r["setup_q"],
        "options": lambda r: r["opt_score"],
        "relvol":  lambda r: r["rel_vol"],
        "gap":     lambda r: abs(r["gap_pct"]),
        "change":  lambda r: abs(r["change_pct"]),
        "lag":     lambda r: r.get("lag_score", 0.0),
        # EV is the right ranking key, but only when a calibrated model exists.
        # Uncalibrated rows carry expected_value=None, so asking for --sort ev
        # without one silently falls back to setup quality rather than ordering
        # every row by a fabricated zero.
        "ev":      lambda r: (r.get("expected_value") if r.get("expected_value")
                              is not None else r["setup_q"] - 1e6),
    }
    if s == "ev" and not any(r.get("expected_value") is not None for r in results):
        s = "setup"
    return sorted(results, key=keys.get(s, keys["setup"]), reverse=True)


# ─── Calibrated probability / expected value ─────────────────────────────────
def annotate_calibration(results: List[Dict]) -> List[Dict]:
    """
    Attach win_prob / expected_value / calibration to each scan result.

    Deliberately non-fatal: if the calibration module or its released model is
    missing, every row is marked "uncalibrated" with None probabilities and the
    order is left alone. The scanner must keep working — and keep telling the
    truth about what it does not know — with no model on disk, which is the
    state it is in today.
    """
    try:
        from core import calibration
    except Exception:
        for r in results:
            r.setdefault("win_prob", None)
            r.setdefault("expected_value", None)
            r.setdefault("calibration", "uncalibrated")
        return results

    model = calibration.load_model()
    payoffs = None
    if model is not None:
        try:
            with open(calibration.MODEL_PATH) as fh:
                payoffs = json.load(fh).get("payoffs")
        except Exception:
            payoffs = None
        payoffs = tuple(payoffs) if payoffs else None
    return calibration.annotate(results, model, payoffs)

# ─── Table Rendering ──────────────────────────────────────────────────────────
def _gap_fill_pct(r: Dict) -> str:
    """Return gap fill % as a string (e.g. '67%') or '' if trivial / uncomputable."""
    open_p = r.get("open_p", 0)
    pc     = r.get("prior_close", 0)
    if not open_p or not pc or abs(open_p - pc) < 0.01:
        return ""
    if r.get("gap_flag") == "gap_up" and open_p > pc:
        tl  = r.get("today_low", r["price"])
        pct = min(100, max(0, (open_p - tl) / (open_p - pc) * 100))
    elif r.get("gap_flag") == "gap_down" and open_p < pc:
        th  = r.get("today_high", r["price"])
        pct = min(100, max(0, (th - open_p) / (pc - open_p) * 100))
    else:
        return ""
    return f"{pct:.0f}%" if pct >= 5 else ""


def build_setups(r: Dict) -> str:
    b = []

    # ── Signal combo badge (backtest-graded) ─────────────────────────────────
    combo = r.get("signal_combo", "")
    regime = r.get("hv_regime", "")
    if combo == "HV_PURE":
        # Pure HV in volatile name — Sharpe 4.94, best setup
        c = Fore.GREEN + Style.BRIGHT if regime == "volatile" else Fore.GREEN
        b.append(c + "HV★" + Style.RESET_ALL)
    elif combo == "HV+ID":
        b.append(Fore.GREEN + Style.BRIGHT + "HV+ID" + Style.RESET_ALL)
    elif combo == "HV+GD":
        b.append(Fore.GREEN + "HV+G↓" + Style.RESET_ALL)
    elif combo == "HV+GU_FADE":
        b.append(Fore.RED + Style.BRIGHT + "HV+G↑FADE" + Style.RESET_ALL)
    elif combo == "HV+BK":
        b.append(Fore.CYAN + "HV+BK" + Style.RESET_ALL)
    else:
        # Individual flags when no special combo
        if r["gap_flag"] == "gap_up":
            fill = _gap_fill_pct(r)
            b.append(Fore.YELLOW + f"G+{fill}" + Style.RESET_ALL)
        if r["gap_flag"] == "gap_down":
            fill = _gap_fill_pct(r)
            b.append(Fore.CYAN + f"G-{fill}" + Style.RESET_ALL)
        if r["inside_day"]:
            b.append(Fore.MAGENTA + Style.BRIGHT + "ID" + Style.RESET_ALL)
        if r["high_vol"]:
            hv_c = Fore.GREEN if regime == "volatile" else (Fore.YELLOW if regime == "normal" else Fore.WHITE)
            b.append(hv_c + "HV" + Style.RESET_ALL)
        bk = r.get("breakout")
        if bk == "bull": b.append(Fore.CYAN + "BK↑" + Style.RESET_ALL)
        elif bk == "bear": b.append(Fore.RED + "BK↓" + Style.RESET_ALL)

    # HV regime warning — calm HV = no edge
    if r.get("high_vol") and regime == "calm":
        b.append(Fore.WHITE + Style.DIM + "[calm]" + Style.RESET_ALL)

    # Level strength
    nl = r.get("near_level")
    if nl and nl["strength"] >= 5:   b.append(Fore.RED + Style.BRIGHT + "**" + Style.RESET_ALL)
    elif nl and nl["strength"] >= 3: b.append(Fore.RED + "*" + Style.RESET_ALL)
    elif nl:                         b.append(Fore.WHITE + "L" + Style.RESET_ALL)

    if r.get("is_laggard"):
        lag_c = Fore.CYAN if r.get("lag_direction") == "up" else Fore.YELLOW
        b.append(lag_c + f"LAG{r['lag_pct']:+.0f}%" + Style.RESET_ALL)

    rs = r.get("rs_vs_spy", 0.0)
    if rs >= 2.0:    b.append(Fore.GREEN  + f"RS+{rs:.1f}" + Style.RESET_ALL)
    elif rs <= -2.0: b.append(Fore.YELLOW + f"RS{rs:.1f}"  + Style.RESET_ALL)

    return " ".join(b) if b else "—"


def render_table(
    results: List[Dict],
    sort_by: str = "setup",
    filter_by: str = "any",
) -> Tuple[str, List[Dict]]:
    filtered = apply_filter(results, filter_by)
    # Annotate before sorting so --sort ev has something to sort on. With no
    # released model this is a no-op that stamps every row "uncalibrated".
    annotate_calibration(filtered)
    ordered  = apply_sort(filtered, sort_by)

    rows = []
    for i, r in enumerate(ordered, 1):
        g = trade_grade(r["setup_q"], r["opt_score"], bool(r["contract"]))
        rows.append([
            f"{i:2d}",
            Fore.WHITE + Style.BRIGHT + f"{r['ticker']:<5}" + Style.RESET_ALL,
            f"${r['price']:.2f}",
            color_change(r["change_pct"]),
            f"{r['rel_vol']:.2f}x",
            build_setups(r),
            level_str(r.get("near_level")),
            fmt_contract(r["contract"]),
            f"{r['opt_score']:3d}",
            g,
        ])

    headers = [
        "#", "TICKER", "PRICE", "CHG%", "RVOL",
        "FLAG", "NEAREST LVL", "CONTRACT", "SCR", "GRD",
    ]
    return tabulate(rows, headers=headers, tablefmt="simple"), ordered


def print_summary(results: List[Dict], vix: float = -1.0) -> None:
    gap_up    = sum(1 for r in results if r["gap_flag"] == "gap_up")
    gap_down  = sum(1 for r in results if r["gap_flag"] == "gap_down")
    inside    = sum(1 for r in results if r["inside_day"])
    hvol      = sum(1 for r in results if r["high_vol"])
    bkouts    = sum(1 for r in results if r.get("breakout"))
    opt60     = sum(1 for r in results if r["opt_score"] >= 60)
    near_lvl  = sum(1 for r in results if r.get("near_level") and r["near_level"]["strength"] >= 3)
    contracts = sum(1 for r in results if r["contract"])
    laggards  = sum(1 for r in results if r.get("is_laggard"))
    ts        = datetime.now().strftime("%Y-%m-%d %H:%M")
    sep       = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL

    # VIX label + regime
    if vix > 0:
        if vix >= 35:   vix_c, vix_regime = Fore.RED + Style.BRIGHT, "EXTREME FEAR"
        elif vix >= 25: vix_c, vix_regime = Fore.RED,                "ELEVATED"
        elif vix >= 18: vix_c, vix_regime = Fore.YELLOW,             "CAUTIOUS"
        elif vix >= 13: vix_c, vix_regime = Fore.GREEN,              "NORMAL"
        else:           vix_c, vix_regime = Fore.GREEN + Style.BRIGHT,"LOW/COMPLACENT"
        vix_str = f"  |  VIX {vix_c}{vix:.2f} [{vix_regime}]{Style.RESET_ALL}"
        tdelta_str = f"  |  δ-target {Fore.WHITE}{vix_delta_target(vix):.2f}{Style.RESET_ALL}"
    else:
        vix_str = ""
        tdelta_str = ""

    print(sep)
    print(Fore.WHITE + Style.BRIGHT
          + f"  ELITE SCANNER  {ts}  |  {len(results)} tickers scanned"
          + Style.RESET_ALL
          + vix_str + tdelta_str)
    print(
        "  "
        + Fore.CYAN    + f"Gap+ {gap_up}  "
        + Fore.YELLOW  + f"Gap- {gap_down}  "
        + Fore.MAGENTA + f"Inside {inside}  "
        + Fore.GREEN   + f"Hi-Vol {hvol}  "
        + Fore.GREEN   + Style.BRIGHT + f"Breakout {bkouts}  " + Style.RESET_ALL
        + "  "
        + Fore.WHITE   + f"Opt>=60 {opt60}  "
        + Fore.RED     + f"Near Level {near_lvl}  "
        + Fore.CYAN    + f"Contracts {contracts}  "
        + Fore.MAGENTA + f"Laggards {laggards}"
        + Style.RESET_ALL
    )
    print(sep)

# ─── CSV Export ───────────────────────────────────────────────────────────────
_BASE_FIELDS = [
    "ticker", "price", "change_pct", "gap_pct", "gap_flag", "inside_day",
    "rel_vol", "high_vol", "today_vol", "avg_vol", "ivr_proxy",
    "spread_label", "opt_score", "setup_q", "direction",
    "is_laggard", "lag_pct", "lag_score", "lag_direction",
]


def export_csv(results: List[Dict], filename: str = "scan_results.csv") -> None:
    extra = ["lvl_price", "lvl_strength", "lvl_type",
             "contract_exp", "contract_strike", "contract_type",
             "contract_delta", "contract_oi", "contract_vol", "contract_mid"]
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_BASE_FIELDS + extra)
        writer.writeheader()
        for r in results:
            row = {k: r[k] for k in _BASE_FIELDS}
            nl = r.get("near_level")
            row["lvl_price"]     = nl["price"]    if nl else ""
            row["lvl_strength"]  = nl["strength"] if nl else ""
            row["lvl_type"]      = nl["type"]     if nl else ""
            c = r.get("contract")
            row["contract_exp"]    = c["exp"]              if c else ""
            row["contract_strike"] = c["strike"]           if c else ""
            row["contract_type"]   = c["type"]             if c else ""
            row["contract_delta"]  = f"{c['delta']:.3f}"  if c else ""
            row["contract_oi"]     = c["oi"]               if c else ""
            row["contract_vol"]    = c["vol"]              if c else ""
            row["contract_mid"]    = f"{c['mid']:.2f}"    if c else ""
            writer.writerow(row)
    print(Fore.GREEN + f"\n  Exported → {filename}" + Style.RESET_ALL)


# ─── Signal journal ───────────────────────────────────────────────────────────
# The scan prints its ranking and then forgets it. Recording each emitted signal
# is what makes "we tailed these plays — did they work?" answerable later. The
# write is deliberately best-effort: a scanner that dies at the open because the
# disk is full is strictly worse than one that drops a log line.

def record_scan_signals(results: List[Dict], params: Dict,
                        run_id: Optional[str] = None,
                        journal=None, scan_kind: str = "scan",
                        verbose: bool = True) -> Optional[str]:
    """Persist a scan's signals. Returns the run id, or None if nothing was
    written. Never raises — every failure path prints and returns None."""
    try:
        from data.signal_journal import SignalJournal, new_run_id
        j = journal or SignalJournal()
        rid = run_id or new_run_id()
        j.start_run(rid, scan_kind, params)
        n = j.record_signals(
            rid, results,
            grade_fn=lambda r: grade_letter(r.get("setup_q", 0.0),
                                            r.get("opt_score", 0),
                                            bool(r.get("contract"))),
        )
        if verbose:
            print(f"  {Fore.CYAN}Journaled {n} signals → run {rid}{Style.RESET_ALL}")
        return rid
    except Exception as e:
        # Non-fatal by design. Say so loudly enough to notice, then carry on.
        print(f"  {Fore.YELLOW}Signal journal write failed ({e}) — scan continues."
              f"{Style.RESET_ALL}")
        return None


def print_signal_history(start: Optional[str] = None, end: Optional[str] = None,
                         symbol: Optional[str] = None, grade: Optional[str] = None,
                         limit: int = 50, journal=None) -> None:
    """Dump recorded signal history as a table. The read side of the journal."""
    try:
        from data.signal_journal import SignalJournal
        j = journal or SignalJournal()
        rows = j.query(start=start, end=end, symbol=symbol, grade=grade, limit=limit)
    except Exception as e:
        print(Fore.RED + f"  Could not read the signal journal: {e}" + Style.RESET_ALL)
        return

    if not rows:
        print(f"  {Fore.YELLOW}No signals recorded for that query.{Style.RESET_ALL}")
        return

    table = []
    for r in rows:
        if r["contract_expiry"]:
            ctype = "C" if r["contract_type"] == "call" else "P"
            contract = f"{r['contract_expiry'][5:]} ${r['contract_strike']:.0f}{ctype}"
            quote = f"{r['contract_bid'] or 0:.2f}/{r['contract_ask'] or 0:.2f}"
        else:
            contract, quote = "—", "—"
        table.append([
            r["emitted_at"][:19], r["symbol"], r["direction"], r["grade"] or "—",
            f"{r['setup_q']:.2f}" if r["setup_q"] is not None else "—",
            r["opt_score"] if r["opt_score"] is not None else "—",
            r["whale_score"] if r["whale_score"] is not None else "—",
            f"${r['underlying_px']:.2f}" if r["underlying_px"] is not None else "—",
            contract, quote, r["run_id"],
        ])
    print()
    print(tabulate(table,
                   headers=["EMITTED (UTC)", "SYM", "DIR", "GR", "SETUP",
                            "OPT", "WHALE", "PX", "CONTRACT", "BID/ASK", "RUN"],
                   tablefmt="simple"))
    print(f"\n  {len(rows)} signal(s).\n")

_TIER_COLORS = {
    "whale":         Fore.RED   + Style.BRIGHT,
    "block":         Fore.YELLOW + Style.BRIGHT,
    "institutional": Fore.CYAN,
    "retail":        Fore.WHITE,
}


def fmt_bias_bar(call_flow: float, put_flow: float, width: int = 10) -> str:
    """Visual call/put split bar.  Cyan = calls, Yellow = puts."""
    total  = call_flow + put_flow
    if total <= 0:
        return Fore.WHITE + "─" * width + Style.RESET_ALL
    call_w = round(call_flow / total * width)
    put_w  = width - call_w
    return Fore.CYAN + "█" * call_w + Fore.YELLOW + "█" * put_w + Style.RESET_ALL


def fmt_voi(voi: float) -> str:
    """Color-coded vol/OI ratio string."""
    if voi >= 20:   c = Fore.RED   + Style.BRIGHT
    elif voi >= 10: c = Fore.RED
    elif voi >= 5:  c = Fore.YELLOW + Style.BRIGHT
    elif voi >= 2:  c = Fore.YELLOW
    else:           c = Fore.WHITE
    return f"{c}x{voi:.1f}{Style.RESET_ALL}"


def print_inline_inside_days(results: List[Dict]) -> None:
    """Inside day alert with exact prev-high/low breakout watch levels."""
    ids = [r for r in results if r["inside_day"]]
    if not ids:
        return
    sep = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL
    print(sep)
    print(
        Fore.MAGENTA + Style.BRIGHT + "  INSIDE DAY" + Style.RESET_ALL
        + Fore.WHITE + "  (coiling — wait for range break)" + Style.RESET_ALL
    )
    for r in ids:
        yh = r.get("yest_high", 0)
        yl = r.get("yest_low",  0)
        if yh > 0 and yl > 0:
            watch = (
                "  Watch: "
                + Fore.CYAN  + f"↑${yh:.2f} → CALLS" + Style.RESET_ALL
                + "  |  "
                + Fore.YELLOW + f"↓${yl:.2f} → PUTS"  + Style.RESET_ALL
            )
        else:
            watch = ""
        print(
            f"  {Fore.MAGENTA + Style.BRIGHT}{r['ticker']:<6}{Style.RESET_ALL}"
            f"  ${r['price']:.2f}  {color_change(r['change_pct'])}"
            + watch
            + f"    {fmt_contract(r.get('contract'))}"
        )


def print_inline_laggards(results: List[Dict], sector_data: Dict[str, Dict], top_n: int = 5) -> None:
    """Compact laggard section — top N by lag score, one line each."""
    lags = sorted(
        [r for r in results if r.get("is_laggard")],
        key=lambda r: r.get("lag_score", 0),
        reverse=True,
    )[:top_n]
    if not lags:
        return
    sep = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL
    print(sep)
    print(Fore.CYAN + Style.BRIGHT + "  LAGGARDS" + Style.RESET_ALL
          + Fore.WHITE + "  (sector catch-up plays)" + Style.RESET_ALL)
    rows = []
    for r in lags:
        sname   = TICKER_SECTOR.get(r["ticker"], "?")
        sd      = sector_data.get(sname, {})
        sec_chg = sd.get("change_pct", 0)
        sec_s   = (Fore.GREEN + f"+{sec_chg:.2f}%" + Style.RESET_ALL if sec_chg >= 0
                   else Fore.RED + f"{sec_chg:.2f}%" + Style.RESET_ALL)
        lag_c   = Fore.CYAN if r.get("lag_direction") == "up" else Fore.YELLOW
        play    = lag_c + ("↑ CALLS" if r.get("lag_direction") == "up" else "↓ PUTS") + Style.RESET_ALL
        rows.append([
            Fore.WHITE + Style.BRIGHT + r["ticker"] + Style.RESET_ALL,
            f"${r['price']:.2f}",
            color_change(r["change_pct"]),
            sec_s,
            sname,
            lag_c + f"{r['lag_pct']:+.1f}%" + Style.RESET_ALL,
            play,
            fmt_contract(r.get("contract")),
        ])
    print(tabulate(rows,
                   headers=["TICKER", "PRICE", "CHG%", "SECTOR", "SECTOR NAME", "LAG", "PLAY", "CONTRACT"],
                   tablefmt="simple"))


def merge_whale_scores(results: List[Dict], flow_signals: List[Dict]) -> None:
    """Inject whale scores from flow scan back into main results so whaled tickers rank higher."""
    whale_map = {f["ticker"]: f.get("whale_score", 0) for f in flow_signals}
    for r in results:
        ws = whale_map.get(r["ticker"], 0)
        # Keep the raw score on the result. Previously it was consumed here and
        # discarded, which meant the whale read that pushed a ticker up the
        # rankings was invisible to anything downstream — including the journal.
        r["whale_score"] = ws
        if ws >= 60:
            r["opt_score"] = min(100, r.get("opt_score", 0) + 15)
            r["setup_q"]   = min(1.0, r.get("setup_q",   0) + 0.12)
        elif ws >= 40:
            r["opt_score"] = min(100, r.get("opt_score", 0) + 8)
            r["setup_q"]   = min(1.0, r.get("setup_q",   0) + 0.06)


def print_unusual_flow(flow_signals: List[Dict], top_n: int = 10) -> None:
    """Unified unusual flow — non-retail only, sorted by whale score. 0DTE/weekly odd flow surfaces automatically."""
    if not flow_signals:
        return

    # Only show signals with real institutional conviction — filter retail noise
    unusual = [f for f in flow_signals if f.get("whale_score", 0) >= 40 or
               f.get("premium_tier", "retail") in ("block", "whale")]
    if not unusual:
        return

    unusual.sort(key=lambda f: (f.get("whale_score", 0), f.get("total_flow", 0)), reverse=True)

    sep = Fore.WHITE + Style.BRIGHT + "─" * 100 + Style.RESET_ALL
    print(sep)

    net_calls = sum(f["call_flow"] for f in flow_signals)
    net_puts  = sum(f["put_flow"]  for f in flow_signals)
    net_total = net_calls + net_puts
    call_pct  = net_calls / net_total * 100 if net_total > 0 else 50
    net_bar   = fmt_bias_bar(net_calls, net_puts, width=12)
    net_c     = Fore.CYAN if net_calls >= net_puts else Fore.YELLOW
    net_dir   = net_c + ("CALL HEAVY" if net_calls >= net_puts else "PUT HEAVY") + Style.RESET_ALL
    print(
        Fore.WHITE + Style.BRIGHT + "  UNUSUAL FLOW  " + Style.RESET_ALL
        + f"[{net_bar}] {call_pct:.0f}% calls  {net_dir}"
        + f"  |  {fmt_flow(net_total)} total  {len(unusual)} unusual signals"
    )

    rows = []
    for f in unusual[:top_n]:
        tc     = f.get("top_contract")
        voi    = tc.get("vol_oi", 0) if tc else 0
        dte    = tc.get("dte", 0) if tc else 0
        ws     = f.get("whale_score", 0)

        # DTE label — highlight short-dated unusual flow
        if dte == 0:    dte_s = Fore.RED + Style.BRIGHT + "0DTE" + Style.RESET_ALL
        elif dte <= 7:  dte_s = Fore.RED + f"{dte}DTE" + Style.RESET_ALL
        elif dte <= 30: dte_s = Fore.YELLOW + f"{dte}DTE" + Style.RESET_ALL
        else:           dte_s = Fore.WHITE + f"{dte}DTE" + Style.RESET_ALL

        bias_c = Fore.CYAN if f["flow_bias"] == "call" else Fore.YELLOW
        bias_s = bias_c + f["flow_bias"].upper() + Style.RESET_ALL
        tier_c = _TIER_COLORS.get(f.get("premium_tier", "retail"), Fore.WHITE)

        flag = ""
        if f.get("golden_sweep"):         flag = Fore.RED + Style.BRIGHT + "★GOLDEN" + Style.RESET_ALL
        elif tc and tc.get("sweep"):      flag = Fore.YELLOW + "SWEEP" + Style.RESET_ALL
        elif voi >= 20:                   flag = Fore.RED + f"x{voi:.0f}VOI" + Style.RESET_ALL
        elif voi >= 10:                   flag = Fore.YELLOW + f"x{voi:.0f}VOI" + Style.RESET_ALL

        contract_s = fmt_flow_contract(tc) if tc else "—"
        rows.append([
            fmt_whale_score(ws),
            Fore.WHITE + Style.BRIGHT + f["ticker"] + Style.RESET_ALL,
            fmt_flow(f["total_flow"]),
            bias_s,
            dte_s,
            tier_c + f.get("premium_tier", "retail").upper() + Style.RESET_ALL,
            flag or "—",
            contract_s,
        ])
    print(tabulate(rows,
                   headers=["WHALE", "TICKER", "$FLOW", "BIAS", "DTE", "TIER", "FLAG", "CONTRACT"],
                   tablefmt="simple"))
    print(sep)


# keep these for backward compat / direct calls
def print_inline_flow(flow_signals: List[Dict], top_n: int = 7) -> None:
    print_unusual_flow(flow_signals, top_n=top_n)


def print_hot_contracts(flow_signals: List[Dict], top_n: int = 14) -> None:
    print_unusual_flow(flow_signals, top_n=top_n)


# ─── Interactive Menu ─────────────────────────────────────────────────────────
def interactive_loop(results: List[Dict], args: argparse.Namespace,
                     sector_data: Dict[str, Dict], vix: float = -1.0,
                     flow_cache: Optional[List[Dict]] = None) -> None:
    sort_by    = args.sort   or "setup"
    filter_by  = args.filter or "all"
    tickers    = [r["ticker"] for r in results]
    laggards   = find_sector_laggards(results, sector_data)
    flow_cache = list(flow_cache) if flow_cache else []

    while True:
        os.system("clear" if os.name == "posix" else "cls")
        print_sector_heatmap(sector_data)
        print_summary(results, vix=vix)

        # ── Inline priority sections ──────────────────────────────────────────
        print_inline_inside_days(results)
        print_inline_laggards(results, sector_data, top_n=5)
        if flow_cache:
            print_inline_flow(flow_cache, top_n=6)

        sep = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL
        print(sep)
        print(
            f"  Filter: {Fore.CYAN}{FILTER_LABELS[filter_by]}{Style.RESET_ALL}"
            f"  |  Sort: {Fore.CYAN}{SORT_LABELS[sort_by]}{Style.RESET_ALL}\n"
        )
        table, _ = render_table(results, sort_by=sort_by, filter_by=filter_by)
        print(table)
        print(Fore.WHITE
              + "\n  FILTER  [1]All  [2]Gap  [3]Inside  [4]Hi-Vol  [5]Opt>=60"
                "  [6]Any Setup  [7]Grade A  [8]Laggards"
              + Style.RESET_ALL)
        print(Fore.WHITE
              + "  SORT    [s1]Setup  [s2]OptScore  [s3]RelVol"
                "  [s4]Gap%  [s5]Change%  [s6]LagScore"
              + Style.RESET_ALL)
        flow_note = (f"  {Fore.GREEN}flow:{len(flow_cache)} signals{Style.RESET_ALL}"
                     if flow_cache else
                     f"  {Fore.YELLOW}[e] to load flow{Style.RESET_ALL}")
        print(Fore.WHITE
              + "  [r]Rescan  [rs]Sectors  [e]Enrich+Flow"
                "  [l]Laggards  [f]Flow  [c]CSV  [q]Quit"
              + Style.RESET_ALL + flow_note)

        try:
            cmd = input("\n  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Bye.")
            break

        if cmd == "q":
            print("  Bye.")
            break
        elif cmd == "rs":
            sector_data = scan_sectors()
            laggards = find_sector_laggards(results, sector_data)
        elif cmd == "r":
            print(f"\n  Rescanning sectors + {len(tickers)} tickers...")
            vix = fetch_vix()
            sector_data = scan_sectors()
            results = scan_tickers(tickers)
            apply_forward_directions(results, sector_data)
            laggards = find_sector_laggards(results, sector_data)
            enrich_contracts(results, top_n=getattr(args, "enrich_top", 15), vix=vix)
            flow_tickers = [r["ticker"] for r in
                            sorted(results, key=lambda x: x["opt_score"], reverse=True)[:15]]
            print(f"  Scanning options flow for {len(flow_tickers)} tickers...")
            flow_cache = scan_options_flow(flow_tickers, show_progress=True)
        elif cmd == "e":
            enrich_contracts(results, top_n=getattr(args, "enrich_top", 15), vix=vix)
            flow_tickers = [r["ticker"] for r in
                            sorted(results, key=lambda x: x["opt_score"], reverse=True)[:15]]
            print(f"  Scanning options flow for {len(flow_tickers)} tickers...")
            flow_cache = scan_options_flow(flow_tickers, show_progress=True)
        elif cmd == "l":
            os.system("clear" if os.name == "posix" else "cls")
            print_sector_laggards(laggards, sector_data)
            input("\n  (press enter to continue)")
        elif cmd == "f":
            os.system("clear" if os.name == "posix" else "cls")
            flow_tickers = [r["ticker"] for r in
                            sorted(results, key=lambda x: x["opt_score"], reverse=True)[:20]]
            print(f"\n  Scanning options flow for {len(flow_tickers)} tickers...")
            flow_cache = scan_options_flow(flow_tickers, show_progress=True)
            merge_whale_scores(results, flow_cache)
            print_unusual_flow(flow_cache, top_n=10)
            input("\n  (press enter to continue)")
        elif cmd == "c":
            export_csv(results)
            input("  (press enter)")
        elif cmd in FILTER_MAP:
            filter_by = FILTER_MAP[cmd]
        elif cmd in SORT_MAP:
            sort_by = SORT_MAP[cmd]

# ─── Entry Point ──────────────────────────────────────────────────────────────
def load_tv_universe(path: str, cap: int = TV_DEFAULT_CAP):
    """
    Load a TradingView screener export as the universe, reporting what it found.

    Exits on an unreadable or unusable file rather than falling back to the
    built-in universe: the user asked for *his* list, and quietly scanning a
    different one is worse than telling him the file did not work.
    """
    try:
        tv = load_tradingview_csv(path, cap=cap if cap and cap > 0 else None)
    except OSError as exc:
        print(Fore.RED + f"  Could not read TradingView CSV: {exc}" + Style.RESET_ALL)
        sys.exit(1)

    if not tv.symbols:
        print(Fore.RED + f"  No symbols found in {path}." + Style.RESET_ALL)
        print(Fore.YELLOW + "  Expected a TradingView screener export with a "
                            "'Symbol' or 'Ticker' column." + Style.RESET_ALL)
        sys.exit(1)

    print(f"  {Fore.CYAN}TradingView universe:{Style.RESET_ALL} {tv.summary()}")
    for line in tv.detail_lines():
        print(f"    {Fore.YELLOW}{line}{Style.RESET_ALL}")
    return tv


def print_tv_drops(tv, requested: List[str], survived: List[str], reason: str) -> None:
    """Print a drop report if the TradingView universe lost symbols. No-op otherwise."""
    if tv is None:
        return
    note = tv_drop_report(requested, survived, reason)
    if note:
        print(f"  {Fore.YELLOW}{note}{Style.RESET_ALL}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Elite market scanner — gap fills, key levels, options contracts."
    )
    parser.add_argument("--tickers",    nargs="+", metavar="TICKER",
                        help="Specific tickers to scan")
    parser.add_argument("--watchlist",  metavar="FILE",
                        help="Text file with one ticker per line")
    parser.add_argument("--tradingview", "--tv", metavar="FILE", dest="tradingview",
                        help="TradingView screener CSV export — use its symbols, "
                             "in its order, as the universe")
    parser.add_argument("--tv-limit",   type=int, default=TV_DEFAULT_CAP, metavar="N",
                        help=f"Max symbols to take from a TradingView export, from the "
                             f"top of its ordering (default: {TV_DEFAULT_CAP}, 0 = no cap)")
    parser.add_argument("--csv",        action="store_true",
                        help="Print table, export CSV, and exit")
    parser.add_argument("--no-enrich",  action="store_true",
                        help="Skip options chain fetching (faster)")
    parser.add_argument("--enrich-top", type=int, default=20,
                        help="Enrich top N setups with contract data (default: 20)")
    parser.add_argument("--filter",     choices=list(FILTER_LABELS.keys()), default="all",
                        help="Initial filter (default: all)")
    parser.add_argument("--sort",       choices=list(SORT_LABELS.keys()),   default="setup",
                        help="Initial sort (default: setup)")
    parser.add_argument("--dynamic",    action="store_true",
                        help="Add today's movers from extended watchlist to find sleepers")
    parser.add_argument("--live",       action="store_true",
                        help="Launch the live FlowDeck terminal dashboard")
    parser.add_argument("--interval",   type=int, default=45,
                        help="Live refresh interval in seconds (default: 45)")
    # Signal-journal inspection. Expressed as flags rather than an argparse
    # subparser because this CLI has always been flat — adding subcommands now
    # would break every existing `scanner.py --tickers ...` invocation.
    parser.add_argument("--signals",       action="store_true",
                        help="Dump recorded signal history and exit")
    parser.add_argument("--signals-since", metavar="ISO",
                        help="Only signals emitted on/after this UTC date or timestamp")
    parser.add_argument("--signals-until", metavar="ISO",
                        help="Only signals emitted on/before this UTC date or timestamp")
    parser.add_argument("--signals-symbol", metavar="TICKER",
                        help="Only signals for this symbol")
    parser.add_argument("--signals-grade", metavar="GRADE",
                        choices=["A", "B", "C", "D"],
                        help="Only signals with this letter grade")
    parser.add_argument("--signals-limit", type=int, default=50,
                        help="Max signal rows to print (default: 50)")
    parser.add_argument("--no-journal",    action="store_true",
                        help="Do not record this scan's signals to the journal")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Reading history touches no market data — answer it before any network work.
    if getattr(args, "signals", False):
        print_signal_history(start=args.signals_since, end=args.signals_until,
                             symbol=args.signals_symbol, grade=args.signals_grade,
                             limit=args.signals_limit)
        return

    if getattr(args, "live", False):
        from core.live_flow import run as run_live
        run_live(interval=args.interval,
                 top=getattr(args, "enrich_top", 30) or 30,
                 min_score=35)
        return

    # Held across the scan so that, when the universe came from TradingView, we
    # can account for every symbol the user handed us instead of just showing
    # him a shorter table than the list he exported.
    tv_universe = None

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif getattr(args, "tradingview", None):
        tv_universe = load_tv_universe(args.tradingview, cap=getattr(args, "tv_limit", TV_DEFAULT_CAP))
        tickers = tv_universe.symbols
    elif args.watchlist:
        try:
            with open(args.watchlist) as f:
                tickers = [ln.strip().upper() for ln in f if ln.strip()]
        except FileNotFoundError:
            print(Fore.RED + f"  File not found: {args.watchlist}" + Style.RESET_ALL)
            sys.exit(1)
    else:
        print(f"  {Fore.CYAN}Building live universe...{Style.RESET_ALL}", end="", flush=True)
        tickers = get_universe()
        sys.stdout.write(f"\r  {universe_summary()}\n")
        sys.stdout.flush()

    # Step 0: fetch VIX — sets IV regime for contract selection
    print(f"  {Fore.CYAN}Fetching VIX...{Style.RESET_ALL}", end="", flush=True)
    vix = fetch_vix()
    if vix > 0:
        sys.stdout.write(f"\r  VIX: {vix:.2f}  δ-target: {vix_delta_target(vix):.2f}\n")
    else:
        sys.stdout.write("\r  VIX: unavailable\n")

    # Step 1: sectors first — know the macro before the micros
    sector_data = scan_sectors()

    print(Fore.WHITE + Style.BRIGHT + f"\n  Scanning {len(tickers)} tickers...\n" + Style.RESET_ALL)
    results = scan_tickers(tickers)

    if not results:
        print(Fore.RED + "  No data fetched. Check tickers or internet connection." + Style.RESET_ALL)
        sys.exit(1)

    print_tv_drops(tv_universe, tickers, [r["ticker"] for r in results],
                   "no price data from the scanner's feed")

    # Step 2: apply forward-looking direction using sector context
    apply_forward_directions(results, sector_data)

    # Step 3: tag sector laggards
    find_sector_laggards(results, sector_data)

    # Step 4: enrich contracts + auto-scan flow so first view is fully loaded
    flow_results: List[Dict] = []
    if not args.no_enrich:
        enrich_contracts(results, top_n=args.enrich_top, vix=vix)
        # Only the enriched subset was ever asked for a chain, so that subset —
        # not the whole universe — is the honest denominator here.
        _enriched = [r for r in results if "contract" in r]
        print_tv_drops(tv_universe,
                       [r["ticker"] for r in _enriched],
                       [r["ticker"] for r in _enriched if r.get("contract")],
                       "no options chain")
        flow_tickers = [r["ticker"] for r in
                        sorted(results, key=lambda x: x["opt_score"], reverse=True)[:15]]
        print(f"  {Fore.CYAN}Scanning options flow ({len(flow_tickers)} tickers)...{Style.RESET_ALL}")
        flow_results = scan_options_flow(flow_tickers, show_progress=True)
        merge_whale_scores(results, flow_results)

    # Step 5: journal what this scan just decided, before anything is printed or
    # the interactive loop starts mutating scores. Recorded for the whole result
    # set, not only the rows the current filter happens to show — the filter is a
    # view, and an attribution pass needs the population the scanner actually saw.
    if not args.no_journal:
        record_scan_signals(results, params={
            "tickers":     len(tickers),
            "enrich_top":  args.enrich_top,
            "no_enrich":   args.no_enrich,
            "dynamic":     args.dynamic,
            "filter":      args.filter,
            "sort":        args.sort,
            "vix":         vix,
            "watchlist":   args.watchlist,
            "explicit_tickers": bool(args.tickers),
        })

    if args.csv:
        print_sector_heatmap(sector_data)
        print_summary(results, vix=vix)
        table, _ = render_table(results, sort_by=args.sort, filter_by=args.filter)
        print(f"\n  Filter: {FILTER_LABELS[args.filter]}  |  Sort: {SORT_LABELS[args.sort]}\n")
        print(table)
        export_csv(results)
        return

    interactive_loop(results, args, sector_data, vix=vix, flow_cache=flow_results)


if __name__ == "__main__":
    main()
