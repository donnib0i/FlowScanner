"""
core/scanner.py -- Elite Market Scanner v2.

The scan is split across focused modules; this file is the front door that
re-exports them, so `from core.scanner import X` keeps working and there is
one place to see everything the scanner exposes.

  constants    tables and thresholds
  market_data  quotes, chains, VIX, flow provenance
  fmt          terminal formatting helpers
  technicals   breakouts, key levels, gaps
  options      contract math, scoring, and selection
  sectors      sector scans, laggards, heatmaps
  flow         options-flow scanning (TastyTrade / yfinance)
  pipeline     per-ticker scan, enrich, filter, sort
  report       tables, panels, CSV output
  cli          argument parsing and the interactive loop

Edit the module that owns the behavior; add its new public names here.
"""
# Run directly as a script (`python3 core/scanner.py`) and the repo root is not
# on sys.path yet, so the `core.` imports below would fail. Fix that first.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import runtime as _runtime  # noqa: F401,E402

# Re-exported so `core.scanner` stays the single import surface it has always
# been. Names are grouped by owning module.

from core.constants import (  # noqa: F401
    DEEP_ITM_PCT,
    DEEP_ITM_VOL,
    FILTER_LABELS,
    FILTER_MAP,
    LADDER_DELTA_MAX,
    LADDER_DELTA_MIN,
    LADDER_MIN_OI,
    LADDER_MIN_VOL,
    MIN_MID,
    MIN_OI,
    MIN_VOL,
    RS_THRESH,
    RS_VOL,
    SECTOR_ETFS,
    SORT_LABELS,
    SORT_MAP,
    TICKER_SECTOR,
    UNIVERSE,
    WIDE_SPREAD_PCT,
    _BASE_FIELDS,
    _GRADE_COLORS,
    _TIER_COLORS,
)

from core.market_data import (  # noqa: F401
    _CHAIN_CACHE_MAX,
    _CHAIN_TTL_S,
    _FLOW_SOURCE,
    _YF_SESSION,
    _YF_TICKER_MAP,
    _cffi_requests,
    _chain_cache,
    _extract_ticker_hist,
    _fetch_batch_history,
    _fetch_live_prices,
    _get_spy_change,
    _option_chain,
    _quotes_for,
    _set_flow_source,
    _yf,
    _yf_ticker,
    clear_chain_cache,
    fetch_dynamic_universe,
    fetch_vix,
    get_flow_source,
    reset_flow_source,
    vix_delta_target,
)

from core.fmt import (  # noqa: F401
    color_change,
    color_gap,
    fmt_bias_bar,
    fmt_flow,
    fmt_num,
    fmt_voi,
    fmt_vol,
    fmt_whale_score,
    grade_letter,
    grade_score,
    level_str,
    score_bar,
    score_colored,
    trade_grade,
)

from core.technicals import (  # noqa: F401
    classify_breakout,
    find_key_levels,
    find_unfilled_gaps,
)

from core.options import (  # noqa: F401
    _itm_pct,
    _nan0,
    _num,
    _score_contract,
    bs_delta,
    calc_iv_rank_proxy,
    calc_iv_skew,
    calc_options_score,
    classify_trade_side,
    contract_economics,
    contract_quality,
    fmt_contract,
    fmt_flow_contract,
    get_best_contract,
    get_contract_display,
    get_spread_tier,
    ladder_rows,
    norm_cdf,
)

from core.sectors import (  # noqa: F401
    _HEATMAP_CACHE,
    _HEATMAP_TTL,
    _PLAYS_CACHE,
    _PLAYS_TTL,
    find_sector_laggards,
    rank_breakout_constituents,
    scan_sectors,
    sector_breakout_plays,
    sector_heatmap,
    top_individual_laggard,
)

from core.flow import (  # noqa: F401
    _TT_AVAILABLE,
    _TT_PASS,
    _TT_USER,
    _scan_options_flow_yf,
    _tt_last_error,
    _tt_load_creds,
    calc_whale_score,
    merge_whale_scores,
    scan_dark_pool_prints,
    scan_options_flow,
    scan_options_flow_tt,
)

from core.pipeline import (  # noqa: F401
    _gap_fill_pct,
    _process_ticker,
    annotate_calibration,
    apply_filter,
    apply_forward_directions,
    apply_sort,
    build_setups,
    enrich_contracts,
    get_forward_direction,
    record_scan_signals,
    scan_tickers,
)

from core.report import (  # noqa: F401
    export_csv,
    print_hot_contracts,
    print_inline_flow,
    print_inline_inside_days,
    print_inline_laggards,
    print_options_flow,
    print_sector_heatmap,
    print_sector_laggards,
    print_signal_history,
    print_summary,
    print_unusual_flow,
    print_whale_alerts,
    render_table,
)

from core.cli import (  # noqa: F401
    build_parser,
    interactive_loop,
    load_tv_universe,
    main,
    print_tv_drops,
)


# Domain names the scanner has always re-exported from its dependencies.
from core.universe import get_universe, ANCHOR, universe_summary  # noqa: F401
from core.market_calendar import is_market_open, minutes_to_close  # noqa: F401
from core.tv_universe import (  # noqa: F401
    DEFAULT_CAP as TV_DEFAULT_CAP,
    drop_report as tv_drop_report,
    load_tradingview_csv,
)
from data.sector_constituents import constituents_for, SECTORS  # noqa: F401


if __name__ == "__main__":
    main()
