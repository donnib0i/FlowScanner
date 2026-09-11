"""
core/futures.py -- where a gamma surface sits in futures prices.

Dealers hedging index options hedge in the futures, not the cash index, and
outside the cash session the futures are the only thing still printing. So the
levels the surface measures -- the flip, the walls, every strike on the ladder
-- are worth reading in ES or NQ terms, and overnight that is the *only* way to
read them: the index print is frozen at its close while the futures have moved.

MEASURED, NOT MODELLED. The conversion is a ratio taken from two closes that
printed at the same moment, never a fair-value formula and never a cost-of-carry
assumption. The ratio is multiplicative because that is the shape of the real
relationship -- carry is a fraction of the index level, F = S(1 + (r-q)T), and
the ETF pairs carry a divisor on top of it (QQQ is ~1/41 of NQ). One
multiplication covers both.

The synchronisation matters more than it looks. Taking the ratio from two *live*
prints instead conflates the basis with however far futures have travelled since
the cash close, which would slide the whole gamma map by the overnight move --
the map would drift away from the strikes it was computed on, and every level
would read wrong by exactly the amount you most wanted to know.
"""
from typing import Any, Dict, Optional

# Underlying -> the future that hedges it. Both the index and its tracking ETF
# map to the same contract; the ratio absorbs the divisor.
FUTURES_MAP: Dict[str, str] = {
    "SPX": "ES=F",  "^SPX": "ES=F",  "^GSPC": "ES=F", "SPY": "ES=F",
    "NDX": "NQ=F",  "^NDX": "NQ=F",  "QQQ": "NQ=F",
    "RUT": "RTY=F", "^RUT": "RTY=F", "IWM": "RTY=F",
    "DJI": "YM=F",  "^DJI": "YM=F",  "DIA": "YM=F",
}

FUTURES_NAME: Dict[str, str] = {
    "ES=F":  "E-mini S&P 500",
    "NQ=F":  "E-mini Nasdaq-100",
    "RTY=F": "E-mini Russell 2000",
    "YM=F":  "E-mini Dow",
}

# Shown instead of the yfinance symbol: nobody reading a chart calls it "ES=F".
FUTURES_TICKER: Dict[str, str] = {
    "ES=F": "ES", "NQ=F": "NQ", "RTY=F": "RTY", "YM=F": "YM",
}


def future_for(symbol: str) -> Optional[str]:
    """The futures contract that hedges `symbol`, or None if there isn't one."""
    return FUTURES_MAP.get((symbol or "").upper())


def _last(t) -> float:
    try:
        return float(t.fast_info.last_price or 0.0)
    except Exception:
        return 0.0


def _synced_closes(under_sym: str, fut_sym: str):
    """
    The most recent session close that BOTH instruments printed.

    Daily bars are used rather than live quotes precisely because the two bars
    are stamped to the same session -- that is what makes the ratio a basis and
    not a basis plus an overnight drift.
    """
    from core.market_data import _yf

    hu = _yf(under_sym).history(period="10d")
    hf = _yf(fut_sym).history(period="10d")
    if hu.empty or hf.empty:
        return None
    udates = {d.date(): d for d in hu.index}
    for d in sorted((d.date() for d in hf.index), reverse=True):
        if d not in udates:
            continue
        cu = float(hu.loc[udates[d], "Close"])
        cf = float(hf.loc[[i for i in hf.index if i.date() == d][-1], "Close"])
        if cu > 0 and cf > 0:
            return cu, cf, d
    return None


def link_for(symbol: str) -> Optional[Dict[str, Any]]:
    """
    How to read `symbol`'s strikes as futures prices, plus where the future is
    trading right now. None when the ticker has no futures counterpart -- most
    single names don't, and saying so is better than inventing a proxy.

    `ratio` converts a strike: futures_price = strike * ratio.
    `implied_underlying` runs it backwards, putting the live futures print onto
    the strike axis so the surface can be read against it out of hours.
    """
    from core.market_data import _yf

    fut = future_for(symbol)
    if not fut:
        return None

    synced = _synced_closes(symbol, fut)
    if not synced:
        return None
    under_close, fut_close, asof = synced
    ratio = fut_close / under_close
    if not (ratio > 0) or ratio != ratio:
        return None

    last = _last(_yf(fut))
    if last <= 0:
        last = fut_close

    return {
        "future":     FUTURES_TICKER.get(fut, fut),
        "yf_symbol":  fut,
        "name":       FUTURES_NAME.get(fut, fut),
        "ratio":      ratio,
        "ratio_asof": asof.isoformat(),
        # The basis at that close, in index points. Reported because a reader
        # checking the conversion by hand will reach for it first.
        "basis":      fut_close - under_close if 0.9 < ratio < 1.1 else None,
        "last":       last,
        "prev_close": fut_close,
        "change":     last - fut_close,
        "change_pct": (last - fut_close) / fut_close if fut_close else 0.0,
        "implied_underlying": last / ratio,
    }
