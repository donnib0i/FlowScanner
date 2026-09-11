"""
core/futures.py -- where a gamma surface sits in futures prices, and what a
move to each level is worth on the contract you actually trade.

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

Multipliers and tick sizes below are CME contract specs, not measurements.
"""
from typing import Any, Dict, List, Optional

# Underlying -> the contract family that hedges it. Both the index and its
# tracking ETF map to the same family; the ratio absorbs the divisor.
FUTURES_MAP: Dict[str, str] = {
    "SPX": "ES",  "^SPX": "ES",  "^GSPC": "ES", "SPY": "ES",
    "NDX": "NQ",  "^NDX": "NQ",  "QQQ": "NQ",
    "RUT": "RTY", "^RUT": "RTY", "IWM": "RTY",
    "DJI": "YM",  "^DJI": "YM",  "DIA": "YM",
}

# Every family trades in two sizes. The full contract and its micro track the
# same index and quote the same price to within a tick -- what separates them is
# the multiplier, and on a four-figure account that is the only number deciding
# which one is tradeable at all. A 100-point move in NQ is $2,000; the same move
# in MNQ is $200. Both are listed so the reader picks the one they trade.
CONTRACTS: Dict[str, List[Dict[str, Any]]] = {
    "ES": [
        {"code": "ES",  "yf": "ES=F",  "name": "E-mini S&P 500",
         "multiplier": 50.0, "tick": 0.25, "micro": False},
        {"code": "MES", "yf": "MES=F", "name": "Micro E-mini S&P 500",
         "multiplier": 5.0,  "tick": 0.25, "micro": True},
    ],
    "NQ": [
        {"code": "NQ",  "yf": "NQ=F",  "name": "E-mini Nasdaq-100",
         "multiplier": 20.0, "tick": 0.25, "micro": False},
        {"code": "MNQ", "yf": "MNQ=F", "name": "Micro E-mini Nasdaq-100",
         "multiplier": 2.0,  "tick": 0.25, "micro": True},
    ],
    "RTY": [
        {"code": "RTY", "yf": "RTY=F", "name": "E-mini Russell 2000",
         "multiplier": 50.0, "tick": 0.10, "micro": False},
        {"code": "M2K", "yf": "M2K=F", "name": "Micro E-mini Russell 2000",
         "multiplier": 5.0,  "tick": 0.10, "micro": True},
    ],
    "YM": [
        {"code": "YM",  "yf": "YM=F",  "name": "E-mini Dow",
         "multiplier": 5.0,  "tick": 1.0, "micro": False},
        {"code": "MYM", "yf": "MYM=F", "name": "Micro E-mini Dow",
         "multiplier": 0.5,  "tick": 1.0, "micro": True},
    ],
}


# A future is not a thing with an option chain -- it is a thing that hedges one.
# Typing "MNQ" into a gamma scanner is a completely reasonable request, and it
# means "show me the Nasdaq surface in MNQ prices", so it resolves to the index
# whose options dealers hedge with that contract. ^DJI carries no chain at all
# on this feed, so YM resolves to the ETF instead of the index.
SURFACE_UNDERLYING: Dict[str, str] = {
    "ES": "SPX", "NQ": "NDX", "RTY": "RUT", "YM": "DIA",
}

# Every code that should resolve, full size and micro alike.
_CODE_TO_FAMILY: Dict[str, str] = {
    c["code"]: fam for fam, sizes in CONTRACTS.items() for c in sizes
}


def resolve(symbol: str):
    """
    (underlying to build the surface on, contract code to preselect).

    Accepts the ways a trader actually writes a contract -- MNQ, /MNQ, MNQ=F --
    and passes anything else straight through untouched, so an ordinary ticker
    is unaffected.
    """
    raw = (symbol or "").strip().upper().lstrip("/")
    if raw.endswith("=F"):
        raw = raw[:-2]
    fam = _CODE_TO_FAMILY.get(raw)
    if not fam:
        return (symbol or "").strip().upper(), None
    return SURFACE_UNDERLYING[fam], raw


def contracts_for(symbol: str) -> List[Dict[str, Any]]:
    """Both sizes of the contract that hedges `symbol`, full-size first."""
    fam = FUTURES_MAP.get((symbol or "").upper())
    return [dict(c) for c in CONTRACTS.get(fam, [])] if fam else []


def future_for(symbol: str) -> Optional[str]:
    """The yfinance symbol of the full-size future hedging `symbol`."""
    cs = contracts_for(symbol)
    return cs[0]["yf"] if cs else None


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
    How to read `symbol`'s strikes as futures prices, what the contract is
    trading at now, and what one point is worth in each size. None when the
    ticker has no futures counterpart -- most single names don't, and saying so
    is better than inventing a proxy.

    `ratio` converts a strike: futures_price = strike * ratio.
    `implied_underlying` runs it backwards, putting the live futures print onto
    the strike axis so the surface can be read against it out of hours.

    The ratio is taken from the full-size contract in every case. It is the
    deeper book and the cleaner daily bar, and the micro tracks it to within a
    tick, so deriving the basis from the micro would add noise and change
    nothing. The micro's own print is still reported, because that is the one
    being traded.
    """
    from core.market_data import _yf

    sizes = contracts_for(symbol)
    if not sizes:
        return None
    primary = sizes[0]

    synced = _synced_closes(symbol, primary["yf"])
    if not synced:
        return None
    under_close, fut_close, asof = synced
    ratio = fut_close / under_close
    if not (ratio > 0) or ratio != ratio:
        return None

    for c in sizes:
        c["last"] = _last(_yf(c["yf"])) or (fut_close if c is primary else 0.0)
        # Worth stating outright: one tick is the smallest move the contract
        # can make, and on the micros it is pocket change per contract.
        c["tick_value"] = c["tick"] * c["multiplier"]
    # A micro whose own quote failed still has a usable price: it tracks the
    # full contract to within a tick by construction.
    for c in sizes:
        if c["last"] <= 0:
            c["last"] = primary["last"]

    last = primary["last"]
    return {
        "future":     primary["code"],
        "yf_symbol":  primary["yf"],
        "name":       primary["name"],
        "contracts":  sizes,
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
