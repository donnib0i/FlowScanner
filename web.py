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
    }


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

        # ── Step 3: Contracts (optional) ──────────────────────────────────────
        if load_contracts:
            yield _emit({"type": "progress", "msg": "Fetching options chains...", "pct": 65})
            try:
                vix = await loop.run_in_executor(None, fetch_vix)
                with contextlib.redirect_stdout(io.StringIO()):
                    await loop.run_in_executor(
                        None, lambda: enrich_contracts(results, top_n=15, vix=vix)
                    )
                    for r in laggards[:6]:
                        if not r.get("contract"):
                            r["contract"] = get_best_contract(
                                r["ticker"], r["direction"], r["price"], vix=vix
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
