"""
core/technicals.py -- Price-structure reads off a history frame: relative-strength breakout
labels, support/resistance levels, and unfilled gaps.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from typing import List, Dict, Tuple
import math
import pandas as pd

from core.constants import RS_THRESH, RS_VOL


def classify_breakout(change_pct: float, spy_chg: float, rel_vol: float) -> tuple:
    """RS vs SPY + breakout label. When spy_chg == 0.0, rs falls back to change_pct
    (same convention as the per-ticker RS calc). Returns (rs_vs_spy, breakout)."""
    rs = round(change_pct - spy_chg, 2) if spy_chg != 0.0 else round(change_pct, 2)
    if rs >= RS_THRESH and change_pct > 0 and rel_vol >= RS_VOL:
        return rs, "up"
    if rs <= -RS_THRESH and change_pct < 0 and rel_vol >= RS_VOL:
        return rs, "down"
    return rs, "none"


# ─── Key Level Detection ──────────────────────────────────────────────────────
def find_key_levels(hist: pd.DataFrame, price: float) -> List[Dict]:
    """
    Detects support/resistance from:
      - Swing highs/lows (5-bar pivot)
      - Yesterday's high/low (key intraday reference)
      - Round numbers (±8%)
    Returns up to 8 levels sorted by proximity, filtered to ±6%.
    """
    if len(hist) < 10:
        return []

    highs    = hist["High"].values.astype(float)
    lows     = hist["Low"].values.astype(float)
    vols     = hist["Volume"].values.astype(float)
    n        = len(hist)
    mean_vol = max(vols.mean(), 1)
    raw: List[Tuple[float, str, float, int]] = []  # (price, type, vol_ratio, bars_ago)

    lb = min(5, n // 4)
    for i in range(lb, n - lb):
        wh = highs[max(0, i - lb) : i + lb + 1]
        wl = lows[max(0, i - lb)  : i + lb + 1]
        if highs[i] >= max(wh) - 0.001:
            raw.append((highs[i], "resistance", vols[i] / mean_vol, n - 1 - i))
        if lows[i] <= min(wl) + 0.001:
            raw.append((lows[i],  "support",    vols[i] / mean_vol, n - 1 - i))

    # Yesterday's extremes — reliable intraday reference
    if n >= 2:
        raw.append((highs[-2], "resistance", vols[-2] / mean_vol * 1.5, 1))
        raw.append((lows[-2],  "support",    vols[-2] / mean_vol * 1.5, 1))

    # Round numbers ±8%
    lo, hi = price * 0.92, price * 1.08
    step = (1.0 if price < 30 else 2.5 if price < 100 else
            5.0 if price < 300 else 10.0 if price < 1000 else
            25.0 if price < 3000 else 50.0)
    rp = math.floor(lo / step) * step
    while rp <= hi:
        if lo <= rp <= hi and abs(rp - price) / price > 0.002:
            raw.append((rp, "resistance" if rp > price else "support", 0.5, 0))
        rp += step

    if not raw:
        return []

    # Cluster within 1% buckets
    raw.sort(key=lambda x: x[0])
    clusters: List[List] = []
    grp = [raw[0]]
    for item in raw[1:]:
        if item[0] / grp[-1][0] - 1 < 0.010:
            grp.append(item)
        else:
            clusters.append(grp)
            grp = [item]
    clusters.append(grp)

    levels = []
    for g in clusters:
        rep   = sum(x[0] for x in g) / len(g)
        dist  = (rep - price) / price * 100
        if abs(dist) < 0.10:       # filter levels too close to current price
            continue

        tot_vol_r  = sum(x[2] for x in g)
        min_bars   = min(x[3] for x in g)
        touches    = len(g)

        # Strength 0–10
        strength = (
            min(4, touches)
            + min(3, int(tot_vol_r))
            + (2 if min_bars <= 3 else 1 if min_bars <= 10 else 0)
            + (1 if abs(rep % step) < step * 0.05 or abs(rep % step - step) < step * 0.05 else 0)
        )
        strength = min(10, strength)

        levels.append({
            "price":    round(rep, 2),
            "type":     "support" if rep < price else "resistance",
            "strength": strength,
            "touches":  touches,
            "dist_pct": dist,
        })

    levels = [l for l in levels if 0.10 < abs(l["dist_pct"]) < 6.0]
    levels.sort(key=lambda x: abs(x["dist_pct"]))
    return levels[:8]


def find_unfilled_gaps(
    hist: pd.DataFrame,
    current_price: float,
    lookback: int = 60,
    min_gap_pct: float = 0.20,
    max_gap_pct: float = 8.0,
) -> List[Dict]:
    """
    Scans historical OHLCV for gaps that have NOT been filled yet.

    Gap definition (Dante's model):
      - Gap UP:   today's open > prior close → leaves a void zone = [prior_close, open]
                  Unfilled if price has NEVER traded back down to prior_close since then.
      - Gap DOWN: today's open < prior close → leaves a void zone = [open, prior_close]
                  Unfilled if price has NEVER traded back up to prior_close since then.

    These open gap zones are price magnets / trade targets, not signals themselves.

    Returns list of unfilled gaps sorted by distance from current price (closest first),
    capped at 5. Each dict includes zone bounds, midpoint, distance, and direction
    relative to current price (above or below) — which tells you the trade direction.
    """
    if hist is None or len(hist) < 10:
        return []

    df = hist.tail(lookback + 5).copy().reset_index(drop=True)
    if len(df) < 5:
        return []

    opens  = df["Open"].values.astype(float)
    highs  = df["High"].values.astype(float)
    lows   = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    n      = len(df)

    unfilled: List[Dict] = []

    for i in range(1, n - 1):  # leave at least 1 bar after to test fill
        prior_close = closes[i - 1]
        this_open   = opens[i]

        if prior_close <= 0:
            continue

        gap_pct = (this_open - prior_close) / prior_close * 100

        if abs(gap_pct) < min_gap_pct or abs(gap_pct) > max_gap_pct:
            continue

        if gap_pct > 0:
            # Gap UP: void zone is [prior_close, this_open] — BELOW this_open
            # Filled if any subsequent LOW came back down to prior_close
            gap_top    = round(float(this_open),   2)
            gap_bottom = round(float(prior_close), 2)
            gap_type   = "gap_up"
            filled = any(float(lows[j]) <= prior_close for j in range(i + 1, n))
        else:
            # Gap DOWN: void zone is [this_open, prior_close] — ABOVE this_open
            # Filled if any subsequent HIGH came back up to prior_close
            gap_top    = round(float(prior_close), 2)
            gap_bottom = round(float(this_open),   2)
            gap_type   = "gap_down"
            filled = any(float(highs[j]) >= prior_close for j in range(i + 1, n))

        if filled:
            continue  # gap was filled at some point — not a target

        # Still open — record it
        gap_mid  = round((gap_top + gap_bottom) / 2, 2)
        dist_pct = round((gap_mid - current_price) / current_price * 100, 2)

        # Direction to trade: gap above = go UP to fill it (calls), gap below = go DOWN (puts)
        direction_to_fill = "up" if gap_mid > current_price else "down"

        bars_ago = n - 1 - i

        unfilled.append({
            "type":              gap_type,           # "gap_up" or "gap_down"
            "top":               gap_top,
            "bottom":            gap_bottom,
            "mid":               gap_mid,
            "gap_pct":           round(abs(gap_pct), 2),
            "dist_pct":          dist_pct,           # + = gap is above price, - = below
            "direction_to_fill": direction_to_fill,  # which way price needs to move
            "bars_ago":          bars_ago,
        })

    if not unfilled:
        return []

    # Deduplicate nearby gaps (within 0.5% of each other — cluster them)
    unfilled.sort(key=lambda g: g["mid"])
    deduped: List[Dict] = [unfilled[0]]
    for g in unfilled[1:]:
        prev = deduped[-1]
        if abs(g["mid"] - prev["mid"]) / prev["mid"] < 0.005:
            # Keep the closer one
            if abs(g["dist_pct"]) < abs(prev["dist_pct"]):
                deduped[-1] = g
        else:
            deduped.append(g)

    # Sort by distance, return up to 5 nearest
    deduped.sort(key=lambda g: abs(g["dist_pct"]))
    return deduped[:5]
