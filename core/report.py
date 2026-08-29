"""
core/report.py -- Terminal rendering of scan output -- tables, inline panels, and CSV.
Everything here prints or writes; nothing here fetches or scores.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from colorama import Fore, Style
from datetime import datetime
from tabulate import tabulate
from typing import Optional, List, Dict, Tuple
import csv

from core.constants import TICKER_SECTOR, _BASE_FIELDS, _TIER_COLORS
from core.market_data import vix_delta_target
from core.fmt import (
    color_change,
    fmt_bias_bar,
    fmt_flow,
    fmt_whale_score,
    level_str,
    trade_grade,
)
from core.options import fmt_contract, fmt_flow_contract
from core.pipeline import annotate_calibration, apply_filter, apply_sort, build_setups


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


def render_table(
    results: List[Dict],
    sort_by: str = "setup",
    filter_by: str = "any",
) -> Tuple[str, List[Dict]]:
    filtered = apply_filter(results, filter_by)
    # Annotate before sorting so --sort ev has something to sort on. With no
    # released model this is a no-op that stamps every row "uncalibrated".
    annotate_calibration(filtered)
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
    bkouts    = sum(1 for r in results if r.get("breakout"))
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
        + Fore.GREEN   + Style.BRIGHT + f"Breakout {bkouts}  " + Style.RESET_ALL
        + "  "
        + Fore.WHITE   + f"Opt>=60 {opt60}  "
        + Fore.RED     + f"Near Level {near_lvl}  "
        + Fore.CYAN    + f"Contracts {contracts}  "
        + Fore.MAGENTA + f"Laggards {laggards}"
        + Style.RESET_ALL
    )
    print(sep)


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


def print_signal_history(start: Optional[str] = None, end: Optional[str] = None,
                         symbol: Optional[str] = None, grade: Optional[str] = None,
                         limit: int = 50, journal=None) -> None:
    """Dump recorded signal history as a table. The read side of the journal."""
    try:
        from data.signal_journal import SignalJournal
        j = journal or SignalJournal()
        rows = j.query(start=start, end=end, symbol=symbol, grade=grade, limit=limit)
    except Exception as e:
        print(Fore.RED + f"  Could not read the signal journal: {e}" + Style.RESET_ALL)
        return

    if not rows:
        print(f"  {Fore.YELLOW}No signals recorded for that query.{Style.RESET_ALL}")
        return

    table = []
    for r in rows:
        if r["contract_expiry"]:
            ctype = "C" if r["contract_type"] == "call" else "P"
            contract = f"{r['contract_expiry'][5:]} ${r['contract_strike']:.0f}{ctype}"
            quote = f"{r['contract_bid'] or 0:.2f}/{r['contract_ask'] or 0:.2f}"
        else:
            contract, quote = "—", "—"
        table.append([
            r["emitted_at"][:19], r["symbol"], r["direction"], r["grade"] or "—",
            f"{r['setup_q']:.2f}" if r["setup_q"] is not None else "—",
            r["opt_score"] if r["opt_score"] is not None else "—",
            r["whale_score"] if r["whale_score"] is not None else "—",
            f"${r['underlying_px']:.2f}" if r["underlying_px"] is not None else "—",
            contract, quote, r["run_id"],
        ])
    print()
    print(tabulate(table,
                   headers=["EMITTED (UTC)", "SYM", "DIR", "GR", "SETUP",
                            "OPT", "WHALE", "PX", "CONTRACT", "BID/ASK", "RUN"],
                   tablefmt="simple"))
    print(f"\n  {len(rows)} signal(s).\n")


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


def print_unusual_flow(flow_signals: List[Dict], top_n: int = 10) -> None:
    """Unified unusual flow — non-retail only, sorted by whale score. 0DTE/weekly odd flow surfaces automatically."""
    if not flow_signals:
        return

    # Only show signals with real institutional conviction — filter retail noise
    unusual = [f for f in flow_signals if f.get("whale_score", 0) >= 40 or
               f.get("premium_tier", "retail") in ("block", "whale")]
    if not unusual:
        return

    unusual.sort(key=lambda f: (f.get("whale_score", 0), f.get("total_flow", 0)), reverse=True)

    sep = Fore.WHITE + Style.BRIGHT + "─" * 100 + Style.RESET_ALL
    print(sep)

    net_calls = sum(f["call_flow"] for f in flow_signals)
    net_puts  = sum(f["put_flow"]  for f in flow_signals)
    net_total = net_calls + net_puts
    call_pct  = net_calls / net_total * 100 if net_total > 0 else 50
    net_bar   = fmt_bias_bar(net_calls, net_puts, width=12)
    net_c     = Fore.CYAN if net_calls >= net_puts else Fore.YELLOW
    net_dir   = net_c + ("CALL HEAVY" if net_calls >= net_puts else "PUT HEAVY") + Style.RESET_ALL
    print(
        Fore.WHITE + Style.BRIGHT + "  UNUSUAL FLOW  " + Style.RESET_ALL
        + f"[{net_bar}] {call_pct:.0f}% calls  {net_dir}"
        + f"  |  {fmt_flow(net_total)} total  {len(unusual)} unusual signals"
    )

    rows = []
    for f in unusual[:top_n]:
        tc     = f.get("top_contract")
        voi    = tc.get("vol_oi", 0) if tc else 0
        dte    = tc.get("dte", 0) if tc else 0
        ws     = f.get("whale_score", 0)

        # DTE label — highlight short-dated unusual flow
        if dte == 0:    dte_s = Fore.RED + Style.BRIGHT + "0DTE" + Style.RESET_ALL
        elif dte <= 7:  dte_s = Fore.RED + f"{dte}DTE" + Style.RESET_ALL
        elif dte <= 30: dte_s = Fore.YELLOW + f"{dte}DTE" + Style.RESET_ALL
        else:           dte_s = Fore.WHITE + f"{dte}DTE" + Style.RESET_ALL

        bias_c = Fore.CYAN if f["flow_bias"] == "call" else Fore.YELLOW
        bias_s = bias_c + f["flow_bias"].upper() + Style.RESET_ALL
        tier_c = _TIER_COLORS.get(f.get("premium_tier", "retail"), Fore.WHITE)

        flag = ""
        if f.get("golden_sweep"):         flag = Fore.RED + Style.BRIGHT + "★GOLDEN" + Style.RESET_ALL
        elif tc and tc.get("sweep"):      flag = Fore.YELLOW + "SWEEP" + Style.RESET_ALL
        elif voi >= 20:                   flag = Fore.RED + f"x{voi:.0f}VOI" + Style.RESET_ALL
        elif voi >= 10:                   flag = Fore.YELLOW + f"x{voi:.0f}VOI" + Style.RESET_ALL

        contract_s = fmt_flow_contract(tc) if tc else "—"
        rows.append([
            fmt_whale_score(ws),
            Fore.WHITE + Style.BRIGHT + f["ticker"] + Style.RESET_ALL,
            fmt_flow(f["total_flow"]),
            bias_s,
            dte_s,
            tier_c + f.get("premium_tier", "retail").upper() + Style.RESET_ALL,
            flag or "—",
            contract_s,
        ])
    print(tabulate(rows,
                   headers=["WHALE", "TICKER", "$FLOW", "BIAS", "DTE", "TIER", "FLAG", "CONTRACT"],
                   tablefmt="simple"))
    print(sep)


# keep these for backward compat / direct calls
def print_inline_flow(flow_signals: List[Dict], top_n: int = 7) -> None:
    print_unusual_flow(flow_signals, top_n=top_n)


def print_hot_contracts(flow_signals: List[Dict], top_n: int = 14) -> None:
    print_unusual_flow(flow_signals, top_n=top_n)
