"""
core/options.py -- Option math and contract selection: Black-Scholes delta, the strike
ladder, IV rank/skew, contract scoring, and the tradeability gates.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from colorama import Fore, Style
from core.market_calendar import is_market_open, minutes_to_close
from datetime import datetime
from typing import Optional, List, Dict, Tuple
import math
import pandas as pd

from core.constants import (
    DEEP_ITM_PCT,
    DEEP_ITM_VOL,
    LADDER_DELTA_MAX,
    LADDER_DELTA_MIN,
    LADDER_MIN_OI,
    LADDER_MIN_VOL,
    MIN_MID,
    MIN_OI,
    MIN_VOL,
    WIDE_SPREAD_PCT,
)
from core.market_data import _option_chain, _yf, vix_delta_target
from core.fmt import fmt_flow, fmt_num


def norm_cdf(x: float) -> float:
    """Abramowitz & Stegun approximation (max error 7.5e-8)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    p = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    c = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * p
    return c if x >= 0 else 1.0 - c


def bs_delta(S: float, K: float, T: float, sigma: float, opt_type: str = "call") -> float:
    """Black-Scholes delta without scipy."""
    T     = max(T, 1.0 / (365 * 1440))   # floor: 1 minute
    sigma = max(sigma, 0.05)
    if S <= 0 or K <= 0:
        return (1.0 if opt_type == "call" else -1.0) if S > K else 0.0
    # Near expiry: d1 → ±∞ and delta collapses to 0/1. Use limit directly.
    if T < 0.0001:   # < ~52 minutes — digital payoff regime
        if opt_type == "call":
            return 1.0 if S >= K else 0.0
        else:
            return -1.0 if S <= K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    d  = norm_cdf(d1)
    return d if opt_type == "call" else d - 1.0


def _num(v, default=0.0) -> float:
    """float() that survives None/NaN/strings."""
    try:
        f = float(v)
        return default if f != f else f   # NaN != NaN
    except (TypeError, ValueError):
        return default


def ladder_rows(raw, opt_type: str, price: float, dte: int,
                top_n: int = 8) -> List[Dict]:
    """
    Clean, score and rank one side of an option chain for the calls-vs-puts ladder.

    `raw` is an iterable of chain rows (dicts or a DataFrame's records) carrying
    strike / volume / openInterest / bid / ask / lastPrice / impliedVolatility.

    Ranking is by dollar flow, but only across strikes that are actually
    tradeable. Ranking the whole chain by dollar flow alone surfaces deep-ITM
    contracts — they cost 10x more, so vol x mid puts them on top even when the
    real positioning is at the money. Delta comes from Black-Scholes on the
    chain's own IV, not a moneyness approximation.
    """
    rows: List[Dict] = []
    T = max(dte, 0) / 365.0

    for r in raw:
        strike = _num(r.get("strike"))
        if strike <= 0:
            continue
        vol = int(_num(r.get("volume")))
        oi  = int(_num(r.get("openInterest")))
        if vol < LADDER_MIN_VOL or oi < LADDER_MIN_OI:
            continue

        bid, ask = _num(r.get("bid")), _num(r.get("ask"))
        # Outside RTH yfinance zeroes bid/ask — fall back to the last print.
        mid = (bid + ask) / 2 if bid + ask > 0 else _num(r.get("lastPrice"))
        if mid <= 0:
            continue

        iv = _num(r.get("impliedVolatility"))
        delta = abs(bs_delta(price, strike, T, iv, opt_type))
        if not (LADDER_DELTA_MIN <= delta <= LADDER_DELTA_MAX):
            continue

        rows.append({
            "strike": strike,
            "type": opt_type,
            "vol": vol,
            "oi": oi,
            "mid": round(mid, 2),
            "iv": round(iv * 100, 1),
            "dollar_flow": round(vol * mid * 100, 0),
            "ddoi": round(delta * oi, 0),
            "delta": round(delta, 3),
        })

    rows.sort(key=lambda x: x["dollar_flow"], reverse=True)
    return rows[:top_n]


# ─── Trade Side / Whale Analytics ────────────────────────────────────────────
def classify_trade_side(bid: float, ask: float, last: float) -> str:
    """
    Lee-Ready heuristic: classify whether a trade hit the ask (buyer aggression)
    or the bid (seller aggression).
    Top 25% of spread → 'ask', bottom 25% → 'bid', else → 'mid'.
    """
    if bid <= 0 or ask <= 0 or last <= 0:
        return "mid"
    spread = ask - bid
    if spread <= 0:
        return "mid"
    if last >= ask - spread * 0.25:
        return "ask"   # buyer hitting ask = bullish
    if last <= bid + spread * 0.25:
        return "bid"   # seller hitting bid = bearish
    return "mid"


def calc_iv_skew(calls_df: pd.DataFrame, puts_df: pd.DataFrame, price: float) -> float:
    """
    IV skew = avg call IV − avg put IV (ATM ±5% strikes).
    Positive = calls pricier = bullish positioning.
    Negative = puts pricier = fear / hedging.
    """
    try:
        lo, hi = price * 0.95, price * 1.05
        c_iv = pd.to_numeric(
            calls_df.loc[(calls_df["strike"] >= lo) & (calls_df["strike"] <= hi),
                         "impliedVolatility"],
            errors="coerce",
        ).dropna()
        p_iv = pd.to_numeric(
            puts_df.loc[(puts_df["strike"] >= lo) & (puts_df["strike"] <= hi),
                        "impliedVolatility"],
            errors="coerce",
        ).dropna()
        if c_iv.empty or p_iv.empty:
            return 0.0
        return round(float(c_iv.mean()) - float(p_iv.mean()), 4)
    except Exception:
        return 0.0


# ─── Options score ────────────────────────────────────────────────────────────
def get_spread_tier(avg_vol: float) -> Tuple[str, int, float]:
    if avg_vol > 50_000_000: return "~$0.01", 40, 0.30
    if avg_vol > 10_000_000: return "~$0.05", 32, 0.50
    if avg_vol >  1_000_000: return "~$0.15", 18, 1.00
    return "~$0.40+", 5, 2.00


def calc_options_score(avg_vol: float, ivr_proxy: float) -> int:
    _, sp, tm = get_spread_tier(avg_vol)
    vs = max(0.0, min(30.0, (math.log10(max(avg_vol, 1)) - 4) / (math.log10(1e8) - 4) * 30))
    iv = ivr_proxy * 20.0
    oe = max(avg_vol * tm, 1)
    os_ = max(0.0, min(10.0, (math.log10(oe) - 3) / (math.log10(1e7) - 3) * 10))
    return min(100, max(0, round(sp + vs + iv + os_)))


# ─── IV Rank Proxy (HV20/HV60 ratio) ────────────────────────────────────────
def calc_iv_rank_proxy(hist: pd.DataFrame) -> Dict:
    """
    Uses HV20/HV60 ratio as an IV rank signal (no live options chain needed).
    Returns dict with keys: hv20, hv60, ratio, ivr_score (0-100), label.
      ratio > 1.4  → elevated IV (IVR > 70) — options expensive
      ratio > 1.1  → normal
      ratio < 0.8  → compressed (IVR < 30) — options cheap
    """
    result = {"hv20": 0.0, "hv60": 0.0, "ratio": 1.0, "ivr_score": 50, "label": "NORMAL"}
    try:
        closes = hist["Close"].dropna()
        if len(closes) < 65:
            return result

        def _hv(n: int) -> float:
            c = closes.tail(n + 1)
            if len(c) < n:
                return 0.0
            lr = [math.log(float(c.iloc[i]) / float(c.iloc[i - 1])) for i in range(1, len(c))]
            mean = sum(lr) / len(lr)
            var = sum((x - mean) ** 2 for x in lr) / len(lr)
            return round(math.sqrt(var * 252), 4)

        hv20 = _hv(20)
        hv60 = _hv(60)
        if hv60 <= 0:
            return result

        ratio = round(hv20 / hv60, 3)
        if ratio > 1.4:
            ivr_score = min(100, int(50 + (ratio - 1.0) * 50))
            label = "ELEVATED"
        elif ratio > 1.1:
            ivr_score = int(40 + (ratio - 1.0) * 33)
            label = "NORMAL"
        elif ratio < 0.8:
            ivr_score = min(100, max(0, int(30 * ratio / 0.8)))
            label = "COMPRESSED"
        else:
            ivr_score = int(30 + (ratio - 0.8) * 50)
            label = "NORMAL"

        return {"hv20": hv20, "hv60": hv60, "ratio": ratio,
                "ivr_score": ivr_score, "label": label}
    except Exception:
        return result


def get_contract_display(c: Optional[Dict], market_open: bool = True) -> Dict:
    """
    Return a clean structured dict for web UI display — no ANSI codes.
    Fields: label, exp_short, strike_str, type_char, delta_str, price_str,
            dte_str, stale, ivr_display, vol_oi_str, confidence.
    """
    if not c:
        return {"label": "—", "stale": False, "empty": True}

    exp_short  = c["exp"][5:]  # MM-DD
    type_char  = "C" if c["type"] == "call" else "P"
    strike_str = f"${c['strike']:.0f}" if c["strike"] == int(c["strike"]) else f"${c['strike']:.2f}"
    delta_str  = f"δ{c['delta']:+.2f}"
    dte_str    = "0DTE" if c["dte"] == 0 else f"{c['dte']}DTE"
    stale      = c.get("stale", False) or (c["bid"] == 0 and c["ask"] == 0)

    if stale or not market_open:
        price_str = f"Last: ${c['mid']:.2f}"
    else:
        price_str = f"${c['bid']:.2f}/${c['ask']:.2f}"

    mid_str    = f"${c['mid']:.2f}"
    voi        = c["vol"] / max(c["oi"], 1)
    vol_oi_str = f"x{voi:.1f}" if voi >= 1 else "—"
    label      = f"{exp_short} {strike_str}{type_char} · {delta_str} · {mid_str} · {dte_str}"

    if stale and not market_open:
        status_tag = "AFTER HOURS"
    elif stale:
        status_tag = "STALE"
    else:
        status_tag = ""

    return {
        "label":      label,
        "exp":        c["exp"],
        "exp_short":  exp_short,
        "strike":     c["strike"],
        "strike_str": strike_str,
        "type":       c["type"],
        "type_char":  type_char,
        "delta":      round(c["delta"], 3),
        "delta_str":  delta_str,
        "bid":        c["bid"],
        "ask":        c["ask"],
        "mid":        c["mid"],
        "mid_str":    mid_str,
        "price_str":  price_str,
        "dte":        c["dte"],
        "dte_str":    dte_str,
        "stale":      stale,
        "status_tag": status_tag,
        "vol":        c["vol"],
        "oi":         c["oi"],
        "vol_oi_str": vol_oi_str,
        "iv":           round(c.get("iv", 0), 3),
        "roi":          c.get("roi", 0),
        "score":        c.get("score", 0),
        "target_price": c.get("target_price"),
        "empty":        False,
    }


# ─── Options Contract Finder ──────────────────────────────────────────────────
def _nan0(v):
    """Convert a value to float, treating None/NaN as 0."""
    try:
        f = float(v)
        return 0.0 if f != f else f   # f != f is True only for NaN
    except Exception:
        return 0.0


def _score_contract(row: pd.Series, S: float, T: float, direction: str,
                    target_delta: float = 0.45, dte_mode: str = "all",
                    target_price: float = 0.0) -> float:
    """Score a contract row. When target_price is set, strike proximity to that
    target and OI/volume at that strike become the dominant ranking factors —
    matching how a trader picks the strike at their gap/level/ATH target."""
    try:
        K     = float(row["strike"])
        iv    = _nan0(row.get("impliedVolatility", 0.3)) or 0.3
        oi    = int(_nan0(row.get("openInterest", 0)))
        cvol  = int(_nan0(row.get("volume", 0)))
        bid   = _nan0(row.get("bid", 0))
        ask   = _nan0(row.get("ask", 0))
        mid   = (bid + ask) / 2 if (bid + ask) > 0 else float(row.get("lastPrice", 0) or 0)
        stale = bid == 0 and ask == 0   # market closed / no quote

        if mid <= 0.05:
            return -1.0

        # Hard reject illiquid contracts — no point scoring zero-OI junk
        if oi < 50 and cvol < 25:
            return -1.0

        # Penalize stale quotes (market closed / no live bid-ask) — still score but discount
        stale_penalty = 0.60 if stale else 1.0

        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        # Index options (SPX/MDY) have tighter natural spreads relative to premium;
        # high-priced instruments: use 10% spread limit vs 40% for stocks
        spread_limit = 0.10 if S > 500 else 0.40
        if spread_pct > spread_limit:
            return -1.0

        otype = "call" if direction == "up" else "put"
        delta = abs(bs_delta(S, K, T, iv, otype))

        # Hard reject deep ITM contracts — delta > 0.65 means the contract moves
        # like the stock. You're paying for intrinsic value, not leverage.
        if delta > 0.65:
            return -1.0

        # Expected 1-sigma move (annualised IV → daily move for this DTE window)
        sigma_move  = S * max(iv, 0.05) * math.sqrt(max(T, 1.0 / 365))
        if otype == "call":
            ev_1sigma = max(0.0, (S + sigma_move) - K)
        else:
            ev_1sigma = max(0.0, K - (S - sigma_move))
        # ROI at 1-sigma target, capped to prevent micro-priced outliers distorting rank
        roi_score = min(1.0, max(0.0, (ev_1sigma - mid) / (mid + 0.01)) / 5.0)

        # Liquidity: OI + volume (log-scaled). Heavy OI at a level = institutional agreement.
        vol_oi_ratio = cvol / max(oi, 1)
        voi_score    = min(1.0, math.log10(max(vol_oi_ratio, 1.0)) / math.log10(50))
        delta_score  = max(0.0, 1.0 - abs(delta - target_delta) / 0.15)
        liq_score    = min(1.0, math.log10(max(oi + cvol + 1, 1)) / 5.5)
        # Soft OI penalty — thin contracts still score, just lower
        if oi < 200 and cvol < 100:
            liq_score *= 0.75
        elif oi < 500 and cvol < 200:
            liq_score *= 0.88
        spread_score = max(0.0, 1.0 - spread_pct * 2.5)

        # Soft penalty for expensive contracts (>$10 mid) — still ranked, just lower priority
        price_penalty = 1.0 if mid <= 10.0 else max(0.6, 1.0 - (mid - 10.0) / 40.0)

        # Strike gravity — round number strikes attract institutional OI (gamma levels)
        if K % 50 == 0:
            strike_gravity = 1.08
        elif K % 25 == 0:
            strike_gravity = 1.05
        elif K % 10 == 0:
            strike_gravity = 1.03
        elif K % 5 == 0:
            strike_gravity = 1.01
        else:
            strike_gravity = 1.0

        # ── Target-price mode ─────────────────────────────────────────────────
        # When caller provides a target (unfilled gap, heavy level, ATH), the
        # strike closest to that target with the most OI/volume wins.
        # Target score decays with distance as a % of the underlying price.
        if target_price > 0:
            dist_pct   = abs(K - target_price) / max(target_price, 1.0)
            # Full score within 1% of target; drops to 0 at 8% away
            target_score = max(0.0, 1.0 - dist_pct / 0.08)
            # Heavy OI at the target strike = market sees the same level
            heavy_oi_score = min(1.0, math.log10(max(oi + 1, 1)) / 4.5)
            # Weights: target proximity 40%, OI at target 30%, spread 15%, liq 15%
            return (target_score * 40 + heavy_oi_score * 30 +
                    spread_score * 15 + liq_score * 15) * stale_penalty * price_penalty * strike_gravity

        return (roi_score * 20 + delta_score * 15 + liq_score * 35 + spread_score * 15 + voi_score * 15) * stale_penalty * price_penalty * strike_gravity
    except Exception:
        return -1.0


def get_best_contract(ticker: str, direction: str, price: float,
                      vix: float = -1.0, top_n: int = 1,
                      dte_mode: str = "all",
                      target_price: float = 0.0) -> Optional[Dict]:
    """
    direction: "up" → calls, "down" → puts.
    dte_mode: "0dte" (same-day only), "weekly" (2–7 DTE), "monthly" (8–45 DTE), "all" (no constraint)
    VIX is used to set the ideal delta target (high VIX → further OTM).
    Returns the contract with the best composite score or None.
    """
    try:
        t = _yf(ticker)

        # Guard against stale/missing price — always fetch live.
        try:
            live = float(t.fast_info.last_price or 0)
            if live > 0 and (price <= 0 or abs(live - price) / price > 0.02):
                price = live
        except Exception:
            pass

        exps = t.options
        if not exps:
            return None

        today = datetime.now().date()

        def dte(e: str) -> int:
            return (datetime.strptime(e, "%Y-%m-%d").date() - today).days

        # Allow 0DTE during market hours (9:30–16:00 ET); exclude outside hours
        # Holiday- and early-close-aware; weekday() < 5 called Thanksgiving a session.
        _market_open = is_market_open()
        min_dte = 0 if _market_open else 1
        future = [e for e in exps if dte(e) >= min_dte]

        # Filter candidates by dte_mode
        if dte_mode == "0dte":
            cands = [e for e in future if dte(e) == 0]
            if not cands and not _market_open:
                cands = [e for e in future if dte(e) <= 1]  # after hours: next day
        elif dte_mode == "weekly":
            cands = (
                [e for e in future if 2 <= dte(e) <= 7]
                or [e for e in future if dte(e) <= 14]
            )
        elif dte_mode == "monthly":
            cands = [e for e in future if 8 <= dte(e) <= 45]
            if not cands:
                cands = [e for e in future if dte(e) <= 90]
        else:  # "all" — no constraint
            cands = list(future)
        if not cands:
            cands = list(future[:2])
        if not cands:
            cands = list(exps[:2])

        # Base delta target varies by DTE mode; VIX further adjusts within each mode
        _mode_delta = {"0dte": 0.45, "weekly": 0.38, "monthly": 0.30, "all": 0.38}
        target_delta = _mode_delta.get(dte_mode, 0.45)
        # Apply VIX overlay only if VIX is known and moves the target further OTM
        vix_dt = vix_delta_target(vix)
        if vix > 0:
            target_delta = min(target_delta, vix_dt)
        scored: List[tuple] = []   # (score, contract_dict)

        for exp in cands[:3]:
            d = dte(exp)
            # For 0DTE: real minutes left in the session, not an arbitrary floor.
            # minutes_to_close honours the 13:00 ET half sessions — assuming a
            # 16:00 close on those days overstates remaining time by 3 hours,
            # which inflates every 0DTE time-value estimate.
            if d == 0:
                mins_left = max(1.0, minutes_to_close() or 60.0)
                T = (mins_left / 1440.0) / 365.0
            else:
                T = d / 365.0
            T = max(T, 1.0 / (1440 * 365))   # absolute floor: 1 minute
            try:
                chain = _option_chain(t, ticker, exp)
            except Exception:
                continue
            df = chain.calls if direction == "up" else chain.puts
            if df.empty:
                continue

            # Narrow to ±15% moneyness; tight for high-price instruments (SPX ±4% = ±$220)
            # SPX ~$5500 at ±15% = ±$825 — way too wide; use ±4% for price > $500
            # MDY ~$643 at ±15% = ±$96 — still wide; ±4% gives ±$26 which is reasonable
            if price > 500:
                moneyness_band = 0.05 if vix > 30 else 0.04
            else:
                moneyness_band = 0.20 if vix > 30 else 0.15
            df = df[
                (df["strike"] >= price * (1 - moneyness_band)) &
                (df["strike"] <= price * (1 + moneyness_band))
            ]
            if df.empty:
                continue

            for _, row in df.iterrows():
                sc = _score_contract(row, price, T, direction, target_delta, dte_mode, target_price)
                if sc <= 0:
                    continue
                K     = float(row["strike"])
                iv    = _nan0(row.get("impliedVolatility", 0.3)) or 0.3
                oi    = int(_nan0(row.get("openInterest", 0)))
                cvol  = int(_nan0(row.get("volume", 0)))
                bid   = _nan0(row.get("bid", 0))
                ask   = _nan0(row.get("ask", 0))
                mid   = (bid + ask) / 2 if (bid + ask) > 0 else _nan0(row.get("lastPrice", 0))
                stale = bid == 0 and ask == 0
                otype = "call" if direction == "up" else "put"
                delta = bs_delta(price, K, T, iv, otype)
                # Expected ROI at 1-sigma move (for display)
                sigma_move_raw = price * max(iv, 0.05) * math.sqrt(max(T, 1.0 / 365))
                if otype == "call":
                    ev_raw = max(0.0, (price + sigma_move_raw) - K)
                else:
                    ev_raw = max(0.0, K - (price - sigma_move_raw))
                roi_pct = round((ev_raw - mid) / (mid + 0.01) * 100, 1) if mid > 0 else 0.0

                scored.append((sc, {
                    "exp":          exp,
                    "dte":          d,
                    "strike":       K,
                    "type":         otype,
                    "delta":        delta,
                    "iv":           iv,
                    "oi":           oi,
                    "vol":          cvol,
                    "bid":          bid,
                    "ask":          ask,
                    "mid":          mid,
                    "stale":        stale,
                    "score":        round(sc, 1),
                    "roi":          roi_pct,
                    "target_price": round(target_price, 2) if target_price else None,
                }))

        # Sort by score descending; tiebreak by OI descending (contracts within 3 pts prefer higher OI)
        scored.sort(key=lambda x: (-x[0], -x[1].get("oi", 0)))
        top = [c for _, c in scored[:top_n]]
        if not top:
            return None
        return top[0] if top_n == 1 else top
    except Exception:
        return None


def fmt_contract(c: Optional[Dict]) -> str:
    if not c:
        return "—"
    exp_s  = c["exp"][5:]                         # MM-DD
    ctype  = "C" if c["type"] == "call" else "P"
    cc     = Fore.CYAN if ctype == "C" else Fore.YELLOW
    dc     = Fore.GREEN if c["type"] == "call" else Fore.YELLOW
    delta  = f"{dc}δ{c['delta']:+.2f}{Style.RESET_ALL}"
    vol_s  = fmt_num(c["vol"])
    oi_s   = fmt_num(c["oi"])
    dte_s  = f"{c['dte']}DTE" if c["dte"] > 0 else "0DTE"
    stale_tag = f" {Fore.RED}[STALE]{Style.RESET_ALL}" if c.get("stale") else ""
    if c.get("stale") or (c["bid"] == 0 and c["ask"] == 0):
        price_s = f"last${c['mid']:.2f}"
    else:
        price_s = f"${c['bid']:.2f}/${c['ask']:.2f}"
    # VOI — the headline signal (vol/OI ratio)
    voi = c["vol"] / max(c["oi"], 1)
    if voi >= 20:   vc = Fore.RED + Style.BRIGHT
    elif voi >= 10: vc = Fore.RED
    elif voi >= 5:  vc = Fore.YELLOW + Style.BRIGHT
    elif voi >= 2:  vc = Fore.YELLOW
    else:           vc = Fore.WHITE
    voi_s = f"{vc}x{voi:.1f}{Style.RESET_ALL}"
    return (
        f"{cc}{exp_s} ${c['strike']:.0f}{ctype}{Style.RESET_ALL}"
        f"  {voi_s}  {delta}  {price_s}  V:{vol_s}/OI:{oi_s}  {Fore.WHITE}{dte_s}{Style.RESET_ALL}"
        f"{stale_tag}"
    )


def _itm_pct(strike: float, opt_type: str, spot: float) -> float:
    """How far in the money, as a percentage of spot. 0.0 when out of the money."""
    if spot <= 0:
        return 0.0
    intrinsic = (spot - strike) if opt_type == "call" else (strike - spot)
    return max(0.0, intrinsic / spot * 100.0)


def contract_quality(c: Dict, spot: float, market_open: bool = True) -> Tuple[bool, str]:
    """
    (ok, reason) — whether a contract represents tradeable directional flow.

    Deliberately NOT an extrinsic-value test. Extrinsic ratio rejects the AMD
    575P correctly and the TSLA 355P incorrectly: a stale mid can sit below
    intrinsic on the most heavily traded contract of the day. Distance from
    spot plus liquidity is the rule that holds up against real data.
    """
    if spot <= 0:
        return True, ""   # nothing to judge against; dropping would hide real flow

    dte = c.get("dte", -1)
    if dte == 0 and not market_open:
        return False, "expired — 0DTE after the close"

    mid = float(c.get("mid", 0) or 0)
    if mid <= MIN_MID:
        return False, "no premium"

    vol = int(c.get("vol", 0) or 0)
    oi  = int(c.get("oi", 0) or 0)

    # Depth is checked before liquidity: a junk contract usually trips both, and
    # "deep ITM (26%) on 160 lots" explains why its premium looked large, which
    # bare "illiquid" does not.
    itm = _itm_pct(float(c.get("strike", 0) or 0), c.get("type", "call"), spot)
    if itm > DEEP_ITM_PCT and vol < DEEP_ITM_VOL:
        return False, f"deep ITM ({itm:.0f}%) on {vol} lots"

    if oi < MIN_OI and vol < MIN_VOL:
        return False, "illiquid"

    return True, ""


def contract_economics(c: Dict, spot: float) -> Dict:
    """
    What the contract needs in order to pay: breakeven, the move to reach it,
    where the strike sits against spot, and how much of the price is spread.

    pct_to_breakeven is signed against the *direction of the trade*: negative
    means spot is already through breakeven, positive means it still has to get
    there. A put's breakeven is below its strike, so the arithmetic differs.
    """
    strike = float(c.get("strike", 0) or 0)
    mid    = float(c.get("mid", 0) or 0)
    otype  = c.get("type", "call")
    bid    = float(c.get("bid", 0) or 0)
    ask    = float(c.get("ask", 0) or 0)

    breakeven = strike + mid if otype == "call" else strike - mid

    if spot > 0:
        raw = (breakeven - spot) / spot * 100.0
        # A call needs spot to rise to breakeven; a put needs it to fall.
        pct_to_be = raw if otype == "call" else -raw
        moneyness = (strike - spot) / spot * 100.0
    else:
        pct_to_be = None
        moneyness = None

    spread_pct = None
    if bid > 0 and ask > 0 and mid > 0:
        spread_pct = (ask - bid) / mid * 100.0

    return {
        "breakeven":        round(breakeven, 2),
        "pct_to_breakeven": round(pct_to_be, 2) if pct_to_be is not None else None,
        "moneyness_pct":    round(moneyness, 2) if moneyness is not None else None,
        "spread_pct":       round(spread_pct, 2) if spread_pct is not None else None,
        "wide_spread":      bool(spread_pct is not None and spread_pct >= WIDE_SPREAD_PCT),
    }


def fmt_flow_contract(c: Optional[Dict]) -> str:
    if not c:
        return "—"
    ctype = "C" if c["type"] == "call" else "P"
    cc    = Fore.CYAN if ctype == "C" else Fore.YELLOW
    sweep = f" {Fore.RED}[SWEEP]{Style.RESET_ALL}" if c.get("sweep") else ""
    dte_s = f"{c['dte']}DTE" if c.get("dte", 1) > 0 else "0DTE"
    price_s = f"${c['mid']:.2f}"
    return (
        f"{cc}{c['exp'][5:]} ${c['strike']:.0f}{ctype}{Style.RESET_ALL}"
        f"  {Fore.WHITE}{dte_s}{Style.RESET_ALL}"
        f"  vol:{fmt_num(c['vol'])}  OI:{fmt_num(c['oi'])}"
        f"  x{c['vol_oi']:.1f}  {price_s}  {fmt_flow(c['flow'])}{sweep}"
    )
