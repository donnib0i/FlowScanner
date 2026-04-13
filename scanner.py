#!/usr/bin/env python3
# scanner.py — Elite Market Scanner v2
# pip install yfinance colorama tabulate

import yfinance as yf
import colorama
from colorama import Fore, Style
from tabulate import tabulate
import argparse, time, sys, csv, os, math, warnings, requests
from datetime import datetime
from typing import Optional, List, Dict, Tuple
import pandas as pd

warnings.filterwarnings("ignore")
colorama.init(autoreset=True)

# ─── yfinance session setup ───────────────────────────────────────────────────
# yfinance 1.2+ prefers curl_cffi for Chrome TLS impersonation.
# On cloud servers where curl_cffi binaries aren't available, fall back to a
# requests.Session with a browser User-Agent (works for most endpoints).
_YF_SESSION = None
try:
    from curl_cffi.requests import Session as CurlSession
    _YF_SESSION = CurlSession(impersonate="chrome")
except Exception:
    try:
        _YF_SESSION = requests.Session()
        _YF_SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
    except Exception:
        pass

# ─── yfinance ticker normalization ───────────────────────────────────────────
_YF_TICKER_MAP: Dict[str, str] = {
    "SPX": "^SPX", "VIX": "^VIX", "RUT": "^RUT", "NDX": "^NDX",
}

def _yf_ticker(sym: str) -> str:
    return _YF_TICKER_MAP.get(sym.upper(), sym)

def _yf(sym: str) -> yf.Ticker:
    """Create a Ticker with the best available session."""
    try:
        return yf.Ticker(_yf_ticker(sym), session=_YF_SESSION)
    except Exception:
        return yf.Ticker(_yf_ticker(sym))

# ─── Sector ETFs & Ticker→Sector Map ─────────────────────────────────────────
SECTOR_ETFS: Dict[str, str] = {
    "Tech":        "XLK",
    "Financials":  "XLF",
    "Energy":      "XLE",
    "Health":      "XLV",
    "Industrials": "XLI",
    "Staples":     "XLP",
    "Utilities":   "XLU",
    "Materials":   "XLB",
    "CommSvcs":    "XLC",
    "RealEstate":  "XLRE",
}

TICKER_SECTOR: Dict[str, str] = {
    # Tech / semis / software
    "AAPL":"Tech","MSFT":"Tech","NVDA":"Tech","AMD":"Tech","AVGO":"Tech",
    "INTC":"Tech","QCOM":"Tech","TXN":"Tech","AMAT":"Tech","LRCX":"Tech",
    "MU":"Tech","ARM":"Tech","SMCI":"Tech","PLTR":"Tech","SNOW":"Tech",
    "CRWD":"Tech","PANW":"Tech","DDOG":"Tech","NET":"Tech","ZS":"Tech",
    "TWLO":"Tech","OKTA":"Tech","NFLX":"Tech","AMZN":"Tech","TSLA":"Tech",
    "SHOP":"Tech","RIVN":"Tech","LCID":"Tech","NIO":"Tech","LI":"Tech","XPEV":"Tech",
    "MSTR":"Tech","MARA":"Tech","RIOT":"Tech","CLSK":"Tech","BITF":"Tech","HUT":"Tech",
    "TQQQ":"Tech","SOXL":"Tech","SQQQ":"Tech","SOXS":"Tech","TECL":"Tech","TECS":"Tech",
    "FNGU":"Tech","FNGD":"Tech","QQQ":"Tech",
    # Financials
    "GS":"Financials","MS":"Financials","JPM":"Financials","BAC":"Financials",
    "C":"Financials","WFC":"Financials","V":"Financials","MA":"Financials",
    "SCHW":"Financials","IBKR":"Financials","SQ":"Financials","PYPL":"Financials",
    "AFRM":"Financials","UPST":"Financials","HOOD":"Financials","COIN":"Financials",
    "SOFI":"Financials",
    # Energy
    "XOM":"Energy","CVX":"Energy","USO":"Energy","CPER":"Materials",
    # Health / Biotech
    "HIMS":"Health","MRNA":"Health","PFE":"Health","BNTX":"Health",
    "LABU":"Health","LABD":"Health",
    # Drones / Defense Tech
    "ONDS":"Tech",
    # CommSvcs / consumer
    "META":"CommSvcs","GOOGL":"CommSvcs","GOOG":"CommSvcs","SNAP":"CommSvcs",
    "PINS":"CommSvcs","RBLX":"CommSvcs","ABNB":"CommSvcs","BKNG":"CommSvcs",
    "EBAY":"CommSvcs","ETSY":"CommSvcs","UBER":"CommSvcs","LYFT":"CommSvcs",
    "DASH":"CommSvcs","GME":"CommSvcs","AMC":"CommSvcs",
    "BABA":"CommSvcs","JD":"CommSvcs","PDD":"CommSvcs","KWEB":"CommSvcs","FXI":"CommSvcs",
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
    # Index ETFs + cash-settled index
    "SPX","SPY","QQQ","IWM","DIA","MDY",
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
    # High retail options vol
    "COIN","PLTR","MSTR","HOOD","SOFI","MARA","RIOT","CLSK","BITF","HUT",
    "RBLX","SNAP","UBER","LYFT","DASH","AFRM","UPST","GME","AMC",
    # Growth tech
    "NFLX","CRWD","PANW","DDOG","NET","ZS","SNOW","TWLO",
    "SHOP","SQ","PYPL","ABNB","BKNG","EBAY","ETSY",
    # EV
    "RIVN","LCID","NIO","LI","XPEV","F","GM",
    # Financials
    "GS","MS","JPM","BAC","C","WFC","V","MA","SCHW","IBKR",
    # Energy / commodities
    "XOM","CVX","GLD","SLV","CPER","USO",
    # Healthcare / speculative
    "HIMS","MRNA","PFE","BNTX","ONDS",
    # China
    "BABA","JD","PDD","KWEB","FXI",
    # Other
    "PINS","OKTA","GOOG","AVGO",
]
UNIVERSE = list(dict.fromkeys(UNIVERSE))  # dedupe

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
    Composite 0–100 institutional signal score. Factors (with weights):
      Trade at ask:      +25  (buyer aggression)
      Dollar flow tier:  0–30 ($100K=10, $500K=20, $1M+=30)
      Golden sweep:      +20  (vol>OI×10, at ask, flow>$100K)
      Stacked flow:      +15  (3+ unique unusual strikes)
      Vol/OI ratio:      0–10 (top contract, capped at x10)
    """
    score = 0

    if signal.get("trade_side") == "ask":
        score += 25

    flow = signal.get("total_flow", 0)
    if flow >= 1_000_000:   score += 30
    elif flow >= 500_000:   score += 20
    elif flow >= 100_000:   score += 10

    if signal.get("golden_sweep"):
        score += 20

    if signal.get("stacked_flow"):
        score += 15

    tc = signal.get("top_contract")
    if tc:
        score += min(10, int(tc.get("vol_oi", 0)))

    return min(100, max(0, score))


def fmt_whale_score(score: int) -> str:
    bar = score_bar(score, width=8)
    if score >= 80:   c = Fore.RED + Style.BRIGHT
    elif score >= 60: c = Fore.YELLOW + Style.BRIGHT
    elif score >= 40: c = Fore.YELLOW
    else:             c = Fore.WHITE
    return f"{c}{bar} {score:3d}{Style.RESET_ALL}"


def trade_grade(setup_q: float, opt_score: int, has_contract: bool) -> str:
    score = setup_q * 50 + opt_score * 0.30 + (20 if has_contract else 0)
    if score >= 75: return Fore.GREEN  + Style.BRIGHT + "A" + Style.RESET_ALL
    if score >= 55: return Fore.YELLOW + "B" + Style.RESET_ALL
    if score >= 35: return Fore.WHITE  + "C" + Style.RESET_ALL
    return Fore.RED + "D" + Style.RESET_ALL

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

# ─── Options Contract Finder ──────────────────────────────────────────────────
def _nan0(v):
    """Convert a value to float, treating None/NaN as 0."""
    try:
        f = float(v)
        return 0.0 if f != f else f   # f != f is True only for NaN
    except Exception:
        return 0.0


def _score_contract(row: pd.Series, S: float, T: float, direction: str,
                    target_delta: float = 0.45) -> float:
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

        # Expected 1-sigma move (annualised IV → daily move for this DTE window)
        sigma_move  = S * max(iv, 0.05) * math.sqrt(max(T, 1.0 / 365))
        if otype == "call":
            ev_1sigma = max(0.0, (S + sigma_move) - K)
        else:
            ev_1sigma = max(0.0, K - (S - sigma_move))
        # ROI at 1-sigma target, capped to prevent micro-priced outliers distorting rank
        roi_score = min(1.0, max(0.0, (ev_1sigma - mid) / (mid + 0.01)) / 5.0)

        # Target delta (VIX-adjusted, target range 0.25–0.40)
        # Fixed ±0.15 tolerance band — symmetric, no asymmetric penalty for low strikes
        vol_oi_ratio = cvol / max(oi, 1)
        voi_score    = min(1.0, math.log10(max(vol_oi_ratio, 1.0)) / math.log10(50))
        delta_score  = max(0.0, 1.0 - abs(delta - target_delta) / 0.15)
        liq_score    = min(1.0, math.log10(max(oi + cvol + 1, 1)) / 5.5)
        spread_score = max(0.0, 1.0 - spread_pct * 2.5)

        # Soft penalty for expensive contracts (>$10 mid) — still ranked, just lower priority
        price_penalty = 1.0 if mid <= 10.0 else max(0.6, 1.0 - (mid - 10.0) / 40.0)

        # ROI is the primary rank signal; delta/liq keep us from chasing worthless lotto contracts
        return (roi_score * 35 + delta_score * 25 + liq_score * 22 + spread_score * 10 + voi_score * 8) * stale_penalty * price_penalty
    except Exception:
        return -1.0


def get_best_contract(ticker: str, direction: str, price: float,
                      vix: float = -1.0, top_n: int = 1,
                      dte_type: str = "weekly") -> Optional[Dict]:
    """
    direction: "up" → calls, "down" → puts.
    dte_type: "0dte" (today only), "weekly" (≤7 DTE), "swing" (8–60 DTE)
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
        try:
            import zoneinfo
            _now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        except ImportError:
            import pytz
            _now_et = datetime.now(pytz.timezone("America/New_York"))
        _mins = _now_et.hour * 60 + _now_et.minute
        _market_open = _now_et.weekday() < 5 and (9 * 60 + 30) <= _mins <= 16 * 60
        min_dte = 0 if _market_open else 1
        future = [e for e in exps if dte(e) >= min_dte]

        # Filter candidates by dte_type
        if dte_type == "0dte":
            cands = [e for e in future if dte(e) == 0]
            if not cands and not _market_open:
                cands = [e for e in future if dte(e) <= 1]  # after hours: next day
        elif dte_type == "swing":
            cands = [e for e in future if 8 <= dte(e) <= 60]
            if not cands:
                cands = [e for e in future if dte(e) <= 90]
        else:  # weekly (default)
            cands = (
                [e for e in future if dte(e) <= 7]
                or [e for e in future if dte(e) <= 14]
            )
        if not cands:
            cands = list(future[:2])
        if not cands:
            cands = list(exps[:2])

        target_delta = vix_delta_target(vix)
        scored: List[tuple] = []   # (score, contract_dict)

        for exp in cands[:3]:
            d = dte(exp)
            # For 0DTE: use actual minutes remaining until 4 PM ET instead of arbitrary floor
            if d == 0:
                close_et = _now_et.replace(hour=16, minute=0, second=0, microsecond=0)
                mins_left = max(1.0, (_now_et.replace(tzinfo=None) - _now_et.replace(tzinfo=None)).total_seconds())
                try:
                    mins_left = max(1.0, (close_et - _now_et).total_seconds() / 60)
                except Exception:
                    mins_left = 60.0
                T = (mins_left / 1440.0) / 365.0
            else:
                T = d / 365.0
            T = max(T, 1.0 / (1440 * 365))   # absolute floor: 1 minute
            try:
                chain = t.option_chain(exp)
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
                sc = _score_contract(row, price, T, direction, target_delta)
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
                    "exp":    exp,
                    "dte":    d,
                    "strike": K,
                    "type":   otype,
                    "delta":  delta,
                    "iv":     iv,
                    "oi":     oi,
                    "vol":    cvol,
                    "bid":    bid,
                    "ask":    ask,
                    "mid":    mid,
                    "stale":  stale,
                    "score":  round(sc, 1),
                    "roi":    roi_pct,    # expected % ROI at 1-sigma move
                }))

        scored.sort(key=lambda x: x[0], reverse=True)
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

    print(f"  {Fore.CYAN}Scanning sectors (batch)...{Style.RESET_ALL}", end="", flush=True)
    batch = _fetch_batch_history(etfs, period="30d")
    sys.stdout.write("\r" + " " * 50 + "\r")
    sys.stdout.flush()

    # If batch download failed (cloud IP block, timeout, etc.), fall back to
    # individual _yf() calls which use the curl_cffi session and work on Render.
    use_batch = not batch.empty
    if not use_batch:
        print(f"  {Fore.YELLOW}Batch failed — falling back to individual fetches{Style.RESET_ALL}", end="", flush=True)

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

            sector_data[name] = {
                "etf":        etf,
                "price":      price,
                "change_pct": change_pct,
                "rel_vol":    rel_vol,
                "price_loc":  price_loc,
                "mom_3d":     mom_3d,
                "strength":   strength,
                "bias":       "up" if strength >= 0 else "down",
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


# ─── Options Flow Scanner (Enhanced) ─────────────────────────────────────────
def scan_options_flow(tickers: List[str], show_progress: bool = True,
                      on_signal=None, on_progress=None) -> List[Dict]:
    """
    Detect unusual options activity per ticker.
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
    _m = _et.hour * 60 + _et.minute
    _market_open = _et.weekday() < 5 and (9 * 60 + 30) <= _m <= 16 * 60

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
                    chain = t.option_chain(exp)
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
                            "flow":         round(flow_v, 0),
                            "sweep":        is_sweep,
                            "golden_sweep": is_golden,
                            "trade_side":   trade_side,
                            "premium_tier": tier,
                        }
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
            }
            signal["whale_score"] = calc_whale_score(signal)
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

    # 2. Gap direction — confirmed institutional order flow
    if r["gap_flag"] == "gap_up":
        score_up += 2.5
    elif r["gap_flag"] == "gap_down":
        score_dn += 2.5

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


def _process_ticker(ticker: str, hist: pd.DataFrame, live_price: float = 0.0) -> Optional[Dict]:
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

        # Price location in today's range (0 = at low, 1 = at high)
        day_range = today_high - today_low
        price_loc = (price - today_low) / day_range if day_range > 0 else 0.5

        # IVR proxy: 52-week high/low from downloaded history — no fast_info needed
        try:
            yr_high   = float(hist["High"].max())
            yr_low    = float(hist["Low"].min())
            ivr_proxy = (price - yr_low) / (yr_high - yr_low) if yr_high > yr_low else 0.5
            ivr_proxy = max(0.0, min(1.0, ivr_proxy))
        except Exception:
            ivr_proxy = 0.5

        spread_label, _, _ = get_spread_tier(avg_vol)
        opt_score          = calc_options_score(avg_vol, ivr_proxy)

        # Key levels (use 60d window — same as before)
        levels     = find_key_levels(hist60, price)
        near_level = next((l for l in levels if l["strength"] >= 2), levels[0] if levels else None)

        # Setup quality 0.0–1.0
        sq = 0.0
        if gap_flag:                                              sq += 0.35
        if inside_day:                                            sq += 0.20
        if high_vol:                                              sq += 0.20
        if near_level and near_level["strength"] >= 5:            sq += 0.25
        elif near_level and near_level["strength"] >= 3:          sq += 0.15
        elif near_level and near_level["strength"] >= 1:          sq += 0.05
        if gap_flag and near_level and near_level["strength"] >= 4:  sq = min(1.0, sq + 0.20)

        # Provisional direction — overwritten by apply_forward_directions()
        direction = "up" if change_pct >= 0 else "down"

        return {
            "ticker":        ticker,
            "price":         price,
            "change_pct":    change_pct,
            "gap_pct":       gap_pct,
            "gap_flag":      gap_flag,
            "inside_day":    inside_day,
            "rel_vol":       rel_vol,
            "high_vol":      high_vol,
            "today_vol":     today_vol,
            "avg_vol":       avg_vol,
            "ivr_proxy":     ivr_proxy,
            "spread_label":  spread_label,
            "opt_score":     opt_score,
            "levels":        levels,
            "near_level":    near_level,
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
        }
    except Exception:
        return None


def scan_tickers(tickers: List[str], show_progress: bool = True) -> List[Dict]:
    # One batch download for all tickers — no per-ticker rate limiting
    if show_progress:
        sys.stdout.write(
            f"  {Fore.CYAN}Downloading {len(tickers)} tickers (batch)...{Style.RESET_ALL}"
        )
        sys.stdout.flush()
    batch = _fetch_batch_history(tickers, period="1y")
    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

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
        r    = _process_ticker(ticker, hist, live_price=live_prices.get(ticker, 0.0))
        if r:
            results.append(r)
    if show_progress:
        sys.stdout.write("\r" + " " * 72 + "\r")
        sys.stdout.flush()
    return results


def enrich_contracts(results: List[Dict], top_n: int = 20, vix: float = -1.0) -> None:
    """
    Fetch options chains for the top-N tickers by setup quality.
    Populates result['contract'] in place. VIX adjusts delta target.
    """
    ranked = sorted(
        [r for r in results if r["gap_flag"] or r["inside_day"] or r["high_vol"]],
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
        r["contract"] = get_best_contract(r["ticker"], r["direction"], r["price"], vix=vix)
        time.sleep(0.15)
    sys.stdout.write("\r" + " " * 55 + "\r")
    sys.stdout.flush()

# ─── Filter / Sort ────────────────────────────────────────────────────────────
FILTER_LABELS = {
    "all":     "All",
    "gap":     "Gap Fills",
    "inside":  "Inside Day",
    "highvol": "High Vol",
    "options": "Options >=60",
    "any":     "Any Setup",
    "a_grade": "Grade A",
    "laggard": "Sector Laggards",
}

SORT_LABELS = {
    "setup":   "Setup Quality",
    "options": "Options Score",
    "relvol":  "Rel Vol",
    "gap":     "Gap %",
    "change":  "Change %",
    "lag":     "Lag Score",
}

FILTER_MAP = {"1": "all", "2": "gap", "3": "inside", "4": "highvol",
              "5": "options", "6": "any", "7": "a_grade", "8": "laggard"}
SORT_MAP   = {"s1": "setup", "s2": "options", "s3": "relvol",
              "s4": "gap",   "s5": "change",  "s6": "lag"}


def apply_filter(results: List[Dict], f: str) -> List[Dict]:
    if f == "gap":     return [r for r in results if r["gap_flag"]]
    if f == "inside":  return [r for r in results if r["inside_day"]]
    if f == "highvol": return [r for r in results if r["high_vol"]]
    if f == "options": return [r for r in results if r["opt_score"] >= 60]
    if f == "any":     return [r for r in results if (
        r["gap_flag"] or r["inside_day"] or r["high_vol"] or
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
    }
    return sorted(results, key=keys.get(s, keys["setup"]), reverse=True)

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
    if r["gap_flag"] == "gap_up":
        fill = _gap_fill_pct(r)
        b.append(Fore.CYAN + f"G+{fill}" + Style.RESET_ALL)
    if r["gap_flag"] == "gap_down":
        fill = _gap_fill_pct(r)
        b.append(Fore.YELLOW + f"G-{fill}" + Style.RESET_ALL)
    if r["inside_day"]:              b.append(Fore.MAGENTA + Style.BRIGHT + "ID" + Style.RESET_ALL)
    if r["high_vol"]:                b.append(Fore.GREEN   + "HV" + Style.RESET_ALL)
    nl = r.get("near_level")
    if nl and nl["strength"] >= 5:   b.append(Fore.RED + Style.BRIGHT + "**" + Style.RESET_ALL)
    elif nl and nl["strength"] >= 3: b.append(Fore.RED     + "*"  + Style.RESET_ALL)
    elif nl:                         b.append(Fore.WHITE   + "L"  + Style.RESET_ALL)
    if r.get("is_laggard"):
        lag_c = Fore.CYAN if r.get("lag_direction") == "up" else Fore.YELLOW
        b.append(lag_c + f"LAG{r['lag_pct']:+.0f}%" + Style.RESET_ALL)
    return " ".join(b) if b else "—"


def render_table(
    results: List[Dict],
    sort_by: str = "setup",
    filter_by: str = "any",
) -> Tuple[str, List[Dict]]:
    filtered = apply_filter(results, filter_by)
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


def print_inline_flow(flow_signals: List[Dict], top_n: int = 7) -> None:
    """Compact flow section with net bias bar, VOI, and golden/sweep flags."""
    if not flow_signals:
        return
    sep = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL
    print(sep)

    # Net session summary line
    net_calls = sum(f["call_flow"] for f in flow_signals)
    net_puts  = sum(f["put_flow"]  for f in flow_signals)
    net_total = net_calls + net_puts
    call_pct  = net_calls / net_total * 100 if net_total > 0 else 50
    net_bar   = fmt_bias_bar(net_calls, net_puts, width=14)
    net_c     = Fore.CYAN if net_calls >= net_puts else Fore.YELLOW
    net_dir   = net_c + ("CALL HEAVY" if net_calls >= net_puts else " PUT HEAVY") + Style.RESET_ALL
    print(
        Fore.WHITE + Style.BRIGHT + "  OPTIONS FLOW  " + Style.RESET_ALL
        + f"[{net_bar}]  {call_pct:.0f}% calls  {net_dir}"
        + f"  |  total: {fmt_flow(net_total)}  signals: {len(flow_signals)}"
    )

    rows = []
    for f in flow_signals[:top_n]:
        tc     = f.get("top_contract")
        voi    = tc.get("vol_oi", 0) if tc else 0
        voi_s  = fmt_voi(voi) if voi > 0 else "—"
        bar    = fmt_bias_bar(f["call_flow"], f["put_flow"], width=8)
        cpct   = f["call_flow"] / f["total_flow"] * 100 if f["total_flow"] > 0 else 50
        bias_c = Fore.CYAN if f["flow_bias"] == "call" else Fore.YELLOW
        bias_s = bias_c + f["flow_bias"].upper() + Style.RESET_ALL
        tier_c = _TIER_COLORS.get(f.get("premium_tier", "retail"), Fore.WHITE)
        golden = (Fore.RED + Style.BRIGHT + "★GOLDEN" + Style.RESET_ALL) if f.get("golden_sweep") else (
                  Fore.YELLOW + "SWEEP" + Style.RESET_ALL if (tc and tc.get("sweep")) else "—")
        contract_s = fmt_flow_contract(tc) if tc else "—"
        rows.append([
            Fore.WHITE + Style.BRIGHT + f["ticker"] + Style.RESET_ALL,
            fmt_flow(f["total_flow"]),
            f"[{bar}] {cpct:.0f}%",
            bias_s,
            voi_s,
            tier_c + f.get("premium_tier", "retail").upper() + Style.RESET_ALL,
            golden,
            contract_s,
        ])
    print(tabulate(rows,
                   headers=["TICKER", "$TOTAL", "C/P", "BIAS", "VOI", "TIER", "FLAG", "TOP CONTRACT"],
                   tablefmt="simple"))


def print_hot_contracts(flow_signals: List[Dict], top_n: int = 14) -> None:
    """
    CheddarFlow-style hot contract list — all unusual contracts from every flow signal,
    sorted by vol/OI ratio (the key conviction signal).
    """
    all_contracts: List[Dict] = []
    for sig in flow_signals:
        for c in sig.get("call_contracts", []) + sig.get("put_contracts", []):
            all_contracts.append({**c, "ticker": sig["ticker"]})

    if not all_contracts:
        print(f"  {Fore.YELLOW}No unusual contract activity detected.{Style.RESET_ALL}")
        return

    all_contracts.sort(key=lambda c: c.get("vol_oi", 0), reverse=True)

    sep = Fore.WHITE + Style.BRIGHT + "─" * 110 + Style.RESET_ALL
    print(sep)
    print(Fore.WHITE + Style.BRIGHT
          + "  HOT CONTRACTS  (all unusual prints, sorted by vol/OI conviction)"
          + Style.RESET_ALL)
    rows = []
    for c in all_contracts[:top_n]:
        voi    = c.get("vol_oi", 0)
        ctype  = c["type"]
        cc     = Fore.CYAN if ctype == "call" else Fore.YELLOW
        exp_s  = c["exp"][5:]
        side_c = (Fore.GREEN if c.get("trade_side") == "ask" else
                  Fore.RED   if c.get("trade_side") == "bid" else Fore.WHITE)
        side_s = side_c + (c.get("trade_side", "mid")).upper() + Style.RESET_ALL
        tier_c = _TIER_COLORS.get(c.get("premium_tier", "retail"), Fore.WHITE)
        flags  = []
        if c.get("golden_sweep"): flags.append(Fore.RED + Style.BRIGHT + "★GOLDEN" + Style.RESET_ALL)
        elif c.get("sweep"):      flags.append(Fore.YELLOW + "SWEEP" + Style.RESET_ALL)
        rows.append([
            fmt_voi(voi),
            Fore.WHITE + Style.BRIGHT + c["ticker"] + Style.RESET_ALL,
            f"{cc}{exp_s} ${c['strike']:.0f}{'C' if ctype == 'call' else 'P'}{Style.RESET_ALL}",
            f"{c['dte']}DTE",
            fmt_num(c["vol"]),
            fmt_num(c["oi"]),
            f"${c['mid']:.2f}",
            fmt_flow(c["flow"]),
            side_s,
            tier_c + c.get("premium_tier", "retail").upper() + Style.RESET_ALL,
            " ".join(flags) or "—",
        ])
    print(tabulate(rows,
                   headers=["VOI", "TICKER", "CONTRACT", "DTE", "VOL", "OI",
                             "MID", "$FLOW", "SIDE", "TIER", "FLAG"],
                   tablefmt="simple"))
    print(sep)


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
                "  [l]Laggards  [f]Flow  [w]Whales  [h]Hot Contracts  [c]CSV  [q]Quit"
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
            print_options_flow(flow_cache)
            print_whale_alerts(flow_cache)
            input("\n  (press enter to continue)")
        elif cmd == "w":
            os.system("clear" if os.name == "posix" else "cls")
            if not flow_cache:
                flow_tickers = [r["ticker"] for r in
                                sorted(results, key=lambda x: x["opt_score"], reverse=True)[:20]]
                print(f"\n  Scanning options flow for {len(flow_tickers)} tickers...")
                flow_cache = scan_options_flow(flow_tickers, show_progress=True)
            print_whale_alerts(flow_cache)
            input("\n  (press enter to continue)")
        elif cmd == "h":
            os.system("clear" if os.name == "posix" else "cls")
            if not flow_cache:
                flow_tickers = [r["ticker"] for r in
                                sorted(results, key=lambda x: x["opt_score"], reverse=True)[:20]]
                print(f"\n  Scanning options flow for {len(flow_tickers)} tickers...")
                flow_cache = scan_options_flow(flow_tickers, show_progress=True)
            print_hot_contracts(flow_cache, top_n=14)
            input("\n  (press enter to continue)")
        elif cmd == "c":
            export_csv(results)
            input("  (press enter)")
        elif cmd in FILTER_MAP:
            filter_by = FILTER_MAP[cmd]
        elif cmd in SORT_MAP:
            sort_by = SORT_MAP[cmd]

# ─── Entry Point ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Elite market scanner — gap fills, key levels, options contracts."
    )
    parser.add_argument("--tickers",    nargs="+", metavar="TICKER",
                        help="Specific tickers to scan")
    parser.add_argument("--watchlist",  metavar="FILE",
                        help="Text file with one ticker per line")
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
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif args.watchlist:
        try:
            with open(args.watchlist) as f:
                tickers = [ln.strip().upper() for ln in f if ln.strip()]
        except FileNotFoundError:
            print(Fore.RED + f"  File not found: {args.watchlist}" + Style.RESET_ALL)
            sys.exit(1)
    else:
        tickers = UNIVERSE

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

    # Step 2: apply forward-looking direction using sector context
    apply_forward_directions(results, sector_data)

    # Step 3: tag sector laggards
    find_sector_laggards(results, sector_data)

    # Step 4: enrich contracts + auto-scan flow so first view is fully loaded
    flow_results: List[Dict] = []
    if not args.no_enrich:
        enrich_contracts(results, top_n=args.enrich_top, vix=vix)
        flow_tickers = [r["ticker"] for r in
                        sorted(results, key=lambda x: x["opt_score"], reverse=True)[:15]]
        print(f"  {Fore.CYAN}Scanning options flow ({len(flow_tickers)} tickers)...{Style.RESET_ALL}")
        flow_results = scan_options_flow(flow_tickers, show_progress=True)

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
