"""
core/bars.py -- today's session, one minute at a time, for the chart beside the
flow feed.

MEASURED FRESHNESS. The feed does not promise a latency and neither does this:
every response carries the timestamp of its last bar and the minutes since,
computed here, so the chart can say "last bar 14:32, 3 min ago" rather than
"live". Out of hours the last bar is the close and the lag is however long ago
that was, which is also the truth.

A futures code charts the future. The gamma surface has to resolve MNQ to the
NDX option chain, because there is no chain on the contract -- but the chart
has no such constraint, the contract trades 23 hours a day, and a chart of the
index at 6pm is a flat line at the close while ES has moved forty points. So
here ES means ES=F.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.futures import _CODE_TO_FAMILY, CONTRACTS

# What the chart draws for a futures code: the contract itself.
_FUT_YF: Dict[str, str] = {
    c["code"]: c["yf"] for sizes in CONTRACTS.values() for c in sizes
}


def chart_symbol(symbol: str):
    """(yfinance symbol to fetch, label to show). A futures code becomes its
    =F contract; anything else passes through the usual index map."""
    raw = (symbol or "").strip().upper().lstrip("/")
    if raw.endswith("=F"):
        raw = raw[:-2]
    if raw in _FUT_YF:
        return _FUT_YF[raw], raw
    return raw, raw


def _num(v) -> float:
    try:
        f = float(v)
        return 0.0 if f != f else f
    except (TypeError, ValueError):
        return 0.0


def session_bars(symbol: str, interval: str = "1m") -> Dict[str, Any]:
    """
    The most recent session's bars for `symbol`, plus the numbers the chart
    draws against them: the prior close, the session VWAP, and how stale the
    last bar is.
    """
    from core.market_data import _yf

    yf_sym, label = chart_symbol(symbol)
    t = _yf(yf_sym)
    # Two days so the prior close is in hand and a pre-market request still has
    # yesterday's session to draw while today's is empty.
    h = t.history(period="2d", interval=interval)
    if h is None or h.empty:
        raise ValueError(f"No intraday bars for {label}.")

    # The last session is whatever date the newest bar carries. Futures roll
    # through midnight, so this is a session boundary, not a calendar one.
    days = sorted({i.date() for i in h.index})
    today = days[-1]
    cur = h[[i.date() == today for i in h.index]]
    prev = h[[i.date() < today for i in h.index]]

    bars: List[Dict[str, Any]] = []
    cum_pv = cum_v = 0.0
    for ts, r in cur.iterrows():
        o, hi, lo, c, v = (_num(r["Open"]), _num(r["High"]), _num(r["Low"]),
                           _num(r["Close"]), _num(r["Volume"]))
        if c <= 0:
            continue
        cum_pv += ((hi + lo + c) / 3.0) * v
        cum_v += v
        bars.append({"t": int(ts.timestamp()), "o": o, "h": hi, "l": lo, "c": c, "v": v})
    if not bars:
        raise ValueError(f"No bars yet today for {label}.")

    prev_close: Optional[float] = None
    if not prev.empty:
        prev_close = _num(prev["Close"].iloc[-1]) or None

    last_ts = datetime.fromtimestamp(bars[-1]["t"], tz=timezone.utc)
    lag_min = max(0.0, (datetime.now(timezone.utc) - last_ts).total_seconds() / 60.0)

    return {
        "symbol":     label,
        "yf_symbol":  yf_sym,
        "interval":   interval,
        "session":    today.isoformat(),
        "bars":       bars,
        "prev_close": prev_close,
        "vwap":       (cum_pv / cum_v) if cum_v > 0 else None,
        "last":       bars[-1]["c"],
        "asof":       last_ts.isoformat(),
        "lag_min":    round(lag_min, 1),
        "source":     "yfinance",
    }
