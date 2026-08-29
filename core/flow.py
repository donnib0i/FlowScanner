"""
core/flow.py -- Options-flow scanning. TastyTrade live feed when it authenticates,
15-minute-delayed yfinance otherwise -- and the record of which ran.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from colorama import Fore, Style
from core.market_calendar import is_market_open
from datetime import datetime
from typing import List, Dict
import time
import sys
import math
import pandas as pd

from core.market_data import (
    _extract_ticker_hist,
    _fetch_batch_history,
    _option_chain,
    _set_flow_source,
    _yf,
)
from core.options import (
    calc_iv_skew,
    classify_trade_side,
    contract_economics,
    contract_quality,
)


# ─── TastyTrade real flow (drop-in replacement for yfinance scan) ─────────────
try:
    from data.tt_flow import (
        scan_options_flow_tt,
        load_credentials as _tt_load_creds,
        last_error as _tt_last_error,
    )
    _TT_USER, _TT_PASS = _tt_load_creds()
    # Credentials being *present* says nothing about the session authenticating.
    _TT_AVAILABLE = bool(_TT_USER and _TT_PASS)
except Exception:
    _TT_AVAILABLE = False
    scan_options_flow_tt = None
    def _tt_last_error() -> str:
        return "tt_flow module unavailable"


def calc_whale_score(signal: Dict) -> int:
    """
    Composite 0–100 institutional signal score. Log-scaled dollar flow base
    prevents a $1M print from being treated the same as $10M.
      Base (log-scaled flow): 0–60
      Side multiplier:        ask=1.0x, bid/mid=0.75x
      Expiry bonus:           0DTE-7DTE +15, <=30DTE +5
      Unusual vol/OI:         vol_oi >= 10 → +10
    """
    flow = signal.get("total_flow", 0)
    if flow <= 0:
        return 0
    base = min(60, max(0, int(math.log10(max(flow, 1000) / 1000) / 3 * 60)))
    side_mult = 1.0 if signal.get("trade_side") == "ask" else 0.75
    expiry_bonus = 15 if signal.get("dte", 99) <= 7 else 5 if signal.get("dte", 99) <= 30 else 0
    unusual_bonus = 10 if signal.get("vol_oi_ratio", 0) >= 10 else 0
    return min(100, int(base * side_mult) + expiry_bonus + unusual_bonus)


# ─── Options Flow Scanner (Enhanced) ─────────────────────────────────────────
def scan_options_flow(tickers: List[str], show_progress: bool = True,
                      on_signal=None, on_progress=None) -> List[Dict]:
    """
    Detect unusual options activity per ticker.

    Tries the TastyTrade OPRA stream first and falls back to yfinance. Whichever
    one produced the returned signals is recorded via get_flow_source() so the
    UI can label the data honestly instead of trusting that credentials exist.
    """
    if _TT_AVAILABLE and scan_options_flow_tt is not None:
        try:
            if show_progress:
                sys.stdout.write(f"\r  {Fore.GREEN}[TT LIVE]{Style.RESET_ALL} Streaming real options flow...\n")
                sys.stdout.flush()
            tt_signals = scan_options_flow_tt(
                tickers, _TT_USER, _TT_PASS,
                window_secs=90, max_dte=14,
                show_progress=show_progress,
            )
            if tt_signals:
                _set_flow_source("tastytrade-live", "live OPRA trade prints")
                return tt_signals
            err = _tt_last_error()
            if err:
                reason = f"TastyTrade unavailable ({err})"
            elif not is_market_open():
                reason = "market closed — no live prints to collect"
            else:
                reason = "TastyTrade returned no prints"
            if show_progress:
                sys.stdout.write(f"  [TT] {reason} — falling back to yfinance\n")
        except Exception as e:
            reason = f"TastyTrade error: {e}"
            if show_progress:
                sys.stdout.write(f"  [TT] {reason} — falling back to yfinance\n")
    else:
        reason = "TastyTrade credentials not configured"

    signals = _scan_options_flow_yf(
        tickers, show_progress=show_progress,
        on_signal=on_signal, on_progress=on_progress,
    )
    _set_flow_source("yfinance-delayed", reason)
    return signals


def _scan_options_flow_yf(tickers: List[str], show_progress: bool = True,
                          on_signal=None, on_progress=None) -> List[Dict]:
    """
    yfinance flow path: a 15-minute-delayed daily snapshot, not trade prints.
    Enhanced vs CheddarFlow/Unusual Whales:
      - Trade side classification (bid/ask aggression)
      - Premium tiers: retail / institutional ($100K+) / block ($500K+) / whale ($1M+)
      - Golden sweep: vol > OI×10, at ask, flow > $100K
      - Stacked flow: 3+ unique unusual strikes = institutional position building
      - IV skew: call vs put implied vol differential
      - DTE breakdown: 0DTE / 1-7DTE / 8+DTE flow buckets
      - Whale score: composite 0-100 signal strength
    Sorted by whale_score descending.
    """
    flow_signals: List[Dict] = []
    today = datetime.now().date()

    def dte_of(e: str) -> int:
        return (datetime.strptime(e, "%Y-%m-%d").date() - today).days

    # Detect market hours — after close, bid/ask are stale zeros; use lastPrice instead
    try:
        import zoneinfo
        _et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except ImportError:
        import pytz
        _et = datetime.now(pytz.timezone("America/New_York"))
    _market_open = is_market_open()

    for i, ticker in enumerate(tickers, 1):
        if show_progress:
            sys.stdout.write(
                f"\r  Flow [{i}/{len(tickers)}] {Fore.CYAN}{ticker:<6}{Style.RESET_ALL}  "
            )
            sys.stdout.flush()
        if on_progress:
            on_progress({"ticker": ticker, "i": i, "n": len(tickers)})
        try:
            t    = _yf(ticker)
            exps = t.options
            if not exps:
                continue

            near_exps = [e for e in exps if 0 <= dte_of(e) <= 14] or list(exps[:2])

            call_flow = put_flow = 0.0
            dte0_flow = dte1_7_flow = dte8p_flow = 0.0
            filtered_n = 0
            filtered_premium = 0.0
            filtered_reasons: List[str] = []
            top_call = top_put = None
            call_contracts: List[Dict] = []
            put_contracts:  List[Dict] = []
            all_unusual_strikes: List[float] = []
            all_calls_dfs: List[pd.DataFrame] = []
            all_puts_dfs:  List[pd.DataFrame] = []

            # Live price for IV skew calculation
            try:
                cur_price = float(t.fast_info.last_price or 0)
            except Exception:
                cur_price = 0.0

            for exp in near_exps[:3]:
                d = dte_of(exp)
                try:
                    chain = _option_chain(t, ticker, exp)
                except Exception:
                    continue

                all_calls_dfs.append(chain.calls)
                all_puts_dfs.append(chain.puts)

                for df, otype in [(chain.calls, "call"), (chain.puts, "put")]:
                    if df.empty:
                        continue
                    df = df.copy()
                    df["vol_n"]  = pd.to_numeric(df["volume"],       errors="coerce").fillna(0).clip(lower=0)
                    df["oi_n"]   = pd.to_numeric(df["openInterest"], errors="coerce").fillna(0).clip(lower=0)
                    df["bid_n"]  = pd.to_numeric(df["bid"],          errors="coerce").fillna(0).clip(lower=0)
                    df["ask_n"]  = pd.to_numeric(df["ask"],          errors="coerce").fillna(0).clip(lower=0)
                    df["last_n"] = pd.to_numeric(
                        df.get("lastPrice", pd.Series(dtype=float)), errors="coerce"
                    ).fillna(0).clip(lower=0)
                    mid_base     = (df["bid_n"] + df["ask_n"]) / 2
                    df["mid_n"]  = mid_base.where(mid_base > 0, df["last_n"])
                    df["vol_oi"] = df["vol_n"] / df["oi_n"].clip(lower=1)
                    df["flow"]   = df["vol_n"] * df["mid_n"] * 100
                    df["has_mkt"]= (df["bid_n"] > 0) & (df["ask_n"] > 0)

                    # After hours: bid/ask are 0 but volume/OI data is still valid.
                    # Use lastPrice as mid and skip live-market-quote requirement.
                    mkt_filter = df["has_mkt"] if _market_open else (df["mid_n"] > 0.05)
                    unusual = df[
                        (df["vol_n"]    >= 50) &
                        (df["oi_n"]     >= 10) &
                        (df["vol_oi"]   >= 2.0) &
                        (df["flow"]     >= 5_000) &
                        (df["mid_n"]    >  0.05) &
                        mkt_filter
                    ]

                    for _, row in unusual.iterrows():
                        bid_v  = float(row["bid_n"])
                        ask_v  = float(row["ask_n"])
                        last_v = float(row["last_n"])
                        mid_v  = float(row["mid_n"])
                        vol_n  = int(row["vol_n"])
                        oi_n   = int(row["oi_n"])
                        flow_v = float(row["flow"])
                        strike = float(row["strike"])

                        trade_side = classify_trade_side(bid_v, ask_v, last_v)
                        is_sweep   = bool(vol_n > oi_n * 5)
                        is_golden  = bool(
                            vol_n > oi_n * 10 and
                            trade_side == "ask" and
                            flow_v >= 100_000
                        )

                        if flow_v >= 1_000_000:   tier = "whale"
                        elif flow_v >= 500_000:   tier = "block"
                        elif flow_v >= 100_000:   tier = "institutional"
                        else:                     tier = "retail"

                        entry = {
                            "ticker":       ticker,
                            "exp":          exp,
                            "dte":          d,
                            "strike":       strike,
                            "type":         otype,
                            "vol":          vol_n,
                            "oi":           oi_n,
                            "vol_oi":       round(float(row["vol_oi"]), 1),
                            "mid":          round(mid_v, 2),
                            "bid":          round(bid_v, 2),
                            "ask":          round(ask_v, 2),
                            "flow":         round(flow_v, 0),
                            "sweep":        is_sweep,
                            "golden_sweep": is_golden,
                            "trade_side":   trade_side,
                            "premium_tier": tier,
                        }
                        entry.update(contract_economics(entry, cur_price))

                        # Premium measures conviction only if the contract could
                        # carry any. A deep-ITM contract nobody traded books
                        # millions on its price alone and drags the bias with it,
                        # so junk is excluded from the totals — but counted, and
                        # reported, rather than silently dropped.
                        ok, why = contract_quality(entry, cur_price, _market_open)
                        if not ok:
                            filtered_n += 1
                            filtered_premium += flow_v
                            if why not in filtered_reasons:
                                filtered_reasons.append(why)
                            continue

                        all_unusual_strikes.append(strike)

                        # DTE bucket
                        if d == 0:      dte0_flow    += flow_v
                        elif d <= 7:    dte1_7_flow  += flow_v
                        else:           dte8p_flow   += flow_v

                        if otype == "call":
                            call_flow += flow_v
                            call_contracts.append(entry)
                            if top_call is None or flow_v > top_call["flow"]:
                                top_call = entry
                        else:
                            put_flow += flow_v
                            put_contracts.append(entry)
                            if top_put is None or flow_v > top_put["flow"]:
                                top_put = entry

            total_flow = call_flow + put_flow
            if total_flow < 5_000:
                continue

            # IV skew — use only the closest expiry (mixing multiple DTEs distorts skew)
            iv_skew = 0.0
            if all_calls_dfs and all_puts_dfs and cur_price > 0:
                try:
                    iv_skew = calc_iv_skew(all_calls_dfs[0], all_puts_dfs[0], cur_price)
                except Exception:
                    pass

            bias           = "call" if call_flow >= put_flow else "put"
            top_contract   = top_call if bias == "call" else top_put
            unique_strikes = len(set(round(s, 0) for s in all_unusual_strikes))
            stacked_flow   = unique_strikes >= 3
            golden_sweep   = any(c.get("golden_sweep") for c in call_contracts + put_contracts)
            dom_side       = top_contract.get("trade_side", "mid") if top_contract else "mid"
            top_tier       = top_contract.get("premium_tier", "retail") if top_contract else "retail"

            signal = {
                # Original fields (backward-compatible)
                "ticker":         ticker,
                "call_flow":      call_flow,
                "put_flow":       put_flow,
                "total_flow":     total_flow,
                "flow_bias":      bias,
                "pc_ratio":       put_flow / call_flow if call_flow > 0 else 999.0,
                "top_call":       top_call,
                "top_put":        top_put,
                "top_contract":   top_contract,
                "call_contracts": call_contracts,
                "put_contracts":  put_contracts,
                # Enhanced fields
                "trade_side":     dom_side,
                "iv_skew":        iv_skew,
                "stacked_flow":   stacked_flow,
                "unique_strikes": unique_strikes,
                "golden_sweep":   golden_sweep,
                "premium_tier":   top_tier,
                "dte0_flow":      dte0_flow,
                "dte1_7_flow":    dte1_7_flow,
                "dte8p_flow":     dte8p_flow,
                "whale_score":    0,  # computed below
                # Spot travels with the signal so downstream consumers can place
                # strikes against price without re-fetching a quote.
                "spot":             round(cur_price, 2),
                "filtered_n":       filtered_n,
                "filtered_premium": round(filtered_premium, 0),
                "filtered_reasons": filtered_reasons,
            }
            signal["whale_score"] = calc_whale_score(signal)

            # FIX 6: IV skew adjustment — apply to flow-level options confidence
            # iv_skew = avg_call_IV - avg_put_IV (from calc_iv_skew)
            # Negative skew = puts pricier = fear/hedging → reduce call edge
            # Positive skew = calls pricier = bullish positioning → boost call edge
            _skew_adj = 0.0
            if iv_skew < -0.05 and signal["flow_bias"] == "call":
                _skew_adj = -0.10   # bearish fear positioning — penalize call recommendations
            elif iv_skew > 0.05 and signal["flow_bias"] == "call":
                _skew_adj = 0.05    # bullish skew — slight call edge boost
            signal["iv_skew_options_adj"] = round(_skew_adj, 2)

            flow_signals.append(signal)
            if on_signal:
                on_signal(signal)
        except Exception:
            pass
        time.sleep(0.15)

    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    # Sort by whale score (strength of institutional signal) then dollar flow
    flow_signals.sort(key=lambda x: (x["whale_score"], x["total_flow"]), reverse=True)
    return flow_signals


# ─── Dark Pool Approximation ──────────────────────────────────────────────────
def scan_dark_pool_prints(tickers: List[str], show_progress: bool = True) -> List[Dict]:
    """
    Detect potential dark pool / off-exchange block prints in equity data.
    Heuristic: today's volume ≥ 2.5× 20-day avg AND intraday price move < 0.5%.
    Price-insensitive volume = characteristic of institutional off-exchange execution.
    Returns list sorted by relative volume descending.
    """
    prints: List[Dict] = []

    if show_progress:
        sys.stdout.write(
            f"  {Fore.CYAN}Dark pool scan ({len(tickers)} tickers, batch)...{Style.RESET_ALL}"
        )
        sys.stdout.flush()
    batch = _fetch_batch_history(tickers, period="30d")
    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    for ticker in tickers:
        try:
            hist = _extract_ticker_hist(batch, ticker)
            if hist.empty or len(hist) < 5:
                continue

            hist = hist.dropna(subset=["Close", "High", "Low", "Volume"]).copy()
            if len(hist) < 5:
                continue

            today_row  = hist.iloc[-1]
            vol_today  = int(today_row["Volume"])
            open_p     = float(today_row["Open"])
            close_p    = float(today_row["Close"])
            high_p     = float(today_row["High"])
            low_p      = float(today_row["Low"])
            avg_vol_20 = float(hist["Volume"].iloc[:-1].tail(20).mean())

            if avg_vol_20 <= 0:
                continue

            rel_vol    = vol_today / avg_vol_20
            intra_move = abs(close_p - open_p) / open_p * 100
            day_range  = (high_p - low_p) / max(low_p, 0.01) * 100

            if rel_vol < 2.5 or intra_move >= 0.5:
                continue

            prints.append({
                "ticker":     ticker,
                "price":      round(close_p, 2),
                "rel_vol":    round(rel_vol, 2),
                "intra_move": round(intra_move, 3),
                "day_range":  round(day_range, 3),
                "vol_today":  vol_today,
                "avg_vol_20": int(avg_vol_20),
                "dollar_vol": round(vol_today * close_p, 0),
                "dp_bias":    "accumulation" if close_p >= open_p else "distribution",
            })
        except Exception:
            pass

    prints.sort(key=lambda x: x["rel_vol"], reverse=True)
    return prints


def merge_whale_scores(results: List[Dict], flow_signals: List[Dict]) -> None:
    """Inject whale scores from flow scan back into main results so whaled tickers rank higher."""
    whale_map = {f["ticker"]: f.get("whale_score", 0) for f in flow_signals}
    for r in results:
        ws = whale_map.get(r["ticker"], 0)
        # Keep the raw score on the result. Previously it was consumed here and
        # discarded, which meant the whale read that pushed a ticker up the
        # rankings was invisible to anything downstream — including the journal.
        r["whale_score"] = ws
        if ws >= 60:
            r["opt_score"] = min(100, r.get("opt_score", 0) + 15)
            r["setup_q"]   = min(1.0, r.get("setup_q",   0) + 0.12)
        elif ws >= 40:
            r["opt_score"] = min(100, r.get("opt_score", 0) + 8)
            r["setup_q"]   = min(1.0, r.get("setup_q",   0) + 0.06)
