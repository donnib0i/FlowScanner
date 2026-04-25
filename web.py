#!/usr/bin/env python3
# web.py — FlowScanner Premium Web UI (FastAPI)
# Replaces scanner_web.py (Gradio) with a professional dark terminal-style dashboard.
# Run: uvicorn web:app --host 0.0.0.0 --port 7860

import asyncio
import contextlib
import io
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ── Scanner imports ────────────────────────────────────────────────────────────
from scanner import (
    scan_sectors,
    scan_tickers,
    apply_forward_directions,
    enrich_contracts,
    find_sector_laggards,
    apply_filter,
    apply_sort,
    get_best_contract,
    fetch_vix,
    fetch_dynamic_universe,
    get_contract_display,
    calc_iv_rank_proxy,
    FILTER_LABELS,
    SORT_LABELS,
    TICKER_SECTOR,
    SECTOR_ETFS,
    UNIVERSE,
    fmt_flow,
)

# ── App setup ──────────────────────────────────────────────────────────────────
app = FastAPI(title="FlowScanner", version="2.0")

TEMPLATES_DIR = Path(__file__).parent / "templates"

# ── Fast default universe ──────────────────────────────────────────────────────
FAST_UNIVERSE = [
    # Indices / broad market — deepest option chains
    "SPX", "SPY", "QQQ", "IWM", "DIA",
    # Mega-cap tech — highest OI, most liquid options
    "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA",
    # Semis
    "AMD", "AVGO", "MU", "ARM", "INTC", "SMCI",
    # High-beta / meme / crypto-adjacent — real options volume
    "COIN", "PLTR", "MSTR", "HOOD", "MARA",
    # Growth tech with active chains
    "NFLX", "CRWD", "PANW", "NET", "APP", "RDDT",
    # Financials — large OI, active options
    "GS", "JPM", "BAC", "MS",
    # Consumer / retail / misc high-OI names
    "AMGN", "LLY", "MRNA",
    # Energy — legit options volume
    "XOM", "CVX",
    # Mobility / EV with real US options chains
    "UBER",
    # Smaller active names (keep if chain is real, drop if thin)
    "GME", "AMC", "SOFI", "AFRM",
]


# ── Market status helpers ──────────────────────────────────────────────────────

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


def market_status_str() -> str:
    et = _et_now()
    if et.weekday() >= 5:
        return "WEEKEND"
    mins = et.hour * 60 + et.minute
    if mins < 9 * 60 + 30:
        return "PRE-MARKET"
    if mins <= 16 * 60:
        return "MARKET OPEN"
    return "AFTER HOURS"


# ── Grade calculator (plain, no ANSI) ─────────────────────────────────────────

def trade_grade_plain(setup_q: float, opt_score: int, has_contract: bool) -> str:
    score = setup_q * 50 + opt_score * 0.30 + (20 if has_contract else 0)
    if score >= 75:
        return "A"
    if score >= 55:
        return "B"
    if score >= 35:
        return "C"
    return "D"


# ── Signal badge builder (HTML-safe, no ANSI) ──────────────────────────────────

def build_signal_badges(r: Dict) -> List[Dict]:
    """Return list of {text, cls, tooltip} for HTML badge rendering."""
    badges = []
    combo  = r.get("signal_combo", "")
    regime = r.get("hv_regime", "normal")

    combo_map = {
        "HV_PURE":     ("HV★",        "badge-green-bright", "Pure HV volatile — Sharpe 4.94, best setup"),
        "HV+ID":       ("HV+ID",       "badge-green",        "HV + Inside Day — Sharpe 3.07"),
        "HV+GD":       ("HV+G↓",       "badge-green",        "HV + Gap Down fill — Sharpe 2.91"),
        "HV+GU_FADE":  ("HV+G↑FADE",   "badge-red-bright",   "HV + Gap Up — FADE direction (Sharpe 2.42)"),
        "HV+BK":       ("HV+BK",       "badge-cyan",         "HV + Breakout confirmation"),
        "BK":          ("BK",          "badge-cyan",         "Breakout with vol confirmation"),
        "ID":          ("ID",          "badge-purple",       "Inside Day — coiling, wait for range break"),
        "GD":          ("G↓",          "badge-cyan",         "Gap Down — fill bias (bullish)"),
        "GU":          ("G↑",          "badge-yellow",       "Gap Up — continuation lean"),
    }

    if combo in combo_map:
        text, cls, tip = combo_map[combo]
        if regime == "calm" and combo.startswith("HV"):
            cls = "badge-dim"
            tip += " — WARNING: HV in calm regime has no edge"
        badges.append({"text": text, "cls": cls, "tooltip": tip})
    else:
        if r.get("gap_flag") == "gap_up":
            badges.append({"text": "G↑", "cls": "badge-yellow", "tooltip": "Gap Up"})
        if r.get("gap_flag") == "gap_down":
            badges.append({"text": "G↓", "cls": "badge-cyan", "tooltip": "Gap Down — fill bias"})
        if r.get("inside_day"):
            badges.append({"text": "ID", "cls": "badge-purple", "tooltip": "Inside Day"})
        if r.get("high_vol"):
            hv_cls = "badge-green" if regime == "volatile" else ("badge-yellow" if regime == "normal" else "badge-dim")
            badges.append({"text": "HV", "cls": hv_cls, "tooltip": f"High Vol ({regime} regime)"})
        bk = r.get("breakout")
        if bk == "bull":
            badges.append({"text": "BK↑", "cls": "badge-cyan", "tooltip": "Bull Breakout"})
        elif bk == "bear":
            badges.append({"text": "BK↓", "cls": "badge-red", "tooltip": "Bear Breakout"})

    if r.get("high_vol") and regime == "calm":
        badges.append({"text": "⚠CALM", "cls": "badge-dim", "tooltip": "HV in calm regime — no edge"})

    nl = r.get("near_level")
    if nl and nl["strength"] >= 5:
        badges.append({"text": "★★LVL", "cls": "badge-red-bright", "tooltip": f"Strong level @ ${nl['price']:.2f}"})
    elif nl and nl["strength"] >= 3:
        badges.append({"text": "★LVL", "cls": "badge-red", "tooltip": f"Level @ ${nl['price']:.2f}"})

    if r.get("is_laggard"):
        lag_dir = r.get("lag_direction", "up")
        badges.append({
            "text":    f"LAG{r['lag_pct']:+.0f}%",
            "cls":     "badge-cyan" if lag_dir == "up" else "badge-yellow",
            "tooltip": f"Sector laggard — catch-up play ({lag_dir.upper()})",
        })

    rs = r.get("rs_vs_spy", 0.0)
    if rs >= 2.0:
        badges.append({"text": f"RS+{rs:.1f}", "cls": "badge-green", "tooltip": "Strong RS vs SPY"})
    elif rs <= -2.0:
        badges.append({"text": f"RS{rs:.1f}", "cls": "badge-yellow", "tooltip": "Weak RS vs SPY"})

    return badges


# ── Result serializer (JSON-safe) ──────────────────────────────────────────────

def serialize_result(r: Dict, market_open: bool) -> Dict:
    """Convert a scanner result dict to a JSON-safe dict for the web UI."""
    c = r.get("contract")
    contract_display = get_contract_display(c, market_open)

    grade = trade_grade_plain(r["setup_q"], r["opt_score"], bool(c))
    badges = build_signal_badges(r)

    iv_rank = r.get("iv_rank_data", {})
    ivr_score = iv_rank.get("ivr_score", 50)
    ivr_label = iv_rank.get("label", "NORMAL")

    em_pct = r.get("expected_move_pct", 0.0)

    direction = r.get("direction", "up")

    return {
        "ticker":       r["ticker"],
        "sector":       TICKER_SECTOR.get(r["ticker"], "Other"),
        "price":        round(r["price"], 2),
        "change_pct":   round(r["change_pct"], 2),
        "gap_pct":      round(r.get("gap_pct", 0), 2),
        "rel_vol":      round(r["rel_vol"], 2),
        "hv20":         round(r.get("hv20", 0), 4),
        "hv_regime":    r.get("hv_regime", "normal"),
        "rs_vs_spy":    round(r.get("rs_vs_spy", 0), 2),
        "opt_score":    r["opt_score"],
        "setup_q":      round(r["setup_q"], 3),
        "grade":        grade,
        "direction":    direction,
        "badges":       badges,
        "signal_combo": r.get("signal_combo", ""),
        "contract":     contract_display,
        "near_level":   r.get("near_level"),
        "is_laggard":   r.get("is_laggard", False),
        "lag_pct":      round(r.get("lag_pct", 0), 2),
        "ivr_score":    ivr_score,
        "ivr_label":    ivr_label,
        "expected_move_pct": em_pct,
        "yest_high":    round(r.get("yest_high", 0), 2),
        "yest_low":     round(r.get("yest_low", 0), 2),
        "today_high":   round(r.get("today_high", 0), 2),
        "today_low":    round(r.get("today_low", 0), 2),
        "earnings_days": r.get("earnings_days"),
        "earnings_warn": r.get("earnings_days") is not None and r.get("earnings_days", 999) <= 7,
        "sparkline":    r.get("sparkline", []),
    }


# ── Earnings calendar helper ───────────────────────────────────────────────────

def _fetch_earnings_map(tickers: list) -> dict:
    """Returns {ticker: days_until_earnings_or_None}"""
    import yfinance as yf
    import warnings
    from datetime import date
    warnings.filterwarnings("ignore")
    result = {}
    today = date.today()
    for sym in tickers:
        try:
            info = yf.Ticker(sym).info
            # Try multiple fields yfinance uses
            eds = info.get("earningsDate") or info.get("earningsTimestamp")
            if eds:
                if isinstance(eds, (int, float)):
                    from datetime import datetime
                    ed = datetime.utcfromtimestamp(eds).date()
                elif hasattr(eds, 'date'):
                    ed = eds.date()
                else:
                    ed = None
                if ed:
                    result[sym] = max(0, (ed - today).days)
        except Exception:
            pass
    return result


# ── Sparkline helper ───────────────────────────────────────────────────────────

def _fetch_sparklines(tickers):
    import yfinance as yf, warnings
    warnings.filterwarnings("ignore")
    sparklines = {}
    try:
        raw = yf.download(tickers, period="1d", interval="5m", progress=False, auto_adjust=True)
        for sym in tickers:
            try:
                if isinstance(raw.columns, __import__('pandas').MultiIndex):
                    closes = raw["Close"][sym].dropna().tolist()
                else:
                    closes = raw["Close"].dropna().tolist()
                if len(closes) >= 3:
                    mn, mx = min(closes), max(closes)
                    span = mx - mn or 1
                    norm = [round((c - mn) / span, 3) for c in closes[-20:]]
                    sparklines[sym] = norm
            except Exception:
                pass
    except Exception:
        pass
    return sparklines


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = TEMPLATES_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(), status_code=200)


@app.get("/health")
async def health():
    vix = -1.0
    try:
        vix = await asyncio.get_event_loop().run_in_executor(None, fetch_vix)
    except Exception:
        pass
    return {"status": "ok", "market_open": is_market_open(), "vix": vix}


@app.get("/api/flow")
async def flow_heatmap():
    """Batch-fetch change% and relVol for all FAST_UNIVERSE tickers. Used by ticker heat map."""
    loop = asyncio.get_event_loop()

    def _fetch():
        import yfinance as yf
        import pandas as pd
        import warnings
        warnings.filterwarnings("ignore")

        # SPX needs ^GSPC in yfinance — skip it (SPY covers it)
        syms = [t for t in FAST_UNIVERSE if t != "SPX"]

        try:
            raw = yf.download(syms, period="5d", interval="1d", progress=False, auto_adjust=True)
        except Exception:
            return []

        results = []
        for sym in syms:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    close = raw["Close"][sym].dropna()
                    vol   = raw["Volume"][sym].dropna()
                else:
                    close = raw["Close"].dropna()
                    vol   = raw["Volume"].dropna()

                if len(close) < 2:
                    results.append({"ticker": sym, "change_pct": 0.0, "rel_vol": 1.0})
                    continue

                prev = float(close.iloc[-2])
                curr = float(close.iloc[-1])
                chg  = (curr - prev) / prev * 100 if prev > 0 else 0.0

                today_vol = float(vol.iloc[-1]) if len(vol) >= 1 else 0.0
                avg_vol   = float(vol.iloc[:-1].mean()) if len(vol) >= 2 else max(today_vol, 1.0)
                rel_vol   = today_vol / avg_vol if avg_vol > 0 else 1.0

                results.append({
                    "ticker":     sym,
                    "change_pct": round(chg, 2),
                    "rel_vol":    round(min(rel_vol, 9.9), 2),
                })
            except Exception:
                results.append({"ticker": sym, "change_pct": 0.0, "rel_vol": 1.0})

        # Sort by rel_vol descending so hottest tickers are first
        results.sort(key=lambda x: x["rel_vol"], reverse=True)
        return results

    data = await loop.run_in_executor(None, _fetch)
    return JSONResponse(content={"tickers": data})


@app.get("/api/market")
async def market_context():
    """Returns SPY/QQQ change%, VIX, market status, and sector heat strip."""
    loop = asyncio.get_event_loop()

    def _fetch():
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        result = {"spy_chg": 0.0, "qqq_chg": 0.0, "vix": -1.0,
                  "market_open": is_market_open(),
                  "market_status": market_status_str(),
                  "sectors": []}
        try:
            vix = fetch_vix()
            result["vix"] = vix
        except Exception:
            pass

        for sym, key in [("SPY", "spy_chg"), ("QQQ", "qqq_chg")]:
            try:
                t = yf.Ticker(sym)
                h = t.history(period="2d")
                if len(h) >= 2:
                    prev = float(h["Close"].iloc[-2])
                    curr = float(h["Close"].iloc[-1])
                    if prev > 0:
                        result[key] = round((curr - prev) / prev * 100, 2)
            except Exception:
                pass

        try:
            sector_data = scan_sectors()
            sectors = []
            for name, d in sorted(sector_data.items(),
                                   key=lambda x: x[1]["strength"], reverse=True):
                sectors.append({
                    "name":       name,
                    "etf":        d["etf"],
                    "change_pct": round(d["change_pct"], 2),
                    "rel_vol":    round(d["rel_vol"], 2),
                    "bias":       d["bias"],
                    "mom_3d":     round(d["mom_3d"], 2),
                })
            result["sectors"] = sectors
        except Exception:
            pass

        return result

    data = await loop.run_in_executor(None, _fetch)
    return JSONResponse(content=data)


@app.post("/api/scan")
async def scan_endpoint(request: Request):
    """
    SSE streaming scan. Body JSON:
      tickers: str (space/comma separated, blank = fast default)
      filter: str
      sort: str
      load_contracts: bool
      dynamic: bool
    Streams JSON lines: {"type": "progress"|"result"|"sector"|"done"|"error", ...}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    tickers_input  = body.get("tickers", "").strip()
    filter_by      = body.get("filter", "any")
    sort_by        = body.get("sort", "setup")
    load_contracts = bool(body.get("load_contracts", False))
    dynamic_mode   = bool(body.get("dynamic", False))
    dte_mode       = body.get("dte_mode", "0dte")

    if tickers_input:
        tickers = [t.strip().upper() for t in tickers_input.replace(",", " ").split() if t.strip()]
    else:
        tickers = list(FAST_UNIVERSE)
        if dynamic_mode:
            try:
                dynamic_tickers = fetch_dynamic_universe(top_n=40)
                added = [t for t in dynamic_tickers if t not in tickers]
                tickers = list(dict.fromkeys(tickers + added))
            except Exception:
                pass

    mo = is_market_open()

    async def generate():
        loop = asyncio.get_event_loop()

        def _emit(obj: Dict) -> str:
            return json.dumps(obj, default=str) + "\n"

        # ── Step 1: Sectors ───────────────────────────────────────────────────
        yield _emit({"type": "progress", "msg": "Scanning sectors...", "pct": 2})

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                sector_data = await loop.run_in_executor(None, scan_sectors)
        except Exception as e:
            yield _emit({"type": "error", "msg": f"Sector scan failed: {e}"})
            return

        # Emit sector heat strip
        sector_strip = []
        for name, d in sorted(sector_data.items(),
                               key=lambda x: x[1]["strength"], reverse=True):
            sector_strip.append({
                "name":       name,
                "change_pct": round(d["change_pct"], 2),
                "bias":       d["bias"],
            })
        yield _emit({"type": "sector", "sectors": sector_strip})

        # ── Step 2: Tickers ───────────────────────────────────────────────────
        yield _emit({"type": "progress", "msg": f"Scanning {len(tickers)} tickers...", "pct": 10})

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                results = await loop.run_in_executor(
                    None, lambda: scan_tickers(tickers, show_progress=False)
                )
                apply_forward_directions(results, sector_data)
                laggards = find_sector_laggards(results, sector_data)
        except Exception as e:
            yield _emit({"type": "error", "msg": f"Ticker scan failed: {e}"})
            return

        yield _emit({"type": "progress", "msg": "Applying filters...", "pct": 60})

        # ── Step 2b: Earnings + Sparklines (parallel) ─────────────────────────
        result_tickers = [r["ticker"] for r in results]
        try:
            earnings_future = loop.run_in_executor(
                None, lambda: _fetch_earnings_map(result_tickers)
            )
            sparkline_future = loop.run_in_executor(
                None, lambda: _fetch_sparklines(result_tickers)
            )
            earnings_map = await earnings_future
            sparklines_map = await sparkline_future
            for r in results:
                sym = r["ticker"]
                if sym in earnings_map:
                    r["earnings_days"] = earnings_map[sym]
                r["sparkline"] = sparklines_map.get(sym, [])
        except Exception:
            pass  # earnings/sparklines are optional

        # ── Step 3: Contracts (optional) ──────────────────────────────────────
        if load_contracts:
            yield _emit({"type": "progress", "msg": "Fetching options chains...", "pct": 65})
            try:
                vix = await loop.run_in_executor(None, fetch_vix)
                with contextlib.redirect_stdout(io.StringIO()):
                    await loop.run_in_executor(
                        None, lambda: enrich_contracts(results, top_n=15, vix=vix,
                                                       dte_mode=dte_mode)
                    )
                    for r in laggards[:6]:
                        if not r.get("contract"):
                            r["contract"] = get_best_contract(
                                r["ticker"], r["direction"], r["price"], vix=vix,
                                dte_mode=dte_mode
                            )
            except Exception:
                pass  # contracts are optional — don't abort on failure

        # ── Step 4: Filter + Sort + Emit ─────────────────────────────────────
        yield _emit({"type": "progress", "msg": "Building results...", "pct": 85})

        filtered = apply_filter(results, filter_by)
        ordered  = apply_sort(filtered, sort_by)

        # Stats
        a_count = sum(1 for r in ordered
                      if trade_grade_plain(r["setup_q"], r["opt_score"], bool(r.get("contract"))) == "A")
        top_ticker = ordered[0]["ticker"] if ordered else "—"

        yield _emit({
            "type":          "stats",
            "total_scanned": len(results),
            "signals_found": len(filtered),
            "a_grade_count": a_count,
            "top_ticker":    top_ticker,
        })

        # Serialize and emit all results
        serialized = []
        for r in ordered:
            try:
                serialized.append(serialize_result(r, mo))
            except Exception:
                pass  # skip bad results

        yield _emit({
            "type":    "results",
            "results": serialized,
            "market_open": mo,
            "scan_time": datetime.now().strftime("%H:%M:%S"),
        })

        # Done
        yield _emit({
            "type":      "done",
            "msg":       f"Scan complete — {len(results)} tickers",
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        })

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.get("/api/breadth")
async def market_breadth():
    loop = asyncio.get_event_loop()
    def _fetch():
        import yfinance as yf, warnings
        warnings.filterwarnings("ignore")
        # Sector ETFs as breadth proxy
        SECTORS = ["XLK","XLF","XLE","XLV","XLI","XLP","XLU","XLB","XLC","XLRE"]
        SPY_SUBSET = ["SPY","QQQ","IWM","DIA"]
        all_syms = SECTORS + SPY_SUBSET
        pos, total = 0, 0
        details = []
        try:
            raw = yf.download(all_syms, period="2d", interval="1d", progress=False, auto_adjust=True)
            for sym in all_syms:
                try:
                    if isinstance(raw.columns, __import__('pandas').MultiIndex):
                        c = raw["Close"][sym].dropna()
                    else:
                        c = raw["Close"].dropna()
                    if len(c) >= 2:
                        chg = (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
                        total += 1
                        if chg > 0: pos += 1
                        details.append({"sym": sym, "chg": round(chg, 2)})
                except Exception:
                    pass
        except Exception:
            pass
        # Try NYSE A/D tickers
        adv, decl = 0, 0
        for sym, key in [("^ADVN", "adv"), ("^DECLN", "decl")]:
            try:
                t = yf.Ticker(sym)
                h = t.history(period="1d")
                val = int(h["Close"].iloc[-1]) if len(h) else 0
                if key == "adv": adv = val
                else: decl = val
            except Exception:
                pass
        score = round(pos / total * 100) if total else 50
        return {
            "pos_sectors": pos, "total_sectors": total,
            "breadth_score": score,
            "adv": adv, "decl": decl,
            "details": details,
        }
    data = await loop.run_in_executor(None, _fetch)
    return JSONResponse(content=data)


@app.get("/api/events")
async def economic_events():
    from datetime import date, timedelta
    today = date.today()
    window_end = today + timedelta(days=7)

    # Key 2026 economic dates (hardcoded — update annually)
    ALL_EVENTS = [
        # FOMC decisions
        {"date":"2026-01-29","time":"14:00 ET","name":"FOMC Decision","impact":"high","emoji":"🏦"},
        {"date":"2026-03-19","time":"14:00 ET","name":"FOMC Decision","impact":"high","emoji":"🏦"},
        {"date":"2026-05-07","time":"14:00 ET","name":"FOMC Decision","impact":"high","emoji":"🏦"},
        {"date":"2026-06-18","time":"14:00 ET","name":"FOMC Decision","impact":"high","emoji":"🏦"},
        {"date":"2026-07-30","time":"14:00 ET","name":"FOMC Decision","impact":"high","emoji":"🏦"},
        {"date":"2026-09-17","time":"14:00 ET","name":"FOMC Decision","impact":"high","emoji":"🏦"},
        {"date":"2026-10-29","time":"14:00 ET","name":"FOMC Decision","impact":"high","emoji":"🏦"},
        {"date":"2026-12-10","time":"14:00 ET","name":"FOMC Decision","impact":"high","emoji":"🏦"},
        # FOMC minutes (3 weeks after)
        {"date":"2026-02-18","time":"14:00 ET","name":"FOMC Minutes","impact":"medium","emoji":"📋"},
        {"date":"2026-04-08","time":"14:00 ET","name":"FOMC Minutes","impact":"medium","emoji":"📋"},
        # CPI
        {"date":"2026-01-15","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-02-12","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-03-12","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-04-10","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-05-14","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-06-11","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-07-14","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-08-13","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-09-10","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-10-14","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-11-12","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        {"date":"2026-12-10","time":"08:30 ET","name":"CPI Release","impact":"high","emoji":"📊"},
        # NFP (first Friday)
        {"date":"2026-01-09","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-02-06","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-03-06","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-04-03","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-05-01","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-06-05","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-07-10","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-08-07","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-09-04","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-10-02","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-11-06","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        {"date":"2026-12-04","time":"08:30 ET","name":"Jobs Report (NFP)","impact":"high","emoji":"💼"},
        # GDP
        {"date":"2026-01-30","time":"08:30 ET","name":"GDP (Advance)","impact":"medium","emoji":"📈"},
        {"date":"2026-04-29","time":"08:30 ET","name":"GDP (Advance)","impact":"medium","emoji":"📈"},
        {"date":"2026-07-29","time":"08:30 ET","name":"GDP (Advance)","impact":"medium","emoji":"📈"},
        {"date":"2026-10-28","time":"08:30 ET","name":"GDP (Advance)","impact":"medium","emoji":"📈"},
    ]

    upcoming = []
    for ev in ALL_EVENTS:
        try:
            ev_date = date.fromisoformat(ev["date"])
            if today <= ev_date <= window_end:
                days_away = (ev_date - today).days
                upcoming.append({**ev, "days_away": days_away, "is_today": days_away == 0})
        except Exception:
            pass

    upcoming.sort(key=lambda e: e["date"])
    return JSONResponse(content={"events": upcoming})
