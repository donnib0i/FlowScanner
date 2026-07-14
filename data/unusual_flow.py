"""
unusual_flow.py — Unusual options activity detector.

Spots contracts where flow TODAY is anomalous vs baseline (OI + historical avg).
No hardcoded ticker lists. Pool is built live from:
  - S&P 500 constituents + GICS sectors (Wikipedia)
  - Nasdaq-100 constituents (Wikipedia)
  - Yahoo Finance most-active, gainers, losers screeners
  - yfinance sector lookup for screener tickers not in S&P 500

Sectors come from the actual GICS data, not a hand-typed map.
ETF anchors (SPY, QQQ, etc.) are the only hardcoded names — they're not
in any index constituent table but are always options-active.

Anomaly scoring (same signals as Flow God / unusual whales accounts):
  vol/OI ratio    — today's volume vs accumulated open interest
  notional ($)    — total premium spent (vol × mid × 100)
  OTM positioning — directional bet vs hedging
  DTE urgency     — near-expiry = most aggressive
  ask-side buys   — buyer hitting ask = conviction
"""
from __future__ import annotations

import math
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf

from data.sources import get_option_chain, available_sources

warnings.filterwarnings("ignore")

# ── ETF anchors — the only hardcoded names ────────────────────────────────────
# These are always options-active but never appear in S&P 500 / index tables.
_ETF_ANCHORS: Dict[str, str] = {
    "SPY":  "ETF / Broad Market",  "QQQ":  "ETF / Broad Market",
    "IWM":  "ETF / Broad Market",  "DIA":  "ETF / Broad Market",
    "VXX":  "ETF / Volatility",    "UVXY": "ETF / Volatility",
    "TQQQ": "ETF / Leveraged",     "SQQQ": "ETF / Leveraged",
    "SOXL": "ETF / Leveraged",     "SOXS": "ETF / Leveraged",
    "UPRO": "ETF / Leveraged",     "SPXS": "ETF / Leveraged",
    "XLK":  "ETF / Sector",        "XLF":  "ETF / Sector",
    "XLE":  "ETF / Sector",        "XLV":  "ETF / Sector",
    "XLI":  "ETF / Sector",        "XLC":  "ETF / Sector",
    "XLB":  "ETF / Sector",        "XLRE": "ETF / Sector",
    "GLD":  "ETF / Commodity",     "SLV":  "ETF / Commodity",
    "USO":  "ETF / Commodity",     "KWEB": "ETF / China",
    "FXI":  "ETF / China",         "XBI":  "ETF / Biotech",
    "ARKK": "ETF / Innovation",
}

# ── Cache ─────────────────────────────────────────────────────────────────────
_POOL_CACHE: dict = {"ticker_sector": {}, "ts": 0.0}
_POOL_TTL   = 3600 * 6   # rebuild pool every 6 hours (sectors don't change intraday)

# ── Scoring constants ─────────────────────────────────────────────────────────
MIN_VOLUME   = 100
MIN_NOTIONAL = 50_000
VOL_OI_NOTABLE = 0.25
VOL_OI_EXTREME = 5.0
MAX_DTE      = 60
WORKERS      = 10


# ── Pool builders ─────────────────────────────────────────────────────────────

def _fetch_sp500_with_sectors() -> Dict[str, str]:
    """
    Pull S&P 500 from Wikipedia. Returns {ticker: GICS_sector}.
    The Wikipedia table has Symbol + GICS Sector columns — no manual mapping needed.
    """
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            storage_options={"User-Agent": "Mozilla/5.0"},
        )
        df = tables[0]
        # Column names vary slightly — find symbol and sector columns
        sym_col  = next((c for c in df.columns if "symbol" in str(c).lower()), None)
        sec_col  = next((c for c in df.columns if "gics sector" in str(c).lower()), None)
        if sym_col is None or sec_col is None:
            return {}
        result = {}
        for _, row in df.iterrows():
            sym = str(row[sym_col]).strip().replace(".", "-")
            sec = str(row[sec_col]).strip()
            if sym and sec and len(sym) <= 5:
                result[sym] = sec
        return result
    except Exception:
        return {}


def _fetch_nasdaq100() -> List[str]:
    """Nasdaq-100 tickers from Wikipedia (no sector column in this table)."""
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
                    return [s for s in syms
                            if isinstance(s, str) and s.replace("-","").isalpha() and len(s) <= 5]
        return []
    except Exception:
        return []


def _yf_screener(scr_id: str, n: int = 100) -> List[str]:
    """Yahoo Finance predefined screener → list of symbols."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
            params={"formatted": "false", "scrIds": scr_id, "count": n},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        r.raise_for_status()
        quotes = r.json()["finance"]["result"][0]["quotes"]
        return [q["symbol"] for q in quotes if "symbol" in q]
    except Exception:
        return []


def _lookup_sectors_yf(tickers: List[str]) -> Dict[str, str]:
    """
    For tickers not in S&P 500, query yfinance for sector info.
    Batches via fast_info to keep it reasonable.
    """
    result: Dict[str, str] = {}
    for sym in tickers:
        try:
            info = yf.Ticker(sym).fast_info
            sec = getattr(info, "sector", None) or ""
            result[sym] = sec if sec else "Active Market"
        except Exception:
            result[sym] = "Active Market"
    return result


def build_ticker_sector_map() -> Dict[str, str]:
    """
    Build a complete {ticker: sector} map from live data sources.
    No hardcoded tickers beyond the ETF anchors.

    Sources:
      1. S&P 500 + GICS sectors from Wikipedia  (~500 tickers, real sector names)
      2. Nasdaq-100 from Wikipedia              (fills gaps, assigned via yfinance)
      3. Yahoo screeners: most-active, gainers, losers  (today's hot names)
         → sector looked up via yfinance for any not already in the map
      4. ETF anchors always added last
    """
    # S&P 500 with real GICS sectors
    sp500_map = _fetch_sp500_with_sectors()

    # Nasdaq-100 — some overlap, fill missing via yfinance
    ndx = _fetch_nasdaq100()
    ndx_new = [t for t in ndx if t not in sp500_map]
    ndx_sectors = _lookup_sectors_yf(ndx_new) if ndx_new else {}

    # Live screener tickers — hot names right now
    screener_tickers = list(dict.fromkeys(
        _yf_screener("most_actives", 100)
        + _yf_screener("day_gainers", 75)
        + _yf_screener("day_losers", 75)
    ))
    screener_new = [t for t in screener_tickers
                    if t not in sp500_map and t not in ndx_sectors and t not in _ETF_ANCHORS]
    screener_sectors = _lookup_sectors_yf(screener_new[:50]) if screener_new else {}

    # Merge — S&P 500 sectors are ground truth, others fill in
    merged: Dict[str, str] = {}
    merged.update(sp500_map)
    merged.update(ndx_sectors)
    merged.update(screener_sectors)
    # ETFs are intentionally NOT injected — the scanner is single-stocks only.
    # (ETF exclusion is enforced downstream via apply_pool_exclusions / filter_etfs.)

    return merged


def get_ticker_sector_map(force: bool = False) -> Dict[str, str]:
    """Cached sector map. Rebuilds every 6 hours."""
    now = time.time()
    if not force and now - _POOL_CACHE["ts"] < _POOL_TTL and _POOL_CACHE["ticker_sector"]:
        return _POOL_CACHE["ticker_sector"]
    m = build_ticker_sector_map()
    if m:
        _POOL_CACHE["ticker_sector"] = m
        _POOL_CACHE["ts"] = now
    return _POOL_CACHE["ticker_sector"] or dict(_ETF_ANCHORS)


def get_scan_pool() -> List[str]:
    """All tickers we know about right now, sector-mapped."""
    return list(get_ticker_sector_map().keys())


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _days_to_expiry(expiry_str: str) -> int:
    try:
        return max(0, (datetime.strptime(expiry_str, "%Y-%m-%d").date() - date.today()).days)
    except Exception:
        return 99


def _classify_side(bid: float, ask: float, last: float) -> str:
    if bid <= 0 or ask <= 0 or last <= 0:
        return "mid"
    spread = ask - bid
    if spread <= 0:
        return "mid"
    if last >= ask - spread * 0.25:
        return "ask"
    if last <= bid + spread * 0.25:
        return "bid"
    return "mid"


def _otm_pct(strike: float, price: float, opt_type: str) -> float:
    if price <= 0:
        return 0.0
    return ((strike - price) / price * 100) if opt_type == "call" else ((price - strike) / price * 100)


def _anomaly_score(notional: float, vol_oi: float, dte: int,
                   otm_pct: float, trade_side: str) -> int:
    """
    0–100 composite unusualness score.

    Notional base (log-scaled $50K→$10M):  0–40
    Vol/OI ratio bonus:                     0–30
    DTE urgency:                            0–15
    OTM directional bonus:                  0–10
    Ask-side aggression:                    +5
    """
    if notional < MIN_NOTIONAL:
        return 0
    n_score  = min(40, int(math.log10(max(notional, 50_000) / 50_000) / math.log10(200) * 40))
    voi_score = min(30, int((min(vol_oi, VOL_OI_EXTREME) / VOL_OI_EXTREME) * 30)) if vol_oi >= VOL_OI_NOTABLE else 0
    dte_score = 15 if dte <= 1 else 10 if dte <= 7 else 5 if dte <= 21 else 0
    otm_score = 10 if 0 < otm_pct <= 10 else 5 if 0 < otm_pct <= 25 else 2 if otm_pct <= 0 else 0
    ask_bonus = 5 if trade_side == "ask" else 0
    return min(100, n_score + voi_score + dte_score + otm_score + ask_bonus)


def _label(score: int) -> str:
    if score >= 80: return "🔴 EXTREME"
    if score >= 65: return "🟠 UNUSUAL"
    if score >= 50: return "🟡 NOTABLE"
    return "⚪ NORMAL"


def intra_chain_z(value: float, chain_values: list) -> float:
    """How much of an outlier is `value` vs the rest of this ticker's chain today."""
    vals = [float(v) for v in chain_values if v and v > 0]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
    if std == 0:
        return 0.0
    return round((value - mean) / std, 2)


def baseline_multiplier(oi_vs_avg, ticker_optvol_rvol, dampen: bool = False) -> float:
    """
    Score multiplier in [0.5, 1.5]. Boost names spiking vs their own norm;
    dampen perennial mega-caps. Neutral 1.0 when no baseline yet.
    """
    m = 1.0
    if oi_vs_avg is not None:
        if oi_vs_avg >= 5:
            m += 0.5
        elif oi_vs_avg >= 3:
            m += 0.3
        elif oi_vs_avg >= 2:
            m += 0.15
    if ticker_optvol_rvol is not None and ticker_optvol_rvol >= 3:
        m += 0.15
    if dampen:
        m -= 0.4
    return round(max(0.5, min(1.5, m)), 2)


def adjusted_score(base_score: int, multiplier: float) -> int:
    return max(0, min(100, int(round(base_score * multiplier))))


def apply_pool_exclusions(tickers: list, exclude_etfs: bool = True) -> list:
    """Drop ETFs from a ticker pool when exclude_etfs is set (order-preserving)."""
    if not exclude_etfs:
        return list(tickers)
    from data.etf_filter import filter_etfs
    return filter_etfs(tickers)


def score_contract(contract: dict, price: float, sector: str,
                   chain_volumes: list,
                   oi_vs_avg, ticker_optvol_rvol, dampen: bool,
                   ticker: str = "") -> Optional[dict]:
    """Pure scoring of a single option contract dict -> signal dict (or None)."""
    vol    = int(contract.get("volume") or 0)
    oi     = int(contract.get("open_interest") or 0)
    bid    = float(contract.get("bid") or 0)
    ask    = float(contract.get("ask") or 0)
    last   = float(contract.get("last") or 0)
    mid    = float(contract.get("mid") or 0)
    strike = float(contract.get("strike") or 0)
    iv     = float(contract.get("iv") or 0)
    expiry = contract.get("expiry", "")
    dte    = int(contract.get("dte") or _days_to_expiry(expiry))
    opt_type = contract.get("type", "")

    if vol < MIN_VOLUME or strike <= 0 or price <= 0:
        return None
    if not mid:
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    if not mid:
        return None

    notional = vol * mid * 100
    vol_oi   = vol / max(oi, 1)
    otm      = _otm_pct(strike, price, opt_type)
    side     = _classify_side(bid, ask, last)
    base     = _anomaly_score(notional, vol_oi, dte, otm, side)
    z        = intra_chain_z(vol, chain_volumes)
    if z >= 2:
        base = min(100, base + 5)
    mult     = baseline_multiplier(oi_vs_avg, ticker_optvol_rvol, dampen)
    score    = adjusted_score(base, mult)

    if score < 25 or notional < MIN_NOTIONAL:
        return None

    return {
        "ticker": ticker or contract.get("ticker", ""),
        "sector": sector, "price": round(price, 2),
        "type": opt_type, "strike": strike, "expiry": expiry, "dte": dte,
        "volume": vol, "open_interest": oi, "vol_oi": round(vol_oi, 2),
        "mid_price": round(mid, 2), "notional": round(notional, 0),
        "iv": round(iv * 100, 1) if iv < 10 else round(iv, 1),
        "otm_pct": round(otm, 1), "trade_side": side,
        "intra_chain_z": z,
        "ticker_oi_vs_avg": oi_vs_avg,
        "ticker_optvol_rvol": ticker_optvol_rvol,
        "score": score, "label": _label(score),
        "data_source": contract.get("source", "unknown"),
        "ts": datetime.now().strftime("%H:%M:%S"),
    }


# ── Phase 1: relative-volume screen ──────────────────────────────────────────

def _screen_relvol(tickers: List[str], top_n: int = 80) -> List[str]:
    """Return top_n tickers by relative volume (today vs 4-day avg)."""
    if not tickers:
        return []
    try:
        batch = yf.download(
            tickers, period="5d", interval="1d",
            group_by="ticker", progress=False, threads=True,
            timeout=20, auto_adjust=True,
        )
        scored: List[Tuple[str, float]] = []
        for t in tickers:
            try:
                h = (batch[t].dropna(how="all")
                     if isinstance(batch.columns, pd.MultiIndex) and t in batch.columns.get_level_values(0)
                     else batch.dropna(how="all"))
                if len(h) < 3:
                    continue
                avg = float(h["Volume"].iloc[:-1].mean())
                today = float(h["Volume"].iloc[-1])
                if avg > 0:
                    scored.append((t, today / avg))
            except Exception:
                continue
        scored.sort(key=lambda x: x[1], reverse=True)
        return [t for t, _ in scored[:top_n]]
    except Exception:
        return tickers[:top_n]


# ── Phase 2: options chain scan for one ticker ────────────────────────────────

def _scan_ticker_options(ticker: str, price: float, sector: str,
                         store=None, dampen: bool = False) -> List[Dict]:
    """
    Fetch options chain via best available source (Tradier → Schwab → Polygon →
    Yahoo direct → yfinance) and score each contract for unusual activity.

    When a BaselineStore is supplied, each contract is scored relative to this
    name's own OI/option-volume history (baseline multiplier + intra-chain z).
    """
    signals = []
    try:
        contracts = get_option_chain(ticker)
        if not contracts:
            return []
        chain_volumes = [int(c.get("volume") or 0) for c in contracts]
        ticker_vol = sum(chain_volumes)
        rvol = store.ticker_optvol_rvol(ticker, ticker_vol) if store else None
        for c in contracts:
            oi_vs_avg = None
            if store:
                oi_vs_avg = store.contract_oi_vs_avg(
                    ticker, c.get("type", ""), float(c.get("strike") or 0),
                    c.get("expiry", ""), int(c.get("open_interest") or 0),
                )
            sig = score_contract(c, price, sector, chain_volumes,
                                  oi_vs_avg, rvol, dampen, ticker=ticker)
            if sig:
                signals.append(sig)
    except Exception:
        pass
    return signals


# ── Phase 3: full scan ────────────────────────────────────────────────────────

def scan_unusual_flow(
    tickers: Optional[List[str]] = None,
    top_tickers: int = 80,
    min_score: int = 35,
    max_results: int = 150,
    exclude_etfs: bool = True,
) -> List[Dict]:
    """
    Full unusual flow scan — entirely data-driven, no hardcoded lists.

    1. Get ticker→sector map from S&P 500 Wikipedia + screeners (cached 6h)
    2. Screen for relative volume to find today's hot names
    3. Fetch options chains in parallel (10 threads)
    4. Score every contract and return ranked results
    """
    sector_map = get_ticker_sector_map()
    pool = tickers or list(sector_map.keys())
    pool = apply_pool_exclusions(pool, exclude_etfs)

    # Phase 1: find hot tickers
    hot = _screen_relvol(pool, top_n=top_tickers)

    # Get current prices
    prices: Dict[str, float] = {}
    try:
        raw = yf.download(hot, period="1d", interval="1m", group_by="ticker",
                          progress=False, threads=True, timeout=15, auto_adjust=True)
        for t in hot:
            try:
                h = (raw[t].dropna(how="all")
                     if isinstance(raw.columns, pd.MultiIndex) and t in raw.columns.get_level_values(0)
                     else raw.dropna(how="all"))
                if not h.empty:
                    prices[t] = float(h["Close"].iloc[-1])
            except Exception:
                pass
    except Exception:
        pass

    # Phase 2: options chains in parallel
    all_signals: List[Dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {
            ex.submit(
                _scan_ticker_options,
                t, prices.get(t, 0.0), sector_map.get(t, "Unknown")
            ): t for t in hot
        }
        for fut in as_completed(futures):
            try:
                all_signals.extend(fut.result())
            except Exception:
                pass

    all_signals.sort(key=lambda x: x["score"], reverse=True)
    return [s for s in all_signals if s["score"] >= min_score][:max_results]


# ── Sector summary ────────────────────────────────────────────────────────────

def sector_flow_summary(signals: List[Dict]) -> Dict[str, Dict]:
    """Roll up flow by sector."""
    summary: Dict[str, Dict] = {}
    for s in signals:
        sec = s["sector"]
        if sec not in summary:
            summary[sec] = {"notional": 0, "calls": 0, "puts": 0, "count": 0, "scores": []}
        summary[sec]["notional"] += s["notional"]
        summary[sec]["calls" if s["type"] == "call" else "puts"] += 1
        summary[sec]["count"] += 1
        summary[sec]["scores"].append(s["score"])
    for sec in summary:
        sc = summary[sec].pop("scores")
        summary[sec]["avg_score"] = round(sum(sc) / len(sc), 1) if sc else 0
    return dict(sorted(summary.items(), key=lambda x: x[1]["notional"], reverse=True))


# ── Formatted CLI output ──────────────────────────────────────────────────────

def fmt_notional(n: float) -> str:
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000:     return f"${n/1_000:.0f}K"
    return f"${n:.0f}"


def print_unusual_flow(signals: List[Dict], top_n: int = 30) -> None:
    if not signals:
        print("  No unusual flow detected.")
        return
    w = 95
    print(f"\n  {'─'*w}")
    print(f"  {'UNUSUAL OPTIONS FLOW':^{w}}")
    print(f"  {'─'*w}")
    print(f"  {'SIGNAL':<14} {'TICKER':<7} {'SECTOR':<24} {'TYPE':<5} "
          f"{'STRIKE':>7} {'EXP':<10} {'DTE':>4} "
          f"{'VOL':>7} {'OI':>7} {'V/OI':>5} "
          f"{'NOTIONAL':>9} {'SIDE':<5} {'SCR':>4}")
    print(f"  {'─'*w}")
    for s in signals[:top_n]:
        print(f"  {s['label']:<14} {s['ticker']:<7} {s['sector']:<24} "
              f"{s['type'].upper():<5} ${s['strike']:>6.0f} {s['expiry']:<10} {s['dte']:>4}d "
              f"{s['volume']:>7,} {s['open_interest']:>7,} {s['vol_oi']:>5.1f}x "
              f"{fmt_notional(s['notional']):>9} {s['trade_side']:<5} {s['score']:>4}")
    print(f"  {'─'*w}")
    print(f"  {len(signals)} unusual contracts found\n")
