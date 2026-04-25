#!/usr/bin/env python3
# scanner_web.py — Elite Scanner Web UI + LiteLLM AI Brief (DEPRECATED)
# DEPRECATED: Replaced by web.py (FastAPI + premium HTML dashboard).
# To run the new UI: uvicorn web:app --host 0.0.0.0 --port 7860
# This file is kept for backward compatibility only.
# Run: python3 scanner_web.py  → prints a shareable link

import os
import sys
import re
from datetime import datetime
from typing import List, Dict

import gradio as gr
import pandas as pd
import plotly.graph_objects as go
from litellm import completion

# Import all scanner logic from scanner.py
from scanner import (
    scan_sectors,
    scan_tickers,
    apply_forward_directions,
    enrich_contracts,
    find_sector_laggards,
    scan_options_flow,
    scan_dark_pool_prints,
    apply_filter,
    apply_sort,
    get_best_contract,
    fetch_vix,
    fetch_dynamic_universe,
    FILTER_LABELS,
    SORT_LABELS,
    TICKER_SECTOR,
    SECTOR_ETFS,
    UNIVERSE,
    fmt_flow,
    fmt_whale_score,
)
from tt_flow import scan_options_flow_tt, load_credentials, save_credentials

# ── Fast default universe (runs in ~25s vs 3min for full) ──────────────────
FAST_UNIVERSE = [
    "SPX", "SPY", "QQQ", "IWM", "VXX", "UVXY",
    "TQQQ", "SQQQ", "SOXL", "SOXS", "UPRO", "SPXS",
    "NVDA", "AMD", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA",
    "COIN", "PLTR", "MSTR", "HOOD", "MARA", "BULL",
    "NFLX", "CRWD", "PANW", "NET", "APP", "RDDT",
    "GS", "JPM", "SOFI", "AFRM", "NU",
    "HIMS", "GME", "AMC", "ONDS",
    "ARM", "AVGO", "MU", "SMCI",
    "RIVN", "NIO", "XPEV",
    "XOM", "CVX", "FCX", "CPER",
    "RXRX", "MRNA", "LLY",
]

ALL_SECTOR_NAMES = list(SECTOR_ETFS.keys()) + ["Index", "Vol", "Other"]


# ── Heatmap ────────────────────────────────────────────────────────────────

def build_heatmap_fig(results: List[Dict], sector_data: Dict) -> go.Figure:
    """Plotly treemap: sectors as groups, tickers inside, colored by change%."""

    # Group tickers by sector
    sector_tickers: Dict[str, List[Dict]] = {}
    for r in results:
        sec = TICKER_SECTOR.get(r["ticker"], "Other")
        sector_tickers.setdefault(sec, []).append(r)

    labels  = ["Market"]
    parents = [""]
    values  = [0]
    colors  = [0.0]
    custom  = [""]

    # Sector nodes (use sector_data for ETF-level metrics)
    added_sectors: set = set()
    for name, sd in sector_data.items():
        labels.append(name)
        parents.append("Market")
        values.append(max(len(sector_tickers.get(name, [])), 1))
        colors.append(sd["change_pct"])
        custom.append(
            f"{sd['etf']}: {sd['change_pct']:+.2f}%"
            f"<br>RelVol {sd['rel_vol']:.2f}x | {sd['bias'].upper()}"
        )
        added_sectors.add(name)

    # Extra pseudo-sectors (Index, Vol, Other) if we have tickers there
    for sec_name, tks in sector_tickers.items():
        if sec_name not in added_sectors and tks:
            labels.append(sec_name)
            parents.append("Market")
            values.append(len(tks))
            avg_chg = sum(t["change_pct"] for t in tks) / len(tks)
            colors.append(avg_chg)
            custom.append(f"{len(tks)} tickers | avg {avg_chg:+.2f}%")
            added_sectors.add(sec_name)

    # Ticker nodes
    for r in results:
        sec = TICKER_SECTOR.get(r["ticker"], "Other")
        if sec not in added_sectors:
            sec = "Market"
        labels.append(r["ticker"])
        parents.append(sec)
        # Size = relative volume so high-vol tickers stand out
        values.append(max(1.0, r["rel_vol"] * 3))
        colors.append(r["change_pct"])
        flags = []
        if r["gap_flag"]:    flags.append(r["gap_flag"].replace("_", " ").upper())
        if r["inside_day"]:  flags.append("ID")
        if r["high_vol"]:    flags.append("HVOL")
        if r.get("is_laggard"): flags.append("LAG")
        flag_str = " | ".join(flags)
        custom.append(
            f"${r['price']:.2f} | {r['change_pct']:+.2f}%"
            f"<br>RVol {r['rel_vol']:.2f}x"
            + (f"<br>{flag_str}" if flag_str else "")
        )

    fig = go.Figure(go.Treemap(
        labels=labels,
        parents=parents,
        values=values,
        customdata=custom,
        hovertemplate="<b>%{label}</b><br>%{customdata}<extra></extra>",
        texttemplate="<b>%{label}</b><br>%{customdata}",
        marker=dict(
            colors=colors,
            colorscale=[
                [0.00, "#8B0000"],
                [0.30, "#CC2222"],
                [0.47, "#551111"],
                [0.50, "#1a1a2e"],
                [0.53, "#113322"],
                [0.70, "#009933"],
                [1.00, "#00CC44"],
            ],
            cmid=0,
            cmin=-4,
            cmax=4,
            showscale=True,
            colorbar=dict(title="Chg%", thickness=14, len=0.8),
        ),
        root_color="#0d0d0d",
        pathbar=dict(visible=True),
    ))

    fig.update_layout(
        paper_bgcolor="#111111",
        plot_bgcolor="#111111",
        font=dict(color="#e0e0e0", size=10),
        margin=dict(t=10, l=0, r=0, b=0),
        height=640,
    )

    return fig


# ── Helpers ────────────────────────────────────────────────────────────────

def fmt_strike(k: float) -> str:
    """Format a strike price: whole number → no decimals, half-dollar → one decimal."""
    return f"${k:.0f}" if k == int(k) else f"${k:.2f}"


# ── Sector Browser ─────────────────────────────────────────────────────────

def filter_sector_tickers(results_state: List[Dict], sector: str) -> pd.DataFrame:
    """Return a table of scanned tickers in the chosen sector, sorted weakest→strongest."""
    if not results_state or not sector:
        return pd.DataFrame({"Note": ["Run a scan first."]})

    filtered = [r for r in results_state if TICKER_SECTOR.get(r["ticker"], "Other") == sector]
    if not filtered:
        return pd.DataFrame({"Note": [f"No scanned tickers found in {sector}. Add more tickers to the scan."]})

    # Weakest → strongest change% so the laggard is at the top
    filtered.sort(key=lambda r: r["change_pct"])

    rows = []
    for r in filtered:
        flags = []
        if r["gap_flag"] == "gap_up":   flags.append("GAP↑")
        if r["gap_flag"] == "gap_down": flags.append("GAP↓")
        if r["inside_day"]:             flags.append("ID")
        if r["high_vol"]:               flags.append("HVOL")
        if r.get("is_laggard"):         flags.append(f"LAG{r['lag_pct']:+.1f}%")
        c = r.get("contract")
        contract_str = (
            f"{c['exp'][5:]} {fmt_strike(c['strike'])}{'C' if c['type']=='call' else 'P'}"
            f" δ{c['delta']:+.2f} ${c['mid']:.2f}"
        ) if c else "—"
        rows.append({
            "Ticker":   r["ticker"],
            "Price":    f"${r['price']:.2f}",
            "Chg%":     f"{r['change_pct']:+.2f}%",
            "RelVol":   f"{r['rel_vol']:.2f}x",
            "Setups":   " | ".join(flags) if flags else "—",
            "OptScore": r["opt_score"],
            "Dir":      "↑" if r["direction"] == "up" else "↓",
            "Setup Q":  f"{r['setup_q']:.2f}",
            "Contract": contract_str,
        })

    return pd.DataFrame(rows)


# ── AI Brief ───────────────────────────────────────────────────────────────

def generate_brief(results: List[Dict], sector_data: Dict,
                   laggards: List[Dict] = None,
                   flow_signals: List[Dict] = None,
                   dp_signals: List[Dict] = None) -> str:
    """Call LiteLLM (Claude) to generate a D-style daily brief from scan data."""

    top = sorted(
        [r for r in results if r["gap_flag"] or r["inside_day"] or r["high_vol"]],
        key=lambda r: r["setup_q"],
        reverse=True,
    )[:8]

    if not top:
        top = sorted(results, key=lambda r: r["opt_score"], reverse=True)[:5]

    sector_lines = "\n".join(
        f"  {name}: {d['change_pct']:+.2f}% | bias={d['bias']} | "
        f"3d_mom={d['mom_3d']:+.2f}% | rel_vol={d['rel_vol']:.2f}x"
        for name, d in sorted(sector_data.items(), key=lambda x: x[1]["strength"], reverse=True)
    )

    setup_lines = []
    for r in top:
        flags = []
        if r["gap_flag"]:          flags.append(r["gap_flag"])
        if r["inside_day"]:        flags.append("inside_day")
        if r["high_vol"]:          flags.append("high_vol")
        if r.get("is_laggard"):    flags.append(f"laggard({r['lag_pct']:+.1f}%)")
        nl  = r.get("near_level")
        lvl = f" | near=${nl['price']:.2f} ({nl['type']}, str={nl['strength']})" if nl else ""
        c   = r.get("contract")
        cnt = (
            f" | contract={c['exp'][5:]} {fmt_strike(c['strike'])}{'C' if c['type']=='call' else 'P'}"
            f" δ{c['delta']:+.2f} ${c['mid']:.2f}"
        ) if c else ""
        setup_lines.append(
            f"  {r['ticker']}: ${r['price']:.2f} | {r['change_pct']:+.2f}% | "
            f"{','.join(flags) or 'no_flag'} | opt={r['opt_score']} | "
            f"dir={r['direction']} | sq={r['setup_q']:.2f}{lvl}{cnt}"
        )

    lag_lines = ""
    if laggards:
        lag_rows = []
        for r in laggards[:5]:
            sname = TICKER_SECTOR.get(r["ticker"], "?")
            sd = sector_data.get(sname, {})
            sec_chg = sd.get("change_pct", 0)
            lag_rows.append(
                f"  {r['ticker']}: ticker={r['change_pct']:+.2f}% vs sector={sec_chg:+.2f}%"
                f" | lag={r['lag_pct']:+.2f}% | play={r['lag_direction'].upper()}"
            )
        lag_lines = "\nSECTOR LAGGARDS:\n" + "\n".join(lag_rows)

    flow_lines = ""
    if flow_signals:
        flow_rows = []
        for f in flow_signals[:5]:
            tc = f.get("top_contract")
            cstr = ""
            if tc:
                sweep = " [SWEEP]" if tc.get("sweep") else ""
                cstr = (
                    f" | top={tc['exp'][5:]} {fmt_strike(tc['strike'])}"
                    f"{'C' if tc['type']=='call' else 'P'}"
                    f" x{tc['vol_oi']:.1f}vol/OI ${tc['mid']:.2f}{sweep}"
                )
            flow_rows.append(
                f"  {f['ticker']}: total={fmt_flow(f['total_flow'])}"
                f" calls={fmt_flow(f['call_flow'])} puts={fmt_flow(f['put_flow'])}"
                f" bias={f['flow_bias'].upper()}"
                f" side={f.get('trade_side','?')} score={f.get('whale_score',0)}"
                f" golden={'YES' if f.get('golden_sweep') else 'no'}"
                f" stacked={'YES' if f.get('stacked_flow') else 'no'}"
                f" iv_skew={f.get('iv_skew',0):+.3f}{cstr}"
            )
        flow_lines = "\nOPTIONS FLOW (unusual activity):\n" + "\n".join(flow_rows)

    whale_lines = ""
    if flow_signals:
        whales = [f for f in flow_signals if f.get("whale_score", 0) >= 60]
        if whales:
            wrows = [
                f"  {f['ticker']}: score={f['whale_score']} tier={f.get('premium_tier','?')}"
                f" side={f.get('trade_side','?')} golden={'YES' if f.get('golden_sweep') else 'no'}"
                f" 0DTE={fmt_flow(f.get('dte0_flow',0))} bias={f['flow_bias'].upper()}"
                for f in whales[:4]
            ]
            whale_lines = "\nWHALE ALERTS (score ≥ 60):\n" + "\n".join(wrows)

    dp_lines = ""
    if dp_signals:
        dp_rows = [
            f"  {dp['ticker']}: rvol={dp['rel_vol']:.1f}x move={dp['intra_move']:.2f}%"
            f" signal={dp['dp_bias'].upper()} $vol={fmt_flow(dp['dollar_vol'])}"
            for dp in dp_signals[:3]
        ]
        dp_lines = "\nDARK POOL PRINTS (high vol, low price impact):\n" + "\n".join(dp_rows)

    prompt = f"""You are D — a sharp trading AI for a 0DTE options trader (SPX/QQQ on Robinhood).
Rules: +$50 daily target → close app, done. -$50 hard stop. Do not overtrade.

Scan data ({datetime.now().strftime('%Y-%m-%d %H:%M')}):

SECTORS:
{sector_lines}

TOP SETUPS:
{chr(10).join(setup_lines)}{lag_lines}{flow_lines}{whale_lines}{dp_lines}

Write a tight daily brief using this exact format:

BIAS:  Bull / Bear / Neutral
PLAY TYPE:  [gap fill / level play / inside bar / trend day / chop day / sector laggard]

KEY LEVELS
  Resistance:  XXXX.XX / XXXX.XX
  Support:     XXXX.XX / XXXX.XX

TARGETS
  Long target:   +X pts
  Short target:  -X pts
  Stop area:     XXXX.XX

PLAN
  [2–3 sentences: setup, entry trigger, what kills it]

TOP PLAYS
  [2–3 tickers: ticker — reason — contract if available]

No filler. Lead with the answer."""

    try:
        response = completion(
            model="claude-3-5-sonnet-20241022",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"⚠ AI brief unavailable: {e}"


# ── Main scan generator ────────────────────────────────────────────────────

def run_scan(tickers_input: str, enrich: bool, filter_by: str, sort_by: str,
             run_flow: bool = False, run_dp: bool = False,
             tt_user: str = "", tt_pass: str = "", tt_window: int = 90,
             dynamic_mode: bool = False):
    """Generator: yields (sector_df, results_df, laggard_df, prints_df, flow_df,
                          whale_df, dp_df, sector_flow_df, ai_brief, status, heatmap_fig, results_state)."""

    if tickers_input.strip():
        tickers = [t.strip().upper() for t in tickers_input.replace(",", " ").split() if t.strip()]
    else:
        tickers = list(FAST_UNIVERSE)
        if dynamic_mode:
            dynamic_tickers = fetch_dynamic_universe(top_n=40)
            added = [t for t in dynamic_tickers if t not in tickers]
            tickers = list(dict.fromkeys(tickers + added))

    empty     = pd.DataFrame()
    empty_fig = go.Figure()
    empty_fig.update_layout(paper_bgcolor="#111111", plot_bgcolor="#111111",
                            font=dict(color="#666"), height=640,
                            annotations=[dict(text="Run a scan to generate heatmap",
                                             showarrow=False, font=dict(size=16, color="#666"),
                                             xref="paper", yref="paper", x=0.5, y=0.5)])
    _flow_placeholder = "### FLOW BY SECTOR\n*Enable **Options Flow Scan** and run to see sector flow.*"

    yield empty, empty, empty, empty, empty, empty, empty, _flow_placeholder, "", "Scanning sectors...", empty_fig, []

    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        sector_data = scan_sectors()

    yield empty, empty, empty, empty, empty, empty, empty, _flow_placeholder, "", f"Scanning {len(tickers)} tickers...", empty_fig, []

    with contextlib.redirect_stdout(io.StringIO()):
        results = scan_tickers(tickers, show_progress=False)
        apply_forward_directions(results, sector_data)
        laggards = find_sector_laggards(results, sector_data)
        vix = fetch_vix()
        if enrich:
            enrich_contracts(results, top_n=10, vix=vix)
            for r in laggards[:6]:
                if not r.get("contract"):
                    r["contract"] = get_best_contract(r["ticker"], r["direction"], r["price"], vix=vix)

    # ── Sector DataFrame ──────────────────────────────────────────────────
    sector_df = pd.DataFrame([
        {
            "Sector":   name,
            "ETF":      d["etf"],
            "Change%":  f"{d['change_pct']:+.2f}%",
            "Rel Vol":  f"{d['rel_vol']:.2f}x",
            "3D Mom":   f"{d['mom_3d']:+.2f}%",
            "Bias":     "↑ Bull" if d["bias"] == "up" else "↓ Bear",
        }
        for name, d in sorted(sector_data.items(), key=lambda x: x[1]["strength"], reverse=True)
    ])

    # ── Results DataFrame ─────────────────────────────────────────────────
    filtered = apply_filter(results, filter_by)
    ordered  = apply_sort(filtered, sort_by)

    rows = []
    for r in ordered:
        flags = []
        if r["gap_flag"] == "gap_up":   flags.append("GAP↑")
        if r["gap_flag"] == "gap_down": flags.append("GAP↓")
        if r["inside_day"]:             flags.append("ID")
        if r["high_vol"]:               flags.append("HVOL")
        if r.get("is_laggard"):         flags.append(f"LAG{r['lag_pct']:+.1f}%")
        nl = r.get("near_level")
        if nl:
            flags.append(f"LVL@${nl['price']:.2f}(★{nl['strength']})")

        c = r.get("contract")
        contract_str = (
            f"{c['exp'][5:]} {fmt_strike(c['strike'])}{'C' if c['type']=='call' else 'P'}"
            f" δ{c['delta']:+.2f} ${c['mid']:.2f}"
        ) if c else "—"

        rows.append({
            "Ticker":    r["ticker"],
            "Price":     f"${r['price']:.2f}",
            "Chg%":      f"{r['change_pct']:+.2f}%",
            "Gap%":      f"{r['gap_pct']:+.2f}%",
            "RelVol":    f"{r['rel_vol']:.2f}x",
            "Setups":    " | ".join(flags) if flags else "—",
            "OptScore":  r["opt_score"],
            "Dir":       "↑" if r["direction"] == "up" else "↓",
            "Setup Q":   f"{r['setup_q']:.2f}",
            "Contract":  contract_str,
        })

    results_df = pd.DataFrame(rows) if rows else empty

    # ── Laggard DataFrame ─────────────────────────────────────────────────
    lag_rows = []
    for r in laggards:
        sname = TICKER_SECTOR.get(r["ticker"], "?")
        sd    = sector_data.get(sname, {})
        c = r.get("contract")
        lag_rows.append({
            "Ticker":      r["ticker"],
            "Price":       f"${r['price']:.2f}",
            "Ticker Chg":  f"{r['change_pct']:+.2f}%",
            "Sector Chg":  f"{sd.get('change_pct', 0):+.2f}%",
            "Sector":      sname,
            "Lag":         f"{r['lag_pct']:+.2f}%",
            "Play":        "↑ CALLS" if r["lag_direction"] == "up" else "↓ PUTS",
            "Contract":    (
                f"{c['exp'][5:]} {fmt_strike(c['strike'])}{'C' if c['type']=='call' else 'P'}"
                f" δ{c['delta']:+.2f} ${c['mid']:.2f}"
            ) if c else "—",
        })
    laggard_df = pd.DataFrame(lag_rows) if lag_rows else empty

    # ── Options Flow ──────────────────────────────────────────────────────
    flow_signals: List[Dict] = []
    flow_df = empty
    whale_df = empty
    prints_df = empty
    sector_flow_df = _flow_placeholder
    if run_flow:
        # Resolve TT credentials: explicit inputs > env vars > saved file
        _u = tt_user.strip()
        _p = tt_pass.strip()
        if not _u or not _p:
            _u, _p = load_credentials()
        use_tt = bool(_u and _p)

        status_label = (
            f"Scanning real options flow via Tastytrade ({tt_window}s window)..."
            if use_tt else
            "Scanning options flow (yfinance snapshot)..."
        )
        yield sector_df, results_df, laggard_df, empty, empty, empty, empty, _flow_placeholder, "", status_label, empty_fig, []

        flow_tickers = [r["ticker"] for r in
                        sorted(results, key=lambda x: x["opt_score"], reverse=True)[:20]]

        if use_tt:
            # Save credentials if they came from the UI inputs (not already saved)
            if tt_user.strip() and tt_pass.strip():
                try:
                    save_credentials(_u, _p)
                except Exception:
                    pass
            flow_signals = scan_options_flow_tt(
                flow_tickers, _u, _p,
                window_secs=tt_window,
                show_progress=False,
            )
        else:
            with contextlib.redirect_stdout(io.StringIO()):
                flow_signals = scan_options_flow(flow_tickers, show_progress=False)

        flow_rows = []
        whale_rows = []
        all_prints: List[Dict] = []
        for f in flow_signals:
            tc = f.get("top_contract")
            sweep = " [SWEEP]" if tc and tc.get("sweep") else ""
            if tc:
                vol_oi_str = f" x{tc['vol_oi']:.1f}" if tc.get("vol_oi", 0) > 0 else f" {tc.get('n_prints', '')}prints"
                top_str = (
                    f"{tc['exp'][5:]} {fmt_strike(tc['strike'])}"
                    f"{'C' if tc['type']=='call' else 'P'}"
                    f"{vol_oi_str} ${tc['mid']:.2f}{sweep}"
                )
            else:
                top_str = "—"
            flow_rows.append({
                "Ticker":       f["ticker"],
                "Total $Flow":  fmt_flow(f["total_flow"]),
                "Call Flow":    fmt_flow(f["call_flow"]),
                "Put Flow":     fmt_flow(f["put_flow"]),
                "Bias":         "↑ CALLS" if f["flow_bias"] == "call" else "↓ PUTS",
                "P/C Ratio":    f"{f['pc_ratio']:.2f}" if f["pc_ratio"] < 99 else "—",
                "Trade Side":   f.get("trade_side", "mid").upper(),
                "Whale Score":  f.get("whale_score", 0),
                "Golden":       "YES" if f.get("golden_sweep") else "—",
                "Top Contract": top_str,
            })
            ws = f.get("whale_score", 0)
            if ws >= 20:
                whale_rows.append({
                    "Ticker":       f["ticker"],
                    "Whale Score":  ws,
                    "Total $Flow":  fmt_flow(f["total_flow"]),
                    "Tier":         f.get("premium_tier", "retail").upper(),
                    "Trade Side":   f.get("trade_side", "mid").upper(),
                    "Golden Sweep": "YES" if f.get("golden_sweep") else "—",
                    "Stacked":      f"YES ({f['unique_strikes']} strikes)" if f.get("stacked_flow") else "—",
                    "IV Skew":      f"{f.get('iv_skew', 0):+.3f}",
                    "0DTE $":       fmt_flow(f.get("dte0_flow", 0)),
                    "1-7DTE $":     fmt_flow(f.get("dte1_7_flow", 0)),
                    "8+DTE $":      fmt_flow(f.get("dte8p_flow", 0)),
                    "Bias":         "↑ CALLS" if f["flow_bias"] == "call" else "↓ PUTS",
                    "Top Contract": top_str,
                })
        # Build flat feed of every individual unusual print, sorted by $ flow
        for f in flow_signals:
            for c in f.get("call_contracts", []) + f.get("put_contracts", []):
                flags = []
                if c.get("golden_sweep"): flags.append("GOLDEN")
                if c.get("sweep"):        flags.append("SWEEP")
                all_prints.append({
                    "Ticker":   c["ticker"],
                    "Exp":      c["exp"][5:],
                    "Strike":   fmt_strike(c["strike"]),
                    "Type":     "CALL" if c["type"] == "call" else "PUT",
                    "DTE":      c["dte"],
                    "Vol":      c["vol"],
                    "OI":       c["oi"],
                    "Vol/OI":   f"{c['vol_oi']:.1f}x" if c.get("vol_oi", 0) > 0 else "—",
                    "Mid":      f"${c['mid']:.2f}",
                    "$ Flow":   fmt_flow(c["flow"]),
                    "_flow_raw": c["flow"],
                    "Side":     c.get("trade_side", "mid").upper(),
                    "Tier":     c.get("premium_tier", "retail").upper(),
                    "Flags":    " ".join(flags) if flags else "—",
                })
        all_prints.sort(key=lambda x: x["_flow_raw"], reverse=True)
        for p in all_prints:
            del p["_flow_raw"]

        flow_df   = pd.DataFrame(flow_rows)   if flow_rows   else empty
        whale_df  = pd.DataFrame(whale_rows)  if whale_rows  else empty
        prints_df = pd.DataFrame(all_prints)  if all_prints  else empty

        # ── Sector Flow Breakdown ──────────────────────────────────────────
        sector_call: Dict[str, float] = {}
        sector_put:  Dict[str, float] = {}
        for f in flow_signals:
            sec = TICKER_SECTOR.get(f["ticker"], "Other")
            sector_call[sec] = sector_call.get(sec, 0.0) + f.get("call_flow", 0.0)
            sector_put[sec]  = sector_put.get(sec, 0.0)  + f.get("put_flow",  0.0)

        total_calls = sum(sector_call.values()) or 1.0
        total_puts  = sum(sector_put.values())  or 1.0

        all_secs = sorted(
            set(list(sector_call.keys()) + list(sector_put.keys())),
            key=lambda s: (sector_call.get(s, 0) + sector_put.get(s, 0)),
            reverse=True,
        )

        if all_secs:
            md_rows = ["### FLOW BY SECTOR — where is the money going?",
                       "| Sector | Call $ | Call % of Bullish | Put $ | Put % of Bearish | Dominant |",
                       "|--------|--------|-------------------|-------|-----------------|----------|"]
            for sec in all_secs:
                cf = sector_call.get(sec, 0.0)
                pf = sector_put.get(sec, 0.0)
                cp = cf / total_calls * 100
                pp = pf / total_puts  * 100
                dom = f"CALLS {cp:.0f}%" if cf >= pf else f"PUTS {pp:.0f}%"
                md_rows.append(
                    f"| **{sec}** | {fmt_flow(cf)} | {cp:.1f}% | {fmt_flow(pf)} | {pp:.1f}% | {dom} |"
                )
            sector_flow_df = "\n".join(md_rows)
        else:
            sector_flow_df = "### FLOW BY SECTOR\n*No flow data — flow scan returned no results.*"

    # ── Dark Pool Scan ────────────────────────────────────────────────────
    dp_signals: List[Dict] = []
    dp_df = empty
    if run_dp:
        yield sector_df, results_df, laggard_df, prints_df, flow_df, whale_df, empty, sector_flow_df, "", "Scanning dark pool prints...", empty_fig, []
        dp_tickers = [r["ticker"] for r in results]
        with contextlib.redirect_stdout(io.StringIO()):
            dp_signals = scan_dark_pool_prints(dp_tickers, show_progress=False)

        dp_rows = []
        for dp in dp_signals:
            dp_rows.append({
                "Ticker":    dp["ticker"],
                "Price":     f"${dp['price']:.2f}",
                "Rel Vol":   f"{dp['rel_vol']:.2f}x",
                "Price Move": f"{dp['intra_move']:.3f}%",
                "Day Range": f"{dp['day_range']:.3f}%",
                "Volume":    f"{dp['vol_today']:,}",
                "$ Volume":  fmt_flow(dp["dollar_vol"]),
                "Signal":    "ACCUM" if dp["dp_bias"] == "accumulation" else "DISTRIB",
            })
        dp_df = pd.DataFrame(dp_rows) if dp_rows else empty

    yield sector_df, results_df, laggard_df, prints_df, flow_df, whale_df, dp_df, sector_flow_df, "Generating AI brief...", "Generating brief...", empty_fig, []

    ai_brief    = generate_brief(results, sector_data, laggards, flow_signals, dp_signals)
    heatmap_fig = build_heatmap_fig(results, sector_data)
    ts          = datetime.now().strftime("%H:%M")

    yield sector_df, results_df, laggard_df, prints_df, flow_df, whale_df, dp_df, sector_flow_df, ai_brief, f"Done — {len(results)} tickers @ {ts}", heatmap_fig, results


# ── Gradio UI ──────────────────────────────────────────────────────────────
with gr.Blocks(title="D — Elite Scanner", theme=gr.themes.Monochrome()) as demo:
    gr.Markdown("## D — Elite Options Scanner")

    results_state = gr.State([])  # raw results stored for sector browser

    with gr.Row():
        tickers_input = gr.Textbox(
            label="Tickers (blank = fast default universe ~35 names)",
            placeholder="SPY QQQ NVDA AMD TSLA  — space or comma separated",
            scale=4,
        )
        filter_sel = gr.Dropdown(
            choices=list(FILTER_LABELS.keys()),
            value="any",
            label="Filter",
            scale=1,
        )
        sort_sel = gr.Dropdown(
            choices=list(SORT_LABELS.keys()),
            value="setup",
            label="Sort",
            scale=1,
        )

    with gr.Row():
        enrich_cb  = gr.Checkbox(value=False, label="Load Contracts (slower)", scale=1)
        flow_cb    = gr.Checkbox(value=False, label="Options Flow Scan", scale=1)
        dp_cb      = gr.Checkbox(value=False, label="Dark Pool Scan (slower)", scale=1)
        dynamic_cb = gr.Checkbox(value=False, label="Dynamic Mode (find sleepers)", scale=1)
        run_btn    = gr.Button("Run Scan", variant="primary", scale=3)
        status     = gr.Textbox(label="", interactive=False, scale=5)

    # ── Tastytrade / real flow credentials ────────────────────────────────
    _saved_u, _saved_p = load_credentials()
    with gr.Accordion("Tastytrade Credentials (real-time OPRA flow)", open=not bool(_saved_u)):
        gr.Markdown(
            "Enter your [tastytrade](https://tastytrade.com) username + password for **real-time** "
            "options flow via dxFeed OPRA — actual individual trade prints with true aggressor "
            "direction, not yfinance snapshots. Free account, no subscription needed. "
            "Credentials are saved locally to `.tt_creds.json`."
        )
        with gr.Row():
            tt_user_in   = gr.Textbox(label="Username", value=_saved_u, scale=2)
            tt_pass_in   = gr.Textbox(label="Password", value=_saved_p, type="password", scale=2)
            tt_window_in = gr.Slider(minimum=30, maximum=300, value=90, step=15,
                                     label="Collection window (sec)", scale=2)

    # ── Sector Flow Summary (always visible, above tabs) ─────────────────────
    sector_flow_out = gr.Markdown(
        value="### FLOW BY SECTOR\n*Run scan with **Options Flow Scan** checked to populate.*"
    )

    with gr.Tabs():
        with gr.Tab("AI Brief"):
            ai_out = gr.Markdown(value="*Hit Run Scan to generate.*")

        with gr.Tab("Top Setups"):
            results_out = gr.Dataframe(interactive=False)

        with gr.Tab("Sector Laggards"):
            laggard_out = gr.Dataframe(interactive=False)

        with gr.Tab("Flow Feed"):
            gr.Markdown(
                "### All Unusual Prints — sorted by $ flow\n"
                "Every individual contract with unusual activity, highest dollar flow first. "
                "**GOLDEN** = vol > OI×10, at ask, $100K+ — strongest single print. "
                "**SWEEP** = vol > OI×5 — aggressive multi-leg. "
                "**Side ASK** = buyer lifting offers. **BID** = seller hitting bids.\n\n"
                "*Enable 'Options Flow Scan' checkbox first.*"
            )
            source_label = (
                "**Source: Tastytrade dxFeed OPRA** — real individual prints, true aggressor direction."
                if _saved_u else
                "**Source: yfinance snapshot** — add Tastytrade credentials above for real prints."
            )
            gr.Markdown(source_label)
            prints_out = gr.Dataframe(interactive=False)

        with gr.Tab("Options Flow"):
            flow_out = gr.Dataframe(interactive=False)

        with gr.Tab("Whale Alerts"):
            gr.Markdown(
                "### Institutional & Whale Flow\n"
                "**Score ≥ 80** = extreme institutional conviction. "
                "**Golden Sweep** = vol > OI×10, buyer at ask, $100K+ premium — "
                "the highest-signal single print. "
                "**Stacked** = 3+ unusual strikes on same ticker = position building. "
                "**IV Skew** positive = calls pricier = bullish. "
                "**Trade Side ASK** = aggressive buyer. BID = aggressive seller.\n\n"
                "*Enable 'Options Flow Scan' checkbox first.*"
            )
            whale_out = gr.Dataframe(interactive=False)

        with gr.Tab("Dark Pool"):
            gr.Markdown(
                "### Dark Pool Approximation\n"
                "Detects equity sessions with **volume ≥ 2.5× the 20-day avg** but "
                "**intraday price move < 0.5%** — characteristic of large off-exchange "
                "(dark pool / block) execution where institutions avoid moving the tape. "
                "**ACCUM** = closed above open (quiet buying). **DISTRIB** = closed below.\n\n"
                "*Enable 'Dark Pool Scan' checkbox first.*"
            )
            dp_out = gr.Dataframe(interactive=False)

        with gr.Tab("Sector Heatmap"):
            gr.Markdown(
                "**Treemap heatmap** — sectors group tickers, colored by change%. "
                "Click a sector block to drill in. Tile size = relative volume."
            )
            heatmap_plot = gr.Plot(show_label=False)
            gr.Markdown("#### Sector summary table")
            sector_out = gr.Dataframe(interactive=False)

        with gr.Tab("Sector Browser"):
            gr.Markdown(
                "### Browse by sector — sorted **weakest → strongest** so the laggard is at the top.\n"
                "Run a scan first, then pick a sector to see all its tickers."
            )
            sector_sel = gr.Dropdown(
                choices=ALL_SECTOR_NAMES,
                value="Tech",
                label="Select Sector",
                scale=1,
            )
            sector_tickers_out = gr.Dataframe(
                interactive=False,
                label="Tickers in sector (run scan first)",
            )

    # ── Wire up events ─────────────────────────────────────────────────────
    run_btn.click(
        fn=run_scan,
        inputs=[tickers_input, enrich_cb, filter_sel, sort_sel, flow_cb, dp_cb,
                tt_user_in, tt_pass_in, tt_window_in, dynamic_cb],
        outputs=[
            sector_out, results_out, laggard_out, prints_out, flow_out,
            whale_out, dp_out, sector_flow_out,
            ai_out, status,
            heatmap_plot, results_state,
        ],
    )

    # Sector browser: updates whenever sector dropdown changes OR new scan finishes
    sector_sel.change(
        fn=filter_sector_tickers,
        inputs=[results_state, sector_sel],
        outputs=[sector_tickers_out],
    )

    results_state.change(
        fn=filter_sector_tickers,
        inputs=[results_state, sector_sel],
        outputs=[sector_tickers_out],
    )


if __name__ == "__main__":
    demo.launch(share=True)
