"""
DTE is measured against the exchange's date, never the host's.

This bug has now been found four separate times in this codebase (web/app.py,
core/options.py, then core/flow.py, core/pipeline.py, data/unusual_flow.py and
data/sources.py). The consequence is always the same and always silent: Railway
runs UTC and the developer sits on Pacific, so once the host date rolls past
New York's, every contract is labelled one day closer to expiry than it is --
turning 1DTE into 0DTE on the exact contracts this scanner is built to trade.

These tests freeze the two clocks apart so a host-date regression fails here
rather than in a position.
"""
import datetime as dt

import pytest

from core import market_calendar as mc


# 00:30 UTC on the 7th is 20:30 ET on the 6th: the host has rolled over, the
# exchange has not.
EXCHANGE_DAY = dt.date(2026, 1, 6)
HOST_DAY = dt.date(2026, 1, 7)


@pytest.fixture
def split_clocks(monkeypatch):
    """exchange_today() returns the 6th while the host believes it is the 7th."""
    monkeypatch.setattr(mc, "exchange_today", lambda: EXCHANGE_DAY)
    for mod in ("core.flow", "core.pipeline", "core.live_flow"):
        try:
            m = __import__(mod, fromlist=["x"])
        except Exception:
            continue
        if hasattr(m, "exchange_today"):
            monkeypatch.setattr(m, "exchange_today", lambda: EXCHANGE_DAY)
    return EXCHANGE_DAY


def test_exchange_today_trails_the_host_in_the_evening(monkeypatch):
    fixed = dt.datetime(2026, 1, 7, 0, 30, tzinfo=dt.timezone.utc)

    class _Fixed(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(mc._dt, "datetime", _Fixed)
    assert mc.exchange_today() == EXCHANGE_DAY
    assert mc.exchange_today() != HOST_DAY


def test_sources_dte_uses_the_exchange_date(monkeypatch):
    from data import sources
    monkeypatch.setattr(mc, "exchange_today", lambda: EXCHANGE_DAY)
    # An expiry on the 7th is 1DTE at the exchange, 0DTE on the host's clock.
    assert sources._dte("2026-01-07") == 1


def test_unusual_flow_dte_uses_the_exchange_date(monkeypatch):
    from data import unusual_flow
    monkeypatch.setattr(mc, "exchange_today", lambda: EXCHANGE_DAY)
    assert unusual_flow._days_to_expiry("2026-01-07") == 1


def test_dte_helpers_agree_with_each_other(monkeypatch):
    """Two copies of the same calculation must not drift apart."""
    from data import sources, unusual_flow
    monkeypatch.setattr(mc, "exchange_today", lambda: EXCHANGE_DAY)
    for exp in ("2026-01-06", "2026-01-07", "2026-01-16"):
        assert sources._dte(exp) == unusual_flow._days_to_expiry(exp)


def test_dte_never_goes_negative_on_a_past_expiry(monkeypatch):
    from data import sources
    monkeypatch.setattr(mc, "exchange_today", lambda: EXCHANGE_DAY)
    assert sources._dte("2020-01-01") == 0


def test_a_malformed_expiry_does_not_raise():
    from data import sources, unusual_flow
    assert sources._dte("not-a-date") == 99
    assert unusual_flow._days_to_expiry("") == 99


def test_no_expiry_math_reaches_for_the_host_clock():
    """
    A grep guard. It is blunt, but this bug has recurred four times and each
    instance was invisible until a contract was mislabelled in production.
    """
    import pathlib
    offenders = []
    for path in ("core/flow.py", "core/pipeline.py", "core/options.py",
                 "core/live_flow.py", "data/unusual_flow.py", "data/sources.py",
                 "web/app.py"):
        text = pathlib.Path(path).read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            if "date.today()" in line or "datetime.now().date()" in line:
                if line.strip().startswith("#"):
                    continue
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "host clock used where the exchange date belongs:\n" + "\n".join(offenders)
