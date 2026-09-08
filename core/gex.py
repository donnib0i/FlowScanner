"""
core/gex.py -- Dealer gamma exposure: strike-level gamma notional, the
zero-gamma flip, and the call/put walls.

Convention, stated because vendors disagree and the reader needs to know which
one they are looking at:

    gamma_notional(K) = gamma(K) * OI(K) * 100 * S^2 * 0.01

Units: dollars of delta dealers must hedge per 1% move in spot.
Sign:  positive = dealers long gamma at that strike, negative = short.

MEASUREMENT, NOT PREDICTION. Every output is a quantity with units and a
derivation. There is no composite score, no regime verdict, and nothing here
feeds contract ranking. The arithmetic is exact; the step from dealer inventory
to a price path is not, and this module does not take it. Dealers hedge late,
hedge in another product, or run an already-flat book, and the open interest is
prior-session settled -- stalest on precisely the days with the heaviest
overnight repositioning. See the spec at
docs/superpowers/specs/2026-09-06-dealer-gamma-exposure-design.md
"""
from typing import Dict, List, Optional, Tuple, Any
import math

from core.constants import (
    GEX_CONCENTRATION_FLAG,
    GEX_MIN_OI_COVERAGE,
    GEX_EXPIRIES,            # noqa: F401  (re-exported for callers)
    GEX_FLIP_TOLERANCE,
    GEX_GRID_PCT,
    GEX_GRID_STEPS,
    GEX_INFER_MIN_CONTRACTS,
    GEX_INFER_MIN_SHARE,
)
from core.greeks import _MIN_T, bs_greeks, implied_vol

# Re-exported under shorter names so callers and tests read cleanly.
MIN_T          = _MIN_T
FLIP_TOLERANCE = GEX_FLIP_TOLERANCE


def _num(v, default=0.0) -> float:
    """float() that survives None/NaN/strings, matching core.options._num."""
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def gamma_notional(gamma: float, oi: float, spot: float) -> float:
    """Dollars of delta per 1% move in spot. Unsigned; the caller applies sign."""
    return gamma * _num(oi) * 100.0 * spot * spot * 0.01


def _resolve_iv(r: Dict, spot: float, T: float) -> Optional[float]:
    """
    The row's IV, or one back-solved from its mid price.

    yfinance returns 0.0 or NaN for a large share of far-OTM strikes -- exactly
    the wings carrying the biggest open interest. Treating those as zero vol
    deletes them from the profile and drags the flip toward the money.
    """
    iv = _num(r.get("iv"))
    if iv > 0.0:
        return iv
    bid, ask, last = _num(r.get("bid")), _num(r.get("ask")), _num(r.get("last"))
    mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last
    if mid <= 0:
        return None
    strike = _num(r.get("strike"))
    if strike <= 0 or spot <= 0:
        return None
    return implied_vol(mid, spot, strike, T, opt_type=r.get("type", "call"))


def _dealer_sign(r: Dict, flow: Optional[Dict]) -> Tuple[float, str, float]:
    """
    (sign, src, confidence) for one strike.

    Preferred: infer from today's classified flow. Ask-side volume is a customer
    buying, which leaves the dealer short gamma there; bid-side is the reverse.
    That is observed rather than assumed, which is more than the standard
    industry model (dealers long calls, short puts) can claim.

    Two gates, not one: a strike with three contracts all on the ask is 100%
    lopsided and carries no information, so an absolute floor rejects it
    alongside the share threshold. Mid-side volume is discarded rather than
    split -- it says nothing about the aggressor, and counting it only dilutes
    the share.
    """
    key = (_num(r.get("strike")), r.get("type"))
    f = (flow or {}).get(key)
    if f:
        ask, bid = _num(f.get("ask")), _num(f.get("bid"))
        classified = ask + bid
        if classified >= GEX_INFER_MIN_CONTRACTS:
            share = max(ask, bid) / classified if classified else 0.0
            if share >= GEX_INFER_MIN_SHARE:
                # Customer bought -> dealer short gamma.
                return (-1.0 if ask >= bid else 1.0), "inferred", share

    # Fallback: the naive convention on settled open interest.
    return (1.0 if r.get("type") == "call" else -1.0), "assumed", 0.0


def build_profile(rows: List[Dict], spot: float, T: float,
                  flow: Optional[Dict] = None) -> List[Dict]:
    """
    Signed gamma notional per strike at a given spot.

    `spot` is a parameter rather than a fixture because the flip solver
    re-evaluates the whole profile at hypothetical spots -- gamma is a function
    of moneyness, so every strike moves when spot does.
    """
    out: List[Dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        strike = _num(r.get("strike"))
        opt_type = r.get("type")
        if strike <= 0 or opt_type not in ("call", "put"):
            continue
        # Each expiry has its own time to expiry. Pricing a 30DTE strike as if
        # it were 0DTE overstates its gamma by orders of magnitude, so a row's
        # own T wins over the chain-wide default whenever the caller supplied
        # one.
        row_T = _num(r.get("T"), T) or T
        iv = _resolve_iv(r, spot, row_T)
        if not iv or iv <= 0:
            continue
        gamma = bs_greeks(spot, strike, row_T, iv, opt_type=opt_type)["gamma"]
        raw = gamma_notional(gamma, r.get("oi"), spot)
        sign, src, conf = _dealer_sign(r, flow)
        out.append({
            "strike":          strike,
            "type":            opt_type,
            "expiry":          r.get("expiry"),
            "oi":              int(_num(r.get("oi"))),
            "iv":              iv,
            "T":               row_T,
            "gamma":           gamma,
            "gamma_notional":  sign * raw,
            "src":             src,
            "confidence":      conf,
        })
    return out


def net_gex(profile: List[Dict]) -> float:
    return sum(r["gamma_notional"] for r in profile)


def _find_flips(rows: List[Dict], spot: float, T: float,
                flow: Optional[Dict]) -> List[float]:
    """
    Roots of net gamma as a function of spot, found on a grid then bisected.

    The common shortcut reads the flip off the strike-axis bars, interpolating
    between the last positive and the first negative. That conflates "gamma
    contributed by strike K" with "net gamma when spot is at K" -- different
    functions with different roots. This re-evaluates the entire profile at each
    hypothetical spot, which is the only thing the phrase actually means.
    """
    if spot <= 0 or not rows:
        return []

    lo, hi = spot * (1 - GEX_GRID_PCT), spot * (1 + GEX_GRID_PCT)
    step = (hi - lo) / max(1, GEX_GRID_STEPS - 1)

    def net_at(s: float) -> float:
        return net_gex(build_profile(rows, s, T, flow))

    grid = [(lo + i * step) for i in range(GEX_GRID_STEPS)]
    vals = [net_at(s) for s in grid]

    roots: List[float] = []
    for i in range(len(grid) - 1):
        a, b = vals[i], vals[i + 1]
        if a == 0.0:
            roots.append(grid[i])
            continue
        if a * b < 0:
            x0, x1, f0 = grid[i], grid[i + 1], a
            while x1 - x0 > FLIP_TOLERANCE:
                mid = 0.5 * (x0 + x1)
                fm = net_at(mid)
                if fm == 0.0:
                    x0 = x1 = mid
                    break
                if f0 * fm < 0:
                    x1 = mid
                else:
                    x0, f0 = mid, fm
            roots.append(round(0.5 * (x0 + x1), 2))
    return roots


def compute(rows: List[Dict], spot: float, T: float,
            flow: Optional[Dict] = None,
            oi_source: str = "yfinance",
            oi_asof: str = "prior session settle") -> Dict[str, Any]:
    """
    The full surface: profile, net GEX, flip(s), walls, and provenance.

    Provenance is not optional. It is modelled on `get_flow_source()`, which
    exists because a feed can be fully configured and still be serving stale
    data -- the caller must be able to say how much of this was observed and how
    old the inputs are.
    """
    usable = [r for r in rows if isinstance(r, dict) and _num(r.get("strike")) > 0
              and r.get("type") in ("call", "put")]
    profile = build_profile(usable, spot, T, flow)

    dropped = len(usable) - len(profile)

    # Open interest is the entire input. When the feed returns none -- observed
    # 2026-09-07: yfinance reports zero OI across SPX/SPY/QQQ near expiries --
    # every notional is zero and the walls degenerate to whichever wing strike
    # happens to hold a single contract. That surface is noise wearing the shape
    # of an answer, so it is reported as unusable rather than rendered.
    # Coverage, not the total: a chain can report 8 contracts spread over four
    # strikes out of eleven hundred and still sum to "more than zero". A surface
    # needs open interest across the strike range, so the test is what share of
    # strikes carry any at all.
    total_oi = sum(r["oi"] for r in profile)
    with_oi = sum(1 for r in profile if r["oi"] > 0)
    oi_coverage = (with_oi / len(profile)) if profile else 0.0
    usable_oi = oi_coverage >= GEX_MIN_OI_COVERAGE

    total_mag = sum(abs(r["gamma_notional"]) for r in profile)
    inferred_mag = sum(abs(r["gamma_notional"]) for r in profile
                       if r["src"] == "inferred")
    inferred_pct = (inferred_mag / total_mag) if total_mag else 0.0

    # How much of the surface rides on a single strike. Near expiry the ATM
    # strike genuinely dominates -- gamma diverges as T -> 0 -- and that is the
    # real shape of the book, not an artifact to suppress. Clamping it would be
    # the module editorialising its own input; reporting the concentration lets
    # the reader discount the flip themselves.
    max_share = (max((abs(r["gamma_notional"]) for r in profile), default=0.0)
                 / total_mag) if total_mag else 0.0

    calls = [r for r in profile if r["gamma_notional"] > 0]
    puts  = [r for r in profile if r["gamma_notional"] < 0]

    if usable_oi:
        flips = _find_flips(usable, spot, T, flow)
        flip = min(flips, key=lambda f: abs(f - spot)) if flips else None
    else:
        flips, flip = [], None

    return {
        "spot":      spot,
        "profile":   sorted(profile, key=lambda r: r["strike"]),
        "net_gex":   net_gex(profile),
        "flip":      flip,
        "flips":     flips,
        "call_wall": (max(calls, key=lambda r: r["gamma_notional"])["strike"]
                      if calls and usable_oi else None),
        "put_wall":  (min(puts, key=lambda r: r["gamma_notional"])["strike"]
                      if puts and usable_oi else None),
        "provenance": {
            "oi_source":       oi_source,
            "oi_asof":         oi_asof,
            "oi_stale":        True,
            "oi_total":        int(total_oi),
            "oi_coverage":     oi_coverage,
            "oi_usable":       usable_oi,
            "strikes_total":   len(usable),
            "strikes_dropped": dropped,
            "flip_roots":      len(flips),
            # One clean crossing is a level. Seven is a profile oscillating
            # through zero as spot passes each strike, and the nearest root is
            # then an artifact of where spot happens to sit -- not a level to
            # trade against. Reported so the reader can tell the two apart.
            "flip_stable":     len(flips) == 1,
            "max_strike_share": max_share,
            "concentrated":     max_share >= GEX_CONCENTRATION_FLAG,
            "inferred_pct":    inferred_pct,
            "assumed_pct":     1.0 - inferred_pct,
        },
    }


def years_to_expiry(dte: int, minutes_left: Optional[float] = None) -> float:
    """
    Time to expiry in years, honouring the intraday clock.

    A flat `dte / 365` prices a 0DTE contract as if it had a full day of life
    at 15:55, when it has five minutes. Gamma scales as 1/sqrt(T), so the error
    is not cosmetic: at 13:50 with 2.1 hours left, a half-day floor understates
    0DTE gamma by roughly 2.4x -- on precisely the contracts this is built for.

    `minutes_left` is injectable so the calculation is testable without waiting
    for a particular time of day.
    """
    if minutes_left is None:
        from core.market_calendar import minutes_to_close
        try:
            minutes_left = max(float(minutes_to_close()), 0.0)
        except Exception:
            minutes_left = 0.0
    # Outside RTH there is no intraday life left in today's contracts; the next
    # session's open is the earliest they can trade again.
    today_frac = minutes_left / (365.0 * 24.0 * 60.0)
    return max(max(int(dte), 0) / 365.0 + today_frac, MIN_T)


def flow_from_chain(rows: List[Dict]) -> Dict:
    """
    Classified volume per strike, derived from the chain itself.

    `classify_trade_side` is the Lee-Ready heuristic the rest of the scanner
    already uses: a last price near the ask means buyers were lifting it. That
    is a per-contract read rather than a per-print one, so it is weaker than the
    live tape -- but it is available on every chain fetch, and without it the
    dealer sign falls back to the naive convention on every single strike,
    which is the thing the hybrid model exists to avoid.
    """
    from core.options import classify_trade_side

    out: Dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        strike = _num(r.get("strike"))
        opt_type = r.get("type")
        vol = _num(r.get("volume"))
        if strike <= 0 or opt_type not in ("call", "put") or vol <= 0:
            continue
        side = classify_trade_side(_num(r.get("bid")), _num(r.get("ask")),
                                   _num(r.get("last")))
        if side == "mid":
            continue
        e = out.setdefault((strike, opt_type), {"ask": 0.0, "bid": 0.0})
        e[side] += vol
    return out


def surface_for(symbol: str, flow: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Fetch and compute one symbol's gamma surface.

    Lives here rather than in each caller because the API endpoint and the CLI
    both need identical expiry selection and time-to-expiry handling, and two
    copies of that drift.
    """
    from datetime import datetime as _dtm

    from core.market_calendar import exchange_today
    from core.market_data import _yf, full_chain

    t = _yf(symbol)
    try:
        spot = float(t.fast_info.last_price or 0)
    except Exception:
        spot = 0.0
    if spot <= 0:
        raise ValueError(f"no spot price for {symbol}")

    today = exchange_today()
    dated = []
    for e in list(getattr(t, "options", []) or []):
        try:
            d = _dtm.strptime(e, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= today:
            dated.append((e, (d - today).days, d))
    dated.sort(key=lambda x: x[1])
    chosen = dated[:GEX_EXPIRIES]
    # Front monthly OPEX carries outsized open interest even when it is further
    # out than the near expiries, so it is included regardless.
    monthly = next((x for x in dated
                    if x[2].weekday() == 4 and 15 <= x[2].day <= 21), None)
    if monthly and monthly not in chosen:
        chosen.append(monthly)
    if not chosen:
        raise ValueError(f"no expiries available for {symbol}")

    rows = full_chain(symbol, [e for e, _, _ in chosen])
    t_by_exp = {e: years_to_expiry(d) for e, d, _ in chosen}
    for r in rows:
        r["T"] = t_by_exp.get(r.get("expiry"), years_to_expiry(1))

    if flow is None:
        flow = flow_from_chain(rows)

    out = compute(rows, spot, T=t_by_exp[chosen[0][0]], flow=flow)
    out["symbol"] = symbol
    out["expiries"] = [e for e, _, _ in chosen]
    out["provenance"]["expiries"] = out["expiries"]
    return out
