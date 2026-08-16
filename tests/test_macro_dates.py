"""
FRED series must carry their observation date.

_fetch_series returned float(value) and discarded ob["date"], so a CPI print
from six weeks ago rendered identically to today's VIX. The panel's only
timestamp was last_updated, which is when *we fetched*, not when the data is
from — the one number that tells you whether a reading is current was thrown
away at the boundary.
"""
from datetime import date

import data.fred_macro as fm


class _Resp:
    status_code = 200
    def __init__(self, obs): self._obs = obs
    def json(self): return {"observations": self._obs}


def _stub(monkeypatch, obs):
    monkeypatch.setattr(fm.requests, "get", lambda *a, **k: _Resp(obs))


def test_fetch_series_returns_value_and_date(monkeypatch):
    _stub(monkeypatch, [{"date": "2026-08-14", "value": "4.63"}])
    got = fm._fetch_series("DGS10", "key")
    assert got.value == 4.63
    assert got.date == date(2026, 8, 14)


def test_missing_observations_are_skipped(monkeypatch):
    # FRED uses "." for a missing print; the next real one should win, with its
    # own date — not the date of the hole.
    _stub(monkeypatch, [{"date": "2026-08-15", "value": "."},
                        {"date": "2026-08-14", "value": "4.63"}])
    got = fm._fetch_series("DGS10", "key")
    assert got.value == 4.63 and got.date == date(2026, 8, 14)


def test_all_missing_returns_none(monkeypatch):
    _stub(monkeypatch, [{"date": "2026-08-15", "value": "."}])
    assert fm._fetch_series("DGS10", "key") is None


def test_undated_observation_still_yields_value(monkeypatch):
    _stub(monkeypatch, [{"value": "4.63"}])
    got = fm._fetch_series("DGS10", "key")
    assert got.value == 4.63 and got.date is None


def test_payload_exposes_as_of_and_staleness(monkeypatch):
    monkeypatch.setattr(fm, "load_api_key", lambda: "key")
    monkeypatch.setattr(fm, "_cache", None)
    monkeypatch.setattr(fm, "_cache_ts", 0.0)
    _stub(monkeypatch, [{"date": "2026-06-01", "value": "4.1"}])

    payload = fm.get_macro_context()
    series = next(iter(payload["data"].values()))
    assert series["as_of"] == "2026-06-01"
    assert series["stale_days"] >= 1, "age of the print must be reported"
