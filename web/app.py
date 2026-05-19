#!/usr/bin/env python3
"""
web_combined.py -- Unified Market Scanner Web UI
Combines options flow (scanner_app.py) + full ticker scan (FlowDigger/web.py)
into a single dark-mode FastAPI app with 4-tab mobile-first layout.

Run:   python3 web_combined.py
Open:  http://localhost:8765
"""

from __future__ import annotations
import asyncio, collections, contextlib, hmac, io, json, logging, os, re, sys, threading, time

logger = logging.getLogger(__name__)
from datetime import datetime
from typing import Dict, List, Optional

try:
    from fastapi import FastAPI, Query, Request, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response as _Response
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
except ImportError:
    sys.exit("pip install fastapi 'uvicorn[standard]'")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from data.finra_darkpool import get_darkpool_signals_cached
    _DARKPOOL_OK = True
except ImportError:
    _DARKPOOL_OK = False

try:
    from data.sec_insider import get_insider_signals_cached
    _INSIDER_OK = True
except ImportError:
    _INSIDER_OK = False

try:
    from data.fred_macro import get_macro_context_cached
    _FRED_OK = True
except ImportError:
    _FRED_OK = False

from core.scanner import (
    UNIVERSE, TICKER_SECTOR, SECTOR_ETFS,
    fetch_vix, vix_delta_target,
    scan_options_flow, get_best_contract,
    scan_sectors, calc_whale_score, fmt_whale_score,
    scan_tickers, apply_forward_directions,
    enrich_contracts, find_sector_laggards,
    apply_filter, apply_sort,
    FILTER_LABELS, SORT_LABELS,
    fetch_dynamic_universe,
    _TT_AVAILABLE,
)

PORT = int(os.environ.get("PORT", 8765))

# Insider scan default — equities only (ETFs have no Form 4 filings)
_ETF_PREFIXES = {"SPY","QQQ","IWM","DIA","MDY","VXX","UVXY","SVXY","TQQQ","SOXL","UPRO",
                 "SPXL","TNA","LABU","TECL","FNGU","SQQQ","SOXS","SPXS","TZA","LABD",
                 "TECS","FNGD","SDOW","GLD","SLV","USO","CPER","KWEB","FXI","XBI","ARKK"}
INSIDER_UNIVERSE = [t for t in UNIVERSE if t not in _ETF_PREFIXES][:35]
_PIN = os.environ.get("SCANNER_PIN", "").strip()

# Valid parameter enums
_VALID_DIRECTION  = {"up", "down"}
_VALID_DTE_TYPE   = {"weekly", "0dte", "swing"}
_VALID_BIAS       = {"both", "call", "put"}
_VALID_DTE_FILTER = {"all", "0dte", "7dte"}
_VALID_DTE_MODE   = {"0dte", "weekly", "all"}

_TICKER_RE = re.compile(r'^[A-Z0-9\^\.\-]{1,12}$')

def _validate_ticker(t: str) -> str:
    t = t.strip().upper()
    if not _TICKER_RE.match(t):
        raise HTTPException(400, f"Invalid ticker: {t!r}")
    return t

def _validate_enum(val: str, allowed: set, name: str) -> str:
    if val not in allowed:
        raise HTTPException(400, f"Invalid {name}: {val!r}. Must be one of {sorted(allowed)}")
    return val

# Rate limiter -- sliding window per IP
_MAX_RL_KEYS = 10_000

class _RateLimiter:
    def __init__(self):
        self._windows: Dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_secs: int) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._windows) >= _MAX_RL_KEYS and key not in self._windows:
                try:
                    self._windows.pop(next(iter(self._windows)))
                except StopIteration:
                    pass
            dq = self._windows.setdefault(key, collections.deque())
            cutoff = now - window_secs
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

_rl = _RateLimiter()

# TTL cache (5-min for scan results)
class _TTLCache:
    def __init__(self):
        self._store: dict = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and time.time() < entry[0]:
            return entry[1]
        return None

    def set(self, key: str, value, ttl_secs: int):
        self._store[key] = (time.time() + ttl_secs, value)

_cache = _TTLCache()

# Concurrent flow-scan guard
_active_scan = threading.Event()

def _client_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else "unknown"

def _check_rate(req: Request, endpoint: str, limit: int, window: int):
    key = f"{_client_ip(req)}:{endpoint}"
    if not _rl.allow(key, limit, window):
        raise HTTPException(429, detail="Too many requests -- slow down",
                            headers={"Retry-After": str(window)})

def _check_pin(req: Request):
    if not _PIN:
        return
    ip = _client_ip(req)
    supplied = (req.headers.get("x-pin", "") or req.query_params.get("pin", "")).strip()
    if not hmac.compare_digest(supplied.encode("utf-8", errors="replace"),
                               _PIN.encode("utf-8")):
        if not _rl.allow(f"{ip}:auth_fail", limit=10, window=300):
            raise HTTPException(429, detail="Too many failed attempts -- try later",
                                headers={"Retry-After": "300"})
        raise HTTPException(401, "Unauthorized")

# Default flow tickers
DEFAULT_FLOW_TICKERS = [
    "SPX","SPY","QQQ","IWM",
    "NVDA","AMD","AAPL","MSFT","META","AMZN","TSLA","GOOGL",
    "COIN","PLTR","MSTR","HOOD","MARA",
    "SOFI","AFRM","GME","HIMS",
    "TQQQ","SQQQ","SOXL","SOXS",
    "GS","JPM","VXX","UVXY",
]

# App setup
app = FastAPI(title="Scanner Pro", docs_url=None, redoc_url=None)

@app.exception_handler(Exception)
async def _generic_error(request: Request, exc: Exception):
    # Never expose internal errors to public
    logger.error("Unhandled error on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["x-pin"],
    max_age=600,
)

@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h["X-Content-Type-Options"]  = "nosniff"
    h["X-Frame-Options"]         = "DENY"
    h["X-XSS-Protection"]        = "1; mode=block"
    h["Referrer-Policy"]         = "no-referrer"
    h["Permissions-Policy"]      = "geolocation=(), camera=(), microphone=()"
    h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    h["Server"] = "scanner"
    h["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'"
    )
    if request.url.path == "/":
        h["Cache-Control"] = "no-store, no-cache, must-revalidate"
        h["Pragma"]        = "no-cache"
    elif request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        h["Cache-Control"] = "no-store, no-cache, must-revalidate, no-transform"
        h["Pragma"]        = "no-cache"
    return response

# Helpers
def _fmt(v: float) -> str:
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000:     return f"${v/1_000:.0f}K"
    return f"${v:.0f}"

def _badge(sig: Dict) -> str:
    if sig.get("golden_sweep"):  return "GOLDEN SWEEP"
    if sig.get("stacked_flow"):  return "STACKED"
    tier = sig.get("premium_tier", "retail")
    if tier == "whale":          return "WHALE"
    if tier == "block":          return "BLOCK"
    tc = sig.get("top_contract") or {}
    if tc.get("sweep"):          return "SWEEP"
    return "FLOW"

def _cls(sig: Dict) -> str:
    if sig.get("golden_sweep"):  return "golden"
    s = sig.get("whale_score", 0)
    if s >= 70: return "whale"
    if s >= 50: return "inst"
    return "retail"

def _serialize_flow(sig: Dict) -> Dict:
    tc    = sig.get("top_contract") or {}
    all_c = sig.get("call_contracts", []) + sig.get("put_contracts", [])
    top_s = sig.get("top_strike", tc.get("strike", 0))
    hits  = sum(1 for c in all_c if c.get("strike") == top_s) if top_s else 1

    bias_contracts = sig.get(
        "call_contracts" if sig["flow_bias"] == "call" else "put_contracts", []
    )
    top3 = sorted(bias_contracts, key=lambda c: c.get("flow", 0), reverse=True)[:3]
    top3_out = [{
        "strike":  c.get("strike", 0),
        "exp":     c.get("exp", "")[-5:],
        "dte":     c.get("dte", -1),
        "type":    c.get("type", "call"),
        "vol":     c.get("vol", 0),
        "oi":      c.get("oi", 0),
        "vol_oi":  round(c.get("vol_oi", 0), 1),
        "mid":     round(c.get("mid", 0), 2),
        "flow":    _fmt(c.get("flow", 0)),
        "sweep":   c.get("sweep", False),
        "golden":  c.get("golden_sweep", False),
        "tier":    c.get("premium_tier", "retail"),
    } for c in top3]

    score = sig.get("whale_score", 0)
    tier  = sig.get("premium_tier", "retail")
    is_institutional = score >= 40 or tier in ("block", "whale")

    return {
        "ts":         datetime.now().strftime("%H:%M"),
        "ticker":     sig["ticker"],
        "bias":       sig["flow_bias"],
        "badge":      _badge(sig),
        "cls":        _cls(sig),
        "score":      score,
        "total":      sig.get("total_flow", 0),
        "call_flow":  sig.get("call_flow", 0),
        "put_flow":   sig.get("put_flow", 0),
        "total_fmt":  _fmt(sig.get("total_flow", 0)),
        "call_fmt":   _fmt(sig.get("call_flow", 0)),
        "put_fmt":    _fmt(sig.get("put_flow", 0)),
        "pc_ratio":   round(sig.get("pc_ratio", 1), 2),
        "golden":     bool(sig.get("golden_sweep")),
        "stacked":    bool(sig.get("stacked_flow")),
        "side":       sig.get("trade_side", "mid"),
        "iv_skew":    round(sig.get("iv_skew", 0), 4),
        "hits":       hits,
        "dte0":       _fmt(sig.get("dte0_flow", 0)),
        "dte1_7":     _fmt(sig.get("dte1_7_flow", 0)),
        "dte8p":      _fmt(sig.get("dte8p_flow", 0)),
        "strike":     tc.get("strike", 0),
        "exp":        tc.get("exp", "")[-5:],
        "dte":        tc.get("dte", -1),
        "type":       tc.get("type", "call"),
        "vol":        tc.get("vol", 0),
        "oi":         tc.get("oi", 0),
        "vol_oi":     round(tc.get("vol_oi", 0), 1),
        "mid":        tc.get("mid", 0),
        "tier":       tier,
        "top3":       top3_out,
        "institutional": is_institutional,
    }

# Market helpers
def _et_now():
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except ImportError:
        import pytz
        return datetime.now(pytz.timezone("America/New_York"))

def is_market_open() -> bool:
    et = _et_now()
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= mins <= 16 * 60

# API endpoints

@app.get("/api/vix")
async def api_vix(req: Request):
    _check_pin(req)
    _check_rate(req, "vix", limit=30, window=60)
    loop = asyncio.get_event_loop()
    vix = await loop.run_in_executor(None, fetch_vix)
    tgt = vix_delta_target(vix)
    reg = ("Extreme Fear" if vix >= 40 else "Fear" if vix >= 30 else
           "Elevated" if vix >= 24 else "Normal" if vix >= 16 else
           "Calm" if vix >= 13 else "Complacent")
    return {"vix": round(vix, 2), "delta_target": round(tgt, 3), "regime": reg,
            "ts": datetime.now().strftime("%H:%M:%S")}

@app.get("/api/status")
async def api_status(req: Request):
    _check_pin(req)
    return {
        "flow_source": "tastytrade-live" if _TT_AVAILABLE else "yfinance-delayed",
        "live": _TT_AVAILABLE,
        "darkpool": _DARKPOOL_OK,
        "insider": _INSIDER_OK,
        "macro": _FRED_OK,
    }

@app.get("/api/universe")
async def api_universe(req: Request):
    _check_pin(req)
    _check_rate(req, "universe", limit=10, window=60)
    return {"quick": DEFAULT_FLOW_TICKERS, "full": UNIVERSE}

@app.get("/api/flow")
async def api_flow(
    req:       Request,
    tickers:   str = Query(",".join(DEFAULT_FLOW_TICKERS)),
    bias:      str = Query("both"),
    dte:       str = Query("all"),
    min_score: int = Query(40),
):
    """SSE stream of unusual options flow -- institutional/whale only."""
    _check_pin(req)
    _check_rate(req, "flow", limit=3, window=60)
    _validate_enum(bias, _VALID_BIAS, "bias")
    _validate_enum(dte,  _VALID_DTE_FILTER, "dte")
    if not (0 <= min_score <= 100):
        raise HTTPException(400, "min_score must be 0-100")
    if len(tickers) > 4096:
        raise HTTPException(400, "Ticker list too long")

    raw_tickers = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    ticker_list: List[str] = []
    for t in raw_tickers[:150]:
        try:
            ticker_list.append(_validate_ticker(t))
        except HTTPException:
            continue

    if not ticker_list:
        raise HTTPException(400, "No valid tickers provided")

    if _active_scan.is_set():
        raise HTTPException(503, detail="A scan is already running -- wait for it to finish.",
                            headers={"Retry-After": "30"})

    import queue as _q
    q: _q.Queue = _q.Queue()

    def on_progress(info): q.put({"__progress__": True, **info})
    def on_signal(sig):    q.put({"__signal__": True, "data": _serialize_flow(sig)})

    def run():
        try:
            scan_options_flow(ticker_list, show_progress=False,
                              on_signal=on_signal, on_progress=on_progress)
        except Exception:
            q.put({"__error__": "Scan failed -- check server logs"})
        finally:
            _active_scan.clear()
            q.put({"__done__": True})

    _active_scan.set()
    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        _active_scan.clear()
        raise HTTPException(500, "Failed to start scan")

    _start = time.monotonic()
    _SSE_TIMEOUT = 600

    async def generate():
        import queue as _q2
        while True:
            if time.monotonic() - _start > _SSE_TIMEOUT:
                yield 'data: {"__error__":"Scan timeout"}\n\n'
                break
            try:
                item = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, q.get, True, 1.0),
                    timeout=2.0
                )
            except (asyncio.TimeoutError, _q2.Empty):
                yield 'data: {"__ping__":true}\n\n'
                continue

            if item.get("__signal__"):
                s = item["data"]
                if not s.get("institutional") and s["score"] < min_score:
                    continue
                if bias == "call" and s["bias"] != "call": continue
                if bias == "put"  and s["bias"] != "put":  continue
                if dte  == "0dte" and s["dte"] != 0:       continue
                if dte  == "7dte" and s["dte"] > 7:        continue

            yield f"data: {json.dumps(item)}\n\n"
            if item.get("__done__") or item.get("__error__"):
                break

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={
                                 "Cache-Control":     "no-cache, no-transform",
                                 "X-Accel-Buffering": "no",
                                 "Connection":        "keep-alive",
                                 "Content-Type":      "text/event-stream; charset=utf-8",
                             })

@app.get("/api/scan")
async def api_scan(
    req:      Request,
    filter:   str = Query("any"),
    sort:     str = Query("setup"),
    dte_mode: str = Query("0dte"),
    dynamic:  str = Query("false"),
):
    """Full ticker scan -- 232 tickers ranked by setup quality. Cached 5 min."""
    _check_pin(req)
    _check_rate(req, "scan_full", limit=3, window=60)
    _validate_enum(dte_mode, _VALID_DTE_MODE, "dte_mode")

    cache_key = f"scan:{filter}:{sort}:{dte_mode}:{dynamic}"
    cached = _cache.get(cache_key)
    if cached:
        return JSONResponse(content=cached)

    loop = asyncio.get_event_loop()
    mo = is_market_open()

    tickers = list(UNIVERSE)
    if dynamic == "true":
        try:
            dyn = await loop.run_in_executor(None, lambda: fetch_dynamic_universe(top_n=40))
            added = [t for t in dyn if t not in tickers]
            tickers = list(dict.fromkeys(tickers + added))
        except Exception:
            pass

    with contextlib.redirect_stdout(io.StringIO()):
        try:
            results = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: scan_tickers(tickers, show_progress=False)),
                timeout=120.0
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Ticker scan timed out")

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            sector_data = await loop.run_in_executor(None, scan_sectors)
        apply_forward_directions(results, sector_data)
        find_sector_laggards(results, sector_data)
    except Exception:
        sector_data = {}

    try:
        vix = await loop.run_in_executor(None, fetch_vix)
        with contextlib.redirect_stdout(io.StringIO()):
            await loop.run_in_executor(
                None, lambda: enrich_contracts(results, top_n=15, vix=vix, dte_mode=dte_mode)
            )
    except Exception:
        vix = -1.0

    filtered = apply_filter(results, filter)
    ordered  = apply_sort(filtered, sort)

    def _grade(r):
        s = r["setup_q"] * 50 + r["opt_score"] * 0.30 + (20 if r.get("contract") else 0)
        return "A" if s >= 75 else "B" if s >= 55 else "C" if s >= 35 else "D"

    def _contract_out(c):
        if not c:
            return None
        return {
            "label":  c.get("label", ""),
            "strike": c.get("strike", 0),
            "exp":    c.get("exp", "")[-5:] if c.get("exp") else "",
            "dte":    c.get("dte", -1),
            "type":   c.get("type", "call"),
            "mid":    round(c.get("mid", 0), 2),
            "delta":  round(c.get("delta", 0), 3),
            "iv":     round((c.get("iv", 0) or 0) * 100, 1),
            "score":  c.get("score", 0),
            "roi":    round(c.get("roi", 0) or 0, 1),
        }

    rows = []
    for r in ordered:
        c = r.get("contract")
        rows.append({
            "ticker":     r["ticker"],
            "sector":     TICKER_SECTOR.get(r["ticker"], "Other"),
            "price":      round(r["price"], 2),
            "change_pct": round(r["change_pct"], 2),
            "rel_vol":    round(r["rel_vol"], 2),
            "setup":      r.get("signal_combo", r.get("hv_regime", "")),
            "opt_score":  r["opt_score"],
            "setup_q":    round(r["setup_q"], 3),
            "grade":      _grade(r),
            "direction":  r.get("direction", "up"),
            "contract":   _contract_out(c),
            "gap_pct":    round(r.get("gap_pct", 0), 2),
            "high_vol":   bool(r.get("high_vol")),
            "inside_day": bool(r.get("inside_day")),
            "is_laggard": bool(r.get("is_laggard")),
        })

    result = {
        "results":      rows,
        "total":        len(results),
        "filtered":     len(filtered),
        "vix":          round(vix, 2),
        "market_open":  mo,
        "last_updated": datetime.now().strftime("%H:%M:%S"),
        "filter":       filter,
        "sort":         sort,
    }
    _cache.set(cache_key, result, ttl_secs=300)
    return JSONResponse(content=result)

@app.get("/api/sectors")
async def api_sectors(req: Request):
    _check_pin(req)
    _check_rate(req, "sectors", limit=5, window=60)
    loop = asyncio.get_event_loop()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, scan_sectors), timeout=45.0
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Sector scan timed out")

    laggard = None
    try:
        items = [(k, v) for k, v in data.items()]
        if items:
            avg_chg = sum(v.get("change_pct", 0) for _, v in items) / len(items)
            worst_k, worst_v = min(items, key=lambda x: x[1].get("change_pct", 0) - avg_chg)
            laggard = {
                "ticker":  worst_v.get("etf", worst_k),
                "name":    worst_k,
                "lag_pct": round(worst_v.get("change_pct", 0) - avg_chg, 2),
            }
    except Exception:
        pass

    clean = [{
        "name":     k,
        "etf":      v.get("etf", ""),
        "change":   round(v.get("change_pct", 0), 2),
        "strength": round(v.get("strength", 0), 2),
        "price":    round(v.get("price", 0), 2),
        "rel_vol":  round(v.get("rel_vol", 1), 2),
        "bias":     v.get("bias", "neutral"),
    } for k, v in data.items()]

    if not clean:
        raise HTTPException(503, "No sector data -- market may be closed")

    return {
        "sectors":      sorted(clean, key=lambda x: x["change"], reverse=True),
        "laggard":      laggard,
        "last_updated": datetime.now().strftime("%H:%M:%S"),
    }

@app.get("/api/find")
async def api_find(
    req:       Request,
    ticker:    str = Query("SPY"),
    direction: str = Query("up"),
    dte_mode:  str = Query("all"),
):
    _check_pin(req)
    _check_rate(req, "find", limit=10, window=60)
    ticker    = _validate_ticker(ticker)
    direction = _validate_enum(direction, _VALID_DIRECTION, "direction")
    dte_mode  = _validate_enum(dte_mode, _VALID_DTE_MODE, "dte_mode")

    loop = asyncio.get_event_loop()
    vix  = await loop.run_in_executor(None, fetch_vix)
    try:
        contracts = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: get_best_contract(ticker, direction, 0, vix,
                                                top_n=3, dte_mode=dte_mode,
                                                target_price=0)
            ), timeout=30.0
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Contract lookup timed out")
    if not contracts:
        return JSONResponse({"error": f"No contracts found for {ticker} ({dte_mode})"}, status_code=404)
    if isinstance(contracts, dict):
        contracts = [contracts]
    return {"contracts": contracts, "ticker": ticker, "direction": direction,
            "dte_mode": dte_mode, "last_updated": datetime.now().strftime("%H:%M:%S")}

@app.get("/api/find/both")
async def api_find_both(
    req:      Request,
    ticker:   str = Query("SPY"),
    dte_mode: str = Query("all"),
):
    """
    Returns call vs put ladder comparison:
    dollar flow, volume, OI, DDOI (delta × OI) for top strikes on each side.
    """
    _check_pin(req)
    _check_rate(req, "find_both", limit=10, window=60)
    ticker   = _validate_ticker(ticker)
    dte_mode = _validate_enum(dte_mode, _VALID_DTE_MODE, "dte_mode")

    def _fetch():
        import yfinance as yf
        from scanner import _yf, vix_delta_target, fetch_vix
        import pandas as pd
        from datetime import datetime

        t = _yf(ticker)
        exps = t.options
        if not exps:
            return None

        try:
            price = float(t.fast_info.last_price or 0)
        except Exception:
            price = 0.0

        today = datetime.now().date()
        def dte(e):
            return (datetime.strptime(e, "%Y-%m-%d").date() - today).days

        try:
            import zoneinfo
            _now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        except ImportError:
            import pytz
            _now_et = datetime.now(pytz.timezone("America/New_York"))
        _market_open = _now_et.weekday() < 5 and (9*60+30) <= (_now_et.hour*60+_now_et.minute) <= 960
        min_dte = 0 if _market_open else 1
        future = [e for e in exps if dte(e) >= min_dte]

        if dte_mode == "0dte":
            cands = [e for e in future if dte(e) == 0] or [e for e in future if dte(e) <= 1]
        elif dte_mode == "weekly":
            cands = [e for e in future if 2 <= dte(e) <= 7] or [e for e in future if dte(e) <= 14]
        else:
            cands = future
        if not cands:
            cands = list(exps[:2])

        exp = cands[0]
        d = dte(exp)
        chain = t.option_chain(exp)
        calls_df = chain.calls.copy()
        puts_df  = chain.puts.copy()

        def _safe_int(v):
            try:
                f = float(v)
                return 0 if (f != f) else int(f)  # NaN check
            except Exception:
                return 0

        def _safe_float(v):
            try:
                f = float(v)
                return 0.0 if (f != f) else f
            except Exception:
                return 0.0

        def enrich(df, opt_type):
            rows = []
            for _, r in df.iterrows():
                vol  = _safe_int(r.get("volume", 0))
                oi   = _safe_int(r.get("openInterest", 0))
                bid  = _safe_float(r.get("bid", 0))
                ask  = _safe_float(r.get("ask", 0))
                mid  = (bid + ask) / 2 if bid + ask > 0 else _safe_float(r.get("lastPrice", 0))
                iv   = _safe_float(r.get("impliedVolatility", 0))
                strike = _safe_float(r.get("strike", 0))
                dollar_flow = vol * mid * 100
                # Simple delta proxy from moneyness
                if price > 0 and d > 0:
                    import math
                    moneyness = math.log(price / strike) if strike > 0 else 0
                    delta_proxy = max(0.01, min(0.99, 0.5 + moneyness * 5))
                    if opt_type == "put":
                        delta_proxy = -(1 - delta_proxy)
                else:
                    delta_proxy = 0.5 if opt_type == "call" else -0.5
                ddoi = abs(delta_proxy) * oi
                rows.append({
                    "strike": strike,
                    "type": opt_type,
                    "vol": vol,
                    "oi": oi,
                    "mid": round(mid, 2),
                    "iv": round(iv * 100, 1),
                    "dollar_flow": round(dollar_flow, 0),
                    "ddoi": round(ddoi, 0),
                    "delta": round(abs(delta_proxy), 3),
                })
            # Filter near-the-money strikes (±15% of price)
            if price > 0:
                rows = [r for r in rows if price*0.85 <= r["strike"] <= price*1.15]
            rows.sort(key=lambda x: x["dollar_flow"], reverse=True)
            return rows[:8]

        calls = enrich(calls_df, "call")
        puts  = enrich(puts_df,  "put")

        def side_totals(rows):
            return {
                "dollar_flow": sum(r["dollar_flow"] for r in rows),
                "volume":      sum(r["vol"] for r in rows),
                "oi":          sum(r["oi"] for r in rows),
                "ddoi":        sum(r["ddoi"] for r in rows),
            }

        call_totals = side_totals(calls)
        put_totals  = side_totals(puts)

        # Determine winner per metric
        def winner(c, p, key):
            return "call" if c[key] >= p[key] else "put"

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "exp": exp,
            "dte": d,
            "dte_mode": dte_mode,
            "calls": calls,
            "puts": puts,
            "call_totals": call_totals,
            "put_totals": put_totals,
            "flow_winner":   winner(call_totals, put_totals, "dollar_flow"),
            "vol_winner":    winner(call_totals, put_totals, "volume"),
            "oi_winner":     winner(call_totals, put_totals, "oi"),
            "ddoi_winner":   winner(call_totals, put_totals, "ddoi"),
            "last_updated": datetime.now().strftime("%H:%M:%S"),
        }

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=30.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Timed out")
    if not result:
        raise HTTPException(404, f"No chain data for {ticker}")
    return result


@app.get("/api/darkpool")
async def api_darkpool(req: Request, tickers: str = Query("")):
    _check_pin(req)
    _check_rate(req, "darkpool", limit=5, window=60)
    if not _DARKPOOL_OK:
        raise HTTPException(503, "finra_darkpool module not available")
    raw = tickers.strip().upper()
    if raw:
        ticker_list = [_validate_ticker(t) for t in raw.split(",") if t.strip()]
    else:
        ticker_list = list(UNIVERSE)[:80]  # default: first 80 tickers
    try:
        loop = asyncio.get_event_loop()
        signals = await asyncio.wait_for(
            loop.run_in_executor(None, get_darkpool_signals_cached, ticker_list),
            timeout=45,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Dark pool scan timed out")
    return {"signals": signals, "count": len(signals), "last_updated": datetime.now().strftime("%H:%M:%S")}


@app.get("/api/insider")
async def api_insider(req: Request, tickers: str = Query(""), days: int = Query(30)):
    _check_pin(req)
    _check_rate(req, "insider", limit=3, window=120)
    if not _INSIDER_OK:
        raise HTTPException(503, "sec_insider module not available")
    raw = tickers.strip().upper()
    if raw:
        ticker_list = [_validate_ticker(t) for t in raw.split(",") if t.strip()]
    else:
        ticker_list = list(INSIDER_UNIVERSE)
    days = max(7, min(90, days))
    try:
        loop = asyncio.get_event_loop()
        signals = await asyncio.wait_for(
            loop.run_in_executor(None, get_insider_signals_cached, ticker_list),
            timeout=60,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Insider scan timed out")
    return {"signals": signals, "count": len(signals), "days": days, "last_updated": datetime.now().strftime("%H:%M:%S")}


@app.get("/api/macro")
async def api_macro(req: Request):
    _check_pin(req)
    _check_rate(req, "macro", limit=10, window=60)
    if not _FRED_OK:
        raise HTTPException(503, "fred_macro module not available")
    try:
        loop = asyncio.get_event_loop()
        ctx = await asyncio.wait_for(
            loop.run_in_executor(None, get_macro_context_cached),
            timeout=20,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Macro fetch timed out")
    return ctx


@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Scanner Pro",
        "short_name": "Scanner",
        "description": "Options Flow + Market Scanner",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0a0a0f",
        "theme_color": "#0a0a0f",
        "orientation": "portrait",
        "icons": [{"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}],
    })

_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180" viewBox="0 0 180 180">'
    '<rect width="180" height="180" rx="40" fill="#0a0a0f"/>'
    '<text x="90" y="125" font-family="monospace" font-size="90" font-weight="700"'
    ' text-anchor="middle" fill="#00ff88">S</text>'
    '</svg>'
)

@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    return _Response(content=_ICON_SVG.encode(), media_type="image/svg+xml",
                     headers={"Cache-Control": "public, max-age=86400"})

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML

# HTML is stored in a separate variable below
HTML = ""  # assigned after class definition

# ---- HTML SOURCE -------------------------------------------------------
_HTML_PARTS = []

_HTML_PARTS.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a0a0f">
<meta name="apple-mobile-web-app-title" content="Scanner">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.json">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<title>Scanner Pro</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#0a0a0f;--bg2:#111118;--bg3:#1a1a24;
  --card:rgba(255,255,255,0.04);
  --border:rgba(255,255,255,0.08);--border2:rgba(255,255,255,0.14);
  --text:#e8e8f0;--sub:#6b6b80;
  --green:#00ff88;--green-dim:#00cc6a;
  --red:#ff3355;--red-dim:#cc2244;
  --gold:#ffb800;--amber:#ff8c00;
  --purple:#a855f7;--cyan:#00d4ff;--blue:#3b82f6;
  --safe-b:env(safe-area-inset-bottom,0px);
  --safe-t:env(safe-area-inset-top,0px);
  --font:'JetBrains Mono','SF Mono','Fira Code',monospace;
}
html,body{height:100%;overflow:hidden;font-family:var(--font);
  background:var(--bg);color:var(--text);font-size:13px;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 70% 50% at 20% 30%,rgba(0,255,136,.04) 0%,transparent 60%),
    radial-gradient(ellipse 60% 40% at 80% 70%,rgba(168,85,247,.04) 0%,transparent 60%),
    radial-gradient(ellipse 50% 40% at 50% 100%,rgba(59,130,246,.04) 0%,transparent 55%)}
#app{display:flex;flex-direction:column;height:100dvh;position:relative;z-index:1}
#topbar{flex-shrink:0;padding:calc(10px + var(--safe-t)) 16px 12px;
  background:rgba(10,10,15,0.92);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);border-bottom:1px solid var(--border)}
#content{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;
  padding-bottom:calc(64px + var(--safe-b))}
#tabbar{position:fixed;bottom:0;left:0;right:0;display:flex;
  background:rgba(10,10,15,0.96);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);border-top:1px solid var(--border);
  padding-bottom:var(--safe-b);z-index:50}
.tb-row1{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.app-name{font-size:16px;font-weight:800;letter-spacing:-.5px;flex:1;color:var(--green)}
.app-name span{font-size:10px;font-weight:500;color:var(--sub);letter-spacing:0;margin-left:4px}
.vix-pill{font-size:11px;font-weight:700;padding:4px 10px;border-radius:6px;
  background:var(--bg3);border:1px solid var(--border);color:var(--sub);letter-spacing:.3px}
.vix-pill.fear{color:var(--red);border-color:rgba(255,51,85,.3);background:rgba(255,51,85,.08)}
.vix-pill.calm{color:var(--green);border-color:rgba(0,255,136,.3);background:rgba(0,255,136,.06)}
.vix-pill.elevated{color:var(--amber);border-color:rgba(255,140,0,.3);background:rgba(255,140,0,.06)}
.bias-row{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700}
.bias-calls{color:var(--green)}.bias-puts{color:var(--red)}.bias-sep{color:var(--border2)}
.bias-dir{font-weight:800;font-size:11px}
.bias-dir.bull{color:var(--green)}.bias-dir.bear{color:var(--red)}.bias-dir.neut{color:var(--sub)}
.flow-bar-wrap{height:3px;background:var(--border);border-radius:2px;overflow:hidden;
  margin:4px 0 0;display:none}
.flow-bar-wrap.on{display:block}
.flow-bar-fill{height:100%;background:var(--green);transition:width .4s ease;float:left}
.flow-bar-put{height:100%;background:var(--red);overflow:hidden}
.tab-btn{flex:1;padding:10px 4px 8px;background:none;border:none;color:var(--sub);
  font-size:9px;font-weight:700;letter-spacing:.8px;cursor:pointer;
  display:flex;flex-direction:column;align-items:center;gap:3px;
  font-family:var(--font);transition:color .15s}
.tab-btn svg{width:20px;height:20px;stroke-width:1.8;fill:none;stroke:currentColor}
.tab-btn.active{color:var(--green)}
.tab-pane{display:none}.tab-pane.active{display:block}
.filter-bar{display:flex;gap:6px;padding:12px 14px 8px;overflow-x:auto;
  scrollbar-width:none;align-items:center;flex-wrap:nowrap}
.filter-bar::-webkit-scrollbar{display:none}
.chip{background:var(--bg3);border:1px solid var(--border);border-radius:6px;
  padding:8px 13px;font-size:11px;font-weight:700;color:var(--sub);
  cursor:pointer;white-space:nowrap;flex-shrink:0;font-family:var(--font);
  letter-spacing:.3px;transition:all .15s;min-height:38px;display:flex;align-items:center}
.chip:active{transform:scale(.95)}
.chip.on{background:rgba(0,255,136,.08);border-color:rgba(0,255,136,.3);color:var(--green)}
.scan-btn{background:var(--green);color:#0a0a0f;border:none;border-radius:6px;
  padding:8px 18px;font-size:12px;font-weight:800;cursor:pointer;
  white-space:nowrap;margin-left:auto;flex-shrink:0;min-height:38px;
  font-family:var(--font);letter-spacing:.5px;transition:transform .15s,opacity .15s}
.scan-btn:active{transform:scale(.95)}
.scan-btn.loading{background:var(--bg3);color:var(--sub);cursor:default}
.prog-wrap{height:2px;background:var(--border);overflow:hidden;margin:0}
.prog-bar{height:100%;background:linear-gradient(90deg,var(--green),var(--cyan));
  width:0%;transition:width .3s ease}
.prog-lbl{text-align:center;font-size:10px;color:var(--sub);padding:5px 0 2px;
  font-family:var(--font);letter-spacing:.3px}
.sec-head{padding:12px 14px 6px;font-size:9px;font-weight:700;letter-spacing:1.5px;
  color:var(--sub);text-transform:uppercase;display:flex;align-items:center;justify-content:space-between}
.sec-head .ts{font-size:9px;color:var(--sub);font-weight:500;letter-spacing:0}
#flow-feed{padding:4px 0 8px}
.flow-card{margin:6px 12px;background:var(--card);border:1px solid var(--border);
  border-radius:10px;overflow:hidden;cursor:pointer;transition:border-color .15s}
.flow-card:active{opacity:.85}
.flow-card.golden{border-color:rgba(255,184,0,.25);background:rgba(255,184,0,.04);
  animation:goldPulse 3s ease-in-out infinite}
.flow-card.whale{border-color:rgba(168,85,247,.2);background:rgba(168,85,247,.04)}
.flow-card.inst{border-color:rgba(0,212,255,.15)}
@keyframes goldPulse{0%,100%{box-shadow:0 0 0 0 transparent}50%{box-shadow:0 0 12px rgba(255,184,0,.15)}}
.card-head{padding:12px 14px 8px;display:flex;align-items:flex-start;gap:10px}
.badge{font-size:8px;font-weight:800;letter-spacing:.8px;padding:3px 7px;
  border-radius:4px;text-transform:uppercase;flex-shrink:0;margin-top:2px;font-family:var(--font)}
.badge.golden{background:rgba(255,184,0,.15);color:var(--gold);border:1px solid rgba(255,184,0,.3)}
.badge.whale{background:rgba(168,85,247,.12);color:var(--purple);border:1px solid rgba(168,85,247,.25)}
.badge.stacked{background:rgba(0,212,255,.1);color:var(--cyan);border:1px solid rgba(0,212,255,.2)}
.badge.sweep{background:rgba(255,140,0,.1);color:var(--amber);border:1px solid rgba(255,140,0,.2)}
.badge.block,.badge.flow{background:var(--bg3);color:var(--sub);border:1px solid var(--border)}
.card-title{flex:1;min-width:0}
.card-ticker{font-size:20px;font-weight:800;letter-spacing:-.5px;color:var(--text)}
.card-sub{font-size:10px;color:var(--sub);margin-top:2px}
.card-premium{text-align:right;flex-shrink:0}
.card-amount{font-size:18px;font-weight:800;letter-spacing:-.5px}
.card-amount.call{color:var(--green)}.card-amount.put{color:var(--red)}
.card-alabel{font-size:9px;color:var(--sub);margin-top:2px;text-align:right}
.dte-zero-dot{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--red);margin-right:3px;vertical-align:middle;
  animation:zeroPulse 1.2s ease-in-out infinite}
@keyframes zeroPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}
.card-stats{display:flex;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.cstat{flex:1;padding:8px 10px;text-align:center;border-right:1px solid var(--border)}
.cstat:last-child{border-right:none}
.cstat label{display:block;font-size:8px;letter-spacing:.5px;color:var(--sub);
  text-transform:uppercase;margin-bottom:2px}
.cstat .v{font-size:13px;font-weight:700}
.v.hot{color:var(--red)}.v.warm{color:var(--amber)}.v.cool{color:var(--sub)}
.v.call{color:var(--green)}.v.put{color:var(--red)}
.v.ask{color:var(--green)}.v.bid{color:var(--red)}.v.mid{color:var(--sub)}
.score-bar-wrap{padding:8px 14px;display:flex;align-items:center;gap:8px}
.score-bar-track{flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.score-bar-fill{height:100%;border-radius:2px;transition:width .6s ease}
.score-bar-fill.whale{background:var(--green)}
.score-bar-fill.inst{background:var(--cyan)}
.score-bar-fill.retail{background:var(--sub)}
.score-num{font-size:11px;font-weight:800;min-width:24px;text-align:right}
.score-num.whale{color:var(--green)}.score-num.inst{color:var(--cyan)}.score-num.retail{color:var(--sub)}
.contracts-row{display:flex;gap:6px;padding:8px 12px;overflow-x:auto;scrollbar-width:none}
.contracts-row::-webkit-scrollbar{display:none}
.cc{flex-shrink:0;background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;padding:8px 11px;min-width:100px}
.cc.top1{border-color:rgba(0,255,136,.2);background:rgba(0,255,136,.04)}
.cc.golden-c{border-color:rgba(255,184,0,.2);background:rgba(255,184,0,.04)}
.cc-strike{font-size:14px;font-weight:800;letter-spacing:-.3px}
.cc-strike.call{color:var(--green)}.cc-strike.put{color:var(--red)}
.cc-meta{font-size:9px;color:var(--sub);margin-top:2px}
.cc-price{font-size:12px;font-weight:700;margin-top:4px}
.cc-voi{font-size:9px;margin-top:1px}
.cc-voi.hot{color:var(--red)}.cc-voi.warm{color:var(--amber)}.cc-voi.cool{color:var(--sub)}
.card-detail{padding:10px 14px 12px;border-top:1px solid var(--border);
  display:none;background:rgba(255,255,255,.02)}
.card-detail.open{display:block}
.dg{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}
.dg-item label{font-size:8px;color:var(--sub);letter-spacing:.4px;
  text-transform:uppercase;display:block;margin-bottom:2px}
.dg-item span{font-size:13px;font-weight:700;color:var(--text)}
.dte-segs{display:flex;gap:6px}
.dte-seg{flex:1;background:var(--bg3);border:1px solid var(--border);
  border-radius:7px;padding:8px;text-align:center}
.dte-seg label{font-size:8px;color:var(--sub);display:block;margin-bottom:3px;
  letter-spacing:.5px;text-transform:uppercase}
.dte-seg span{font-size:12px;font-weight:700}
#scan-table-wrap{overflow-x:auto;padding:0 0 8px}
.scan-table{width:100%;border-collapse:collapse;font-size:11px}
.scan-table th{text-align:left;padding:8px 12px;font-size:9px;letter-spacing:.8px;
  color:var(--sub);border-bottom:1px solid var(--border);white-space:nowrap;
  background:var(--bg);position:sticky;top:0;z-index:2;font-weight:700}
.scan-table td{padding:8px 12px;border-bottom:1px solid rgba(255,255,255,.04);
  white-space:nowrap;vertical-align:middle}
.scan-table tr:hover td{background:rgba(255,255,255,.02)}
.ticker-cell{font-size:13px;font-weight:800;color:var(--text)}
.sector-cell{font-size:9px;color:var(--sub);letter-spacing:.3px}
.chg-up{color:var(--green);font-weight:700}.chg-dn{color:var(--red);font-weight:700}
.setup-badge{display:inline-block;font-size:8px;font-weight:800;padding:2px 6px;
  border-radius:4px;letter-spacing:.4px;border:1px solid;white-space:nowrap}
.setup-badge.A{color:var(--green);border-color:rgba(0,255,136,.3);background:rgba(0,255,136,.08)}
.setup-badge.B{color:var(--cyan);border-color:rgba(0,212,255,.3);background:rgba(0,212,255,.06)}
.setup-badge.C{color:var(--amber);border-color:rgba(255,140,0,.3);background:rgba(255,140,0,.06)}
.setup-badge.D{color:var(--sub);border-color:var(--border);background:var(--bg3)}
.contract-cell{font-size:10px;color:var(--sub)}
.opt-bar-wrap{width:40px;display:inline-block;height:3px;background:var(--border);
  border-radius:2px;vertical-align:middle;margin-right:4px}
.opt-bar-fill{height:100%;border-radius:2px;background:var(--cyan)}
.sector-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:12px}
@media(min-width:600px){.sector-grid{grid-template-columns:repeat(3,1fr)}}
@media(min-width:900px){.sector-grid{grid-template-columns:repeat(5,1fr)}}
.sector-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
  padding:14px;cursor:pointer;transition:border-color .15s}
.sector-card:active{transform:scale(.97)}
.sector-card.up{border-color:rgba(0,255,136,.15)}
.sector-card.dn{border-color:rgba(255,51,85,.15)}
.sc-name{font-size:12px;font-weight:700;color:var(--text);margin-bottom:2px}
.sc-etf{font-size:9px;color:var(--sub);letter-spacing:.3px}
.sc-chg{font-size:22px;font-weight:800;letter-spacing:-.5px;margin:10px 0 4px}
.sc-chg.up{color:var(--green)}.sc-chg.dn{color:var(--red)}
.sc-bar-track{height:3px;background:var(--border);border-radius:2px;overflow:hidden;margin-bottom:8px}
.sc-bar-fill{height:100%;border-radius:2px}
.sc-bar-fill.up{background:var(--green)}.sc-bar-fill.dn{background:var(--red)}
.sc-meta{font-size:9px;color:var(--sub);display:flex;gap:8px}
.laggard-box{margin:0 12px 8px;background:rgba(0,212,255,.06);border:1px solid rgba(0,212,255,.2);
  border-radius:10px;padding:12px 14px}
.laggard-label{font-size:9px;font-weight:700;letter-spacing:.8px;color:var(--cyan);
  text-transform:uppercase;margin-bottom:4px}
.laggard-ticker{font-size:18px;font-weight:800;color:var(--text)}
.laggard-desc{font-size:10px;color:var(--sub);margin-top:2px}
.finder{padding:16px 14px}
.find-input{width:100%;background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-size:22px;font-weight:800;
  letter-spacing:2px;padding:14px 16px;text-transform:uppercase;outline:none;
  font-family:var(--font);transition:border-color .2s}
.find-input:focus{border-color:rgba(0,255,136,.4)}
.find-input::placeholder{color:var(--sub);font-weight:500;letter-spacing:1px}
.dir-row{display:flex;gap:8px;margin-top:10px}
.dir-btn{flex:1;padding:11px;border:1px solid var(--border);border-radius:8px;
  background:var(--bg3);color:var(--sub);font-size:13px;font-weight:700;
  cursor:pointer;font-family:var(--font);letter-spacing:.5px;transition:all .15s}
.dir-btn:active{transform:scale(.96)}
.dir-btn.up.on{background:rgba(0,255,136,.08);border-color:rgba(0,255,136,.3);color:var(--green)}
.dir-btn.dn.on{background:rgba(255,51,85,.08);border-color:rgba(255,51,85,.3);color:var(--red)}
.dte-row{display:flex;gap:8px;margin-top:10px}
.dte-btn{flex:1;padding:10px 6px;border:1px solid var(--border);border-radius:8px;
  background:var(--bg3);color:var(--sub);font-size:11px;font-weight:700;
  cursor:pointer;font-family:var(--font);text-align:center;transition:all .15s}
.dte-btn:active{transform:scale(.96)}
.dte-btn.on{background:rgba(0,255,136,.08);border-color:rgba(0,255,136,.3);color:var(--green)}
.dte-sub{display:block;font-size:9px;font-weight:500;color:var(--sub);margin-top:2px}
.dte-btn.on .dte-sub{color:rgba(0,255,136,.6)}
.find-go{width:100%;margin-top:12px;padding:14px;
  background:var(--green);color:#0a0a0f;border:none;border-radius:8px;
  font-size:13px;font-weight:800;cursor:pointer;font-family:var(--font);
  letter-spacing:.8px;transition:transform .15s,opacity .15s}
.find-go:active{transform:scale(.97)}
.find-go.loading{background:var(--bg3);color:var(--sub)}
#find-result{margin-top:14px}
.cont-cards{display:flex;flex-direction:column;gap:8px}
.cont-card{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.cont-card.best{border-color:rgba(0,255,136,.2)}
.cont-hero{padding:14px 14px 10px;display:flex;justify-content:space-between;align-items:flex-start}
.cont-sym{font-size:22px;font-weight:800;letter-spacing:-.5px}
.cont-sym .call{color:var(--green)}.cont-sym .put{color:var(--red)}
.cont-sym .ks{font-size:15px;font-weight:500;color:var(--sub)}
.cont-badge{font-size:9px;font-weight:700;padding:3px 8px;border-radius:4px}
.cont-badge.best{background:rgba(0,255,136,.1);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.cont-badge.alt{background:var(--bg3);color:var(--sub);border:1px solid var(--border)}
.cont-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border)}
.cg{background:var(--bg);padding:10px 12px}
.cg label{font-size:9px;color:var(--sub);display:block;letter-spacing:.4px;
  margin-bottom:2px;text-transform:uppercase}
.cg span{font-size:14px;font-weight:700;color:var(--text)}
.cg span.g{color:var(--green)}.cg span.r{color:var(--red)}
.cg span.cy{color:var(--cyan)}.cg span.gd{color:var(--gold)}
.cont-note{padding:8px 14px;font-size:9px;color:var(--sub);border-top:1px solid var(--border);letter-spacing:.2px}
.skel{background:linear-gradient(90deg,var(--bg3) 25%,rgba(255,255,255,.05) 50%,var(--bg3) 75%);
  background-size:200% 100%;animation:skel 1.4s infinite;border-radius:8px}
@keyframes skel{0%{background-position:200% 0}100%{background-position:-200% 0}}
.empty-st{text-align:center;color:var(--sub);padding:48px 24px 32px;line-height:1.7}
.empty-st .icon{font-size:36px;margin-bottom:10px}
.empty-st h3{font-size:15px;font-weight:700;color:var(--text);margin-bottom:6px}
.empty-st p{font-size:12px}
#toast{position:fixed;top:calc(70px + var(--safe-t));left:50%;transform:translateX(-50%);
  background:rgba(20,20,30,.96);border:1px solid var(--border2);border-radius:8px;
  padding:8px 18px;font-size:12px;font-weight:600;color:var(--text);
  box-shadow:0 8px 32px rgba(0,0,0,.4);z-index:200;opacity:0;
  transition:opacity .2s;pointer-events:none;white-space:nowrap}
#toast.show{opacity:1}
#toast.err{border-color:rgba(255,51,85,.4);color:var(--red)}
::-webkit-scrollbar{width:3px;height:3px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:2px}
.load-btn{display:block;width:calc(100% - 24px);margin:12px;
  background:var(--bg3);border:1px solid var(--border);border-radius:8px;
  padding:13px;text-align:center;color:var(--green);font-weight:700;
  font-size:13px;cursor:pointer;font-family:var(--font);letter-spacing:.5px;transition:all .15s}
.load-btn:active{transform:scale(.97)}
.scan-controls{display:flex;gap:6px;padding:10px 14px 6px;flex-wrap:wrap;align-items:center}
.sel{background:var(--bg3);border:1px solid var(--border);border-radius:6px;
  color:var(--text);font-family:var(--font);font-size:11px;padding:6px 10px;cursor:pointer;outline:none}
.sel option{background:var(--bg2)}
.scan-stat{font-size:10px;color:var(--sub);margin-left:auto;letter-spacing:.3px}
#hot-feed{padding:4px 0 8px}
.hot-card{margin:5px 12px;background:var(--card);border:1px solid var(--border);
  border-radius:8px;padding:11px 14px;display:flex;align-items:center;gap:10px}
.hot-rank{font-size:16px;font-weight:800;color:var(--sub);width:22px;text-align:center;flex-shrink:0}
.hot-rank.t1{color:var(--gold)}.hot-rank.t2{color:var(--sub)}.hot-rank.t3{color:var(--amber)}
.hot-info{flex:1;min-width:0}
.hot-sym{font-size:14px;font-weight:800}
.hot-sym .call{color:var(--green)}.hot-sym .put{color:var(--red)}
.hot-meta{font-size:10px;color:var(--sub);margin-top:1px}
.hot-right{text-align:right;flex-shrink:0}
.hot-voi{font-size:16px;font-weight:800}
.hot-voi.fire{color:var(--red)}.hot-voi.hot{color:var(--amber)}.hot-voi.warm{color:var(--gold)}
.hot-flow{font-size:10px;color:var(--sub);margin-top:1px}
.view-toggle{display:flex;gap:4px;margin:6px 12px 2px;background:var(--bg3);
  border-radius:7px;padding:3px}
.vt-btn{flex:1;padding:6px;border:none;border-radius:5px;background:none;
  color:var(--sub);font-size:11px;font-weight:700;cursor:pointer;
  font-family:var(--font);letter-spacing:.3px;transition:all .15s}
.vt-btn.on{background:var(--bg2);color:var(--text)}
</style>
</head>
<body>
<div id="app">
<div id="topbar">
  <div class="tb-row1">
    <div class="app-name">SCANNER<span>PRO</span></div>
    <div class="vix-pill" id="vix-chip">VIX ...</div>
  </div>
  <div class="bias-row">
    <span class="bias-calls" id="bc">CALLS -</span>
    <span class="bias-sep"> . </span>
    <span class="bias-puts" id="bp">PUTS -</span>
    <span class="bias-sep"> . </span>
    <span class="bias-dir neut" id="bd">-</span>
  </div>
  <div class="flow-bar-wrap" id="flow-bar">
    <div class="flow-bar-fill" id="flow-fill" style="width:50%"></div>
    <div class="flow-bar-put"></div>
  </div>
</div>
<div id="content">
  <div class="tab-pane active" id="tab-flow">
    <div class="filter-bar">
      <button class="chip" id="c-dte" onclick="tDte()">0DTE</button>
      <button class="chip" id="c-whale" onclick="tWhale()">WHALE+</button>
      <button class="chip" id="c-scope" onclick="tScope()">QUICK</button>
      <button class="scan-btn" id="scan-btn" onclick="doFlowScan()">SCAN</button>
    </div>
    <div class="prog-wrap" id="pw" style="display:none"><div class="prog-bar" id="pb"></div></div>
    <div class="prog-lbl" id="pl" style="display:none"></div>
    <div class="view-toggle" id="view-toggle" style="display:none">
      <button class="vt-btn on" id="vt-sig" onclick="setView('signals')">SIGNALS</button>
      <button class="vt-btn" id="vt-hot" onclick="setView('hot')">HOT CONTRACTS</button>
    </div>
    <div id="flow-feed">
      <div class="empty-st">
        <div class="icon">&#9889;</div>
        <h3>Ready to scan flow</h3>
        <p>Hit SCAN to pull live options flow.<br>Institutional only - no retail noise.</p>
      </div>
    </div>
    <div id="hot-feed" style="display:none"></div>
  </div>
  <div class="tab-pane" id="tab-scan">
    <div class="scan-controls">
      <select class="sel" id="scan-filter" onchange="updateScanFilter()">
        <option value="any">ALL SETUPS</option>
        <option value="gap">GAP FILLS</option>
        <option value="hv">HIGH VOL</option>
        <option value="inside">INSIDE DAY</option>
        <option value="breakout">BREAKOUT</option>
        <option value="laggard">LAGGARDS</option>
      </select>
      <select class="sel" id="scan-sort" onchange="updateScanSort()">
        <option value="setup">BY SETUP</option>
        <option value="opt">BY OPT SCORE</option>
        <option value="vol">BY REL VOL</option>
        <option value="chg">BY CHANGE%</option>
      </select>
      <select class="sel" id="scan-dte">
        <option value="0dte">0DTE</option>
        <option value="weekly">WEEKLY</option>
        <option value="all">ALL</option>
      </select>
      <span class="scan-stat" id="scan-stat"></span>
    </div>
    <div style="padding:0 14px 6px">
      <button class="load-btn" id="scan-run-btn" onclick="runFullScan()">RUN FULL SCAN (232 tickers)</button>
    </div>
    <div id="scan-table-wrap">
      <div class="empty-st" id="scan-empty">
        <div class="icon">&#128202;</div>
        <h3>Full ticker scan</h3>
        <p>232 tickers ranked by setup quality,<br>options score, and contract fit.</p>
      </div>
    </div>
  </div>
  <div class="tab-pane" id="tab-sectors">
    <div id="sectors-feed">
      <button class="load-btn" onclick="loadSectors()">LOAD SECTOR MAP</button>
    </div>
  </div>
  <div class="tab-pane" id="tab-intel">
    <div style="padding:12px 16px 6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input id="intel-tickers" placeholder="AAPL,MSFT,NVDA (blank = full scan)" style="flex:1;min-width:160px;background:#111;border:1px solid #222;color:#e0e0e0;padding:8px 10px;border-radius:6px;font-size:12px;font-family:inherit">
      <button class="load-btn" onclick="loadIntel()">LOAD INTEL</button>
    </div>
    <div style="padding:0 16px 60px">
      <div style="font-size:9px;letter-spacing:.8px;color:var(--sub);margin:8px 0 4px">MACRO REGIME</div>
      <div id="intel-macro" style="font-size:12px;color:var(--sub)">— press LOAD INTEL —</div>
      <div style="font-size:9px;letter-spacing:.8px;color:var(--sub);margin:14px 0 4px">DARK POOL ANOMALIES</div>
      <div id="intel-dp" style="font-size:12px;color:var(--sub)">— press LOAD INTEL —</div>
      <div style="font-size:9px;letter-spacing:.8px;color:var(--sub);margin:14px 0 4px">INSIDER ACTIVITY (30 DAYS)</div>
      <div id="intel-ins" style="font-size:12px;color:var(--sub)">— press LOAD INTEL —</div>
    </div>
  </div>

  <div class="tab-pane" id="tab-find">
    <div class="finder">
      <input class="find-input" id="ft" placeholder="NVDA" maxlength="6"
             autocorrect="off" autocapitalize="characters" spellcheck="false"
             autocomplete="off" inputmode="text"
             oninput="this.value=this.value.toUpperCase()"
             onkeydown="if(event.key==='Enter')doFind()">
      <div class="dir-row">
        <button class="dir-btn up on" id="d-up" onclick="setDir('up')">&#9650; CALLS</button>
        <button class="dir-btn dn" id="d-dn" onclick="setDir('down')">&#9660; PUTS</button>
      </div>
      <div class="dte-row">
        <button class="dte-btn on" id="dt-0dte" onclick="setDteMode('0dte')">
          0DTE<span class="dte-sub">Today only</span>
        </button>
        <button class="dte-btn" id="dt-weekly" onclick="setDteMode('weekly')">
          WEEKLY<span class="dte-sub">7 days</span>
        </button>
        <button class="dte-btn" id="dt-all" onclick="setDteMode('all')">
          ALL<span class="dte-sub">Best fit</span>
        </button>
      </div>
      <button class="find-go" id="find-btn" onclick="doFind()">FIND TOP 3 CONTRACTS</button>
      <button class="find-go" id="both-btn" onclick="doFindBoth()" style="margin-top:6px;color:var(--amber)">&#9654; CALLS vs PUTS LADDER</button>
      <div id="find-result"></div>
      <div id="both-result"></div>
    </div>
  </div>
</div>
<div id="source-badge" style="text-align:center;padding:4px 0 0;font-size:9px;letter-spacing:.8px;color:#444">LOADING...</div>
<div id="tabbar">
  <button class="tab-btn active" onclick="showTab('flow',this)">
    <svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>FLOW
  </button>
  <button class="tab-btn" onclick="showTab('scan',this)">
    <svg viewBox="0 0 24 24"><polyline points="3 6 10 6 10 18"/><polyline points="14 6 21 6 21 18"/><line x1="3" y1="12" x2="21" y2="12"/></svg>SCAN
  </button>
  <button class="tab-btn" onclick="showTab('sectors',this)">
    <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>SECTORS
  </button>
  <button class="tab-btn" onclick="showTab('find',this)">
    <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>FIND
  </button>
  <button class="tab-btn" onclick="showTab('intel',this)">
    <svg viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>INTEL
  </button>
</div>
</div>
<div id="toast"></div>""")

_HTML_PARTS.append("""
<script>
// PIN never injected into HTML — stored in localStorage only
let PIN = localStorage.getItem('scanner_pin') || '';
const _pa = p => PIN ? (p.includes('?') ? p+'&pin='+encodeURIComponent(PIN) : p+'?pin='+encodeURIComponent(PIN)) : p;

function _promptPin(msg){
  const p = prompt(msg || 'Enter access PIN:');
  if(p !== null){
    PIN = p.trim();
    localStorage.setItem('scanner_pin', PIN);
  }
}

// On 401, prompt for PIN and reload
function _handleAuth(resp){
  if(resp.status === 401){
    localStorage.removeItem('scanner_pin');
    PIN = '';
    _promptPin('PIN required. Enter access PIN:');
    return true;
  }
  return false;
}

function _isMarketOpen(){
  try{
    const et=new Date(new Date().toLocaleString('en-US',{timeZone:'America/New_York'}));
    const d=et.getDay();
    if(d===0||d===6) return false;
    const m=et.getHours()*60+et.getMinutes();
    return m>=570&&m<=960;
  }catch{return true}
}

const S={
  dte:_isMarketOpen()?'0dte':'all',
  whale:false,full:false,
  dir:'up',dteMode:'0dte',
  scanning:false,scanRunning:false,
  callFlow:0,putFlow:0,
  signals:[],hotContracts:[],
  view:'signals',
  qt:[],ft:[],
  scanData:[],scanFilter:'any',scanSort:'setup',
};

(function(){
  const lbl={'0dte':'0DTE','7dte':'7 DTE','all':'ALL DTE'};
  const el=document.getElementById('c-dte');
  el.textContent=lbl[S.dte]||'0DTE';
  el.className='chip'+(S.dte!=='all'?' on':'');
})();

function _loadVix(attempt){
  attempt=attempt||0;
  fetch(_pa('/api/vix')).then(r=>{if(_handleAuth(r))return Promise.reject('auth');return r.ok?r.json():Promise.reject()}).then(d=>{
    renderVix(d);
    if(d.vix<=0&&attempt<6) setTimeout(()=>_loadVix(attempt+1),15000);
    else setTimeout(()=>_loadVix(0),90000);
  }).catch(()=>{
    if(attempt<8) setTimeout(()=>_loadVix(attempt+1),attempt<2?5000:10000);
  });
}
function _loadUniverse(attempt){
  attempt=attempt||0;
  fetch(_pa('/api/universe')).then(r=>r.ok?r.json():Promise.reject()).then(d=>{
    S.qt=d.quick;S.ft=d.full;
    if(S.full) document.getElementById('c-scope').textContent='FULL ('+S.ft.length+')';
  }).catch(()=>{
    if(attempt<8) setTimeout(()=>_loadUniverse(attempt+1),attempt<2?5000:10000);
  });
}
_loadVix(0);
_loadUniverse(0);
fetch(_pa('/api/status')).then(r=>r.ok?r.json():null).then(d=>{
  if(!d) return;
  const badge=document.getElementById('source-badge');
  if(d.live){
    badge.textContent='● LIVE — TastyTrade OPRA feed';
    badge.style.color='#00ff88';
  } else {
    badge.textContent='○ DELAYED — yfinance 15min';
    badge.style.color='#555';
  }
}).catch(()=>{});

function renderVix(d){
  const el=document.getElementById('vix-chip');
  const closed=!_isMarketOpen();
  if(d.vix<=0){
    el.textContent=closed?'CLOSED':'VIX -';
    el.className=closed?'vix-pill elevated':'vix-pill';
    return;
  }
  el.textContent=closed?'VIX '+d.vix.toFixed(1)+' CLOSED':'VIX '+d.vix.toFixed(1)+' '+d.regime.toUpperCase();
  el.className='vix-pill '+(d.vix>=30?'fear':d.vix>=24?'elevated':d.vix<16?'calm':'');
}

function showTab(n,btn){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+n).classList.add('active');
  btn.classList.add('active');
  if(n!=='flow') document.getElementById('flow-bar').classList.remove('on');
}

const dteOpts=['0dte','7dte','all'];
const dteLbls={'0dte':'0DTE','7dte':'7 DTE','all':'ALL DTE'};
function tDte(){
  S.dte=dteOpts[(dteOpts.indexOf(S.dte)+1)%3];
  const el=document.getElementById('c-dte');
  el.textContent=dteLbls[S.dte];
  el.className='chip'+(S.dte!=='all'?' on':'');
}
function tWhale(){
  S.whale=!S.whale;
  document.getElementById('c-whale').className='chip'+(S.whale?' on':'');
}
function tScope(){
  S.full=!S.full;
  const el=document.getElementById('c-scope');
  el.textContent=S.full?'FULL ('+(S.ft.length||'?')+')':'QUICK';
  el.className='chip'+(S.full?' on':'');
}

function doFlowScan(retryCount){
  if(S.scanning&&!retryCount) return;
  retryCount=retryCount||0;
  if(!retryCount){
    S.scanning=true;S.callFlow=0;S.putFlow=0;S.signals=[];S.hotContracts=[];
    setView('signals');
    document.getElementById('view-toggle').style.display='none';
    document.getElementById('flow-feed').textContent='';
    document.getElementById('hot-feed').textContent='';
    document.getElementById('flow-bar').classList.remove('on');
    document.getElementById('bc').textContent='CALLS -';
    document.getElementById('bp').textContent='PUTS -';
    const bd=document.getElementById('bd');bd.textContent='-';bd.className='bias-dir neut';
    document.getElementById('pw').style.display='block';
    document.getElementById('pl').style.display='block';
  }
  const tickers=(S.full?S.ft:S.qt).join(',')
    ||'SPX,SPY,QQQ,IWM,NVDA,AMD,AAPL,MSFT,META,AMZN,TSLA';
  const n=tickers.split(',').length;
  const btn=document.getElementById('scan-btn');
  btn.textContent='SCANNING '+n+'...';btn.className='scan-btn loading';
  const minScore=S.whale?60:40;
  const url=_pa('/api/flow?tickers='+tickers+'&dte='+S.dte+'&min_score='+minScore);
  const es=new EventSource(url);
  let gotData=false;
  es.onmessage=function(e){
    gotData=true;
    const m=JSON.parse(e.data);
    if(m.__ping__) return;
    if(m.__progress__){
      document.getElementById('pb').style.width=(m.i/m.n*100)+'%';
      document.getElementById('pl').textContent=m.ticker+' . '+m.i+' of '+m.n;
      return;
    }
    if(m.__done__||m.__error__){es.close();endFlowScan(m.__error__);return}
    if(m.__signal__){
      const s=m.data;
      S.signals.push(s);
      S.callFlow+=s.call_flow||0;S.putFlow+=s.put_flow||0;
      (s.top3||[]).forEach(function(c){S.hotContracts.push(Object.assign({},c,{ticker:s.ticker,badge:s.badge,cls:s.cls}))});
      renderFlowCard(s);
      updateFlowBias();
    }
  };
  es.onerror=function(){
    es.close();
    if(!gotData&&retryCount<2){
      const wait=retryCount===0?4:8;
      document.getElementById('pl').textContent='Waking up... retry '+(retryCount+1)+'/2';
      setTimeout(function(){doFlowScan(retryCount+1)},wait*1000);
    } else {
      endFlowScan(gotData?null:'Server unavailable - try again');
    }
  };
}
function endFlowScan(err){
  S.scanning=false;
  const btn=document.getElementById('scan-btn');
  btn.textContent=S.full?'FULL':'SCAN';btn.className='scan-btn';
  document.getElementById('pw').style.display='none';
  document.getElementById('pl').style.display='none';
  document.getElementById('pb').style.width='0%';
  if(err){toast('Error: '+err,'err');return}
  if(!S.signals.length){
    const hint=!_isMarketOpen()&&S.dte==='0dte'
      ?'Market closed - 0DTE expired. Switch to ALL DTE.'
      :S.dte!=='all'?'Try ALL DTE or FULL scan.':'No unusual institutional flow detected.';
    const feed=document.getElementById('flow-feed');
    feed.textContent='';
    const wrap=document.createElement('div');wrap.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='?';
    const h=document.createElement('h3');h.textContent='No signals';
    const p=document.createElement('p');p.textContent=hint;
    wrap.appendChild(icon);wrap.appendChild(h);wrap.appendChild(p);
    feed.appendChild(wrap);
  } else {
    toast(S.signals.length+' signal'+(S.signals.length>1?'s':'')+' - institutional only');
    if(S.hotContracts.length) document.getElementById('view-toggle').style.display='flex';
  }
}
function updateFlowBias(){
  const cf=S.callFlow,pf=S.putFlow;
  document.getElementById('bc').textContent='CALLS '+fmt(cf);
  document.getElementById('bp').textContent='PUTS '+fmt(pf);
  const bd=document.getElementById('bd');
  if(cf>pf*1.2){bd.textContent='BULL';bd.className='bias-dir bull'}
  else if(pf>cf*1.2){bd.textContent='BEAR';bd.className='bias-dir bear'}
  else{bd.textContent='EVEN';bd.className='bias-dir neut'}
  const total=cf+pf;
  if(total>0){
    document.getElementById('flow-fill').style.width=Math.round(cf/total*100)+'%';
    document.getElementById('flow-bar').classList.add('on');
  }
}

function fmt(v){
  if(v>=1e6) return '$'+(v/1e6).toFixed(1)+'M';
  if(v>=1e3) return '$'+(v/1e3).toFixed(0)+'K';
  return '$'+v.toFixed(0);
}
function badgeCls(b){
  var m={'GOLDEN SWEEP':'golden','WHALE':'whale','STACKED':'stacked','SWEEP':'sweep','BLOCK':'block'};
  return m[b]||'flow';
}
function voiCls(v){return v>=10?'hot':v>=3?'warm':'cool'}
function scoreCls(s){return s>=70?'whale':s>=50?'inst':'retail'}

function renderFlowCard(s){
  const feed=document.getElementById('flow-feed');
  const card=document.createElement('div');
  card.className='flow-card '+(s.cls||'');

  // head
  const head=document.createElement('div');
  head.className='card-head';
  head.onclick=function(){toggleDetail(head)};

  const badgeEl=document.createElement('span');
  badgeEl.className='badge '+badgeCls(s.badge);
  badgeEl.textContent=s.badge;

  const titleDiv=document.createElement('div');
  titleDiv.className='card-title';
  const tickEl=document.createElement('div');
  tickEl.className='card-ticker';
  tickEl.textContent=s.ticker+(s.hits>1?' x'+s.hits:'');
  const subEl=document.createElement('div');
  subEl.className='card-sub';
  subEl.style.color=s.bias==='call'?'var(--green)':'var(--red)';
  const dtePart=s.dte===0?' 0DTE':s.dte>=0?' '+s.dte+'DTE':'';
  subEl.textContent=s.ts+' . '+s.bias.toUpperCase()+' FLOW'+dtePart;
  titleDiv.appendChild(tickEl);titleDiv.appendChild(subEl);

  const premDiv=document.createElement('div');
  premDiv.className='card-premium';
  const amtEl=document.createElement('div');
  amtEl.className='card-amount '+s.bias;
  amtEl.textContent=s.total_fmt;
  const lblEl=document.createElement('div');
  lblEl.className='card-alabel';
  lblEl.style.color=s.bias==='call'?'var(--green)':'var(--red)';
  lblEl.textContent=s.bias==='call'?'CALLS':'PUTS';
  premDiv.appendChild(amtEl);premDiv.appendChild(lblEl);

  head.appendChild(badgeEl);head.appendChild(titleDiv);head.appendChild(premDiv);

  // score bar
  const sbWrap=document.createElement('div');sbWrap.className='score-bar-wrap';
  const sbTrack=document.createElement('div');sbTrack.className='score-bar-track';
  const sbFill=document.createElement('div');
  sbFill.className='score-bar-fill '+scoreCls(s.score);
  sbFill.style.width=s.score+'%';
  sbTrack.appendChild(sbFill);
  const sbNum=document.createElement('span');
  sbNum.className='score-num '+scoreCls(s.score);
  sbNum.textContent=s.score;
  sbWrap.appendChild(sbTrack);sbWrap.appendChild(sbNum);

  // stats row
  const statsDiv=document.createElement('div');statsDiv.className='card-stats';
  const stats=[
    ['Vol/OI', s.vol_oi>0?'x'+s.vol_oi.toFixed(1):'-', voiCls(s.vol_oi)],
    ['Side', s.side==='ask'?'AT ASK':s.side==='bid'?'AT BID':'MIXED', s.side],
    ['P/C', s.pc_ratio.toFixed(2), ''],
    ['Tier', (s.tier||'-').toUpperCase(), ''],
  ];
  stats.forEach(function(st){
    const cs=document.createElement('div');cs.className='cstat';
    const lbl=document.createElement('label');lbl.textContent=st[0];
    const val=document.createElement('div');val.className='v '+(st[2]||'');val.textContent=st[1];
    cs.appendChild(lbl);cs.appendChild(val);statsDiv.appendChild(cs);
  });

  // contracts row
  const contractsRow=document.createElement('div');contractsRow.className='contracts-row';
  (s.top3||[]).forEach(function(c,i){
    const cc=document.createElement('div');
    cc.className='cc '+(i===0?(c.golden?'golden-c':'top1'):'');
    const strike=document.createElement('div');
    strike.className='cc-strike '+c.type;
    strike.textContent='$'+c.strike.toFixed(0)+' '+(c.type==='call'?'C':'P');
    const meta=document.createElement('div');meta.className='cc-meta';
    const dLbl=c.dte===0?'0DTE':c.dte>=0?c.dte+'DTE':'-';
    meta.textContent=dLbl+' . '+c.exp;
    const price=document.createElement('div');price.className='cc-price';
    price.textContent=c.mid>0?'$'+c.mid.toFixed(2):'-';
    const voi=document.createElement('div');voi.className='cc-voi '+voiCls(c.vol_oi);
    voi.textContent=c.vol_oi>0?'x'+c.vol_oi.toFixed(1):'-';
    cc.appendChild(strike);cc.appendChild(meta);cc.appendChild(price);cc.appendChild(voi);
    contractsRow.appendChild(cc);
  });

  // detail
  const det=document.createElement('div');det.className='card-detail';
  const dg=document.createElement('div');dg.className='dg';
  const dgItems=[
    ['Calls', s.call_fmt, 'var(--green)'],
    ['Puts', s.put_fmt, 'var(--red)'],
    ['IV Skew', s.iv_skew?(s.iv_skew>0?'+':'')+(s.iv_skew*100).toFixed(2)+'%':'-', ''],
    ['Stacked', s.stacked?'YES':'NO', ''],
    ['Golden', s.golden?'YES':'NO', s.golden?'var(--gold)':'var(--sub)'],
    ['Strike', s.strike?'$'+s.strike:'-', ''],
  ];
  dgItems.forEach(function(it){
    const item=document.createElement('div');item.className='dg-item';
    const lbl=document.createElement('label');lbl.textContent=it[0];
    const sp=document.createElement('span');
    if(it[2]) sp.style.color=it[2];
    sp.textContent=it[1];
    item.appendChild(lbl);item.appendChild(sp);dg.appendChild(item);
  });
  const segs=document.createElement('div');segs.className='dte-segs';
  [['0DTE',s.dte0||'$0','var(--cyan)'],['1-7 DTE',s.dte1_7||'$0','var(--amber)'],['8+ DTE',s.dte8p||'$0','var(--sub)']].forEach(function(sg){
    const seg=document.createElement('div');seg.className='dte-seg';
    const lbl=document.createElement('label');lbl.textContent=sg[0];
    const sp=document.createElement('span');sp.style.color=sg[2];sp.textContent=sg[1];
    seg.appendChild(lbl);seg.appendChild(sp);segs.appendChild(seg);
  });
  det.appendChild(dg);det.appendChild(segs);

  card.appendChild(head);card.appendChild(sbWrap);card.appendChild(statsDiv);
  if(s.top3&&s.top3.length) card.appendChild(contractsRow);
  card.appendChild(det);
  feed.appendChild(card);
}
function toggleDetail(head){
  head.closest('.flow-card').querySelector('.card-detail').classList.toggle('open');
}

function setView(v){
  S.view=v;
  document.getElementById('vt-sig').className='vt-btn'+(v==='signals'?' on':'');
  document.getElementById('vt-hot').className='vt-btn'+(v==='hot'?' on':'');
  document.getElementById('flow-feed').style.display=v==='signals'?'':'none';
  document.getElementById('hot-feed').style.display=v==='hot'?'':'none';
  if(v==='hot') renderHot();
}
function renderHot(){
  const feed=document.getElementById('hot-feed');
  feed.textContent='';
  if(!S.hotContracts.length){
    const wrap=document.createElement('div');wrap.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='!';
    const h=document.createElement('h3');h.textContent='No hot contracts yet';
    const p=document.createElement('p');p.textContent='Run a scan first.';
    wrap.appendChild(icon);wrap.appendChild(h);wrap.appendChild(p);
    feed.appendChild(wrap);return;
  }
  const seen=new Set();
  const deduped=S.hotContracts.filter(function(c){
    const k=c.ticker+'-'+c.strike+'-'+c.type+'-'+c.exp;
    if(seen.has(k))return false;seen.add(k);return true;
  });
  const calls=deduped.filter(function(c){return c.type==='call'})
    .sort(function(a,b){return(b.vol_oi||0)-(a.vol_oi||0)}).slice(0,12);
  const puts=deduped.filter(function(c){return c.type==='put'})
    .sort(function(a,b){return(b.vol_oi||0)-(a.vol_oi||0)}).slice(0,12);
  function buildSec(lbl,clr,list){
    if(!list.length) return;
    const hdr=document.createElement('div');
    hdr.style.cssText='padding:6px 12px 4px;font-size:9px;font-weight:700;letter-spacing:1px;color:'+clr+';text-transform:uppercase';
    hdr.textContent=lbl;feed.appendChild(hdr);
    list.forEach(function(c,i){
      const rc=i===0?'t1':i===1?'t2':i===2?'t3':'';
      const vo=c.vol_oi||0;
      const vc=vo>=10?'fire':vo>=5?'hot':'warm';
      const dLbl=c.dte===0?'0DTE':c.dte>=0?c.dte+'DTE':'-';
      const el=document.createElement('div');el.className='hot-card';
      const rank=document.createElement('div');rank.className='hot-rank '+rc;rank.textContent=i+1;
      const info=document.createElement('div');info.className='hot-info';
      const sym=document.createElement('div');sym.className='hot-sym';
      const sp1=document.createElement('span');sp1.className=c.type;sp1.textContent=c.ticker;
      const sp2=document.createElement('span');
      sp2.style.cssText='color:var(--sub);font-size:12px';
      sp2.textContent=' $'+c.strike.toFixed(0)+' '+(c.type==='call'?'C':'P');
      sym.appendChild(sp1);sym.appendChild(sp2);
      const meta=document.createElement('div');meta.className='hot-meta';
      meta.textContent=dLbl+' . '+c.exp+' . '+(c.mid>0?'$'+c.mid.toFixed(2):'-');
      info.appendChild(sym);info.appendChild(meta);
      const right=document.createElement('div');right.className='hot-right';
      const voiEl=document.createElement('div');voiEl.className='hot-voi '+vc;
      voiEl.textContent='x'+vo.toFixed(1);
      const flowEl=document.createElement('div');flowEl.className='hot-flow';
      flowEl.textContent=(c.flow||'-')+' flow';
      right.appendChild(voiEl);right.appendChild(flowEl);
      el.appendChild(rank);el.appendChild(info);el.appendChild(right);
      feed.appendChild(el);
    });
  }
  buildSec('CALLS','var(--green)',calls);
  buildSec('PUTS','var(--red)',puts);
}

// Full scan
function updateScanFilter(){S.scanFilter=document.getElementById('scan-filter').value}
function updateScanSort(){
  S.scanSort=document.getElementById('scan-sort').value;
  if(S.scanData.length) renderScanTable(S.scanData);
}

async function runFullScan(){
  if(S.scanRunning) return;
  S.scanRunning=true;
  const btn=document.getElementById('scan-run-btn');
  btn.textContent='SCANNING...';btn.style.opacity='.6';
  const wrap=document.getElementById('scan-table-wrap');
  wrap.textContent='';
  const skelWrap=document.createElement('div');skelWrap.style.padding='16px';
  for(let i=0;i<5;i++){
    const s=document.createElement('div');
    s.className='skel';s.style.cssText='height:'+(i===0?'32':'28')+'px;margin-bottom:6px';
    skelWrap.appendChild(s);
  }
  wrap.appendChild(skelWrap);
  const filter=document.getElementById('scan-filter').value;
  const sort=document.getElementById('scan-sort').value;
  const dteMode=document.getElementById('scan-dte').value;
  try{
    const r=await fetch(_pa('/api/scan?filter='+filter+'&sort='+sort+'&dte_mode='+dteMode));
    if(_handleAuth(r))return;
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'Scan failed');}
    const d=await r.json();
    S.scanData=d.results||[];
    document.getElementById('scan-stat').textContent=d.filtered+' / '+d.total+' . '+d.last_updated;
    renderScanTable(S.scanData);
    toast(d.filtered+' setups found . '+d.last_updated);
  }catch(e){
    wrap.textContent='';
    const empt=document.createElement('div');empt.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='!';
    const h=document.createElement('h3');h.textContent='Scan failed';
    const p=document.createElement('p');p.textContent=e.message;
    empt.appendChild(icon);empt.appendChild(h);empt.appendChild(p);
    wrap.appendChild(empt);
    toast('Scan failed: '+e.message,'err');
  }finally{
    S.scanRunning=false;
    btn.textContent='RUN FULL SCAN (232 tickers)';btn.style.opacity='1';
  }
}

function renderScanTable(data){
  const wrap=document.getElementById('scan-table-wrap');
  wrap.textContent='';
  if(!data||!data.length){
    const empt=document.createElement('div');empt.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='?';
    const h=document.createElement('h3');h.textContent='No setups matched';
    const p=document.createElement('p');p.textContent='Try a different filter.';
    empt.appendChild(icon);empt.appendChild(h);empt.appendChild(p);
    wrap.appendChild(empt);return;
  }
  const tbl=document.createElement('table');tbl.className='scan-table';
  const thead=document.createElement('thead');
  const hr=document.createElement('tr');
  ['TICKER','PRICE . CHG%','SETUP','OPT SCORE','CONTRACT','REL VOL'].forEach(function(col){
    const th=document.createElement('th');th.textContent=col;hr.appendChild(th);
  });
  thead.appendChild(hr);tbl.appendChild(thead);
  const tbody=document.createElement('tbody');
  data.forEach(function(r){
    const tr=document.createElement('tr');
    // ticker
    const td1=document.createElement('td');
    const tc=document.createElement('div');tc.className='ticker-cell';tc.textContent=r.ticker;
    const sc=document.createElement('div');sc.className='sector-cell';sc.textContent=r.sector;
    td1.appendChild(tc);td1.appendChild(sc);
    // price/chg
    const td2=document.createElement('td');
    const pr=document.createElement('div');pr.textContent='$'+r.price.toFixed(2);
    const ch=document.createElement('span');
    ch.className=r.change_pct>=0?'chg-up':'chg-dn';
    ch.textContent=(r.change_pct>=0?'+':'')+r.change_pct.toFixed(2)+'%';
    td2.appendChild(pr);td2.appendChild(ch);
    // setup
    const td3=document.createElement('td');
    const sb=document.createElement('span');sb.className='setup-badge '+r.grade;
    sb.textContent=r.grade+' . '+(r.setup||r.direction.toUpperCase());
    td3.appendChild(sb);
    // opt score
    const td4=document.createElement('td');
    if(r.contract){
      const bw=document.createElement('span');bw.className='opt-bar-wrap';
      const bf=document.createElement('span');bf.className='opt-bar-fill';
      bf.style.width=Math.min(r.contract.score||0,100)+'%';
      bw.appendChild(bf);td4.appendChild(bw);
    }
    const sn=document.createTextNode(r.contract?r.contract.score||0:0);
    td4.appendChild(sn);
    // contract
    const td5=document.createElement('td');td5.className='contract-cell';
    if(r.contract){
      const c=r.contract;
      const sk=document.createElement('span');
      sk.className='strike '+(r.direction==='up'?'call':'put');
      sk.textContent='$'+c.strike+' '+(r.direction==='up'?'C':'P');
      const ex=document.createElement('span');
      ex.style.cssText='font-size:9px;color:var(--sub)';
      ex.textContent=' '+c.exp+' . '+(c.dte===0?'0DTE':c.dte+'DTE');
      td5.appendChild(sk);td5.appendChild(document.createElement('br'));td5.appendChild(ex);
    } else {
      td5.textContent='-';
    }
    // rel vol
    const td6=document.createElement('td');
    if(r.rel_vol>=2){
      const sp=document.createElement('span');sp.style.color='var(--amber)';
      sp.textContent=r.rel_vol.toFixed(1)+'x';td6.appendChild(sp);
    } else {
      td6.textContent=r.rel_vol.toFixed(1)+'x';
    }
    tr.appendChild(td1);tr.appendChild(td2);tr.appendChild(td3);
    tr.appendChild(td4);tr.appendChild(td5);tr.appendChild(td6);
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);wrap.appendChild(tbl);
}

// Sectors
async function loadSectors(){
  const feed=document.getElementById('sectors-feed');
  feed.textContent='';
  const skelWrap=document.createElement('div');skelWrap.style.padding='12px';
  for(let i=0;i<3;i++){
    const s=document.createElement('div');
    s.className='skel';s.style.cssText='height:100px;margin-bottom:8px';
    skelWrap.appendChild(s);
  }
  feed.appendChild(skelWrap);
  try{
    const r=await fetch(_pa('/api/sectors'));
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'Failed');}
    const d=await r.json();
    const sectors=d.sectors||[];
    if(!sectors.length) throw new Error('No sector data');
    const max=Math.max.apply(null,sectors.map(function(s){return Math.abs(s.change)}));
    const maxVal=max||0.01;
    feed.textContent='';
    const hdr=document.createElement('div');hdr.className='sec-head';
    const hdrLeft=document.createTextNode('SECTORS');
    const ts=document.createElement('span');ts.className='ts';
    ts.textContent='UPDATED '+(d.last_updated||'');
    hdr.appendChild(hdrLeft);hdr.appendChild(ts);
    feed.appendChild(hdr);
    if(d.laggard){
      const lb=document.createElement('div');lb.className='laggard-box';
      const lbl=document.createElement('div');lbl.className='laggard-label';lbl.textContent='TOP LAGGARD';
      const lt=document.createElement('div');lt.className='laggard-ticker';
      lt.textContent=d.laggard.ticker+' ('+d.laggard.name+')';
      const ld=document.createElement('div');ld.className='laggard-desc';
      ld.textContent='vs sector avg: '+(d.laggard.lag_pct>0?'+':'')+d.laggard.lag_pct+'%';
      lb.appendChild(lbl);lb.appendChild(lt);lb.appendChild(ld);feed.appendChild(lb);
    }
    const grid=document.createElement('div');grid.className='sector-grid';
    sectors.forEach(function(s){
      const up=s.change>=0;
      const pct=Math.abs(s.change/maxVal*100).toFixed(0);
      const biasClr=s.bias==='bull'?'var(--green)':s.bias==='bear'?'var(--red)':'var(--sub)';
      const card=document.createElement('div');
      card.className='sector-card '+(up?'up':'dn');
      const changeStr=(up?'+':'')+s.change.toFixed(2)+'%';
      card.title=s.name+': '+changeStr+' . Strength: '+s.strength;
      card.onclick=function(){toast(s.name+': '+changeStr+' . Strength: '+s.strength)};
      const nm=document.createElement('div');nm.className='sc-name';nm.textContent=s.name;
      const etf=document.createElement('div');etf.className='sc-etf';etf.textContent=s.etf;
      const chg=document.createElement('div');chg.className='sc-chg '+(up?'up':'dn');
      chg.textContent=changeStr;
      const track=document.createElement('div');track.className='sc-bar-track';
      const fill=document.createElement('div');
      fill.className='sc-bar-fill '+(up?'up':'dn');fill.style.width=pct+'%';
      track.appendChild(fill);
      const meta=document.createElement('div');meta.className='sc-meta';
      const biasEl=document.createElement('span');biasEl.style.color=biasClr;
      biasEl.textContent=(s.bias||'NEUT').toUpperCase();
      const volEl=document.createElement('span');
      volEl.textContent=s.rel_vol.toFixed(1)+'x vol';
      meta.appendChild(biasEl);meta.appendChild(volEl);
      card.appendChild(nm);card.appendChild(etf);card.appendChild(chg);
      card.appendChild(track);card.appendChild(meta);
      grid.appendChild(card);
    });
    feed.appendChild(grid);
  }catch(e){
    feed.textContent='';
    const empt=document.createElement('div');empt.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='!';
    const h=document.createElement('h3');h.textContent='Failed to load sectors';
    const p=document.createElement('p');p.textContent=e.message;
    empt.appendChild(icon);empt.appendChild(h);empt.appendChild(p);
    feed.appendChild(empt);
    const btn=document.createElement('button');btn.className='load-btn';
    btn.textContent='TRY AGAIN';btn.onclick=loadSectors;feed.appendChild(btn);
    toast('Sectors failed: '+e.message,'err');
  }
}

// Contract finder
function setDir(d){
  S.dir=d;
  document.getElementById('d-up').className='dir-btn up'+(d==='up'?' on':'');
  document.getElementById('d-dn').className='dir-btn dn'+(d==='down'?' on':'');
}
function setDteMode(m){
  S.dteMode=m;
  ['0dte','weekly','all'].forEach(function(k){
    document.getElementById('dt-'+k).className='dte-btn'+(k===m?' on':'');
  });
}

async function doFind(retry){
  const btn=document.getElementById('find-btn');
  if(!retry&&btn.classList.contains('loading')) return;
  const ticker=document.getElementById('ft').value.trim().toUpperCase()||'SPY';
  btn.textContent=retry?'RETRYING...':'FINDING...';btn.classList.add('loading');
  const res=document.getElementById('find-result');
  if(!retry){
    res.textContent='';
    const skelWrap=document.createElement('div');skelWrap.style.cssText='margin:4px 0';
    [130,110,110].forEach(function(h){
      const s=document.createElement('div');
      s.className='skel';s.style.cssText='height:'+h+'px;border-radius:10px;margin-bottom:8px';
      skelWrap.appendChild(s);
    });
    res.appendChild(skelWrap);
  }
  try{
    const r=await fetch(_pa('/api/find?ticker='+ticker+'&direction='+S.dir+'&dte_mode='+S.dteMode));
    if(!r.ok){const e=await r.json();throw new Error(e.detail||e.error||'No contracts found');}
    const d=await r.json();
    renderContracts(d.ticker,d.contracts,d.last_updated);
    btn.textContent='FIND TOP 3 CONTRACTS';btn.classList.remove('loading');
  }catch(e){
    if(!retry){
      res.textContent='';
      const p=document.createElement('div');p.className='empty-st';p.style.padding='16px';
      p.textContent='Waking up server...';res.appendChild(p);
      setTimeout(function(){doFind(true)},5000);
    }else{
      res.textContent='';
      const p=document.createElement('div');p.className='empty-st';p.style.padding='16px';
      p.textContent=e.message;res.appendChild(p);
      btn.textContent='FIND TOP 3 CONTRACTS';btn.classList.remove('loading');
    }
  }
}

async function doFindBoth(){
  const ticker=(document.getElementById('ft').value.trim()||'SPY').toUpperCase();
  const btn=document.getElementById('both-btn');
  const res=document.getElementById('both-result');
  btn.textContent='Loading...';btn.classList.add('loading');
  res.textContent='';
  try{
    const r=await fetch(_pa('/api/find/both?ticker='+ticker+'&dte_mode='+S.dteMode));
    if(_handleAuth(r))return;
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'Failed');}
    const d=await r.json();
    renderBothLadder(d);
  }catch(e){
    res.textContent='Error: '+e.message;
  }finally{
    btn.textContent='▶ CALLS vs PUTS LADDER';btn.classList.remove('loading');
  }
}

function renderBothLadder(d){
  const res=document.getElementById('both-result');
  res.textContent='';

  const fmtM=v=>v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':v.toFixed(0);
  const fmtN=v=>v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':String(v);

  const ct=d.call_totals, pt=d.put_totals;
  const cw='#00ff88', pw='#ff3355', neu='#888';

  // ── summary scoreboard ──────────────────────────────────────────────────
  const scoreEl=document.createElement('div');
  scoreEl.style.cssText='background:#0d0d16;border:1px solid #1a1a2e;border-radius:10px;padding:14px 16px;margin-bottom:12px';

  const hdr=document.createElement('div');
  hdr.style.cssText='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px';
  hdr.innerHTML=`<span style="font-size:11px;color:#555;letter-spacing:.8px">CALLS vs PUTS — ${d.ticker} ${d.exp} (${d.dte}DTE)</span><span style="font-size:10px;color:#444">${d.last_updated}</span>`;
  scoreEl.appendChild(hdr);

  const metrics=[
    {key:'dollar_flow', label:'$ FLOW',   fmt:v=>'$'+fmtM(v), winner:d.flow_winner},
    {key:'volume',      label:'VOLUME',   fmt:fmtN,            winner:d.vol_winner},
    {key:'oi',          label:'OI',       fmt:fmtN,            winner:d.oi_winner},
    {key:'ddoi',        label:'Δ OI',     fmt:fmtN,            winner:d.ddoi_winner},
  ];

  const grid=document.createElement('div');
  grid.style.cssText='display:grid;grid-template-columns:1fr 1fr;gap:8px';

  metrics.forEach(m=>{
    const cWin=m.winner==='call', pWin=m.winner==='put';
    const cell=document.createElement('div');
    cell.style.cssText='background:#111;border-radius:8px;padding:10px 12px';
    cell.innerHTML=`
      <div style="font-size:9px;color:#555;letter-spacing:.8px;margin-bottom:6px">${m.label}</div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <span style="font-size:11px;color:#555">C </span>
          <span style="font-size:13px;font-weight:700;color:${cWin?cw:neu}">${m.fmt(ct[m.key])}</span>
          ${cWin?'<span style="font-size:9px;color:'+cw+';margin-left:4px">▲</span>':''}
        </div>
        <div>
          <span style="font-size:11px;color:#555">P </span>
          <span style="font-size:13px;font-weight:700;color:${pWin?pw:neu}">${m.fmt(pt[m.key])}</span>
          ${pWin?'<span style="font-size:9px;color:'+pw+';margin-left:4px">▲</span>':''}
        </div>
      </div>`;
    grid.appendChild(cell);
  });
  scoreEl.appendChild(grid);

  // Overall bias
  const cWins=[d.flow_winner,d.vol_winner,d.oi_winner,d.ddoi_winner].filter(x=>x==='call').length;
  const bias=cWins>=3?'CALL HEAVY':cWins<=1?'PUT HEAVY':'MIXED';
  const biasCol=cWins>=3?cw:cWins<=1?pw:neu;
  const biasEl=document.createElement('div');
  biasEl.style.cssText='margin-top:10px;text-align:center;font-size:14px;font-weight:800;letter-spacing:1px;color:'+biasCol;
  biasEl.textContent=bias+' ('+cWins+'/4 metrics call-dominant)';
  scoreEl.appendChild(biasEl);
  res.appendChild(scoreEl);

  // ── ladder table ────────────────────────────────────────────────────────
  const ladderEl=document.createElement('div');
  ladderEl.style.cssText='background:#0d0d16;border:1px solid #1a1a2e;border-radius:10px;padding:14px 16px;margin-bottom:60px';

  const ladderHdr=document.createElement('div');
  ladderHdr.style.cssText='font-size:9px;color:#555;letter-spacing:.8px;margin-bottom:10px';
  ladderHdr.textContent='TOP STRIKES BY $ FLOW';
  ladderEl.appendChild(ladderHdr);

  // header row
  const hrow=document.createElement('div');
  hrow.style.cssText='display:grid;grid-template-columns:60px 1fr 1fr 1fr 1fr;gap:4px;font-size:9px;color:#444;letter-spacing:.5px;margin-bottom:6px;padding:0 4px';
  hrow.innerHTML='<span>STRIKE</span><span style="text-align:right">$FLOW</span><span style="text-align:right">VOL</span><span style="text-align:right">OI</span><span style="text-align:right">ΔOI</span>';
  ladderEl.appendChild(hrow);

  // merge calls + puts, sort by dollar flow
  const allStrikes=[...d.calls.map(r=>({...r,side:'call'})),...d.puts.map(r=>({...r,side:'put'}))];
  allStrikes.sort((a,b)=>b.dollar_flow-a.dollar_flow);

  allStrikes.slice(0,12).forEach(r=>{
    const col=r.side==='call'?cw:pw;
    const row=document.createElement('div');
    row.style.cssText='display:grid;grid-template-columns:60px 1fr 1fr 1fr 1fr;gap:4px;font-size:11px;padding:5px 4px;border-bottom:1px solid #111';
    row.innerHTML=`
      <span style="font-weight:700;color:${col}">${r.side==='call'?'C':'P'} ${r.strike}</span>
      <span style="text-align:right;color:#ccc">$${fmtM(r.dollar_flow)}</span>
      <span style="text-align:right;color:#aaa">${fmtN(r.vol)}</span>
      <span style="text-align:right;color:#888">${fmtN(r.oi)}</span>
      <span style="text-align:right;color:#666">${fmtN(r.ddoi)}</span>`;
    ladderEl.appendChild(row);
  });

  res.appendChild(ladderEl);
}

function renderContracts(ticker,cs,ts){
  if(!Array.isArray(cs)) cs=[cs];
  const isCall=S.dir==='up';
  const dteLbl={'0dte':'0DTE','weekly':'WEEKLY','all':'ALL'}[S.dteMode]||'';
  const res=document.getElementById('find-result');
  res.textContent='';
  const wrap=document.createElement('div');wrap.className='cont-cards';
  cs.forEach(function(c,i){
    const card=document.createElement('div');
    card.className='cont-card'+(i===0?' best':'');
    // hero
    const hero=document.createElement('div');hero.className='cont-hero';
    const symDiv=document.createElement('div');
    const sym=document.createElement('div');sym.className='cont-sym';
    const sp1=document.createElement('span');sp1.className=isCall?'call':'put';sp1.textContent=ticker;
    const sp2=document.createElement('span');sp2.className='ks';
    sp2.textContent=' $'+c.strike.toFixed(0)+' '+(isCall?'C':'P');
    sym.appendChild(sp1);sym.appendChild(sp2);
    const exp=document.createElement('div');
    exp.style.cssText='font-size:10px;color:var(--sub);margin-top:4px';
    exp.textContent=(c.exp?c.exp.slice(5):'-')+' . '+(c.dte===0?'0DTE':c.dte+'DTE')+' . '+dteLbl;
    symDiv.appendChild(sym);symDiv.appendChild(exp);
    const rightDiv=document.createElement('div');
    const badge=document.createElement('div');
    badge.className='cont-badge '+(i===0?'best':'alt');
    badge.textContent=i===0?'BEST FIT':'ALT '+(i+1);
    rightDiv.appendChild(badge);
    if(c.stale){
      const st=document.createElement('div');
      st.style.cssText='font-size:9px;color:var(--amber);margin-top:4px';
      st.textContent='STALE';rightDiv.appendChild(st);
    }
    hero.appendChild(symDiv);hero.appendChild(rightDiv);
    // grid
    const grid=document.createElement('div');grid.className='cont-grid';
    const mid=c.mid?'$'+c.mid.toFixed(2):'-';
    const bidask=(c.bid>0&&c.ask>0)?'$'+c.bid.toFixed(2)+'/$'+c.ask.toFixed(2):'last '+mid;
    const dlt=c.delta!=null?(c.delta>=0?'+':'')+c.delta.toFixed(3):'-';
    const iv=c.iv?(c.iv*100).toFixed(1)+'%':'-';
    const voiN=c.oi>0?c.vol/c.oi:0;
    const voi=c.oi>0?voiN.toFixed(1)+'x':'-';
    const roiClr=c.roi>50?'g':c.roi>0?'cy':c.roi<0?'r':'';
    const roi=c.roi!=null?(c.roi>0?'+':'')+c.roi.toFixed(1)+'%':'-';
    [
      ['Mid',mid,isCall?'g':'r'],
      ['Bid/Ask',bidask,''],
      ['Delta',dlt,isCall?'g':'r'],
      ['IV',iv,'cy'],
      ['Vol/OI',voi,voiN>=10?'gd':voiN>=3?'cy':''],
      ['1s ROI',roi,roiClr],
    ].forEach(function(it){
      const cg=document.createElement('div');cg.className='cg';
      const lbl=document.createElement('label');lbl.textContent=it[0];
      const sp=document.createElement('span');if(it[2]) sp.className=it[2];sp.textContent=it[1];
      cg.appendChild(lbl);cg.appendChild(sp);grid.appendChild(cg);
    });
    // note
    const note=document.createElement('div');note.className='cont-note';
    note.textContent=(c.stale?'Stale quote - market closed':'Ranked: 1s ROI . delta . liquidity . spread')
      +(i===0?' . Score: '+(c.score||'-'):'')
      +(ts?' . '+ts:'');
    card.appendChild(hero);card.appendChild(grid);card.appendChild(note);
    wrap.appendChild(card);
  });
  res.appendChild(wrap);
}

function toast(msg,type){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.className='show'+(type==='err'?' err':'');
  setTimeout(function(){el.className=''},2800);
}

async function loadIntel(){
  const tickers=document.getElementById('intel-tickers').value.trim();
  const qs=tickers?'?tickers='+encodeURIComponent(tickers):'';
  document.getElementById('intel-macro').textContent='Loading macro regime…';
  document.getElementById('intel-dp').textContent='Loading dark pool data…';
  document.getElementById('intel-ins').textContent='Loading insider data…';
  try{
    const [macroRes,dpRes,insRes]=await Promise.all([
      fetch(_pa('/api/macro')),
      fetch(_pa('/api/darkpool'+qs)),
      fetch(_pa('/api/insider'+qs))
    ]);
    if(_handleAuth(macroRes)||_handleAuth(dpRes)||_handleAuth(insRes))return;
    const macro=macroRes.ok?await macroRes.json():{error:'unavailable'};
    const dp=dpRes.ok?await dpRes.json():{error:'unavailable'};
    const ins=insRes.ok?await insRes.json():{error:'unavailable'};
    renderIntelMacro(macro);
    renderIntelDP(dp);
    renderIntelIns(ins);
  }catch(e){
    document.getElementById('intel-macro').textContent='Error: '+e.message;
    document.getElementById('intel-dp').textContent='Error: '+e.message;
    document.getElementById('intel-ins').textContent='Error: '+e.message;
  }
}

function renderIntelMacro(data){
  const el=document.getElementById('intel-macro');
  if(data.error){el.textContent=data.error;return;}
  const col=data.regime==='RISK-ON'?'#00ff88':data.regime==='RISK-OFF'?'#ff3355':'#ffa500';
  const score=data.score>=0?'+'+data.score:String(data.score);
  let html=`<div style="display:flex;align-items:center;gap:16px;margin-bottom:8px">
    <span style="color:${col};font-size:16px;font-weight:800">${data.regime}</span>
    <span style="color:#888;font-size:12px">Score: ${score}</span>
    <span style="color:#444;font-size:10px">${data.source||''}</span>
  </div>`;
  if(data.signals&&data.signals.length){
    html+=data.signals.map(s=>`<div style="font-size:11px;color:#aaa;padding:2px 0">• ${s}</div>`).join('');
  }
  if(data.data&&Object.keys(data.data).length){
    html+=`<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px">`;
    for(const[k,v]of Object.entries(data.data)){
      if(v.value!=null){
        html+=`<span style="background:#111;border:1px solid #222;border-radius:4px;padding:3px 8px;font-size:10px;color:#aaa">${v.label}: <span style="color:#ccc">${v.value.toFixed?v.value.toFixed(2):v.value}${v.unit}</span></span>`;
      }
    }
    html+=`</div>`;
  }
  html+=`<div style="font-size:9px;color:#444;margin-top:6px">Updated: ${data.last_updated||'—'}</div>`;
  el.innerHTML=html;
}

function renderIntelDP(data){
  const el=document.getElementById('intel-dp');
  if(data.error){el.textContent=data.error;return;}
  const sigs=(data.signals||[]).filter(s=>s.score>15).slice(0,20);
  if(!sigs.length){el.textContent='No significant dark pool anomalies detected.';return;}
  const rows=sigs.map(s=>{
    const col=s.signal==='ACCUMULATION'?'#00ff88':s.signal==='DISTRIBUTION'?'#ff3355':'#888';
    const vol=s.vol_ratio!=null?s.vol_ratio.toFixed(1)+'x':'—';
    const impact=s.price_impact_pct!=null?s.price_impact_pct.toFixed(2)+'%':'—';
    return `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a1a2e;font-size:12px">
      <span style="color:${col};font-weight:700;width:60px">${s.ticker}</span>
      <span style="color:${col};width:110px">${s.signal}</span>
      <span style="color:#aaa;width:60px">Vol: ${vol}</span>
      <span style="color:#666;width:70px">D${impact}</span>
      <span style="color:#888">Score: ${Math.round(s.score)}</span>
    </div>`;
  }).join('');
  el.innerHTML=`<div style="max-height:280px;overflow-y:auto">${rows}</div><div style="font-size:9px;color:#444;margin-top:6px">Source: yfinance vol-proxy · Updated: ${data.last_updated||'—'}</div>`;
}

function renderIntelIns(data){
  const el=document.getElementById('intel-ins');
  if(data.error){el.textContent=data.error;return;}
  const sigs=(data.signals||[]).filter(s=>s.score>20).slice(0,20);
  if(!sigs.length){el.textContent='No significant insider activity detected.';return;}
  const rows=sigs.map(s=>{
    const col=s.net_sentiment==='BUYING'?'#00ff88':s.net_sentiment==='SELLING'?'#ff3355':'#ffa500';
    const val=s.buy_value>1e6?(s.buy_value/1e6).toFixed(1)+'M':s.buy_value>1e3?(s.buy_value/1e3).toFixed(0)+'K':'—';
    return `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a1a2e;font-size:12px">
      <span style="color:${col};font-weight:700;width:60px">${s.ticker}</span>
      <span style="color:${col};width:100px">${s.net_sentiment}</span>
      <span style="color:#aaa;width:80px">Buys: ${s.buy_count} ($${val})</span>
      <span style="color:#888">Score: ${Math.round(s.score)}</span>
    </div>`;
  }).join('');
  el.innerHTML=`<div style="max-height:280px;overflow-y:auto">${rows}</div><div style="font-size:9px;color:#444;margin-top:6px">Source: SEC EDGAR Form 4 · Updated: ${data.last_updated||'—'}</div>`;
}
</script>
</body>
</html>""")

HTML = "".join(_HTML_PARTS)

# Entry
if __name__ == "__main__":
    import argparse, socket

    parser = argparse.ArgumentParser(description="Scanner Pro -- Unified Web UI")
    parser.add_argument("--pin", type=str, default="",
                        help="Protect with PIN (also via SCANNER_PIN env var)")
    args = parser.parse_args()
    if args.pin:
        os.environ["SCANNER_PIN"] = args.pin
        _PIN = args.pin

    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        lan_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        lan_ip = "localhost"

    print("\n  Scanner Pro")
    print("  Local:   http://localhost:{}".format(PORT))
    print("  Network: http://{}:{}".format(lan_ip, PORT))
    if _PIN:
        print("  PIN:     {}".format(_PIN))
    print()

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
