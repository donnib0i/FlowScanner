"""
core/market_data.py -- Everything that talks to the outside world for quotes: yfinance session
and ticker normalization, the per-pass option-chain cache, VIX, batch
history/price fetches, and the flow-provenance record.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from typing import Optional, List, Dict, Tuple, Any
import time
import pandas as pd
import yfinance as yf


# ─── Flow provenance ─────────────────────────────────────────────────────────
# Which feed actually produced the flow the user is looking at. Recorded by the
# scan that ran, never inferred from configuration: TastyTrade can be fully
# configured and still fail every login, in which case the served flow is the
# 15-minute-delayed yfinance fallback and must not be labelled live.
_FLOW_SOURCE: Dict[str, Any] = {}


def reset_flow_source() -> None:
    _FLOW_SOURCE.clear()
    _FLOW_SOURCE.update(source=None, reason="no flow scan has run yet", ts=None)


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
