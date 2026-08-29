"""
core/pipeline.py -- The per-ticker scan pipeline: fetch -> process -> enrich -> filter/sort.
`_process_ticker` is the core read; the rest shape its output.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from colorama import Fore, Style
from datetime import datetime
from typing import Optional, List, Dict
import time
import sys
import math
import json
import pandas as pd

from core.constants import TICKER_SECTOR
from core.market_data import (
    _extract_ticker_hist,
    _fetch_batch_history,
    _fetch_live_prices,
    _get_spy_change,
    _option_chain,
    _yf,
)
from core.fmt import grade_letter
from core.technicals import find_key_levels, find_unfilled_gaps
from core.options import (
    calc_iv_rank_proxy,
    calc_options_score,
    get_best_contract,
    get_spread_tier,
)


def get_forward_direction(r: Dict, sector_data: Dict[str, Dict]) -> str:
    """
    Forward-looking direction: what is most likely to happen next.
    Weights: sector bias > gap direction > price location in range > level proximity > vol.
    NOT derived solely from what price already did.
    """
    score_up = 0.0
    score_dn = 0.0

    # 1. Sector bias — the macro tailwind/headwind (highest weight)
    sname = TICKER_SECTOR.get(r["ticker"])
    if sname and sname in sector_data:
        sd  = sector_data[sname]
        mag = abs(sd["strength"])
        if sd["bias"] == "up":
            score_up += mag * 0.6
        else:
            score_dn += mag * 0.6

    # 2. Gap direction — backtest-calibrated (60-day, 2800+ signals)
    # gap_down: fill bias = price recovers UP → bullish → calls
    # gap_up standalone: slight continuation (50.9% up vs 49.1% fade) → mild bullish
    # gap_up + highvol: FADE wins 56.2% → bearish → puts
    if r["gap_flag"] == "gap_down":
        score_up += 2.5   # gap down → fill up → bullish
    elif r["gap_flag"] == "gap_up":
        if r.get("high_vol"):
            score_dn += 3.0   # gap_up + HV = strong fade (Sharpe 2.42 fading)
        else:
            score_up += 1.2   # gap_up alone: slight continuation lean (50.9%)

    # 3. Price location in today's range (upper = buyers in control → calls)
    loc = r.get("price_loc", 0.5)
    if loc > 0.65:
        score_up += 1.5
    elif loc < 0.35:
        score_dn += 1.5

    # 4. Nearest key level — where is price heading?
    nl = r.get("near_level")
    if nl:
        dist     = nl["dist_pct"]   # + = above price (resistance), - = below (support)
        strength = nl["strength"]
        if nl["type"] == "support" and abs(dist) < 2.0 and strength >= 3:
            # Sitting just above support → bounce candidate → calls
            score_up += 0.8
        elif nl["type"] == "resistance" and dist < 2.0 and strength >= 3:
            # Pressing into resistance → could break out (follow sector) or reject
            if score_up > score_dn:
                score_up += 0.5   # sector is up, bet on breakout
            else:
                score_dn += 0.8   # sector weak, bet on rejection

    # 5. High volume confirms the move direction
    if r["high_vol"]:
        if r["change_pct"] > 0:
            score_up += 1.0
        else:
            score_dn += 1.0

    return "up" if score_up >= score_dn else "down"


def apply_forward_directions(results: List[Dict], sector_data: Dict[str, Dict]) -> None:
    """Update direction field for all results using forward-looking logic."""
    for r in results:
        r["direction"] = get_forward_direction(r, sector_data)


def _process_ticker(ticker: str, hist: pd.DataFrame, live_price: float = 0.0, spy_chg: float = 0.0) -> Optional[Dict]:
    """
    Convert pre-fetched OHLCV history into a result dict. Zero network calls.
    `hist` should be 1y of data; last 60 rows used for level detection,
    full range used for IVR proxy (replaces fast_info.year_high/year_low).
    `live_price` is the latest intraday price; used when today's daily Close is NaN.
    """
    try:
        if hist.empty or len(hist) < 5:
            return None

        # Last 60 trading days for gap/inside-day/level detection
        hist60 = hist.tail(60).copy()

        # Handle intraday: if today's Close is NaN (market open), use last complete row
        last = hist60.iloc[-1]
        intraday_open: Optional[float] = None
        if pd.isna(last.get("Close", float("nan"))):
            # Capture today's open for gap detection before dropping the NaN row
            try:
                intraday_open = float(last["Open"]) if not pd.isna(last["Open"]) else None
            except Exception:
                intraday_open = None
            hist60 = hist60.dropna(subset=["Close", "High", "Low"]).copy()
            if len(hist60) < 2:
                return None

        today     = hist60.iloc[-1]
        yesterday = hist60.iloc[-2]

        prior_close = float(yesterday["Close"])

        # Price: use live intraday price if we dropped today's NaN row, else today's close
        if intraday_open is not None and live_price > 0:
            price  = live_price
            open_p = intraday_open
        elif intraday_open is not None:
            price  = float(today["Close"])   # last complete close as fallback
            open_p = intraday_open
        else:
            price  = float(today["Close"])
            open_p = float(today["Open"])
        today_high  = float(today["High"])
        today_low   = float(today["Low"])
        yest_high   = float(yesterday["High"])
        yest_low    = float(yesterday["Low"])
        today_vol   = int(today["Volume"])

        vol_s   = hist60["Volume"].iloc[:-1]
        avg_vol = float(vol_s.tail(20).mean()) if len(vol_s) >= 1 else float(today_vol)

        change_pct = (price - prior_close) / prior_close * 100
        gap_pct    = (open_p - prior_close) / prior_close * 100

        gap_flag: Optional[str] = None
        if abs(gap_pct) > 0.5:
            if gap_pct > 0:
                gap_flag = None if today_low <= prior_close else "gap_up"
            else:
                gap_flag = None if today_high >= prior_close else "gap_down"

        inside_day = today_high < yest_high and today_low > yest_low
        rel_vol    = today_vol / avg_vol if avg_vol > 0 else 0.0
        high_vol   = rel_vol > 1.4

        # Double inside day check
        double_inside_day = False
        if inside_day and len(hist60) >= 3:
            day2 = hist60.iloc[-3]
            double_inside_day = (float(yesterday["High"]) < float(day2["High"]) and
                                 float(yesterday["Low"])  > float(day2["Low"]))

        # Breakout signal: price closed above prior day high (bull) or below prior day low (bear)
        # Requires vol confirmation (>1.2x avg) to filter noise
        breakout: Optional[str] = None
        if not inside_day:
            if price > yest_high and rel_vol > 1.2:
                breakout = "bull"
            elif price < yest_low and rel_vol > 1.2:
                breakout = "bear"

        # RS vs SPY: how much the ticker is outperforming or lagging the market today
        rs_vs_spy = round(change_pct - spy_chg, 2) if spy_chg != 0.0 else 0.0

        # Price location in today's range (0 = at low, 1 = at high)
        day_range = today_high - today_low
        price_loc = (price - today_low) / day_range if day_range > 0 else 0.5

        # HV20: annualized 20-day historical volatility of log returns
        try:
            closes_20 = hist60["Close"].dropna().tail(21)
            if len(closes_20) >= 10:
                log_rets = [math.log(float(closes_20.iloc[i]) / float(closes_20.iloc[i-1]))
                            for i in range(1, len(closes_20))]
                mean_lr = sum(log_rets) / len(log_rets)
                var_lr  = sum((x - mean_lr) ** 2 for x in log_rets) / len(log_rets)
                hv20    = round(math.sqrt(var_lr * 252), 4)
            else:
                hv20 = 0.0
        except Exception:
            hv20 = 0.0

        hv_regime = "volatile" if hv20 >= 0.35 else ("normal" if hv20 >= 0.20 else "calm")

        # ── Ripster EMA Clouds ──────────────────────────────────────────────────
        # EMA9 = short momentum, EMA34 = trend spine, EMA200 = macro direction
        try:
            c_series = hist60["Close"].dropna()
            ema9_val   = float(c_series.ewm(span=9,   adjust=False).mean().iloc[-1])
            ema34_val  = float(c_series.ewm(span=34,  adjust=False).mean().iloc[-1])
            ema200_val = float(c_series.ewm(span=200, adjust=False).mean().iloc[-1])
            # BUG 2: EWM NaN guard — use current price as fallback if EWM returns NaN
            if pd.isna(ema9_val):   ema9_val   = price
            if pd.isna(ema34_val):  ema34_val  = price
            if pd.isna(ema200_val): ema200_val = price
            # EMA34 slope: compare to 3 bars ago
            ema34_prev = float(c_series.ewm(span=34, adjust=False).mean().iloc[-4]) if len(c_series) >= 4 else ema34_val
            if pd.isna(ema34_prev): ema34_prev = ema34_val
            ema_cloud_bull = (price > ema9_val and ema9_val > ema34_val and ema34_val > ema34_prev)
            ema_cloud_bear = (price < ema9_val and ema9_val < ema34_val and ema34_val < ema34_prev)
            above_ema200   = price > ema200_val

            # FIX 8: RSI-14 — computed here alongside EMAs using the same c_series
            delta_close = c_series.diff()
            gain  = delta_close.clip(lower=0).rolling(14).mean()
            loss  = (-delta_close.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, float('nan'))
            rsi14 = float(100 - 100 / (1 + rs.iloc[-1]))
            if pd.isna(rsi14): rsi14 = 50.0
        except Exception:
            ema9_val = ema34_val = ema200_val = 0.0
            ema_cloud_bull = ema_cloud_bear = False
            above_ema200 = True
            rsi14 = 50.0

        # ── Rolling MVWAP (20-bar volume-weighted avg price) ─────────────────
        try:
            h60 = hist60.dropna(subset=["Close", "High", "Low", "Volume"])
            typical_60  = (h60["High"] + h60["Low"] + h60["Close"]) / 3
            mvwap_series = (typical_60 * h60["Volume"]).rolling(20).sum() / h60["Volume"].rolling(20).sum()
            mvwap        = float(mvwap_series.iloc[-1]) if not pd.isna(mvwap_series.iloc[-1]) else None
            mvwap_prev   = float(mvwap_series.iloc[-2]) if len(mvwap_series) >= 2 and not pd.isna(mvwap_series.iloc[-2]) else mvwap
            above_vwap   = bool(price > mvwap) if mvwap else None
            vwap_reclaim = bool(mvwap is not None and mvwap_prev is not None
                                and float(yesterday["Close"]) < mvwap_prev and price >= mvwap)
        except Exception:
            mvwap = mvwap_prev = None
            above_vwap = None
            vwap_reclaim = False

        # IVR proxy: 52-week high/low from downloaded history — no fast_info needed
        # Kept for backward-compat (stored in result dict) but NOT used for options scoring.
        try:
            yr_high   = float(hist["High"].max())
            yr_low    = float(hist["Low"].min())
            ivr_proxy = (price - yr_low) / (yr_high - yr_low) if yr_high > yr_low else 0.5
            ivr_proxy = max(0.0, min(1.0, ivr_proxy))
        except Exception:
            ivr_proxy = 0.5

        # Full IV rank proxy (HV20/HV60 ratio) — authoritative IVR (BUG 3 fix: computed first)
        iv_rank_data = calc_iv_rank_proxy(hist)

        spread_label, _, _ = get_spread_tier(avg_vol)
        # BUG 3: use iv_rank_data["ivr_score"] / 100.0 as the authoritative IVR (0–1 scale)
        # instead of the 52-week scalar ivr_proxy — eliminates duplicate conflicting IVR signals
        opt_score = calc_options_score(avg_vol, iv_rank_data["ivr_score"] / 100.0)

        # Expected move (1-sigma, annualized HV20): ±% of current price
        expected_move_pct = round(hv20 / math.sqrt(252) * 100, 2) if hv20 > 0 else 0.0

        # Key levels (use 60d window — same as before)
        levels     = find_key_levels(hist60, price)
        near_level = next((l for l in levels if l["strength"] >= 2), levels[0] if levels else None)

        # Unfilled historical gaps — price targets/magnets (Dante's gap play model)
        # These are prior session gaps that have never been filled — use as trade targets
        unfilled_gaps   = find_unfilled_gaps(hist60, price)
        nearest_gap     = unfilled_gaps[0] if unfilled_gaps else None

        # ── Signal combo classification (backtest-derived) ──────────────────────
        # Key combos from 60-day backtest on 80 tickers, 2134 signals:
        #   BK+GU+HV+TR (breakout+gap_up+highvol+trend):  85.0% dir WR, +18.3% avg opt P&L ← S TIER
        #   BK+GD+HV+TR (breakout+gap_dn+highvol+trend):  90.0% dir WR  ← S TIER (n=10)
        #   BK+GU (breakout+gap_up):                       75.9% dir WR, +24.0% avg opt P&L ← A TIER
        #   hv_only (no gap/bk/inside) + volatile:         Sharpe 4.94, PF 2.38 ← A TIER
        #   hv + inside + volatile:                        Sharpe 3.07
        #   gap_down + hv + volatile:                      Sharpe 2.91
        #   gap_up + hv (FADE direction):                  Sharpe 2.42  ← note: fade, not continuation
        #   gap_up alone:                                  62.1% dir WR ← B TIER
        #   unfilled_gap alone:                            41.9% dir WR ← AVOID
        #   highvol in calm market (HV20 < 0.30):          PF 0.75 ← LOSING, suppress
        is_hv_only   = high_vol and not gap_flag and not inside_day and not breakout
        is_hv_inside = high_vol and inside_day
        is_hv_gapdn  = high_vol and gap_flag == "gap_down"
        is_hv_gapup  = high_vol and gap_flag == "gap_up"   # fade signal, direction is DOWN
        is_calm_hv   = high_vol and hv_regime == "calm"    # losing in backtest

        # Trend-strong: strong bodied candle (body > 70% of range), meaningful vol
        # Same definition as backtest.py detect_signals() — 85-90% WR when combined with BK+GU/GD
        _body_pct  = abs(price - open_p) / open_p if open_p > 0 else 0.0
        _range_pct = (today_high - today_low) / today_low if today_low > 0 else 0.0
        trend_strong = (_body_pct > 0.025 and
                        _body_pct / max(_range_pct, 0.001) > 0.70 and
                        rel_vol >= 1.3)

        is_bk_gu    = bool(breakout == "bull" and gap_flag == "gap_up")
        is_bk_gd    = bool(breakout == "bear" and gap_flag == "gap_down")

        if is_bk_gu and high_vol and trend_strong:
            signal_combo = "BK+GU+HV+TR"     # 85.0% dir WR — S tier
        elif is_bk_gd and high_vol and trend_strong:
            signal_combo = "BK+GD+HV+TR"     # 90.0% dir WR — S tier
        elif is_bk_gu:
            signal_combo = "BK+GU"            # 75.9% dir WR — A tier
        elif is_hv_only:
            signal_combo = "HV_PURE"
        elif is_hv_inside:
            signal_combo = "HV+ID"
        elif is_hv_gapdn:
            signal_combo = "HV+GD"
        elif is_hv_gapup:
            signal_combo = "HV+GU_FADE"
        elif high_vol and breakout:
            signal_combo = "HV+BK"
        elif breakout:
            signal_combo = "BK"
        elif inside_day:
            signal_combo = "ID"
        elif gap_flag == "gap_down":
            signal_combo = "GD"
        elif gap_flag == "gap_up":
            signal_combo = "GU"
        else:
            signal_combo = ""

        # Combo rank — tiered by backtest-validated direction accuracy
        # S: 80%+ dir WR  A: 70-79%  B: 55-69%  C: <55%  AVOID: <50% avg
        _combo_tier = {
            "BK+GU+HV+TR": "S",   # 85.0% WR
            "BK+GD+HV+TR": "S",   # 90.0% WR
            "BK+GU":        "A",   # 75.9% WR
            "HV_PURE":      "A",   # Sharpe 4.94, volatile only
            "HV+ID":        "A",   # Sharpe 3.07
            "HV+GD":        "B",   # Sharpe 2.91
            "HV+GU_FADE":   "B",   # Sharpe 2.42 (fade)
            "HV+BK":        "B",   # validated but mixed
            "BK":           "B",   # 66.7% WR
            "GU":           "B",   # 62.1% WR
            "GD":           "C",   # 53.4% WR
            "ID":           "C",   # 50.0% WR
        }
        combo_rank = _combo_tier.get(signal_combo, "C")

        # ── Setup quality 0.0–1.0 ────────────────────────────────────────────
        # Weights from backtest. Volatile regime (HV20 >= 0.35) required for full HV credit.
        sq = 0.0

        # Gap signals
        if gap_flag == "gap_down":   sq += 0.28   # PF 1.06 — slight fill edge
        if gap_flag == "gap_up":     sq += 0.10   # PF 0.95 — mostly noise; only useful as fade

        # Core signals
        if inside_day:               sq += 0.20   # PF 1.09

        # Highvol — regime-gated. Edge only exists in volatile stocks (HV20 >= 0.35).
        # Regime analysis (2800+ signals): volatile WR=55.3% PF=1.53 Sharpe=2.48
        #                                   normal   WR=41.4% PF=0.44 Sharpe=-3.58 ← LOSING
        #                                   calm     WR=41.7% PF=0.75 Sharpe=-1.65 ← LOSING
        if high_vol and hv_regime == "volatile":   sq += 0.35
        elif high_vol and hv_regime == "normal":   sq += 0.00   # no edge confirmed by backtest
        elif high_vol and hv_regime == "calm":     sq -= 0.05   # slight penalty — false signal

        # Breakout — regime-gated same way
        # volatile: WR=59.7% PF=1.33 Sharpe=1.65 | calm: WR=36% PF=0.59 Sharpe=-3.2
        if breakout and hv_regime == "volatile":   sq += 0.28
        elif breakout and hv_regime == "normal":   sq += 0.10
        elif breakout and hv_regime == "calm":     sq += 0.00

        # Inside day — INVERTED regime logic. Works in CALM stocks (coiling for breakout).
        # Backtest: calm inside WR=50% PF=2.94 Sharpe=5.44 | volatile inside PF=1.10 Sharpe=0.58
        # Override the base inside_day contribution above
        if inside_day:
            # Remove the flat +0.20 and replace with regime-aware score
            sq -= 0.20
            if hv_regime == "calm":     sq += 0.35   # best inside day setup — coiling in calm
            elif hv_regime == "normal": sq += 0.15
            else:                       sq += 0.12   # volatile inside: moderate

        # Combo bonuses — validated high-edge combinations
        if is_hv_gapdn and hv_regime == "volatile":   sq = min(1.0, sq + 0.12)  # Sharpe 3.1
        if is_hv_inside and hv_regime == "volatile":  sq = min(1.0, sq + 0.10)  # Sharpe 2.7
        if is_hv_gapup:                               sq = min(1.0, sq + 0.08)  # Sharpe 1.76 (fade)
        # New backtest-validated bonuses (60d, 2134 signals):
        if is_bk_gu:                                  sq = min(1.0, sq + 0.25)  # BK+GU 75.9% WR
        if is_bk_gu and high_vol and trend_strong:    sq = min(1.0, sq + 0.10)  # S-tier boost
        if is_bk_gd and high_vol and trend_strong:    sq = min(1.0, sq + 0.35)  # BK+GD+HV+TR 90% WR

        # Level bonuses
        if near_level and near_level["strength"] >= 5:           sq = min(1.0, sq + 0.25)
        elif near_level and near_level["strength"] >= 3:         sq = min(1.0, sq + 0.15)
        elif near_level and near_level["strength"] >= 1:         sq = min(1.0, sq + 0.05)
        if gap_flag and near_level and near_level["strength"] >= 4: sq = min(1.0, sq + 0.20)

        # Unfilled gap bonus: nearby open gap gives a clear target → better edge
        # Bonus scales by proximity: within 2% is strong edge, within 5% is moderate
        if nearest_gap:
            adist = abs(nearest_gap["dist_pct"])
            if adist <= 1.0:   sq = min(1.0, sq + 0.20)   # gap very close — high-conviction target
            elif adist <= 2.5: sq = min(1.0, sq + 0.14)
            elif adist <= 5.0: sq = min(1.0, sq + 0.07)

        # FIX 7: VWAP reclaim → setup quality boost (price reclaimed MVWAP = bullish shift)
        if vwap_reclaim:
            sq = min(1.0, sq + 0.10)

        # FIX 8: RSI-based momentum boost to setup quality
        if rsi14 < 30:
            sq = min(1.0, sq + 0.12)   # oversold — call edge
        elif rsi14 > 70:
            sq = min(1.0, sq + 0.08)   # overbought — put edge (general momentum)

        # Provisional direction — overwritten by apply_forward_directions()
        direction = "up" if change_pct >= 0 else "down"

        return {
            "ticker":        ticker,
            "price":         price,
            "change_pct":    change_pct,
            "gap_pct":       gap_pct,
            "gap_flag":      gap_flag,
            "inside_day":    inside_day,
            "breakout":      breakout,
            "rel_vol":       rel_vol,
            "high_vol":      high_vol,
            "hv20":          hv20,
            "hv_regime":     hv_regime,
            "signal_combo":  signal_combo,
            "combo_rank":    combo_rank,
            "rs_vs_spy":     rs_vs_spy,
            "today_vol":     today_vol,
            "avg_vol":       avg_vol,
            "ivr_proxy":     ivr_proxy,
            "spread_label":  spread_label,
            "opt_score":     opt_score,
            "levels":        levels,
            "near_level":    near_level,
            "unfilled_gaps": unfilled_gaps,
            "nearest_gap":   nearest_gap,
            "setup_q":       sq,
            "price_loc":     price_loc,
            "direction":     direction,
            "contract":      None,   # populated by enrich_contracts()
            "is_laggard":    False,
            "lag_pct":       0.0,
            "lag_score":     0.0,
            "lag_direction": None,
            "open_p":        open_p,
            "prior_close":   prior_close,
            "today_high":    today_high,
            "today_low":     today_low,
            "yest_high":     yest_high,
            "yest_low":      yest_low,
            "iv_rank_data":  iv_rank_data,
            "expected_move_pct": expected_move_pct,
            "rsi14":         rsi14,
            "vwap_reclaim":  vwap_reclaim,
            "above_vwap":    above_vwap,
            "mvwap":         mvwap,
        }
    except Exception:
        return None


def scan_tickers(tickers: List[str], show_progress: bool = True) -> List[Dict]:
    # One batch download for all tickers — no per-ticker rate limiting
    # Always include SPY for RS vs SPY calculation
    download_tickers = tickers if "SPY" in tickers else tickers + ["SPY"]
    if show_progress:
        sys.stdout.write(
            f"  {Fore.CYAN}Downloading {len(tickers)} tickers (batch)...{Style.RESET_ALL}"
        )
        sys.stdout.flush()
    batch = _fetch_batch_history(download_tickers, period="1y")
    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    # Fetch SPY change once for RS calculation across all tickers
    spy_chg = _get_spy_change(batch)

    # Fetch live intraday prices in case today's daily bar is still incomplete (NaN Close)
    live_prices = _fetch_live_prices(tickers)

    results: List[Dict] = []
    total = len(tickers)
    for i, ticker in enumerate(tickers, 1):
        if show_progress:
            done = int(i / total * 24)
            bar  = "█" * done + "░" * (24 - done)
            sys.stdout.write(f"\r  [{bar}] {i}/{total}  {Fore.CYAN}{ticker:<6}{Style.RESET_ALL}  ")
            sys.stdout.flush()
        hist = _extract_ticker_hist(batch, ticker)
        r    = _process_ticker(ticker, hist, live_price=live_prices.get(ticker, 0.0), spy_chg=spy_chg)
        if r:
            results.append(r)
    if show_progress:
        sys.stdout.write("\r" + " " * 72 + "\r")
        sys.stdout.flush()
    return results


def enrich_contracts(results: List[Dict], top_n: int = 20, vix: float = -1.0,
                     dte_mode: str = "all") -> None:
    """
    Fetch options chains for the top-N tickers by setup quality.
    Populates result['contract'] in place. VIX adjusts delta target.
    dte_mode: "0dte" | "weekly" | "monthly" | "all"
    """
    ranked = sorted(
        [r for r in results if r["gap_flag"] or r["inside_day"] or r["high_vol"] or r.get("breakout")],
        key=lambda r: r["setup_q"],
        reverse=True,
    )[:top_n]

    if not ranked:
        print(f"  {Fore.YELLOW}No setups to enrich.{Style.RESET_ALL}")
        return

    print(f"  {Fore.CYAN}Fetching options chains for {len(ranked)} setups...{Style.RESET_ALL}")
    for i, r in enumerate(ranked, 1):
        sys.stdout.write(f"\r  Options [{i}/{len(ranked)}] {Fore.CYAN}{r['ticker']:<6}{Style.RESET_ALL}  ")
        sys.stdout.flush()
        # Derive target price: unfilled gap → heavy level → ATH proxy (none)
        # Unfilled gap: use mid if the gap is in the direction of the trade
        _tgt = 0.0
        ng = r.get("nearest_gap")
        nl = r.get("near_level")
        if ng:
            dtf = ng.get("direction_to_fill", "")
            if (r["direction"] == "up" and dtf == "up") or (r["direction"] == "down" and dtf == "down"):
                _tgt = float(ng.get("mid", 0) or 0)
        if not _tgt and nl and nl.get("strength", 0) >= 3:
            _tgt = float(nl.get("price", 0) or 0)
        r["contract"] = get_best_contract(r["ticker"], r["direction"], r["price"], vix=vix,
                                          dte_mode=dte_mode, target_price=_tgt)

        # FIX 9: IV vs HV comparison — fetch near-ATM call IVs to compute mean_iv
        # and compare against realized HV20. Adjusts opt_score in place.
        try:
            _t = _yf(r["ticker"])
            _exps = _t.options
            if _exps:
                _today = datetime.now().date()
                _near_exp = None
                for _e in _exps:
                    _d = (datetime.strptime(_e, "%Y-%m-%d").date() - _today).days
                    if _d >= 0:
                        _near_exp = _e
                        break
                if _near_exp:
                    _chain = _option_chain(_t, r["ticker"], _near_exp)
                    _calls = _chain.calls
                    _px = r["price"]
                    _atm = _calls[
                        (_calls["strike"] >= _px * 0.97) & (_calls["strike"] <= _px * 1.03)
                    ]
                    if not _atm.empty:
                        _ivs = pd.to_numeric(_atm["impliedVolatility"], errors="coerce").dropna()
                        if not _ivs.empty:
                            mean_iv = float(_ivs.mean())
                            hv20 = r.get("hv20", 0)
                            if hv20 > 0 and mean_iv > 0:
                                iv_vs_hv = mean_iv / hv20
                                _opt = r["opt_score"] / 100.0
                                if iv_vs_hv < 0.8:
                                    _opt = min(1.0, _opt + 0.15)   # options cheap vs realized vol
                                elif iv_vs_hv > 1.3:
                                    _opt = max(0.0, _opt - 0.10)   # options expensive
                                r["opt_score"] = int(round(_opt * 100))
                                r["iv_vs_hv"]  = round(iv_vs_hv, 3)
                                r["mean_iv"]   = round(mean_iv, 4)
        except Exception:
            pass

        time.sleep(0.15)
    sys.stdout.write("\r" + " " * 55 + "\r")
    sys.stdout.flush()


def apply_filter(results: List[Dict], f: str) -> List[Dict]:
    if f == "gap":      return [r for r in results if r["gap_flag"]]
    if f == "inside":   return [r for r in results if r["inside_day"]]
    if f == "highvol":  return [r for r in results if r["high_vol"]]
    if f == "breakout": return [r for r in results if r.get("breakout")]
    if f == "options":  return [r for r in results if r["opt_score"] >= 60]
    if f == "any":     return [r for r in results if (
        r["gap_flag"] or r["inside_day"] or r["high_vol"] or r.get("breakout") or
        r["opt_score"] >= 60 or
        (r.get("near_level") and r["near_level"]["strength"] >= 3)
    )]
    if f == "laggard": return [r for r in results if r.get("is_laggard")]
    if f == "a_grade":
        return [r for r in results
                if r["setup_q"] * 50 + r["opt_score"] * 0.30 + (20 if r["contract"] else 0) >= 75]
    return results


def apply_sort(results: List[Dict], s: str) -> List[Dict]:
    keys: Dict = {
        "setup":   lambda r: r["setup_q"],
        "options": lambda r: r["opt_score"],
        "relvol":  lambda r: r["rel_vol"],
        "gap":     lambda r: abs(r["gap_pct"]),
        "change":  lambda r: abs(r["change_pct"]),
        "lag":     lambda r: r.get("lag_score", 0.0),
        # EV is the right ranking key, but only when a calibrated model exists.
        # Uncalibrated rows carry expected_value=None, so asking for --sort ev
        # without one silently falls back to setup quality rather than ordering
        # every row by a fabricated zero.
        "ev":      lambda r: (r.get("expected_value") if r.get("expected_value")
                              is not None else r["setup_q"] - 1e6),
    }
    if s == "ev" and not any(r.get("expected_value") is not None for r in results):
        s = "setup"
    return sorted(results, key=keys.get(s, keys["setup"]), reverse=True)


# ─── Calibrated probability / expected value ─────────────────────────────────
def annotate_calibration(results: List[Dict]) -> List[Dict]:
    """
    Attach win_prob / expected_value / calibration to each scan result.

    Deliberately non-fatal: if the calibration module or its released model is
    missing, every row is marked "uncalibrated" with None probabilities and the
    order is left alone. The scanner must keep working — and keep telling the
    truth about what it does not know — with no model on disk, which is the
    state it is in today.
    """
    try:
        from core import calibration
    except Exception:
        for r in results:
            r.setdefault("win_prob", None)
            r.setdefault("expected_value", None)
            r.setdefault("calibration", "uncalibrated")
        return results

    model = calibration.load_model()
    payoffs = None
    if model is not None:
        try:
            with open(calibration.MODEL_PATH) as fh:
                payoffs = json.load(fh).get("payoffs")
        except Exception:
            payoffs = None
        payoffs = tuple(payoffs) if payoffs else None
    return calibration.annotate(results, model, payoffs)


# ─── Table Rendering ──────────────────────────────────────────────────────────
def _gap_fill_pct(r: Dict) -> str:
    """Return gap fill % as a string (e.g. '67%') or '' if trivial / uncomputable."""
    open_p = r.get("open_p", 0)
    pc     = r.get("prior_close", 0)
    if not open_p or not pc or abs(open_p - pc) < 0.01:
        return ""
    if r.get("gap_flag") == "gap_up" and open_p > pc:
        tl  = r.get("today_low", r["price"])
        pct = min(100, max(0, (open_p - tl) / (open_p - pc) * 100))
    elif r.get("gap_flag") == "gap_down" and open_p < pc:
        th  = r.get("today_high", r["price"])
        pct = min(100, max(0, (th - open_p) / (pc - open_p) * 100))
    else:
        return ""
    return f"{pct:.0f}%" if pct >= 5 else ""


def build_setups(r: Dict) -> str:
    b = []

    # ── Signal combo badge (backtest-graded) ─────────────────────────────────
    combo = r.get("signal_combo", "")
    regime = r.get("hv_regime", "")
    if combo == "HV_PURE":
        # Pure HV in volatile name — Sharpe 4.94, best setup
        c = Fore.GREEN + Style.BRIGHT if regime == "volatile" else Fore.GREEN
        b.append(c + "HV★" + Style.RESET_ALL)
    elif combo == "HV+ID":
        b.append(Fore.GREEN + Style.BRIGHT + "HV+ID" + Style.RESET_ALL)
    elif combo == "HV+GD":
        b.append(Fore.GREEN + "HV+G↓" + Style.RESET_ALL)
    elif combo == "HV+GU_FADE":
        b.append(Fore.RED + Style.BRIGHT + "HV+G↑FADE" + Style.RESET_ALL)
    elif combo == "HV+BK":
        b.append(Fore.CYAN + "HV+BK" + Style.RESET_ALL)
    else:
        # Individual flags when no special combo
        if r["gap_flag"] == "gap_up":
            fill = _gap_fill_pct(r)
            b.append(Fore.YELLOW + f"G+{fill}" + Style.RESET_ALL)
        if r["gap_flag"] == "gap_down":
            fill = _gap_fill_pct(r)
            b.append(Fore.CYAN + f"G-{fill}" + Style.RESET_ALL)
        if r["inside_day"]:
            b.append(Fore.MAGENTA + Style.BRIGHT + "ID" + Style.RESET_ALL)
        if r["high_vol"]:
            hv_c = Fore.GREEN if regime == "volatile" else (Fore.YELLOW if regime == "normal" else Fore.WHITE)
            b.append(hv_c + "HV" + Style.RESET_ALL)
        bk = r.get("breakout")
        if bk == "bull": b.append(Fore.CYAN + "BK↑" + Style.RESET_ALL)
        elif bk == "bear": b.append(Fore.RED + "BK↓" + Style.RESET_ALL)

    # HV regime warning — calm HV = no edge
    if r.get("high_vol") and regime == "calm":
        b.append(Fore.WHITE + Style.DIM + "[calm]" + Style.RESET_ALL)

    # Level strength
    nl = r.get("near_level")
    if nl and nl["strength"] >= 5:   b.append(Fore.RED + Style.BRIGHT + "**" + Style.RESET_ALL)
    elif nl and nl["strength"] >= 3: b.append(Fore.RED + "*" + Style.RESET_ALL)
    elif nl:                         b.append(Fore.WHITE + "L" + Style.RESET_ALL)

    if r.get("is_laggard"):
        lag_c = Fore.CYAN if r.get("lag_direction") == "up" else Fore.YELLOW
        b.append(lag_c + f"LAG{r['lag_pct']:+.0f}%" + Style.RESET_ALL)

    rs = r.get("rs_vs_spy", 0.0)
    if rs >= 2.0:    b.append(Fore.GREEN  + f"RS+{rs:.1f}" + Style.RESET_ALL)
    elif rs <= -2.0: b.append(Fore.YELLOW + f"RS{rs:.1f}"  + Style.RESET_ALL)

    return " ".join(b) if b else "—"


def record_scan_signals(results: List[Dict], params: Dict,
                        run_id: Optional[str] = None,
                        journal=None, scan_kind: str = "scan",
                        verbose: bool = True) -> Optional[str]:
    """Persist a scan's signals. Returns the run id, or None if nothing was
    written. Never raises — every failure path prints and returns None."""
    try:
        from data.signal_journal import SignalJournal, new_run_id
        j = journal or SignalJournal()
        rid = run_id or new_run_id()
        j.start_run(rid, scan_kind, params)
        n = j.record_signals(
            rid, results,
            grade_fn=lambda r: grade_letter(r.get("setup_q", 0.0),
                                            r.get("opt_score", 0),
                                            bool(r.get("contract"))),
        )
        if verbose:
            print(f"  {Fore.CYAN}Journaled {n} signals → run {rid}{Style.RESET_ALL}")
        return rid
    except Exception as e:
        # Non-fatal by design. Say so loudly enough to notice, then carry on.
        print(f"  {Fore.YELLOW}Signal journal write failed ({e}) — scan continues."
              f"{Style.RESET_ALL}")
        return None
