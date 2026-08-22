"""
web/app.py imported the NYSE-aware is_market_open at line 61 and then redefined
it 300 lines later with a `weekday() >= 5` version. The local def shadowed the
import, so both web call sites went back to believing every weekday is a
session — including the /api/find path that already carried the comment
"Holiday- and early-close-aware".
"""
import datetime as dt
from zoneinfo import ZoneInfo

import core.market_calendar as mc
import web.app as webapp

ET = ZoneInfo("America/New_York")


def test_app_uses_the_calendar_implementation():
    assert webapp.is_market_open is mc.is_market_open


def test_thanksgiving_is_not_a_session():
    # 2026-11-26, 11:00 ET — a Thursday, and a full holiday.
    assert webapp.is_market_open(dt.datetime(2026, 11, 26, 11, 0, tzinfo=ET)) is False


def test_half_session_afternoon_is_shut():
    # 2026-11-27 closes at 13:00; 14:00 ET is after the bell.
    assert webapp.is_market_open(dt.datetime(2026, 11, 27, 14, 0, tzinfo=ET)) is False
    assert webapp.is_market_open(dt.datetime(2026, 11, 27, 11, 0, tzinfo=ET)) is True


def test_regular_session_is_open():
    assert webapp.is_market_open(dt.datetime(2026, 11, 30, 11, 0, tzinfo=ET)) is True
