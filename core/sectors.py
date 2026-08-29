"""
core/sectors.py -- Sector-level scans: the 11 sector ETFs, laggard ranking, constituent
heatmaps, and breakout plays.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from colorama import Fore, Style
from data.sector_constituents import constituents_for
from typing import Optional, List, Dict
import time
import sys

from core.constants import SECTOR_ETFS, TICKER_SECTOR
from core.market_data import (
    _extract_ticker_hist,
    _fetch_batch_history,
    _get_spy_change,
    _quotes_for,
    _yf,
)
from core.technicals import classify_breakout
from core.options import get_best_contract


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
