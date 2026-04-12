#!/usr/bin/env python3
"""
scanner_app.py — Mobile-First Options Flow Scanner PWA
Run:   python3 scanner_app.py
Open:  http://localhost:8765  (or LAN IP on phone)
"""

from __future__ import annotations
import asyncio, collections, contextlib, hmac, io, json, os, queue, re, sys, threading, time
from datetime import datetime
from typing import Dict, List, Optional

try:
    from fastapi import FastAPI, Query, Request, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
except ImportError:
    sys.exit("pip install fastapi 'uvicorn[standard]'  (use python3)")

sys.path.insert(0, os.path.dirname(__file__))
from scanner import (
    UNIVERSE, TICKER_SECTOR,
    fetch_vix, vix_delta_target,
    scan_options_flow, get_best_contract,
    scan_sectors, calc_whale_score,
)

PORT = int(os.environ.get("PORT", 8765))

# ── Optional PIN auth (set env var SCANNER_PIN to enable) ────────────────────
_PIN = os.environ.get("SCANNER_PIN", "").strip()

# ── Valid parameter enums ─────────────────────────────────────────────────────
_VALID_DIRECTION  = {"up", "down"}
_VALID_DTE_TYPE   = {"weekly", "0dte", "swing"}
_VALID_BIAS       = {"both", "call", "put"}
_VALID_DTE_FILTER = {"all", "0dte", "7dte"}

# ── Ticker validation: letters, digits, ^ (index), ., - only, 1–12 chars ─────
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

# ── Rate limiter — sliding window per IP, with memory cap ────────────────────
_MAX_RL_KEYS = 10_000   # cap tracked IPs to prevent unbounded memory growth

class _RateLimiter:
    def __init__(self):
        self._windows: Dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_secs: int) -> bool:
        now = time.monotonic()
        with self._lock:
            # Evict oldest key if over cap (simple FIFO eviction)
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

def _client_ip(req: Request) -> str:
    # On cloud (Render/Railway), the real client IP is in X-Forwarded-For.
    # Always use leftmost IP (added by outermost public proxy, not spoofable past Render's edge).
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else "unknown"

def _check_rate(req: Request, endpoint: str, limit: int, window: int):
    key = f"{_client_ip(req)}:{endpoint}"
    if not _rl.allow(key, limit, window):
        raise HTTPException(429, detail="Too many requests — slow down",
                            headers={"Retry-After": str(window)})

# ── Concurrent scan guard — only one active scan at a time ───────────────────
_active_scan = threading.Event()   # set while a scan is running

# ── PIN check — timing-safe + brute-force lockout ────────────────────────────
def _check_pin(req: Request):
    if not _PIN:
        return
    ip = _client_ip(req)
    supplied = (req.headers.get("x-pin", "") or req.query_params.get("pin", "")).strip()
    # hmac.compare_digest prevents timing oracle (constant-time comparison)
    if not hmac.compare_digest(supplied.encode("utf-8", errors="replace"),
                               _PIN.encode("utf-8")):
        # Only count FAILED attempts toward brute-force lockout.
        # Successful requests must never consume auth slots — otherwise
        # normal usage (load page → VIX → universe → scan = 4 calls) exhausts
        # the limit and locks the user out with 429 → "Connection lost".
        if not _rl.allow(f"{ip}:auth_fail", limit=10, window=300):
            raise HTTPException(429, detail="Too many failed attempts — try later",
                                headers={"Retry-After": "300"})
        raise HTTPException(401, "Unauthorized")

DEFAULT_FLOW_TICKERS = [
    "SPX","SPY","QQQ","IWM",
    "NVDA","AMD","AAPL","MSFT","META","AMZN","TSLA","GOOGL",
    "COIN","PLTR","MSTR","HOOD","MARA",
    "SOFI","AFRM","GME","HIMS",
    "TQQQ","SQQQ","SOXL","SOXS",
    "GS","JPM","VXX","UVXY",
]

app = FastAPI(title="Scanner", docs_url=None, redoc_url=None)

# ── CORS — open to all origins (PIN is the auth layer; this is a local tool) ─
# Restricting origins to a hardcoded IP breaks LAN phones + ngrok tunnels.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["x-pin"],
    max_age=600,
)

# ── Security headers middleware ───────────────────────────────────────────────
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h["X-Content-Type-Options"]  = "nosniff"
    h["X-Frame-Options"]         = "DENY"
    h["X-XSS-Protection"]        = "1; mode=block"
    h["Referrer-Policy"]         = "no-referrer"
    h["Permissions-Policy"]      = "geolocation=(), camera=(), microphone=()"
    # HSTS — tell browsers to always use HTTPS for this domain (1 year)
    h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Hide server fingerprint
    h["Server"] = "scanner"
    # CSP: allow inline scripts/styles for the embedded single-page HTML
    h["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'"
    )
    # API responses must not be cached — live market data only
    # Don't override if already set (SSE endpoints need no-transform for proxy buffering)
    if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        h["Cache-Control"] = "no-store, no-cache, must-revalidate, no-transform"
        h["Pragma"]        = "no-cache"
    return response

# ── helpers ───────────────────────────────────────────────────────────────────
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

def _serialize(sig: Dict) -> Dict:
    tc  = sig.get("top_contract") or {}
    all_c = sig.get("call_contracts", []) + sig.get("put_contracts", [])
    top_s = sig.get("top_strike", tc.get("strike", 0))
    hits  = sum(1 for c in all_c if c.get("strike") == top_s) if top_s else 1

    # Top 3 contracts from dominant side
    bias_contracts = sig.get("call_contracts" if sig["flow_bias"] == "call" else "put_contracts", [])
    top3 = sorted(bias_contracts, key=lambda c: c.get("flow", 0), reverse=True)[:3]
    top3_out = []
    for c in top3:
        top3_out.append({
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
        })

    return {
        "ts":         datetime.now().strftime("%H:%M"),
        "ticker":     sig["ticker"],
        "bias":       sig["flow_bias"],
        "badge":      _badge(sig),
        "cls":        _cls(sig),
        "score":      sig.get("whale_score", 0),
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
        "tier":       sig.get("premium_tier", "retail"),
        "top3":       top3_out,
    }

# ── API ───────────────────────────────────────────────────────────────────────
@app.get("/api/universe")
async def api_universe(req: Request):
    _check_pin(req)
    _check_rate(req, "universe", limit=10, window=60)
    return {"quick": DEFAULT_FLOW_TICKERS, "full": UNIVERSE}

@app.get("/api/vix")
async def api_vix(req: Request):
    _check_pin(req)
    _check_rate(req, "vix", limit=20, window=60)
    loop = asyncio.get_event_loop()
    vix = await loop.run_in_executor(None, fetch_vix)
    tgt = vix_delta_target(vix)
    reg = ("Extreme Fear" if vix >= 40 else "Fear" if vix >= 30 else
           "Elevated" if vix >= 24 else "Normal" if vix >= 16 else
           "Calm" if vix >= 13 else "Complacent")
    return {"vix": round(vix, 2), "delta_target": round(tgt, 3), "regime": reg}

@app.get("/api/contract")
async def api_contract(req: Request, ticker: str = "SPY", direction: str = "up",
                       price: float = 0, dte_type: str = "weekly"):
    _check_pin(req)
    _check_rate(req, "contract", limit=10, window=60)
    ticker    = _validate_ticker(ticker)
    direction = _validate_enum(direction, _VALID_DIRECTION, "direction")
    dte_type  = _validate_enum(dte_type,  _VALID_DTE_TYPE,  "dte_type")
    # Guard NaN/Inf (comparisons with NaN always return False, bypassing range check)
    import math
    if not math.isfinite(price) or not (0 <= price < 1_000_000):
        raise HTTPException(400, "Invalid price")
    loop = asyncio.get_event_loop()
    vix  = await loop.run_in_executor(None, fetch_vix)
    try:
        contracts = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: get_best_contract(ticker, direction, price, vix,
                                                top_n=3, dte_type=dte_type)
            ), timeout=30.0
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Contract lookup timed out")
    if not contracts:
        return JSONResponse({"error": f"No contracts found for {ticker} ({dte_type})"}, status_code=404)
    if isinstance(contracts, dict):
        contracts = [contracts]
    return contracts

@app.get("/api/debug")
async def api_debug(req: Request):
    """Diagnostic endpoint — PIN required, rate-limited to 3/min."""
    _check_pin(req)
    _check_rate(req, "debug", limit=3, window=60)
    from scanner import _YF_SESSION, _yf
    results: Dict = {}
    results["session_type"] = type(_YF_SESSION).__name__
    # VIX
    try:
        t = _yf("VIX")
        h = t.history(period="2d")
        results["vix_ok"]    = not h.empty
        results["vix_rows"]  = len(h)
        results["vix_value"] = round(float(h["Close"].iloc[-1]), 2) if not h.empty else None
    except Exception as e:
        results["vix_ok"] = False
        results["vix_error"] = type(e).__name__ + ": " + str(e)[:200]
    # SPY options list
    try:
        t2   = _yf("SPY")
        exps = t2.options
        results["spy_exps"] = list(exps[:5]) if exps else []
        results["spy_ok"]   = bool(exps)
    except Exception as e:
        results["spy_ok"]    = False
        results["spy_error"] = type(e).__name__ + ": " + str(e)[:200]
    # SPY first chain
    try:
        if results.get("spy_exps"):
            chain = t2.option_chain(results["spy_exps"][0])
            results["spy_chain_calls"] = len(chain.calls)
            results["spy_chain_puts"]  = len(chain.puts)
            results["chain_ok"]        = True
    except Exception as e:
        results["chain_ok"]    = False
        results["chain_error"] = type(e).__name__ + ": " + str(e)[:200]
    return results

@app.get("/api/sectors")
async def api_sectors(req: Request):
    _check_pin(req)
    _check_rate(req, "sectors", limit=3, window=60)
    loop = asyncio.get_event_loop()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, scan_sectors), timeout=45.0
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Sector scan timed out")
    clean = [{"name": k, "etf": v.get("etf",""), "change": round(v.get("change_pct",0),2),
              "strength": round(v.get("strength",0),2), "price": round(v.get("price",0),2)}
             for k, v in data.items()]
    if not clean:
        raise HTTPException(503, "No sector data — market may be closed or data source unavailable")
    return sorted(clean, key=lambda x: x["change"], reverse=True)

@app.get("/api/flow/stream")
async def api_flow_stream(
    req:         Request,
    tickers:     str = Query(",".join(DEFAULT_FLOW_TICKERS)),
    min_score:   int = Query(0),
    bias_filter: str = Query("both"),
    dte_filter:  str = Query("all"),
):
    _check_pin(req)
    _check_rate(req, "scan", limit=2, window=30)   # max 2 scans per 30s per IP
    _validate_enum(bias_filter, _VALID_BIAS,       "bias_filter")
    _validate_enum(dte_filter,  _VALID_DTE_FILTER, "dte_filter")
    if not (0 <= min_score <= 100):
        raise HTTPException(400, "min_score must be 0–100")

    # Guard against oversized ticker string before any processing
    if len(tickers) > 4096:
        raise HTTPException(400, "Ticker list too long")

    # Validate and cap ticker list
    raw_tickers = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    ticker_list: List[str] = []
    for t in raw_tickers[:150]:       # hard cap: 150 tickers
        try: ticker_list.append(_validate_ticker(t))
        except HTTPException: continue  # silently drop invalid tickers

    if not ticker_list:
        raise HTTPException(400, "No valid tickers provided")

    # Only one scan at a time — reject if busy
    if _active_scan.is_set():
        raise HTTPException(503, detail="A scan is already running — wait for it to finish.",
                            headers={"Retry-After": "30"})

    q: queue.Queue = queue.Queue()

    def on_progress(info): q.put({"__progress__": True, **info})
    def on_signal(sig):    q.put({"__signal__": True, "data": _serialize(sig)})

    def run():
        try:
            scan_options_flow(ticker_list, show_progress=False,
                              on_signal=on_signal, on_progress=on_progress)
        except Exception:
            q.put({"__error__": "Scan failed — check server logs"})
        finally:
            _active_scan.clear()
            q.put({"__done__": True})

    _active_scan.set()
    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        _active_scan.clear()
        raise HTTPException(500, "Failed to start scan")

    # SSE max runtime: 10 min hard timeout
    _start = time.monotonic()
    _SSE_TIMEOUT = 600

    async def generate():
        while True:
            if time.monotonic() - _start > _SSE_TIMEOUT:
                yield 'data: {"__error__":"Scan timeout"}\n\n'
                break
            try:
                item = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, q.get, True, 1.0),
                    timeout=2.0
                )
            except (asyncio.TimeoutError, queue.Empty):
                yield 'data: {"__ping__":true}\n\n'; continue

            if item.get("__signal__"):
                s = item["data"]
                if min_score > 0 and s["score"] < min_score: continue
                if bias_filter == "call" and s["bias"] != "call": continue
                if bias_filter == "put"  and s["bias"] != "put":  continue
                if dte_filter == "0dte"  and s["dte"] != 0:       continue
                if dte_filter == "7dte"  and s["dte"] > 7:        continue

            yield f"data: {json.dumps(item)}\n\n"
            if item.get("__done__") or item.get("__error__"): break

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={
                                 "Cache-Control":     "no-cache, no-transform",
                                 "X-Accel-Buffering": "no",
                                 "Connection":        "keep-alive",
                                 "Content-Type":      "text/event-stream; charset=utf-8",
                             })

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Scanner",
        "short_name": "Scanner",
        "description": "Options Flow Scanner",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f5f5f7",
        "theme_color": "#f5f5f7",
        "orientation": "portrait",
        "icons": [
            {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}
        ]
    })

# Minimal blue "S" icon as inline SVG rendered to a 180×180 PNG-equivalent data URI
# Served as a real endpoint so Safari can fetch it for homescreen
_ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180" viewBox="0 0 180 180">
  <rect width="180" height="180" rx="40" fill="#007aff"/>
  <text x="90" y="125" font-family="-apple-system,BlinkMacSystemFont,Helvetica Neue,sans-serif"
        font-size="100" font-weight="700" text-anchor="middle" fill="#ffffff">S</text>
</svg>"""

from fastapi.responses import Response as _Response

@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    # Return the SVG with image/svg+xml; Safari accepts SVG for touch icons
    return _Response(content=_ICON_SVG.encode(), media_type="image/svg+xml",
                     headers={"Cache-Control": "public, max-age=86400"})

@app.get("/", response_class=HTMLResponse)
async def root(): return HTML.replace("__SCANNER_PIN__", _PIN or "")

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#f5f5f7">
<meta name="apple-mobile-web-app-title" content="Scanner">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.json">
<title>Scanner</title>
<style>
/* ── Reset & tokens ──────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{
  --bg:        #f5f5f7;
  --bg2:       #ffffff;
  --bg3:       #f2f2f7;
  --glass:     rgba(255,255,255,0.75);
  --glass2:    rgba(255,255,255,0.9);
  --border:    rgba(0,0,0,0.08);
  --border2:   rgba(0,0,0,0.12);
  --text:      #1d1d1f;
  --sub:       #6e6e73;
  --green:     #34c759;
  --red:       #ff3b30;
  --gold:      #ff9f0a;
  --amber:     #ff9f0a;
  --purple:    #af52de;
  --blue:      #007aff;
  --teal:      #32ade6;
  --cyan:      #5ac8fa;
  --safe-b:    env(safe-area-inset-bottom,0px);
  --safe-t:    env(safe-area-inset-top,0px);
  /* themeable surfaces */
  --card-bg:     rgba(255,255,255,0.85);
  --chip-bg:     rgba(255,255,255,0.8);
  --bar-track:   rgba(0,0,0,0.07);
  --topbar-bg:   rgba(255,255,255,0.85);
  --tabbar-bg:   rgba(255,255,255,0.88);
  --input-bg:    #ffffff;
  --detail-bg:   rgba(242,242,247,0.5);
  --grid-cell:   #ffffff;
  --grid-div:    rgba(0,0,0,0.06);
  --toast-bg:    rgba(255,255,255,0.95);
  --vt-track:    rgba(0,0,0,0.06);
  --vt-active:   #ffffff;
  --golden-bg:   rgba(255,248,235,0.9);
  --whale-bg:    rgba(252,245,255,0.9);
  --sub-badge:   rgba(0,0,0,0.05);
  --scroll-thumb:rgba(0,0,0,0.15);
}
html.dark{
  --bg:          #000000;
  --bg2:         #1c1c1e;
  --bg3:         #2c2c2e;
  --glass:       rgba(28,28,30,0.85);
  --glass2:      rgba(44,44,46,0.95);
  --border:      rgba(255,255,255,0.09);
  --border2:     rgba(255,255,255,0.15);
  --text:        #f5f5f7;
  --sub:         #8e8e93;
  --card-bg:     rgba(28,28,30,0.95);
  --chip-bg:     rgba(44,44,46,0.9);
  --bar-track:   rgba(255,255,255,0.09);
  --topbar-bg:   rgba(0,0,0,0.88);
  --tabbar-bg:   rgba(0,0,0,0.92);
  --input-bg:    #1c1c1e;
  --detail-bg:   rgba(44,44,46,0.6);
  --grid-cell:   #1c1c1e;
  --grid-div:    rgba(255,255,255,0.07);
  --toast-bg:    rgba(44,44,46,0.97);
  --vt-track:    rgba(255,255,255,0.08);
  --vt-active:   #3a3a3c;
  --golden-bg:   rgba(60,40,0,0.6);
  --whale-bg:    rgba(50,20,60,0.6);
  --sub-badge:   rgba(255,255,255,0.07);
  --scroll-thumb:rgba(255,255,255,0.2);
}
html.dark body::before{
  background:
    radial-gradient(ellipse 80% 60% at 15% 40%,rgba(0,50,150,.2) 0%,transparent 60%),
    radial-gradient(ellipse 60% 50% at 85% 15%,rgba(80,0,150,.15) 0%,transparent 55%),
    radial-gradient(ellipse 70% 50% at 50% 90%,rgba(0,80,120,.15) 0%,transparent 55%);
  opacity:.5;
}
html.dark .shimmer{
  background:linear-gradient(90deg,#2c2c2e 25%,#3a3a3c 50%,#2c2c2e 75%);
  background-size:200% 100%;
}
/* colour aliases */
.green{color:var(--green)!important}.red{color:var(--red)!important}
.gold{color:var(--gold)!important}.teal{color:var(--teal)!important}
.amber{color:var(--amber)!important}.purple{color:var(--purple)!important}
html,body{height:100%;overflow:hidden;overscroll-behavior:none;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','SF Pro Text','Inter',sans-serif;
  background:var(--bg);color:var(--text);font-size:14px;-webkit-font-smoothing:antialiased}

/* ── Marble background ───────────────────────────────────────── */
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 80% 60% at 15% 40%,rgba(180,200,240,.18) 0%,transparent 60%),
    radial-gradient(ellipse 60% 50% at 85% 15%,rgba(200,210,240,.14) 0%,transparent 55%),
    radial-gradient(ellipse 70% 50% at 50% 90%,rgba(220,230,250,.12) 0%,transparent 55%),
    radial-gradient(ellipse 40% 30% at 70% 60%,rgba(230,220,240,.10) 0%,transparent 50%),
    radial-gradient(ellipse 50% 40% at 30% 80%,rgba(210,220,235,.15) 0%,transparent 50%);
  opacity:.45;
}

/* ── Layout ──────────────────────────────────────────────────── */
#app{display:flex;flex-direction:column;height:100dvh;position:relative;z-index:1}
#topbar{
  flex-shrink:0;
  padding:calc(12px + var(--safe-t)) 20px 14px;
  background:var(--topbar-bg);
  backdrop-filter:blur(30px) saturate(180%);
  -webkit-backdrop-filter:blur(30px) saturate(180%);
  border-bottom:1px solid var(--border2);
}
#content{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;
  padding-bottom:calc(68px + var(--safe-b))}
#tabbar{
  position:fixed;bottom:0;left:0;right:0;
  display:flex;
  background:var(--tabbar-bg);
  backdrop-filter:blur(30px) saturate(180%);
  -webkit-backdrop-filter:blur(30px) saturate(180%);
  border-top:1px solid var(--border2);
  padding-bottom:var(--safe-b);
  z-index:50;
}

/* ── Topbar ──────────────────────────────────────────────────── */
.tb-row1{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.app-name{font-size:18px;font-weight:700;letter-spacing:-.5px;flex:1}
.app-name span{background:linear-gradient(135deg,var(--blue),var(--purple));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.vix-chip{
  background:rgba(0,0,0,0.04);border:1px solid var(--border);border-radius:20px;
  padding:5px 12px;font-size:12px;font-weight:600;color:var(--sub);
  transition:all .2s;
}
.vix-chip.fear{color:var(--red);border-color:rgba(255,59,48,.2);background:rgba(255,59,48,.07)}
.vix-chip.calm{color:#248a3d;border-color:rgba(52,199,89,.2);background:rgba(52,199,89,.07)}
.vix-chip.elevated{color:#c93400;border-color:rgba(255,159,10,.2);background:rgba(255,159,10,.07)}
.bias-row{display:flex;align-items:center;gap:8px;font-size:12px}
.bias-c{color:#248a3d;font-weight:700}
.bias-p{color:var(--red);font-weight:700}
.bias-sep{color:var(--border2)}
.bias-dir{font-weight:800;font-size:13px}
.bias-dir.bull{color:#248a3d}
.bias-dir.bear{color:var(--red)}
.bias-dir.neut{color:var(--sub)}

/* ── Tab bar ─────────────────────────────────────────────────── */
.tab-btn{
  flex:1;padding:10px 4px 8px;background:none;border:none;
  color:var(--sub);font-size:9px;font-weight:600;letter-spacing:.5px;
  cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:4px;
  transition:color .15s;
}
.tab-btn svg{width:22px;height:22px;stroke-width:1.6;fill:none;stroke:currentColor}
.tab-btn.active{color:var(--blue)}
.tab-pane{display:none}.tab-pane.active{display:block}

/* ── Filter bar ──────────────────────────────────────────────── */
.filter-bar{
  display:flex;gap:8px;padding:14px 16px 10px;
  overflow-x:auto;scrollbar-width:none;align-items:center;flex-wrap:nowrap;
}
.filter-bar::-webkit-scrollbar{display:none}
.chip{
  background:var(--chip-bg);border:1px solid var(--border);border-radius:20px;
  padding:11px 15px;font-size:12px;font-weight:600;color:var(--sub);
  cursor:pointer;white-space:nowrap;transition:all .18s cubic-bezier(.34,1.56,.64,1);
  flex-shrink:0;box-shadow:0 1px 3px rgba(0,0,0,.05);min-height:44px;
  display:flex;align-items:center;
}
.chip:active{transform:scale(.94)}
.chip.on{background:rgba(0,122,255,.08);border-color:rgba(0,122,255,.3);color:var(--blue)}
.chip.call-on{background:rgba(52,199,89,.08);border-color:rgba(52,199,89,.3);color:#248a3d}
.chip.put-on{background:rgba(255,59,48,.08);border-color:rgba(255,59,48,.3);color:var(--red)}
.scan-btn{
  background:var(--blue);
  color:#fff;border:none;border-radius:20px;padding:11px 20px;
  font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap;
  margin-left:auto;flex-shrink:0;min-height:44px;
  transition:transform .15s cubic-bezier(.34,1.56,.64,1),opacity .15s;
  box-shadow:0 2px 12px rgba(0,122,255,.25);
}
.scan-btn:active{transform:scale(.94)}
.scan-btn.loading{background:var(--bg3);color:var(--sub);box-shadow:none;cursor:default}

/* ── Progress ────────────────────────────────────────────────── */
.prog-wrap{height:2px;background:transparent;position:relative;overflow:hidden;margin:0 16px 0}
.prog-bar{height:100%;background:linear-gradient(90deg,var(--blue),var(--purple));
  width:0%;transition:width .35s ease;border-radius:1px}
.prog-lbl{text-align:center;font-size:11px;color:var(--sub);padding:6px 0 2px}

/* ── Flow feed ───────────────────────────────────────────────── */
#flow-feed{padding:4px 0 8px}
.empty-st{text-align:center;color:var(--sub);padding:64px 24px 32px;line-height:1.7}
.empty-st .icon{font-size:40px;margin-bottom:12px}
.empty-st h3{font-size:16px;font-weight:600;color:var(--text);margin-bottom:6px}

/* ── Flow card ───────────────────────────────────────────────── */
.flow-card{
  margin:8px 12px;
  background:var(--card-bg);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border:1px solid var(--border);
  border-radius:20px;overflow:hidden;
  cursor:pointer;
  box-shadow:0 2px 20px rgba(0,0,0,0.08);
  animation:cardIn .3s cubic-bezier(.34,1.56,.64,1) both;
  transition:transform .15s,box-shadow .15s;
}
.flow-card:active{transform:scale(.985);box-shadow:0 1px 8px rgba(0,0,0,.06)}
.flow-card.golden{
  border-color:rgba(255,159,10,.2);
  background:var(--golden-bg);
  box-shadow:0 2px 24px rgba(255,159,10,.12),0 0 0 1px rgba(255,159,10,.1);
  animation:cardIn .3s cubic-bezier(.34,1.56,.64,1) both,
            goldPulse 3s ease-in-out infinite .4s;
}
.flow-card.whale{
  border-color:rgba(175,82,222,.15);
  background:var(--whale-bg);
  box-shadow:0 2px 20px rgba(175,82,222,.1),0 0 0 1px rgba(175,82,222,.08);
}
.flow-card.inst{border-color:rgba(0,122,255,.12);box-shadow:0 2px 16px rgba(0,122,255,.07)}

@keyframes cardIn{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:none}}
@keyframes goldPulse{0%,100%{box-shadow:0 2px 24px rgba(255,159,10,.12),0 0 0 1px rgba(255,159,10,.1)}
  50%{box-shadow:0 4px 32px rgba(255,159,10,.2),0 0 0 1px rgba(255,159,10,.15)}}

/* card header */
.card-head{padding:14px 16px 10px;display:flex;align-items:flex-start;gap:10px}
.badge{
  font-size:9px;font-weight:800;letter-spacing:.8px;padding:4px 9px;
  border-radius:6px;text-transform:uppercase;flex-shrink:0;margin-top:2px;
}
.badge.golden{background:rgba(255,159,10,.12);color:#b06000;border:1px solid rgba(255,159,10,.25)}
.badge.whale{background:rgba(175,82,222,.1);color:#7b2da0;border:1px solid rgba(175,82,222,.2)}
.badge.stacked{background:rgba(0,122,255,.1);color:#0055cc;border:1px solid rgba(0,122,255,.2)}
.badge.sweep{background:rgba(255,159,10,.1);color:#c93400;border:1px solid rgba(255,159,10,.2)}
.badge.block{background:rgba(0,0,0,.05);color:var(--sub);border:1px solid var(--border)}
.badge.flow{background:var(--bg3);color:var(--sub);border:1px solid var(--border)}
.card-title{flex:1;min-width:0}
.card-ticker{font-size:22px;font-weight:800;letter-spacing:-.8px;line-height:1;color:var(--text)}
.card-exp{font-size:12px;color:var(--sub);margin-top:3px}
.card-premium{text-align:right;flex-shrink:0}
.card-premium .amount{font-size:20px;font-weight:800;letter-spacing:-.5px;line-height:1}
.card-premium .amount.call{color:#248a3d}
.card-premium .amount.put{color:var(--red)}
.card-premium .label{font-size:10px;color:var(--sub);margin-top:3px;text-align:right}

/* stats row */
.card-stats{display:flex;gap:0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.stat{flex:1;padding:10px 12px;text-align:center;border-right:1px solid var(--border)}
.stat:last-child{border-right:none}
.stat label{display:block;font-size:9px;letter-spacing:.5px;color:var(--sub);text-transform:uppercase;margin-bottom:3px}
.stat .val{font-size:14px;font-weight:700;color:var(--text)}
.val.hot{color:var(--red)}
.val.warm{color:#c93400}
.val.ask{color:#248a3d}
.val.bid{color:var(--red)}
.val.mid{color:var(--sub)}
.val.call{color:#248a3d}
.val.put{color:var(--red)}

/* top-3 contracts */
.contracts-row{display:flex;gap:8px;padding:10px 12px;overflow-x:auto;scrollbar-width:none}
.contracts-row::-webkit-scrollbar{display:none}
.contract-chip{
  flex-shrink:0;background:var(--chip-bg);border:1px solid var(--border);
  border-radius:14px;padding:10px 13px;min-width:110px;
  transition:border-color .15s;
}
.contract-chip.top1{border-color:rgba(0,122,255,.2);background:rgba(0,122,255,.05)}
.contract-chip.golden-c{border-color:rgba(255,159,10,.2);background:rgba(255,159,10,.05)}
.cc-strike{font-size:16px;font-weight:800;letter-spacing:-.5px}
.cc-strike.call{color:#248a3d}.cc-strike.put{color:var(--red)}
.cc-meta{font-size:10px;color:var(--sub);margin-top:2px}
.cc-price{font-size:13px;font-weight:700;margin-top:6px;color:var(--text)}
.cc-voi{font-size:10px;margin-top:2px}
.cc-voi.hot{color:var(--red)}.cc-voi.warm{color:#c93400}.cc-voi.cool{color:var(--sub)}
.dte-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px;vertical-align:middle}
.dte-dot.zero{background:var(--blue)}.dte-dot.near{background:var(--amber)}.dte-dot.far{background:var(--sub)}

/* expanded detail */
.card-detail{padding:12px 16px 14px;border-top:1px solid var(--border);display:none;background:var(--detail-bg)}
.card-detail.open{display:block;animation:fadeIn .2s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.detail-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:12px}
.dg-item label{font-size:10px;color:var(--sub);letter-spacing:.4px;text-transform:uppercase;display:block;margin-bottom:3px}
.dg-item span{font-size:14px;font-weight:700;color:var(--text)}
.dte-seg-row{display:flex;gap:8px}
.dte-seg{flex:1;background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:10px;text-align:center}
.dte-seg label{font-size:9px;color:var(--sub);display:block;margin-bottom:4px;letter-spacing:.5px;text-transform:uppercase}
.dte-seg span{font-size:13px;font-weight:700}

/* ── Sector tab ──────────────────────────────────────────────── */
.sec-head{padding:16px 20px 8px;font-size:11px;font-weight:700;letter-spacing:1px;color:var(--sub);text-transform:uppercase}
.sec-load-btn{
  display:block;width:calc(100% - 32px);margin:16px;
  background:var(--card-bg);border:1px solid var(--border);
  border-radius:16px;padding:14px;text-align:center;color:var(--blue);
  font-weight:600;font-size:14px;cursor:pointer;font-family:inherit;
  box-shadow:0 1px 6px rgba(0,0,0,.06);
  transition:all .15s cubic-bezier(.34,1.56,.64,1);
  -webkit-appearance:none;
}
.sec-load-btn:active{transform:scale(.97)}
.sec-card{
  margin:6px 12px;background:var(--card-bg);border:1px solid var(--border);
  border-radius:16px;padding:0;
  display:flex;flex-direction:column;
  box-shadow:0 1px 8px rgba(0,0,0,.06);
  animation:cardIn .25s ease both;overflow:hidden;
}
.sec-card>div:first-child{padding:14px 16px}
.sec-name{font-size:14px;font-weight:700;width:90px;flex-shrink:0;color:var(--text)}
.sec-bar-wrap{flex:1;height:5px;background:var(--bar-track);border-radius:3px;overflow:hidden}
.sec-bar{height:100%;border-radius:3px;transition:width .6s cubic-bezier(.34,1.56,.64,1)}
.sec-bar.up{background:linear-gradient(90deg,#248a3d,rgba(52,199,89,.5))}
.sec-bar.dn{background:linear-gradient(90deg,var(--red),rgba(255,59,48,.5))}
.sec-chg{font-size:14px;font-weight:700;width:60px;text-align:right;flex-shrink:0}
.sec-chg.up{color:#248a3d}.sec-chg.dn{color:var(--red)}

/* ── Contract finder ─────────────────────────────────────────── */
.finder{padding:20px 16px}
.find-input{
  width:100%;background:var(--input-bg);border:1.5px solid var(--border);
  border-radius:16px;color:var(--text);font-size:24px;font-weight:800;
  letter-spacing:1px;padding:16px 18px;text-transform:uppercase;outline:none;
  transition:border-color .2s,box-shadow .2s;
  box-shadow:0 1px 6px rgba(0,0,0,.06);
}
.find-input:focus{border-color:rgba(0,122,255,.4);box-shadow:0 0 0 3px rgba(0,122,255,.1)}
.find-input::placeholder{color:var(--sub);font-weight:400;letter-spacing:0}
.dir-row{display:flex;gap:10px;margin-top:12px}
.dir-btn{
  flex:1;padding:13px;border:1.5px solid var(--border);border-radius:14px;
  background:var(--chip-bg);color:var(--sub);font-size:14px;font-weight:700;
  cursor:pointer;transition:all .18s cubic-bezier(.34,1.56,.64,1);
  box-shadow:0 1px 4px rgba(0,0,0,.05);min-height:44px;
}
.dir-btn:active{transform:scale(.96)}
.dir-btn.up.on{background:rgba(52,199,89,.08);border-color:rgba(52,199,89,.3);color:#248a3d}
.dir-btn.dn.on{background:rgba(255,59,48,.08);border-color:rgba(255,59,48,.3);color:var(--red)}
.find-go{
  width:100%;margin-top:12px;padding:16px;
  background:var(--blue);
  color:#fff;border:none;border-radius:16px;font-size:15px;font-weight:700;
  cursor:pointer;transition:transform .15s cubic-bezier(.34,1.56,.64,1),opacity .15s;
  box-shadow:0 4px 20px rgba(0,122,255,.25);
}
.find-go:active{transform:scale(.97)}
.find-go.loading{background:var(--bg3);color:var(--sub);box-shadow:none}
#find-result{margin-top:16px}
.cont-cards{display:flex;flex-direction:column;gap:10px}
.cont-card{
  background:var(--card-bg);border:1px solid var(--border);border-radius:20px;
  overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,.08);
  animation:cardIn .3s cubic-bezier(.34,1.56,.64,1) both;
}
.cont-card:nth-child(2){animation-delay:.06s}.cont-card:nth-child(3){animation-delay:.12s}
.cont-card.best{border-color:rgba(0,122,255,.2);box-shadow:0 4px 24px rgba(0,122,255,.1)}
.cont-hero{padding:18px 18px 12px;display:flex;justify-content:space-between;align-items:flex-start}
.cont-sym{font-size:26px;font-weight:800;letter-spacing:-.8px;color:var(--text)}
.cont-sym .call{color:#248a3d}.cont-sym .put{color:var(--red)}
.cont-sym .strike{color:var(--sub);font-size:18px;font-weight:500}
.cont-badge{font-size:10px;font-weight:700;padding:4px 10px;border-radius:20px;margin-top:4px}
.cont-badge.best{background:rgba(0,122,255,.1);color:#0055cc;border:1px solid rgba(0,122,255,.2)}
.cont-badge.alt{background:var(--sub-badge);color:var(--sub);border:1px solid var(--border)}
.cont-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--grid-div)}
.cg{background:var(--grid-cell);padding:12px 14px}
.cg label{font-size:10px;color:var(--sub);display:block;letter-spacing:.4px;margin-bottom:3px;text-transform:uppercase}
.cg span{font-size:16px;font-weight:700;color:var(--text)}
.cg span.green{color:#248a3d}.cg span.red{color:var(--red)}
.cg span.gold{color:#b06000}.cg span.teal{color:var(--blue)}
.cont-note{padding:10px 16px;font-size:11px;color:var(--sub);border-top:1px solid var(--border)}
.cont-note.stale{color:#c93400}

/* ── Shimmer ─────────────────────────────────────────────────── */
.shimmer{
  background:linear-gradient(90deg,#e8e8ed 25%,#f2f2f7 50%,#e8e8ed 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite;border-radius:12px;
}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* ── Toast ───────────────────────────────────────────────────── */
#toast{
  position:fixed;top:calc(80px + var(--safe-t));left:50%;transform:translateX(-50%);
  background:var(--toast-bg);border:1px solid var(--border2);border-radius:20px;
  padding:10px 20px;font-size:13px;font-weight:500;color:var(--text);
  box-shadow:0 8px 32px rgba(0,0,0,.15);
  z-index:200;opacity:0;transition:opacity .2s;pointer-events:none;white-space:nowrap;
}
#toast.show{opacity:1}

/* ── Scrollbar ───────────────────────────────────────────────── */
::-webkit-scrollbar{width:3px;height:3px}
::-webkit-scrollbar-thumb{background:var(--scroll-thumb);border-radius:2px}

/* ── View toggle ─────────────────────────────────────────────── */
.view-toggle{display:flex;gap:0;margin:0 16px 2px;background:var(--vt-track);border-radius:12px;padding:3px}
.vt-btn{flex:1;padding:7px;border:none;border-radius:9px;background:none;
  color:var(--sub);font-size:12px;font-weight:600;cursor:pointer;transition:all .15s;font-family:inherit}
.vt-btn.on{background:var(--vt-active);color:var(--text);box-shadow:0 1px 4px rgba(0,0,0,.1)}

/* ── HOT contracts list ──────────────────────────────────────── */
#hot-feed{padding:4px 0 8px}
.hot-card{
  margin:6px 12px;background:var(--card-bg);border:1px solid var(--border);
  border-radius:16px;padding:13px 16px;
  display:flex;align-items:center;gap:12px;
  box-shadow:0 1px 8px rgba(0,0,0,.06);
  animation:cardIn .25s cubic-bezier(.34,1.56,.64,1) both;
}
.hot-rank{font-size:18px;font-weight:800;color:var(--sub);width:24px;flex-shrink:0;text-align:center}
.hot-rank.t1{color:#b06000}.hot-rank.t2{color:var(--sub)}.hot-rank.t3{color:#c93400}
.hot-info{flex:1;min-width:0}
.hot-sym{font-size:16px;font-weight:800;color:var(--text)}
.hot-sym .call{color:#248a3d}.hot-sym .put{color:var(--red)}
.hot-meta{font-size:11px;color:var(--sub);margin-top:2px}
.hot-right{text-align:right;flex-shrink:0}
.hot-voi{font-size:18px;font-weight:800}
.hot-voi.fire{color:var(--red)}.hot-voi.hot{color:#c93400}.hot-voi.warm{color:#b06000}
.hot-flow{font-size:11px;color:var(--sub);margin-top:2px}

/* ── DTE type selector (FIND tab) ────────────────────────────── */
.dte-type-row{display:flex;gap:8px;margin-top:12px}
.dte-type-btn{
  flex:1;padding:10px 6px;border:1.5px solid var(--border);border-radius:12px;
  background:var(--chip-bg);color:var(--sub);font-size:12px;font-weight:700;
  cursor:pointer;transition:all .18s cubic-bezier(.34,1.56,.64,1);text-align:center;
  box-shadow:0 1px 4px rgba(0,0,0,.05);
}
.dte-type-btn:active{transform:scale(.96)}
.dte-type-btn.on{background:rgba(0,122,255,.08);border-color:rgba(0,122,255,.3);color:var(--blue)}
.dte-type-label{font-size:10px;font-weight:400;color:var(--sub);display:block;margin-top:2px}
.dte-type-btn.on .dte-type-label{color:rgba(0,122,255,.7)}

/* ── GUIDE tab ───────────────────────────────────────────────── */
.guide{padding:16px 12px 32px}
.guide-section{margin-bottom:8px;border-radius:16px;overflow:hidden;
  background:var(--card-bg);border:1px solid var(--border);
  box-shadow:0 1px 8px rgba(0,0,0,.06)}
.guide-hdr{padding:14px 16px;display:flex;justify-content:space-between;align-items:center;
  cursor:pointer;user-select:none}
.guide-hdr-title{font-size:14px;font-weight:700;color:var(--text)}
.guide-hdr-icon{font-size:16px;color:var(--sub);transition:transform .2s}
.guide-body{padding:0 16px;max-height:0;overflow:hidden;transition:max-height .3s ease,padding .3s ease}
.guide-body.open{max-height:800px;padding:0 16px 16px}
.guide-item{padding:10px 0;border-bottom:1px solid var(--border)}
.guide-item:last-child{border-bottom:none}
.guide-term{font-size:13px;font-weight:700;margin-bottom:4px;color:var(--text)}
.guide-def{font-size:12px;color:var(--sub);line-height:1.55}
.guide-badge{display:inline-block;font-size:9px;font-weight:800;padding:2px 8px;
  border-radius:6px;margin-right:6px;vertical-align:middle;letter-spacing:.5px}
.gb-gold{background:rgba(255,159,10,.12);color:#b06000;border:1px solid rgba(255,159,10,.25)}
.gb-purple{background:rgba(175,82,222,.1);color:#7b2da0;border:1px solid rgba(175,82,222,.2)}
.gb-blue{background:rgba(0,122,255,.1);color:#0055cc;border:1px solid rgba(0,122,255,.2)}
.gb-amber{background:rgba(255,159,10,.1);color:#c93400;border:1px solid rgba(255,159,10,.2)}
.gb-sub{background:var(--sub-badge);color:var(--sub);border:1px solid var(--border)}
.score-pill{display:inline-block;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:700;margin-right:4px}
.sp-hi{background:rgba(255,59,48,.1);color:var(--red)}.sp-md{background:rgba(255,159,10,.1);color:#c93400}
.sp-lo{background:rgba(0,0,0,.05);color:var(--sub)}

/* ── Hit counter ─────────────────────────────────────────────── */
.hits{
  display:inline-flex;align-items:center;justify-content:center;
  background:rgba(175,82,222,.1);color:#7b2da0;border:1px solid rgba(175,82,222,.2);
  border-radius:10px;padding:2px 8px;font-size:10px;font-weight:700;margin-left:4px;
}

/* ── Theme toggle ─────────────────────────────────────────────── */
.theme-btn{
  background:none;border:none;cursor:pointer;font-size:17px;
  color:var(--sub);padding:0;width:36px;height:36px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;
  transition:background .15s;flex-shrink:0;
}
.theme-btn:active{background:var(--border)}

/* ── Sector stocks ────────────────────────────────────────────── */
.sec-card{cursor:pointer}
.sec-tickers{
  display:none;flex-wrap:wrap;gap:6px;padding:10px 16px 14px;
  border-top:1px solid var(--border);
}
.sec-card.open .sec-tickers{display:flex}
.sec-tick{
  font-size:11px;font-weight:700;padding:4px 9px;border-radius:8px;
  background:var(--bg3);color:var(--sub);border:1px solid var(--border);
  letter-spacing:.3px;
}
.sec-tick.call{background:rgba(52,199,89,.08);color:#248a3d;border-color:rgba(52,199,89,.2)}
.sec-tick.put{background:rgba(255,59,48,.08);color:var(--red);border-color:rgba(255,59,48,.2)}
.sec-expand{font-size:11px;color:var(--sub);margin-left:auto;flex-shrink:0;transition:transform .2s}
.sec-card.open .sec-expand{transform:rotate(90deg)}
</style>
</head>
<body>
<div id="app">

<!-- Topbar -->
<div id="topbar">
  <div class="tb-row1">
    <div class="app-name"><span>Scanner</span></div>
    <button class="theme-btn" id="theme-btn" onclick="toggleTheme()" aria-label="Toggle theme">☾</button>
    <div class="vix-chip" id="vix-chip">VIX —</div>
  </div>
  <div class="bias-row">
    <span class="bias-c" id="bc">CALLS —</span>
    <span class="bias-sep">·</span>
    <span class="bias-p" id="bp">PUTS —</span>
    <span class="bias-sep">·</span>
    <span class="bias-dir neut" id="bd">—</span>
  </div>
</div>

<!-- Content -->
<div id="content">

  <!-- FLOW -->
  <div class="tab-pane active" id="tab-flow">
    <div class="filter-bar">
      <button class="chip on" id="c-dte"   onclick="tDte()">0DTE</button>
      <button class="chip"    id="c-whale" onclick="tWhale()">Whale+</button>
      <button class="chip"    id="c-scope" onclick="tScope()">QUICK</button>
      <button class="scan-btn" id="scan-btn" onclick="doScan()">SCAN ▶</button>
    </div>
    <div class="prog-wrap" id="pw" style="display:none"><div class="prog-bar" id="pb"></div></div>
    <div class="prog-lbl"  id="pl" style="display:none"></div>
    <!-- SIGNALS / HOT toggle -->
    <div class="view-toggle" id="view-toggle" style="display:none">
      <button class="vt-btn on" id="vt-sig" onclick="setView('signals')">SIGNALS</button>
      <button class="vt-btn"    id="vt-hot" onclick="setView('hot')">HOT CONTRACTS</button>
    </div>
    <div id="flow-feed">
      <div class="empty-st">
        <div class="icon">⚡</div>
        <h3>Ready to scan</h3>
        Hit SCAN to pull live options flow — Golden Sweeps, Whales, Stacked prints, 0DTE activity.
      </div>
    </div>
    <div id="hot-feed" style="display:none"></div>
  </div>

  <!-- SECTORS -->
  <div class="tab-pane" id="tab-scan">
    <div class="sec-head">Market Sectors</div>
    <div id="sec-feed">
      <button class="sec-load-btn" onclick="loadSectors()">Load Sector Map</button>
    </div>
  </div>

  <!-- FIND -->
  <div class="tab-pane" id="tab-find">
    <div class="finder">
      <input class="find-input" id="ft" placeholder="SPX" maxlength="6"
             autocorrect="off" autocapitalize="characters" spellcheck="false"
             autocomplete="off" inputmode="text"
             oninput="this.value=this.value.toUpperCase()"
             onkeydown="if(event.key==='Enter')doFind()">
      <!-- DTE type -->
      <div class="dte-type-row">
        <button class="dte-type-btn on" id="dt-weekly" onclick="setDteType('weekly')">
          WEEKLY<span class="dte-type-label">≤7 days</span>
        </button>
        <button class="dte-type-btn" id="dt-0dte" onclick="setDteType('0dte')">
          0DTE<span class="dte-type-label">Today only</span>
        </button>
        <button class="dte-type-btn" id="dt-swing" onclick="setDteType('swing')">
          SWING<span class="dte-type-label">8–60 days</span>
        </button>
      </div>
      <!-- Direction -->
      <div class="dir-row">
        <button class="dir-btn up on"  id="d-up"  onclick="setDir('up')">▲ CALLS</button>
        <button class="dir-btn dn"     id="d-dn"  onclick="setDir('down')">▼ PUTS</button>
      </div>
      <button class="find-go" id="find-btn" onclick="doFind()">Find Top 3 Contracts</button>
      <div id="find-result"></div>
    </div>
  </div>

  <!-- GUIDE -->
  <div class="tab-pane" id="tab-guide">
    <div class="guide">

      <div class="guide-section">
        <div class="guide-hdr" onclick="toggleGuide(this)">
          <span class="guide-hdr-title">Signal Badges</span>
          <span class="guide-hdr-icon">›</span>
        </div>
        <div class="guide-body open">
          <div class="guide-item">
            <div class="guide-term"><span class="guide-badge gb-gold">GOLDEN SWEEP</span></div>
            <div class="guide-def">Large aggressive sweep (at ask, big premium) stacked across multiple strikes. The strongest bullish/bearish signal — institutions rarely golden-sweep by accident. Treat as high-conviction directional bet.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term"><span class="guide-badge gb-purple">WHALE</span></div>
            <div class="guide-def">Single massive block or very high whale score (70+). One player putting serious money on one side. Not necessarily sweeping — could be a hedge or a big directional bet.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term"><span class="guide-badge gb-blue">STACKED</span></div>
            <div class="guide-def">Multiple large prints at the same strike. When smart money layers the same contract several times, it signals conviction — they're building a position, not a one-off trade.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term"><span class="guide-badge gb-amber">SWEEP</span></div>
            <div class="guide-def">Order split across multiple exchanges to fill fast at the ask. Sweeping = urgency. The buyer didn't care about price discovery — they needed exposure NOW. Bullish for calls, bearish for puts.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term"><span class="guide-badge gb-sub">BLOCK</span></div>
            <div class="guide-def">Single large print, often negotiated off-exchange. Blocks can be hedges (not directional) — less conviction signal than a sweep. Weight accordingly.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term"><span class="guide-badge gb-sub">FLOW</span></div>
            <div class="guide-def">Normal elevated options activity. Not a single huge print — just unusually high volume relative to open interest on one side. Still worth watching for direction confirmation.</div>
          </div>
        </div>
      </div>

      <div class="guide-section">
        <div class="guide-hdr" onclick="toggleGuide(this)">
          <span class="guide-hdr-title">Key Metrics</span>
          <span class="guide-hdr-icon">›</span>
        </div>
        <div class="guide-body">
          <div class="guide-item">
            <div class="guide-term">Vol ÷ OI (Volume ÷ Open Interest)</div>
            <div class="guide-def">Volume = contracts traded <b>today</b>. Open Interest = total contracts <b>outstanding from all previous days</b>. Dividing them tells you: is today's activity unusual relative to what already exists?<br><br>x10 means today's volume is 10× ALL existing open positions. Someone is opening a position bigger than the entire existing float — that's extreme urgency and conviction. <span class="score-pill sp-hi">10x+</span> explosive. <span class="score-pill sp-md">3–10x</span> elevated. <span class="score-pill sp-lo">&lt;3x</span> normal.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">Delta</div>
            <div class="guide-def">How much the contract moves per $1 move in the stock. <b style="color:var(--teal)">0.25–0.40 is the target range</b> — cheap enough to not blow the account, still moves meaningfully. Below 0.20 = lottery ticket. Above 0.50 = expensive, needs a big move to profit.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">IV (Implied Volatility)</div>
            <div class="guide-def">The market's expected move baked into the option price. High IV = expensive premium. When IV is elevated, buying options is risky — you're overpaying for the move. Best to buy before IV spikes.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">1σ ROI</div>
            <div class="guide-def">Expected ROI if the stock makes a 1 standard-deviation move (68% probability) by expiry. Calculated as: (intrinsic value at target − premium paid) ÷ premium. Positive = contract profits on a normal move. This is the #1 ranking factor.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">P/C Ratio</div>
            <div class="guide-def">Put flow divided by call flow. Above 1.0 = more bearish money. Below 1.0 = more bullish money. Extreme readings (0.3 or 3.0+) signal strong directional conviction.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">At Ask / At Bid / Mixed</div>
            <div class="guide-def">Where the trade printed. <b style="color:var(--green)">At Ask</b> = buyer was aggressive, paid up — bullish signal. <b style="color:var(--red)">At Bid</b> = seller was aggressive — bearish signal. Mixed = unclear, could be a spread.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">Score</div>
            <div class="guide-def">Composite whale score (0–100). Weighted by premium size, sweep aggression, Vol÷OI, and tier. <span class="score-pill sp-hi">70+</span> = Whale. <span class="score-pill sp-md">50–69</span> = Institutional. <span class="score-pill sp-lo">&lt;50</span> = Retail/normal.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">IV Skew</div>
            <div class="guide-def">Call IV minus put IV for near-expiry options. Positive = calls are more expensive than puts (market pricing in upside). Negative = puts are bid up — hedging or bearish positioning.</div>
          </div>
        </div>
      </div>

      <div class="guide-section">
        <div class="guide-hdr" onclick="toggleGuide(this)">
          <span class="guide-hdr-title">Contract Types (FIND tab)</span>
          <span class="guide-hdr-icon">›</span>
        </div>
        <div class="guide-body">
          <div class="guide-item">
            <div class="guide-term">0DTE — Expires Today</div>
            <div class="guide-def">Zero days to expiration. Max gamma exposure — contracts move fast and expire worthless same day. High risk, high reward. Only trade during market hours with tight stops. Your primary instrument for daily $50 target.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">Weekly — ≤7 Days</div>
            <div class="guide-def">Standard weekly options. More time premium than 0DTE, less theta burn per hour. Good for setups that need a day or two to play out. Scanner's default.</div>
          </div>
          <div class="guide-item">
            <div class="guide-term">Swing — 8–60 Days</div>
            <div class="guide-def">Multi-week trades. Lower gamma, slower moves, but less theta erosion per day. Better for trend plays or when you want time for a setup to develop without the daily clock pressure.</div>
          </div>
        </div>
      </div>

      <div class="guide-section">
        <div class="guide-hdr" onclick="toggleGuide(this)">
          <span class="guide-hdr-title">How Contracts Are Scored</span>
          <span class="guide-hdr-icon">›</span>
        </div>
        <div class="guide-body">
          <div class="guide-item">
            <div class="guide-term">Scoring breakdown</div>
            <div class="guide-def">Each contract gets a composite score out of 100:<br><br>
              <b style="color:var(--teal)">1σ ROI — 35pts</b>: Expected profit if stock makes a 1-sigma move<br>
              <b style="color:var(--green)">Delta — 25pts</b>: How close to 0.25–0.40 target range (VIX-adjusted)<br>
              <b style="color:var(--amber)">Liquidity — 22pts</b>: Open interest × volume (can you exit?)<br>
              <b style="color:var(--sub)">Spread — 10pts</b>: Tight bid/ask = lower slippage<br>
              <b style="color:var(--purple)">Vol÷OI — 8pts</b>: Conviction signal<br><br>
              Best contract = highest total. BEST FIT is #1, ALT 2 and ALT 3 are next best alternatives.
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>

</div>

<!-- Tab bar -->
<div id="tabbar">
  <button class="tab-btn active" onclick="showTab('flow',this)">
    <svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>FLOW
  </button>
  <button class="tab-btn" onclick="showTab('scan',this)">
    <svg viewBox="0 0 24 24"><rect x="2" y="3" width="6" height="6" rx="1.5"/><rect x="9" y="3" width="6" height="6" rx="1.5"/><rect x="16" y="3" width="6" height="6" rx="1.5"/><rect x="2" y="10" width="6" height="6" rx="1.5"/><rect x="9" y="10" width="6" height="6" rx="1.5"/><rect x="16" y="10" width="6" height="6" rx="1.5"/></svg>SECTORS
  </button>
  <button class="tab-btn" onclick="showTab('find',this)">
    <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>FIND
  </button>
  <button class="tab-btn" onclick="showTab('guide',this)">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>GUIDE
  </button>
</div>
</div>
<div id="toast"></div>

<script>

// ── Theme init (before first paint) ───────────────────────────────────────
(function(){
  if(localStorage.getItem('theme')==='dark'){
    document.documentElement.classList.add('dark');
  }
})();

// ── Sector → tickers mapping ───────────────────────────────────────────────
const SECTOR_TICKERS = {
  'Tech':       ['NVDA','AMD','AAPL','MSFT','AMZN','TSLA','QQQ','PLTR','COIN','MSTR','TQQQ','SOXL'],
  'Financials': ['GS','JPM','BAC','V','MA','SOFI','HOOD','COIN','AFRM'],
  'CommSvcs':   ['META','GOOGL','SNAP','GME','UBER','LYFT','NFLX','PINS'],
  'Energy':     ['XOM','CVX','USO'],
  'Health':     ['HIMS','MRNA','PFE','ONDS'],
  'Materials':  ['GLD','SLV','CPER'],
  'Industrials':['F','GM'],
  'Index':      ['SPX','SPY','IWM','DIA','TQQQ','SQQQ','UPRO'],
  'Vol':        ['VXX','UVXY','SVXY'],
};

// ── State ──────────────────────────────────────────────────────────────────
const PIN = '__SCANNER_PIN__';
const _pa = p => PIN ? (p.includes('?')?p+'&pin='+PIN:p+'?pin='+PIN) : p;
const S = {
  dte:'0dte', whale:false, full:false, dir:'up', dteType:'weekly',
  scanning:false, callFlow:0, putFlow:0, signals:[], hotContracts:[],
  view:'signals', qt:[], ft:[],
};
fetch(_pa('/api/universe')).then(r=>r.json()).then(d=>{S.qt=d.quick;S.ft=d.full});
fetch(_pa('/api/vix')).then(r=>r.json()).then(renderVix);

// ── Tab ────────────────────────────────────────────────────────────────────
function showTab(n,btn){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  const pane = document.getElementById('tab-'+n);
  pane.classList.add('active');
  btn.classList.add('active');
}

// ── Guide accordion ────────────────────────────────────────────────────────
function toggleGuide(hdr){
  const body=hdr.nextElementSibling;
  const icon=hdr.querySelector('.guide-hdr-icon');
  const isOpen=body.classList.contains('open');
  body.classList.toggle('open',!isOpen);
  icon.style.transform=isOpen?'':'rotate(90deg)';
}

// ── Chips ──────────────────────────────────────────────────────────────────
const dteOpts=['0dte','7dte','all'];
const dteLabels={'0dte':'0DTE','7dte':'≤7 DTE','all':'ALL DTE'};
function tDte(){
  S.dte=dteOpts[(dteOpts.indexOf(S.dte)+1)%3];
  const el=document.getElementById('c-dte');
  el.textContent=dteLabels[S.dte];
  el.className='chip'+(S.dte!=='all'?' on':'');
}
function tWhale(){S.whale=!S.whale;document.getElementById('c-whale').className='chip'+(S.whale?' on':'')}
function tScope(){
  S.full=!S.full;
  const el=document.getElementById('c-scope');
  el.textContent=S.full?`FULL (${S.ft.length||112})`:'QUICK';
  el.className='chip'+(S.full?' on':'');
}
function setDir(d){
  S.dir=d;
  document.getElementById('d-up').className='dir-btn up'+(d==='up'?' on':'');
  document.getElementById('d-dn').className='dir-btn dn'+(d==='down'?' on':'');
}
function setDteType(t){
  S.dteType=t;
  ['weekly','0dte','swing'].forEach(k=>{
    document.getElementById('dt-'+k).className='dte-type-btn'+(k===t?' on':'');
  });
}

// ── View toggle (SIGNALS / HOT) ────────────────────────────────────────────
function setView(v){
  S.view=v;
  document.getElementById('vt-sig').className='vt-btn'+(v==='signals'?' on':'');
  document.getElementById('vt-hot').className='vt-btn'+(v==='hot'?' on':'');
  const ff=document.getElementById('flow-feed');
  const hf=document.getElementById('hot-feed');
  ff.style.display=v==='signals'?'':'none';
  hf.style.display=v==='hot'?'':'none';
  if(v==='hot') renderHot();
}

// ── VIX ────────────────────────────────────────────────────────────────────
function renderVix(d){
  const el=document.getElementById('vix-chip');
  if(d.vix<=0){el.textContent='VIX —';return}
  el.textContent=`VIX ${d.vix.toFixed(1)} · ${d.regime}`;
  el.className='vix-chip '+(d.vix>=30?'fear':d.vix>=24?'elevated':d.vix<16?'calm':'');
}

// ── Scan ───────────────────────────────────────────────────────────────────
function doScan(){
  if(S.scanning)return;
  S.scanning=true;S.callFlow=0;S.putFlow=0;S.signals=[];S.hotContracts=[];
  setView('signals');
  document.getElementById('view-toggle').style.display='none';
  const tickers=(S.full?S.ft:S.qt).join(',')
    ||'SPX,SPY,QQQ,IWM,NVDA,AMD,AAPL,MSFT,META,AMZN,TSLA,COIN,PLTR';
  const n=tickers.split(',').length;
  const btn=document.getElementById('scan-btn');
  btn.textContent=`SCANNING ${n}…`;btn.className='scan-btn loading';
  document.getElementById('flow-feed').innerHTML='';
  document.getElementById('hot-feed').innerHTML='';
  document.getElementById('pw').style.display='block';
  document.getElementById('pl').style.display='block';
  const url=_pa(`/api/flow/stream?tickers=${tickers}&dte_filter=${S.dte}&min_score=${S.whale?60:0}`);
  const es=new EventSource(url);
  es.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.__ping__)return;
    if(m.__progress__){
      document.getElementById('pb').style.width=(m.i/m.n*100)+'%';
      document.getElementById('pl').textContent=`${m.ticker} · ${m.i} of ${m.n}`;
      return;
    }
    if(m.__done__||m.__error__){es.close();endScan(m.__error__);return}
    if(m.__signal__){
      const s=m.data;S.signals.push(s);
      S.callFlow+=s.call_flow||0;S.putFlow+=s.put_flow||0;
      // collect hot contracts from all signals
      (s.top3||[]).forEach(c=>{ S.hotContracts.push({...c, ticker:s.ticker, badge:s.badge, cls:s.cls}); });
      renderCard(s);updateBias();
    }
  };
  es.onerror=()=>{es.close();endScan('Connection lost')};
}
function endScan(err){
  S.scanning=false;
  const btn=document.getElementById('scan-btn');
  btn.textContent=S.full?'FULL ▶':'SCAN ▶';btn.className='scan-btn';
  document.getElementById('pw').style.display='none';
  document.getElementById('pl').style.display='none';
  document.getElementById('pb').style.width='0%';
  if(err){toast('Error: '+err);return}
  if(!S.signals.length){
    document.getElementById('flow-feed').innerHTML=
      '<div class="empty-st"><div class="icon">🔍</div><h3>No signals found</h3>Try removing filters or switching to FULL scan.</div>';
  }else{
    toast(`${S.signals.length} signal${S.signals.length>1?'s':''} found`);
    if(S.hotContracts.length) document.getElementById('view-toggle').style.display='flex';
  }
}
function updateBias(){
  const cf=S.callFlow,pf=S.putFlow;
  document.getElementById('bc').textContent='CALLS '+fmt(cf);
  document.getElementById('bp').textContent='PUTS '+fmt(pf);
  const bd=document.getElementById('bd');
  if(cf>pf*1.2){bd.textContent='▲ BULL';bd.className='bias-dir bull'}
  else if(pf>cf*1.2){bd.textContent='▼ BEAR';bd.className='bias-dir bear'}
  else{bd.textContent='~ EVEN';bd.className='bias-dir neut'}
}

// ── Render HOT contracts ───────────────────────────────────────────────────
function renderHot(){
  const feed=document.getElementById('hot-feed');
  if(!S.hotContracts.length){
    feed.innerHTML='<div class="empty-st"><div class="icon">🔥</div><h3>No hot contracts yet</h3>Run a scan first.</div>';
    return;
  }
  // Dedupe by ticker+strike+type+exp
  const seen=new Set();
  const deduped=S.hotContracts.filter(c=>{
    const k=`${c.ticker}-${c.strike}-${c.type}-${c.exp}`;
    if(seen.has(k))return false;seen.add(k);return true;
  });
  // Split into calls and puts, each sorted by vol÷OI desc
  const calls=deduped.filter(c=>c.type==='call').sort((a,b)=>(b.vol_oi||0)-(a.vol_oi||0)).slice(0,12);
  const puts=deduped.filter(c=>c.type==='put').sort((a,b)=>(b.vol_oi||0)-(a.vol_oi||0)).slice(0,12);

  feed.innerHTML='';

  function buildSection(label,color,list){
    if(!list.length)return;
    const hdr=document.createElement('div');
    hdr.style.cssText=`padding:6px 12px 4px;font-size:11px;font-weight:700;letter-spacing:1px;color:${color};text-transform:uppercase`;
    hdr.textContent=label;
    feed.appendChild(hdr);
    list.forEach((c,i)=>{
      const rankCls=i===0?'t1':i===1?'t2':i===2?'t3':'';
      const voiN=c.vol_oi||0;
      const voiCls=voiN>=10?'fire':voiN>=5?'hot':'warm';
      const dLabel=c.dte===0?'0DTE':c.dte>=0?`${c.dte}DTE`:'—';
      const priceStr=c.mid>0?`$${c.mid.toFixed(2)}`:'—';
      const el=document.createElement('div');
      el.className='hot-card';
      el.innerHTML=`
        <div class="hot-rank ${rankCls}">${i+1}</div>
        <div class="hot-info">
          <div class="hot-sym"><span class="${c.type}">${c.ticker}</span> <span style="color:var(--sub);font-size:14px;font-weight:500">$${c.strike.toFixed(0)} ${c.type==='call'?'C':'P'}</span></div>
          <div class="hot-meta">${dLabel} · ${c.exp} · ${priceStr}</div>
        </div>
        <div class="hot-right">
          <div class="hot-voi ${voiCls}">x${voiN.toFixed(1)}</div>
          <div class="hot-flow">${c.flow||'—'} flow</div>
        </div>`;
      feed.appendChild(el);
    });
  }

  buildSection('▲ Hot Calls','var(--green)',calls);
  buildSection('▼ Hot Puts','var(--red)',puts);
}

// ── Render signal card ─────────────────────────────────────────────────────
function fmt(v){if(v>=1e6)return'$'+(v/1e6).toFixed(1)+'M';if(v>=1e3)return'$'+(v/1e3).toFixed(0)+'K';return'$'+v.toFixed(0)}
function badgeCls(b){const m={GOLDEN_SWEEP:'golden',WHALE:'whale',STACKED:'stacked',SWEEP:'sweep',BLOCK:'block'};return m[b.replace(' ','_')]||'flow'}
function voiCls(v){return v>=10?'hot':v>=3?'warm':'cool'}
function dteCls(d){return d===0?'zero':d<=3?'near':'far'}

function renderCard(s){
  const feed=document.getElementById('flow-feed');
  const card=document.createElement('div');
  card.className='flow-card '+s.cls;

  const hitsHtml=s.hits>1?`<span class="hits">×${s.hits}</span>`:'';
  const biasLabel=s.bias==='call'?'CALL BIAS':'PUT BIAS';
  const biasColor=s.bias==='call'?'var(--green)':'var(--red)';

  const chips=(s.top3||[]).map((c,i)=>{
    const cls=i===0?(c.golden?'golden-c':'top1'):'';
    const dLabel=c.dte===0?'0DTE':c.dte>=0?`${c.dte}DTE`:'—';
    const priceStr=c.mid>0?`$${c.mid.toFixed(2)}`:'—';
    const voiStr=c.vol_oi>0?`x${c.vol_oi.toFixed(1)}`:'—';
    return `<div class="contract-chip ${cls}">
      <div class="cc-strike ${c.type}">$${c.strike.toFixed(0)}<span style="font-size:12px;font-weight:500;color:var(--sub)"> ${c.type==='call'?'C':'P'}</span></div>
      <div class="cc-meta"><span class="dte-dot ${dteCls(c.dte)}"></span>${dLabel} · ${c.exp}</div>
      <div class="cc-price">${priceStr}</div>
      <div class="cc-voi ${voiCls(c.vol_oi)}">${c.vol_oi>0?voiStr:'—'}</div>
    </div>`;
  }).join('');

  const sideLabel=s.side==='ask'?'At Ask ↑':s.side==='bid'?'At Bid ↓':'Mixed';

  card.innerHTML=`
    <div class="card-head" onclick="toggleDetail(this)">
      <span class="badge ${badgeCls(s.badge)}">${s.badge}</span>
      <div class="card-title">
        <div class="card-ticker">${s.ticker}${hitsHtml}</div>
        <div class="card-exp" style="color:${biasColor}">${s.ts} · ${biasLabel}</div>
      </div>
      <div class="card-premium">
        <div class="amount ${s.bias}">${s.total_fmt}</div>
        <div class="label" style="color:${biasColor}">${s.bias==='call'?'↑ CALLS':'↓ PUTS'}</div>
      </div>
    </div>
    <div class="card-stats">
      <div class="stat"><label>Score</label><div class="val">${s.score}</div></div>
      <div class="stat"><label>Vol÷OI</label><div class="val ${voiCls(s.vol_oi)}">${s.vol_oi>0?'x'+s.vol_oi.toFixed(1):'—'}</div></div>
      <div class="stat"><label>Side</label><div class="val ${s.side}">${sideLabel}</div></div>
      <div class="stat"><label>P/C</label><div class="val">${s.pc_ratio.toFixed(2)}</div></div>
    </div>
    ${chips?`<div class="contracts-row">${chips}</div>`:''}
    <div class="card-detail">
      <div class="detail-grid">
        <div class="dg-item"><label>Calls</label><span style="color:var(--green)">${s.call_fmt}</span></div>
        <div class="dg-item"><label>Puts</label><span style="color:var(--red)">${s.put_fmt}</span></div>
        <div class="dg-item"><label>IV Skew</label><span>${s.iv_skew?(s.iv_skew>0?'+':'')+(s.iv_skew*100).toFixed(2)+'%':'—'}</span></div>
        <div class="dg-item"><label>Stacked</label><span>${s.stacked?'Yes ✓':'No'}</span></div>
        <div class="dg-item"><label>Golden</label><span style="color:${s.golden?'var(--gold)':'var(--sub)'}">${s.golden?'Yes ✓':'No'}</span></div>
        <div class="dg-item"><label>Tier</label><span>${s.tier}</span></div>
      </div>
      <div class="dte-seg-row">
        <div class="dte-seg"><label>0DTE</label><span style="color:var(--teal)">${s.dte0||'$0'}</span></div>
        <div class="dte-seg"><label>1–7 DTE</label><span style="color:var(--amber)">${s.dte1_7||'$0'}</span></div>
        <div class="dte-seg"><label>8+ DTE</label><span style="color:var(--sub)">${s.dte8p||'$0'}</span></div>
      </div>
    </div>
  `;
  feed.appendChild(card);
}
function toggleDetail(head){
  const det=head.closest('.flow-card').querySelector('.card-detail');
  const isOpen=det.classList.contains('open');
  det.classList.toggle('open');
  if(MA&&!isOpen) MA(det,{opacity:[0,1],y:[-4,0]},{duration:.18,easing:'ease-out'});
}

// ── Sectors ────────────────────────────────────────────────────────────────
async function loadSectors(){
  const feed=document.getElementById('sec-feed');
  feed.innerHTML='<div style="margin:16px"><div class="shimmer" style="height:64px;margin-bottom:8px"></div><div class="shimmer" style="height:64px;margin-bottom:8px"></div><div class="shimmer" style="height:64px"></div></div>';
  try{
    const r=await fetch(_pa('/api/sectors'));
    if(!r.ok){
      let msg='Data unavailable';
      try{const e=await r.json();msg=e.detail||msg;}catch{}
      feed.innerHTML=`<div class="empty-st"><div class="icon">📡</div><h3>Couldn't load sectors</h3>${msg}<br><br><button class="sec-load-btn" onclick="loadSectors()" style="margin:16px auto;max-width:200px">Try Again</button></div>`;
      return;
    }
    const d=await r.json();
    if(!d.length){
      feed.innerHTML='<div class="empty-st"><div class="icon">📡</div><h3>No sector data</h3>Market may be closed.</div>';
      return;
    }
    const max=Math.max(...d.map(s=>Math.abs(s.change)),.01);
    // build a signal index from last scan (ticker → bias) for color-coding
    const sigMap={};
    S.signals.forEach(sig=>{ sigMap[sig.ticker]=sig.bias; });
    feed.innerHTML='';
    const hdr=document.createElement('div');hdr.className='sec-head';
    hdr.textContent=`Sectors · ${new Date().toLocaleTimeString()}`;feed.appendChild(hdr);
    d.forEach(s=>{
      const up=s.change>=0;
      const pct=Math.abs(s.change/max*100).toFixed(0);
      const tickers=(SECTOR_TICKERS[s.name]||[]);
      const tickHtml=tickers.map(t=>{
        const cls=sigMap[t]||'';
        return `<span class="sec-tick ${cls}">${t}</span>`;
      }).join('');
      const el=document.createElement('div');
      el.className='sec-card';
      el.innerHTML=`
        <div style="display:flex;align-items:center;gap:12px;width:100%" onclick="this.closest('.sec-card').classList.toggle('open')">
          <div class="sec-name">${s.name}<div style="font-size:10px;color:var(--sub);font-weight:400">${s.etf}</div></div>
          <div class="sec-bar-wrap"><div class="sec-bar ${up?'up':'dn'}" style="width:${pct}%"></div></div>
          <div class="sec-chg ${up?'up':'dn'}">${up?'+':''}${s.change.toFixed(2)}%</div>
          <span class="sec-expand">›</span>
        </div>
        ${tickHtml?`<div class="sec-tickers">${tickHtml}</div>`:''}`;
      feed.appendChild(el);
    });
  }catch(e){
    feed.innerHTML='<div class="empty-st"><div class="icon">⚠️</div><h3>Failed to load</h3><button class="sec-load-btn" onclick="loadSectors()" style="margin:16px auto;max-width:200px">Try Again</button></div>';
  }
}

// ── Contract finder ────────────────────────────────────────────────────────
async function doFind(){
  const btn=document.getElementById('find-btn');
  if(btn.classList.contains('loading'))return;
  const ticker=document.getElementById('ft').value.trim().toUpperCase()||'SPY';
  btn.textContent='Finding…';btn.classList.add('loading');
  document.getElementById('find-result').innerHTML=
    '<div style="margin:4px 0"><div class="shimmer" style="height:140px;border-radius:20px;margin-bottom:8px"></div><div class="shimmer" style="height:120px;border-radius:20px;margin-bottom:8px"></div><div class="shimmer" style="height:120px;border-radius:20px"></div></div>';
  try{
    const r=await fetch(_pa(`/api/contract?ticker=${ticker}&direction=${S.dir}&dte_type=${S.dteType}`));
    if(!r.ok){const e=await r.json();document.getElementById('find-result').innerHTML=`<div class="empty-st" style="padding:24px">${e.error||'Not found'}</div>`;return}
    const contracts=await r.json();
    renderContracts(ticker,contracts);
  }catch(e){document.getElementById('find-result').innerHTML='<div class="empty-st" style="padding:24px">Network error</div>'}
  finally{btn.textContent='Find Top 3 Contracts';btn.classList.remove('loading')}
}

function renderContracts(ticker,cs){
  const res=document.getElementById('find-result');
  if(!Array.isArray(cs))cs=[cs];
  const isCall=S.dir==='up';
  const dteTypeLabel={'weekly':'Weekly','0dte':'0DTE','swing':'Swing'}[S.dteType]||'';
  const html=cs.map((c,i)=>{
    const clr=isCall?'call':'put';
    const expS=c.exp?c.exp.slice(5):'—';
    const midS=c.mid?'$'+c.mid.toFixed(2):'—';
    const bidAsk=(c.bid>0&&c.ask>0)?`$${c.bid.toFixed(2)} / $${c.ask.toFixed(2)}`:`last ${midS}`;
    const dltS=c.delta!=null?(c.delta>=0?'+':'')+c.delta.toFixed(3):'—';
    const ivS=c.iv?(c.iv*100).toFixed(1)+'%':'—';
    const voiN=c.oi>0?c.vol/c.oi:0;
    const voiStr=c.oi>0?voiN.toFixed(1)+'x':'—';
    const roiClr=c.roi>50?'green':c.roi>0?'teal':c.roi<0?'red':'';
    return `<div class="cont-card${i===0?' best':''}">
      <div class="cont-hero">
        <div>
          <div class="cont-sym"><span class="${clr}">${ticker}</span><span class="strike"> $${c.strike.toFixed(0)} ${isCall?'C':'P'}</span></div>
          <div style="font-size:12px;color:var(--sub);margin-top:4px">${expS} · ${c.dte===0?'<span style="color:var(--teal)">0DTE</span>':c.dte+'DTE'} · ${dteTypeLabel}</div>
        </div>
        <div>
          <div class="cont-badge ${i===0?'best':'alt'}">${i===0?'BEST FIT':'ALT '+(i+1)}</div>
          ${c.stale?'<div style="font-size:10px;color:var(--amber);margin-top:4px">⚠ STALE</div>':''}
        </div>
      </div>
      <div class="cont-grid">
        <div class="cg"><label>Mid</label><span class="${isCall?'green':'red'}">${midS}</span></div>
        <div class="cg"><label>Bid / Ask</label><span style="font-size:13px">${bidAsk}</span></div>
        <div class="cg"><label>Delta</label><span class="${isCall?'green':'red'}">${dltS}</span></div>
        <div class="cg"><label>IV</label><span class="teal">${ivS}</span></div>
        <div class="cg"><label>Vol ÷ OI</label><span class="${voiN>=10?'gold':voiN>=3?'amber':''}">${voiStr}</span></div>
        <div class="cg"><label>1σ ROI</label><span class="${roiClr}">${c.roi!=null?(c.roi>0?'+':'')+c.roi.toFixed(1)+'%':'—'}</span></div>
      </div>
      <div class="cont-note ${c.stale?'stale':''}">
        ${c.stale?'⚠ Stale quote — market closed.':'Ranked by 1σ ROI · delta · liquidity · spread'}
        ${i===0?` · Score: <b>${c.score}</b>`:''}
      </div>
    </div>`;
  }).join('');
  res.innerHTML=`<div class="cont-cards">${html}</div>`;
}

// ── Theme toggle ───────────────────────────────────────────────────────────
function toggleTheme(){
  const dark=document.documentElement.classList.toggle('dark');
  document.getElementById('theme-btn').textContent=dark?'☀':'☾';
  localStorage.setItem('theme',dark?'dark':'light');
  // update PWA theme-color meta
  document.querySelector('meta[name="theme-color"]').content=dark?'#000000':'#f5f5f7';
}
// Set correct icon on load
(function(){
  if(document.documentElement.classList.contains('dark'))
    document.getElementById('theme-btn').textContent='☀';
})();

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg){
  const el=document.getElementById('toast');el.textContent=msg;el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'),2500);
}
</script>
</body>
</html>"""

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, socket

    parser = argparse.ArgumentParser(description="Options Flow Scanner PWA")
    parser.add_argument("--pin", type=str, default="",
                        help="Protect with PIN (e.g. --pin abc123). Also via SCANNER_PIN env var.")
    args = parser.parse_args()

    if args.pin:
        os.environ["SCANNER_PIN"] = args.pin
        _PIN = args.pin

    # Real LAN IP via UDP trick (no data sent)
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        lan_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        try: lan_ip = socket.gethostbyname(socket.gethostname())
        except: lan_ip = "127.0.0.1"

    C = "\033[36m"; G = "\033[32m"; Y = "\033[33m"; R = "\033[0m"; B = "\033[1m"

    print(f"""
{B}{C}  ── Scanner PWA ─────────────────────────────────────{R}
{C}  Local:{R}   http://localhost:{PORT}
{G}  Phone:{R}   http://{lan_ip}:{PORT}  {Y}← same WiFi{R}""")

    if _PIN:
        print(f"{Y}  PIN:{R}     {_PIN}")
    else:
        print(f"  No PIN — set one: python3 scanner_app.py --pin abc123")

    print(f"""
{C}  iPhone:{R} Safari → http://{lan_ip}:{PORT} → Share → Add to Home Screen
""")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
