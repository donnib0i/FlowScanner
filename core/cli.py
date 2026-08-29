"""
core/cli.py -- Command-line entry point: argument parsing, the interactive loop, and
the TradingView universe loader.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from colorama import Fore, Style
from core.tv_universe import (
    DEFAULT_CAP as TV_DEFAULT_CAP,
    drop_report as tv_drop_report,
    load_tradingview_csv,
)
from core.universe import get_universe, universe_summary
from typing import Optional, List, Dict
import argparse
import sys
import os

from core.constants import FILTER_LABELS, FILTER_MAP, SORT_LABELS, SORT_MAP
from core.market_data import fetch_vix, vix_delta_target
from core.sectors import find_sector_laggards, scan_sectors
from core.flow import merge_whale_scores, scan_options_flow
from core.pipeline import (
    apply_forward_directions,
    enrich_contracts,
    record_scan_signals,
    scan_tickers,
)
from core.report import (
    export_csv,
    print_inline_flow,
    print_inline_inside_days,
    print_inline_laggards,
    print_sector_heatmap,
    print_sector_laggards,
    print_signal_history,
    print_summary,
    print_unusual_flow,
    render_table,
)


# ─── Interactive Menu ─────────────────────────────────────────────────────────
def interactive_loop(results: List[Dict], args: argparse.Namespace,
                     sector_data: Dict[str, Dict], vix: float = -1.0,
                     flow_cache: Optional[List[Dict]] = None) -> None:
    sort_by    = args.sort   or "setup"
    filter_by  = args.filter or "all"
    tickers    = [r["ticker"] for r in results]
    laggards   = find_sector_laggards(results, sector_data)
    flow_cache = list(flow_cache) if flow_cache else []

    while True:
        os.system("clear" if os.name == "posix" else "cls")
        print_sector_heatmap(sector_data)
        print_summary(results, vix=vix)

        # ── Inline priority sections ──────────────────────────────────────────
        print_inline_inside_days(results)
        print_inline_laggards(results, sector_data, top_n=5)
        if flow_cache:
            print_inline_flow(flow_cache, top_n=6)

        sep = Fore.WHITE + Style.BRIGHT + "─" * 88 + Style.RESET_ALL
        print(sep)
        print(
            f"  Filter: {Fore.CYAN}{FILTER_LABELS[filter_by]}{Style.RESET_ALL}"
            f"  |  Sort: {Fore.CYAN}{SORT_LABELS[sort_by]}{Style.RESET_ALL}\n"
        )
        table, _ = render_table(results, sort_by=sort_by, filter_by=filter_by)
        print(table)
        print(Fore.WHITE
              + "\n  FILTER  [1]All  [2]Gap  [3]Inside  [4]Hi-Vol  [5]Opt>=60"
                "  [6]Any Setup  [7]Grade A  [8]Laggards"
              + Style.RESET_ALL)
        print(Fore.WHITE
              + "  SORT    [s1]Setup  [s2]OptScore  [s3]RelVol"
                "  [s4]Gap%  [s5]Change%  [s6]LagScore"
              + Style.RESET_ALL)
        flow_note = (f"  {Fore.GREEN}flow:{len(flow_cache)} signals{Style.RESET_ALL}"
                     if flow_cache else
                     f"  {Fore.YELLOW}[e] to load flow{Style.RESET_ALL}")
        print(Fore.WHITE
              + "  [r]Rescan  [rs]Sectors  [e]Enrich+Flow"
                "  [l]Laggards  [f]Flow  [c]CSV  [q]Quit"
              + Style.RESET_ALL + flow_note)

        try:
            cmd = input("\n  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Bye.")
            break

        if cmd == "q":
            print("  Bye.")
            break
        elif cmd == "rs":
            sector_data = scan_sectors()
            laggards = find_sector_laggards(results, sector_data)
        elif cmd == "r":
            print(f"\n  Rescanning sectors + {len(tickers)} tickers...")
            vix = fetch_vix()
            sector_data = scan_sectors()
            results = scan_tickers(tickers)
            apply_forward_directions(results, sector_data)
            laggards = find_sector_laggards(results, sector_data)
            enrich_contracts(results, top_n=getattr(args, "enrich_top", 15), vix=vix)
            flow_tickers = [r["ticker"] for r in
                            sorted(results, key=lambda x: x["opt_score"], reverse=True)[:15]]
            print(f"  Scanning options flow for {len(flow_tickers)} tickers...")
            flow_cache = scan_options_flow(flow_tickers, show_progress=True)
        elif cmd == "e":
            enrich_contracts(results, top_n=getattr(args, "enrich_top", 15), vix=vix)
            flow_tickers = [r["ticker"] for r in
                            sorted(results, key=lambda x: x["opt_score"], reverse=True)[:15]]
            print(f"  Scanning options flow for {len(flow_tickers)} tickers...")
            flow_cache = scan_options_flow(flow_tickers, show_progress=True)
        elif cmd == "l":
            os.system("clear" if os.name == "posix" else "cls")
            print_sector_laggards(laggards, sector_data)
            input("\n  (press enter to continue)")
        elif cmd == "f":
            os.system("clear" if os.name == "posix" else "cls")
            flow_tickers = [r["ticker"] for r in
                            sorted(results, key=lambda x: x["opt_score"], reverse=True)[:20]]
            print(f"\n  Scanning options flow for {len(flow_tickers)} tickers...")
            flow_cache = scan_options_flow(flow_tickers, show_progress=True)
            merge_whale_scores(results, flow_cache)
            print_unusual_flow(flow_cache, top_n=10)
            input("\n  (press enter to continue)")
        elif cmd == "c":
            export_csv(results)
            input("  (press enter)")
        elif cmd in FILTER_MAP:
            filter_by = FILTER_MAP[cmd]
        elif cmd in SORT_MAP:
            sort_by = SORT_MAP[cmd]


# ─── Entry Point ──────────────────────────────────────────────────────────────
def load_tv_universe(path: str, cap: int = TV_DEFAULT_CAP):
    """
    Load a TradingView screener export as the universe, reporting what it found.

    Exits on an unreadable or unusable file rather than falling back to the
    built-in universe: the user asked for *his* list, and quietly scanning a
    different one is worse than telling him the file did not work.
    """
    try:
        tv = load_tradingview_csv(path, cap=cap if cap and cap > 0 else None)
    except OSError as exc:
        print(Fore.RED + f"  Could not read TradingView CSV: {exc}" + Style.RESET_ALL)
        sys.exit(1)

    if not tv.symbols:
        print(Fore.RED + f"  No symbols found in {path}." + Style.RESET_ALL)
        print(Fore.YELLOW + "  Expected a TradingView screener export with a "
                            "'Symbol' or 'Ticker' column." + Style.RESET_ALL)
        sys.exit(1)

    print(f"  {Fore.CYAN}TradingView universe:{Style.RESET_ALL} {tv.summary()}")
    for line in tv.detail_lines():
        print(f"    {Fore.YELLOW}{line}{Style.RESET_ALL}")
    return tv


def print_tv_drops(tv, requested: List[str], survived: List[str], reason: str) -> None:
    """Print a drop report if the TradingView universe lost symbols. No-op otherwise."""
    if tv is None:
        return
    note = tv_drop_report(requested, survived, reason)
    if note:
        print(f"  {Fore.YELLOW}{note}{Style.RESET_ALL}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Elite market scanner — gap fills, key levels, options contracts."
    )
    parser.add_argument("--tickers",    nargs="+", metavar="TICKER",
                        help="Specific tickers to scan")
    parser.add_argument("--watchlist",  metavar="FILE",
                        help="Text file with one ticker per line")
    parser.add_argument("--tradingview", "--tv", metavar="FILE", dest="tradingview",
                        help="TradingView screener CSV export — use its symbols, "
                             "in its order, as the universe")
    parser.add_argument("--tv-limit",   type=int, default=TV_DEFAULT_CAP, metavar="N",
                        help=f"Max symbols to take from a TradingView export, from the "
                             f"top of its ordering (default: {TV_DEFAULT_CAP}, 0 = no cap)")
    parser.add_argument("--csv",        action="store_true",
                        help="Print table, export CSV, and exit")
    parser.add_argument("--no-enrich",  action="store_true",
                        help="Skip options chain fetching (faster)")
    parser.add_argument("--enrich-top", type=int, default=20,
                        help="Enrich top N setups with contract data (default: 20)")
    parser.add_argument("--filter",     choices=list(FILTER_LABELS.keys()), default="all",
                        help="Initial filter (default: all)")
    parser.add_argument("--sort",       choices=list(SORT_LABELS.keys()),   default="setup",
                        help="Initial sort (default: setup)")
    parser.add_argument("--dynamic",    action="store_true",
                        help="Add today's movers from extended watchlist to find sleepers")
    parser.add_argument("--live",       action="store_true",
                        help="Launch the live FlowDeck terminal dashboard")
    parser.add_argument("--interval",   type=int, default=45,
                        help="Live refresh interval in seconds (default: 45)")
    # Signal-journal inspection. Expressed as flags rather than an argparse
    # subparser because this CLI has always been flat — adding subcommands now
    # would break every existing `scanner.py --tickers ...` invocation.
    parser.add_argument("--signals",       action="store_true",
                        help="Dump recorded signal history and exit")
    parser.add_argument("--signals-since", metavar="ISO",
                        help="Only signals emitted on/after this UTC date or timestamp")
    parser.add_argument("--signals-until", metavar="ISO",
                        help="Only signals emitted on/before this UTC date or timestamp")
    parser.add_argument("--signals-symbol", metavar="TICKER",
                        help="Only signals for this symbol")
    parser.add_argument("--signals-grade", metavar="GRADE",
                        choices=["A", "B", "C", "D"],
                        help="Only signals with this letter grade")
    parser.add_argument("--signals-limit", type=int, default=50,
                        help="Max signal rows to print (default: 50)")
    parser.add_argument("--no-journal",    action="store_true",
                        help="Do not record this scan's signals to the journal")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Reading history touches no market data — answer it before any network work.
    if getattr(args, "signals", False):
        print_signal_history(start=args.signals_since, end=args.signals_until,
                             symbol=args.signals_symbol, grade=args.signals_grade,
                             limit=args.signals_limit)
        return

    if getattr(args, "live", False):
        from core.live_flow import run as run_live
        run_live(interval=args.interval,
                 top=getattr(args, "enrich_top", 30) or 30,
                 min_score=35)
        return

    # Held across the scan so that, when the universe came from TradingView, we
    # can account for every symbol the user handed us instead of just showing
    # him a shorter table than the list he exported.
    tv_universe = None

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif getattr(args, "tradingview", None):
        tv_universe = load_tv_universe(args.tradingview, cap=getattr(args, "tv_limit", TV_DEFAULT_CAP))
        tickers = tv_universe.symbols
    elif args.watchlist:
        try:
            with open(args.watchlist) as f:
                tickers = [ln.strip().upper() for ln in f if ln.strip()]
        except FileNotFoundError:
            print(Fore.RED + f"  File not found: {args.watchlist}" + Style.RESET_ALL)
            sys.exit(1)
    else:
        print(f"  {Fore.CYAN}Building live universe...{Style.RESET_ALL}", end="", flush=True)
        tickers = get_universe()
        sys.stdout.write(f"\r  {universe_summary()}\n")
        sys.stdout.flush()

    # Step 0: fetch VIX — sets IV regime for contract selection
    print(f"  {Fore.CYAN}Fetching VIX...{Style.RESET_ALL}", end="", flush=True)
    vix = fetch_vix()
    if vix > 0:
        sys.stdout.write(f"\r  VIX: {vix:.2f}  δ-target: {vix_delta_target(vix):.2f}\n")
    else:
        sys.stdout.write("\r  VIX: unavailable\n")

    # Step 1: sectors first — know the macro before the micros
    sector_data = scan_sectors()

    print(Fore.WHITE + Style.BRIGHT + f"\n  Scanning {len(tickers)} tickers...\n" + Style.RESET_ALL)
    results = scan_tickers(tickers)

    if not results:
        print(Fore.RED + "  No data fetched. Check tickers or internet connection." + Style.RESET_ALL)
        sys.exit(1)

    print_tv_drops(tv_universe, tickers, [r["ticker"] for r in results],
                   "no price data from the scanner's feed")

    # Step 2: apply forward-looking direction using sector context
    apply_forward_directions(results, sector_data)

    # Step 3: tag sector laggards
    find_sector_laggards(results, sector_data)

    # Step 4: enrich contracts + auto-scan flow so first view is fully loaded
    flow_results: List[Dict] = []
    if not args.no_enrich:
        enrich_contracts(results, top_n=args.enrich_top, vix=vix)
        # Only the enriched subset was ever asked for a chain, so that subset —
        # not the whole universe — is the honest denominator here.
        _enriched = [r for r in results if "contract" in r]
        print_tv_drops(tv_universe,
                       [r["ticker"] for r in _enriched],
                       [r["ticker"] for r in _enriched if r.get("contract")],
                       "no options chain")
        flow_tickers = [r["ticker"] for r in
                        sorted(results, key=lambda x: x["opt_score"], reverse=True)[:15]]
        print(f"  {Fore.CYAN}Scanning options flow ({len(flow_tickers)} tickers)...{Style.RESET_ALL}")
        flow_results = scan_options_flow(flow_tickers, show_progress=True)
        merge_whale_scores(results, flow_results)

    # Step 5: journal what this scan just decided, before anything is printed or
    # the interactive loop starts mutating scores. Recorded for the whole result
    # set, not only the rows the current filter happens to show — the filter is a
    # view, and an attribution pass needs the population the scanner actually saw.
    if not args.no_journal:
        record_scan_signals(results, params={
            "tickers":     len(tickers),
            "enrich_top":  args.enrich_top,
            "no_enrich":   args.no_enrich,
            "dynamic":     args.dynamic,
            "filter":      args.filter,
            "sort":        args.sort,
            "vix":         vix,
            "watchlist":   args.watchlist,
            "explicit_tickers": bool(args.tickers),
        })

    if args.csv:
        print_sector_heatmap(sector_data)
        print_summary(results, vix=vix)
        table, _ = render_table(results, sort_by=args.sort, filter_by=args.filter)
        print(f"\n  Filter: {FILTER_LABELS[args.filter]}  |  Sort: {SORT_LABELS[args.sort]}\n")
        print(table)
        export_csv(results)
        return

    interactive_loop(results, args, sector_data, vix=vix, flow_cache=flow_results)
