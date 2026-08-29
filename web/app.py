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
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
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
    scan_options_flow, get_best_contract, ladder_rows,
    scan_sectors, calc_whale_score, fmt_whale_score,
    scan_tickers, apply_forward_directions,
    enrich_contracts, find_sector_laggards,
    sector_heatmap, top_individual_laggard, sector_breakout_plays,
    apply_filter, apply_sort,
    FILTER_LABELS, SORT_LABELS,
    _TT_AVAILABLE,
    get_flow_source,
)
from core.universe import get_universe, ANCHOR
from core.market_calendar import is_market_open
from data.unusual_flow import scan_unusual_flow, sector_flow_summary
from data.etf_filter import filter_etfs, is_etf
from data.sources import available_sources

# Cache unusual flow results (expensive scan)
_UOA_CACHE: dict = {"signals": [], "summary": {}, "ts": 0.0}
_UOA_TTL = 600  # 10 min
CARD_CONTRACTS = 4  # contracts serialized per side, per card

PORT = int(os.environ.get("PORT", 8765))

# Insider scan default — equities only (ETFs have no Form 4 filings).
# ETF membership comes from data.etf_filter, the single source of truth.
def _get_insider_universe() -> list:
    return filter_etfs(get_universe())[:50]
_PIN             = os.environ.get("SCANNER_PIN", "").strip()
_REQUIRE_PIN     = os.environ.get("SCANNER_REQUIRE_PIN", "").strip() in ("1", "true", "yes")
_ALLOW_PIN_QUERY = os.environ.get("SCANNER_ALLOW_PIN_QUERY", "").strip() in ("1", "true", "yes")
# Number of trusted proxies appending to X-Forwarded-For. Production sits behind
# a CDN edge *and* Railway's proxy, so the chain is [real_client, cdn_edge] and
# the real caller is the 2nd entry from the right. With this at 1 the limiter
# keyed on the CDN edge address, which rotates per request, so it never fired:
# 40 parallel requests against a 30/60s limit all returned 200. A single-proxy
# deploy still works — _client_ip clamps the index to the chain length.
_PROXY_HOPS      = max(1, int(os.environ.get("SCANNER_PROXY_HOPS", "2") or 2))

if not _PIN:
    logging.warning("SCANNER_PIN not set — API is UNAUTHENTICATED")
elif len(_PIN) < 6:
    logging.warning("SCANNER_PIN is weak (<6 chars) — consider a longer PIN")

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

    def size(self) -> int:
        with self._lock:
            return len(self._windows)

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
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            idx = min(_PROXY_HOPS, len(parts))
            return parts[-idx]   # trust only the proxy-appended hop, not forged left entries
    return req.client.host if getattr(req, "client", None) else "unknown"

def _check_rate(req: Request, endpoint: str, limit: int, window: int):
    key = f"{_client_ip(req)}:{endpoint}"
    if not _rl.allow(key, limit, window):
        raise HTTPException(429, detail="Too many requests -- slow down",
                            headers={"Retry-After": str(window)})

def _check_pin(req: Request):
    if not _PIN:
        if _REQUIRE_PIN:
            raise HTTPException(503, "Server auth not configured")
        return
    ip = _client_ip(req)
    supplied = req.headers.get("x-pin", "").strip()
    if not supplied and _ALLOW_PIN_QUERY:
        supplied = req.query_params.get("pin", "").strip()
    if not hmac.compare_digest(supplied.encode("utf-8", errors="replace"),
                               _PIN.encode("utf-8")):
        if not _rl.allow(f"{ip}:auth_fail", 10, 300):
            raise HTTPException(429, detail="Too many failed attempts -- try later",
                                headers={"Retry-After": "300"})
        raise HTTPException(401, "Unauthorized")

# Default flow tickers
# Single-stocks only (plus SPX index for 0DTE). ETFs are filtered out to stay
# consistent with the ETF-free scan universe. SPX is an index, not an ETF.
DEFAULT_FLOW_TICKERS = filter_etfs([
    "SPX",
    "NVDA","AMD","AAPL","MSFT","META","AMZN","TSLA","GOOGL",
    "COIN","PLTR","MSTR","HOOD","MARA",
    "SOFI","AFRM","GME","HIMS",
    "GS","JPM",
])

# App setup
app = FastAPI(title="Scanner Pro", docs_url=None, redoc_url=None, openapi_url=None)

@app.exception_handler(Exception)
async def _generic_error(request: Request, exc: Exception):
    # Never expose internal errors to public
    logger.error("Unhandled error on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# Reject Host-header injection. Opt-in, like the PIN: unset means no filtering.
# The default used to be "localhost,127.0.0.1,testserver", which 400s every
# request the moment this is deployed anywhere with a real domain — a security
# control whose default breaks production just gets ripped out in an outage.
# In prod set SCANNER_ALLOWED_HOSTS=flowscanner-production.up.railway.app
_allowed_hosts = [h.strip() for h in os.environ.get("SCANNER_ALLOWED_HOSTS", "").split(",")
                  if h.strip()]
if not _allowed_hosts:
    # Railway injects RAILWAY_PUBLIC_DOMAIN — use it so host validation is on in
    # production without depending on anyone setting a second variable by hand.
    _platform_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if _platform_domain:
        # Include the platform wildcards too: Railway's healthcheck and internal
        # routing don't always use the public domain as the Host, and a 400 there
        # reads as a failed deploy. A custom domain needs SCANNER_ALLOWED_HOSTS.
        _allowed_hosts = [_platform_domain, "*.railway.app", "*.up.railway.app",
                          "localhost", "127.0.0.1"]
        logging.info("Host validation on via RAILWAY_PUBLIC_DOMAIN=%s", _platform_domain)
if not _allowed_hosts:
    _allowed_hosts = ["*"]
    logging.warning("SCANNER_ALLOWED_HOSTS not set — Host header is not validated")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)
if os.environ.get("SCANNER_FORCE_HTTPS", "").strip() in ("1", "true", "yes"):
    app.add_middleware(HTTPSRedirectMiddleware)

_MAX_BODY = 16 * 1024

@app.middleware("http")
async def _limit_body(request: Request, call_next):
    if request.method == "POST":
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > _MAX_BODY:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
    return await call_next(request)

# The dashboard is served from this same origin, so it needs no CORS grant at
# all. allow_origins=["*"] previously let any site on the internet read every
# API response from a visitor's browser. Opt in per-origin if that's ever needed.
_allowed_origins = [o.strip() for o in os.environ.get("SCANNER_ALLOWED_ORIGINS", "").split(",")
                    if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
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

    # Both sides, always. Serializing only the bias side meant a call-biased
    # ticker rendered no puts at all, however much premium they carried.
    def _contracts(key: str) -> List[Dict]:
        ranked = sorted(sig.get(key, []) or [],
                        key=lambda c: c.get("flow", 0), reverse=True)[:CARD_CONTRACTS]
        return [{
            "strike":  c.get("strike", 0),
            "exp":     c.get("exp", "")[-5:],
            "dte":     c.get("dte", -1),
            "type":    c.get("type", "call"),
            "vol":     c.get("vol", 0),
            "oi":      c.get("oi", 0),
            "vol_oi":  round(c.get("vol_oi", 0), 1),
            "mid":     round(c.get("mid", 0), 2),
            "bid":     c.get("bid", 0),
            "ask":     c.get("ask", 0),
            "flow":    _fmt(c.get("flow", 0)),
            "flow_raw": c.get("flow", 0),
            "sweep":   c.get("sweep", False),
            "golden":  c.get("golden_sweep", False),
            "tier":    c.get("premium_tier", "retail"),
            # What it takes to enter, and what it needs to pay.
            "breakeven":        c.get("breakeven"),
            "pct_to_breakeven": c.get("pct_to_breakeven"),
            "moneyness_pct":    c.get("moneyness_pct"),
            "spread_pct":       c.get("spread_pct"),
            "wide_spread":      bool(c.get("wide_spread", False)),
        } for c in ranked]

    top_calls = _contracts("call_contracts")
    top_puts  = _contracts("put_contracts")

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
        "top_calls":  top_calls,
        "top_puts":   top_puts,
        "spot":       sig.get("spot", 0),
        # Contracts the quality gate excluded. Reported rather than dropped in
        # silence — an invisible filter is indistinguishable from an empty market.
        "filtered_n":       sig.get("filtered_n", 0),
        "filtered_fmt":     _fmt(sig.get("filtered_premium", 0)),
        "filtered_reasons": sig.get("filtered_reasons", []),
        "institutional": is_institutional,
    }

# Market helpers
# is_market_open comes from core.market_calendar (imported above). A local
# weekday()-based copy used to live here and silently shadowed that import,
# putting every holiday and half session back to "open".

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
    _check_rate(req, "status", limit=30, window=60)
    src = get_flow_source()
    return {
        # `live` describes the data served, not the configuration. TastyTrade can
        # be fully configured and still fail every login.
        "flow_source": src.get("source") or "unknown",
        "flow_source_reason": src.get("reason", ""),
        "flow_source_age_s": src.get("age_s"),
        "live": src.get("live", False),
        "tt_configured": _TT_AVAILABLE,
        "darkpool": _DARKPOOL_OK,
        "insider": _INSIDER_OK,
        "macro": _FRED_OK,
        "chain_sources": available_sources(),
    }

@app.get("/api/universe")
async def api_universe(req: Request):
    _check_pin(req)
    _check_rate(req, "universe", limit=10, window=60)
    return {"quick": DEFAULT_FLOW_TICKERS, "full": get_universe()}

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

    # Single stocks only — ETFs never reach the engine, whatever the caller sent.
    # (Cash indexes like SPX are not ETFs and survive.)
    equities = filter_etfs(ticker_list)
    if not equities:
        raise HTTPException(400, "No valid tickers provided — all requested symbols are ETFs")
    ticker_list = equities

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

    tickers = await loop.run_in_executor(None, get_universe)

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

    # Top laggard = the individual stock most diverging against its sector.
    laggard = None
    try:
        laggard = await asyncio.wait_for(
            loop.run_in_executor(None, top_individual_laggard, data), timeout=30.0
        )
    except Exception:
        laggard = None

    clean = [{
        "name":     k,
        "change":   round(v.get("change_pct", 0), 2),
        "strength": round(v.get("strength", 0), 2),
        "price":    round(v.get("price", 0), 2),
        "rel_vol":  round(v.get("rel_vol", 1), 2),
        "bias":     v.get("bias", "neutral"),
        "rs":       round(v.get("rs_vs_spy", 0), 2),
        "breakout": v.get("breakout", "none"),
    } for k, v in data.items()]

    if not clean:
        raise HTTPException(503, "No sector data -- market may be closed")

    return {
        "sectors":      sorted(clean, key=lambda x: x["change"], reverse=True),
        "laggard":      laggard,
        "last_updated": datetime.now().strftime("%H:%M:%S"),
    }

@app.get("/api/sector/{name}/heatmap")
async def api_sector_heatmap(req: Request, name: str):
    _check_pin(req)
    _check_rate(req, "heatmap", limit=20, window=60)
    if name not in SECTOR_ETFS:
        raise HTTPException(404, "Unknown sector")
    loop = asyncio.get_event_loop()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, sector_heatmap, name), timeout=45.0
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Heatmap scan timed out")
    if not data.get("stocks"):
        raise HTTPException(503, "No data -- market may be closed")
    return {
        "sector":       data["sector"],
        "stocks":       data["stocks"],
        "last_updated": datetime.now().strftime("%H:%M:%S"),
    }

@app.get("/api/sector/{name}/plays")
async def api_sector_plays(req: Request, name: str, dte_mode: str = Query("all")):
    _check_pin(req)
    _check_rate(req, "plays", limit=15, window=60)
    if name not in SECTOR_ETFS:
        raise HTTPException(404, "Unknown sector")
    _validate_enum(dte_mode, _VALID_DTE_MODE, "dte_mode")

    loop = asyncio.get_event_loop()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            sector_data = await asyncio.wait_for(
                loop.run_in_executor(None, scan_sectors), timeout=45.0)
        except asyncio.TimeoutError:
            raise HTTPException(504, "Sector scan timed out")
    try:
        vix = await loop.run_in_executor(None, fetch_vix)
    except Exception:
        vix = -1.0
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: sector_breakout_plays(
                    name, sector_data, vix, dte_mode)), timeout=45.0)
        except asyncio.TimeoutError:
            raise HTTPException(504, "Plays scan timed out")

    return {"sector": data["sector"], "breakout": data["breakout"],
            "plays": data["plays"], "last_updated": datetime.now().strftime("%H:%M:%S")}

# Expiry window each dte_mode promises the user, mirroring get_best_contract().
_DTE_WINDOWS = {"0dte": (0, 0), "weekly": (2, 7)}

def _dte_note(dte_mode: str, contracts: list) -> Optional[str]:
    """
    get_best_contract() silently widens its expiry search when the requested
    window is empty (no 0DTE chain after hours, say). Returning the resulting
    contract under a "0DTE / Today only" label is misleading, so say so instead.
    """
    window = _DTE_WINDOWS.get(dte_mode)
    if not window or not contracts:
        return None
    lo, hi = window
    d = contracts[0].get("dte")
    if d is None or lo <= d <= hi:
        return None
    label = "0DTE" if dte_mode == "0dte" else "WEEKLY (2-7DTE)"
    return f"No {label} contracts available — showing nearest expiry ({d}DTE)"

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
            "dte_mode": dte_mode, "dte_note": _dte_note(dte_mode, contracts),
            "last_updated": datetime.now().strftime("%H:%M:%S")}

@app.get("/api/find/both")
async def api_find_both(
    req:      Request,
    ticker:   str = Query("SPY"),
    dte_mode: str = Query("all"),
):
    """
    Returns call vs put ladder comparison:
    dollar flow, volume, OI, DDOI (delta × OI) for top strikes on each side,
    plus the best tradeable contract on each side from the scoring engine.
    """
    _check_pin(req)
    _check_rate(req, "find_both", limit=10, window=60)
    ticker   = _validate_ticker(ticker)
    dte_mode = _validate_enum(dte_mode, _VALID_DTE_MODE, "dte_mode")

    def _fetch():
        from core.scanner import _yf
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

        # Holiday- and early-close-aware; weekday() < 5 called Thanksgiving a session.
        _market_open = is_market_open()
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

        calls = ladder_rows(calls_df.to_dict("records"), "call", price, d)
        puts  = ladder_rows(puts_df.to_dict("records"),  "put",  price, d)

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
            "dte_note": _dte_note(dte_mode, [{"dte": d}]),
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

    # The ladder shows where the money is; these are the contracts actually worth
    # buying on each side, scored by the same engine that backs FIND TOP 3.
    def _pick(direction: str, vix: float):
        try:
            c = get_best_contract(ticker, direction, 0, vix, top_n=1,
                                  dte_mode=dte_mode, target_price=0)
        except Exception:
            return None
        if isinstance(c, list):
            c = c[0] if c else None
        return c

    try:
        vix = await loop.run_in_executor(None, fetch_vix)
        best_call, best_put = await asyncio.wait_for(
            asyncio.gather(
                loop.run_in_executor(None, lambda: _pick("up", vix)),
                loop.run_in_executor(None, lambda: _pick("down", vix)),
            ), timeout=30.0
        )
    except Exception:
        # The ladder is still useful without the picks — never fail the whole
        # response because the scoring engine came up empty or slow.
        best_call = best_put = None

    result["best_call"] = best_call
    result["best_put"]  = best_put
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
        ticker_list = get_universe()[:100]  # default: top 100 live tickers
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
        ticker_list = _get_insider_universe()
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


@app.get("/api/unusual-flow")
async def api_unusual_flow(
    req: Request,
    tickers: str = Query(""),
    min_score: int = Query(35),
    force: bool = Query(False),
):
    """
    Unusual options activity scanner.
    Returns contracts where vol/OI, notional, or DTE signals anomalous flow.
    Results cached 10 min. Pass force=true to rebuild immediately.
    """
    _check_pin(req)
    _check_rate(req, "unusual_flow", limit=3, window=120)

    now = time.time()
    if not force and now - _UOA_CACHE["ts"] < _UOA_TTL and _UOA_CACHE["signals"]:
        return {
            "signals":  _UOA_CACHE["signals"],
            "summary":  _UOA_CACHE["summary"],
            "count":    len(_UOA_CACHE["signals"]),
            "cached":   True,
            "last_updated": datetime.fromtimestamp(_UOA_CACHE["ts"]).strftime("%H:%M:%S"),
        }

    # Custom ticker list or full pool
    pool = [t.strip().upper() for t in tickers.split(",") if t.strip()] or None

    loop = asyncio.get_event_loop()
    try:
        signals = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: scan_unusual_flow(
                tickers=pool, top_tickers=80, min_score=min_score, max_results=150,
            )),
            timeout=120,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Unusual flow scan timed out")

    summary = sector_flow_summary(signals)
    _UOA_CACHE["signals"]  = signals
    _UOA_CACHE["summary"]  = summary
    _UOA_CACHE["ts"]       = time.time()

    return {
        "signals":      signals,
        "summary":      summary,
        "count":        len(signals),
        "cached":       False,
        "last_updated": datetime.now().strftime("%H:%M:%S"),
    }


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
# ---- Page source ------------------------------------------------------------
# The page is authored as real files under web/, not as a Python string: edit
# web/static/app.css or web/static/app.js and restart, no Python to touch. They
# are inlined into one response at import so the app still serves the whole UI
# in a single request (what the PWA install and the offline cache rely on).
_WEB_DIR = os.path.dirname(os.path.abspath(__file__))


def _read(*parts: str) -> str:
    with open(os.path.join(_WEB_DIR, *parts), encoding="utf-8") as fh:
        return fh.read()


def _build_html() -> str:
    return (_read("templates", "index.html")
            .replace("{{APP_CSS}}", _read("static", "app.css"))
            .replace("{{APP_JS}}", _read("static", "app.js")))


HTML = _build_html()


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
