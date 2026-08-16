"""
market_calendar.py — NYSE trading calendar.

`weekday() < 5` is not a trading calendar. It treats Thanksgiving and July 4th
as normal sessions, which made the finder offer 0DTE contracts on days with no
session at all, and it has no concept of the 13:00 ET early closes — where 0DTE
time-value math would otherwise assume three extra hours until expiry.

Holidays are computed from the NYSE observance rules rather than hardcoded, so
this stays correct in future years without maintenance.
"""
from __future__ import annotations

import datetime as _dt
from functools import lru_cache
from typing import Dict, Set

try:
    import zoneinfo
    _ET = zoneinfo.ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover
    import pytz
    _ET = pytz.timezone("America/New_York")

REGULAR_CLOSE = _dt.time(16, 0)
EARLY_CLOSE   = _dt.time(13, 0)
OPEN_TIME     = _dt.time(9, 30)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> _dt.date:
    """n-th `weekday` (Mon=0) of a month, e.g. 3rd Monday of January."""
    d = _dt.date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + _dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> _dt.date:
    """Last `weekday` of a month, e.g. last Monday of May."""
    d = (_dt.date(year, month + 1, 1) - _dt.timedelta(days=1)) if month < 12 \
        else _dt.date(year, 12, 31)
    return d - _dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> _dt.date:
    """Gregorian Easter Sunday (anonymous computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return _dt.date(year, month, day + 1)


def _observed(day: _dt.date) -> _dt.date:
    """NYSE weekend rule: Saturday -> preceding Friday, Sunday -> following Monday."""
    if day.weekday() == 5:
        return day - _dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + _dt.timedelta(days=1)
    return day


@lru_cache(maxsize=32)
def holidays(year: int) -> Set[_dt.date]:
    """The full set of NYSE market holidays for `year`."""
    return {
        _observed(_dt.date(year, 1, 1)),          # New Year's Day
        _nth_weekday(year, 1, 0, 3),              # MLK Day
        _nth_weekday(year, 2, 0, 3),              # Washington's Birthday
        _easter(year) - _dt.timedelta(days=2),    # Good Friday
        _last_weekday(year, 5, 0),                # Memorial Day
        _observed(_dt.date(year, 6, 19)),         # Juneteenth
        _observed(_dt.date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),              # Labor Day
        _nth_weekday(year, 11, 3, 4),             # Thanksgiving
        _observed(_dt.date(year, 12, 25)),        # Christmas
    }


@lru_cache(maxsize=32)
def early_closes(year: int) -> Dict[_dt.date, _dt.time]:
    """Half sessions (13:00 ET close), skipping any that land on a holiday."""
    out: Dict[_dt.date, _dt.time] = {}
    hols = holidays(year)

    # Day after Thanksgiving.
    out[_nth_weekday(year, 11, 3, 4) + _dt.timedelta(days=1)] = EARLY_CLOSE
    # Christmas Eve and July 3rd, only when they are ordinary weekdays.
    for day in (_dt.date(year, 12, 24), _dt.date(year, 7, 3)):
        if day.weekday() < 5:
            out[day] = EARLY_CLOSE

    return {d: t for d, t in out.items() if d not in hols and d.weekday() < 5}


def is_trading_day(day: _dt.date) -> bool:
    """True if the NYSE holds a session (full or half) on `day`."""
    if isinstance(day, _dt.datetime):
        day = day.date()
    if day.weekday() >= 5:
        return False
    # A Jan 1 that falls on a Saturday is observed on Dec 31 of the *previous*
    # year, so late December has to consult the next year's holiday set too.
    if day in holidays(day.year):
        return False
    return not (day.month == 12 and day.day >= 30 and day in holidays(day.year + 1))


def market_close_time(day: _dt.date) -> _dt.time:
    """Closing bell for `day` — 13:00 on half sessions, otherwise 16:00."""
    if isinstance(day, _dt.datetime):
        day = day.date()
    return early_closes(day.year).get(day, REGULAR_CLOSE)


def is_market_open(now: _dt.datetime | None = None) -> bool:
    """True if the regular session is live at `now` (defaults to now, ET)."""
    now = now.astimezone(_ET) if now is not None else _dt.datetime.now(_ET)
    if not is_trading_day(now.date()):
        return False
    return OPEN_TIME <= now.time() <= market_close_time(now.date())


def minutes_to_close(now: _dt.datetime | None = None) -> float:
    """Minutes remaining in the session — honours early closes. 0 when shut."""
    now = now.astimezone(_ET) if now is not None else _dt.datetime.now(_ET)
    if not is_market_open(now):
        return 0.0
    close = _dt.datetime.combine(now.date(), market_close_time(now.date()), tzinfo=_ET)
    return max(0.0, (close - now).total_seconds() / 60)


def next_trading_day(day: _dt.date) -> _dt.date:
    """The next session strictly after `day`."""
    if isinstance(day, _dt.datetime):
        day = day.date()
    nxt = day + _dt.timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += _dt.timedelta(days=1)
    return nxt
