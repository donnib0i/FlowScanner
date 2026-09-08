"""
/api/gex contract tests.

The important one here is test_no_forecast_shaped_key_in_the_response: it turns
the subsystem's prime directive -- measurement, not prediction -- into an
executable assertion, so the constraint outlives the comment explaining it.
"""
import os

os.environ.pop("SCANNER_PIN", None)   # disable auth for these tests

import datetime as dt

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from core.greeks import bs_greeks


TODAY = dt.date(2026, 9, 8)          # a Tuesday
EXPS = ["2026-09-08", "2026-09-09", "2026-09-11", "2026-09-18"]   # last = monthly OPEX


class _FakeInfo:
    last_price = 100.0


class _FakeTicker:
    options = EXPS
    fast_info = _FakeInfo()


def chain_rows(exps=None):
    rows = []
    for e in (exps or EXPS):
        for k in (90.0, 95.0, 100.0, 105.0, 110.0):
            for kind in ("call", "put"):
                rows.append({"expiry": e, "type": kind, "strike": k,
                             "oi": 1000, "volume": 100, "iv": 0.20,
                             "bid": 1.0, "ask": 1.1, "last": 1.05})
    return rows


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    # The endpoint is rate limited at 10/60s and these tests call it far more
    # often than a human would. Clear the window per test rather than raising
    # the limit -- the limit is doing its job.
    webapp._rl._windows.clear()
    monkeypatch.setattr(webapp, "_yf", lambda s: _FakeTicker())
    monkeypatch.setattr(webapp, "exchange_today", lambda: TODAY)
    monkeypatch.setattr(webapp, "full_chain", lambda sym, exps: chain_rows(exps))


client = TestClient(webapp.app, raise_server_exceptions=False)


def get(url="/api/gex?symbol=SPX"):
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


# ── Shape ─────────────────────────────────────────────────────────────────────
def test_response_carries_the_measured_quantities():
    d = get()
    for k in ("spot", "profile", "net_gex", "flip", "flips",
              "call_wall", "put_wall", "provenance"):
        assert k in d, f"missing {k}"


def test_profile_rows_carry_units_and_sourcing():
    d = get()
    r = d["profile"][0]
    for k in ("strike", "type", "oi", "iv", "gamma", "gamma_notional",
              "src", "confidence"):
        assert k in r


def test_profile_is_sorted_by_strike():
    strikes = [r["strike"] for r in get()["profile"]]
    assert strikes == sorted(strikes)


def test_net_gex_equals_the_sum_of_the_profile():
    d = get()
    assert d["net_gex"] == pytest.approx(
        sum(r["gamma_notional"] for r in d["profile"]), rel=1e-9)


# ── Expiry selection ──────────────────────────────────────────────────────────
def test_near_expiries_and_the_front_monthly_are_included():
    d = get()
    assert d["expiries"][:3] == EXPS[:3]
    assert "2026-09-18" in d["expiries"], "front monthly OPEX must be included"


def test_each_expiry_is_priced_with_its_own_time_to_expiry():
    """A 10DTE strike must not be priced as 0DTE."""
    d = get()
    ts = {r["expiry"]: r["T"] for r in d["profile"]}
    assert ts["2026-09-18"] > ts["2026-09-09"]


def test_zero_dte_is_not_priced_as_zero_time():
    d = get()
    same_day = [r for r in d["profile"] if r["expiry"] == "2026-09-08"]
    assert all(r["T"] > 0 for r in same_day)


# ── Provenance ────────────────────────────────────────────────────────────────
def test_open_interest_is_declared_stale():
    """yfinance OI is prior-session settled. The UI must never imply otherwise."""
    p = get()["provenance"]
    assert p["oi_stale"] is True
    assert p["oi_asof"]
    assert p["oi_source"] == "yfinance"


def test_provenance_reports_coverage_and_exclusions():
    p = get()["provenance"]
    assert p["strikes_total"] > 0
    assert p["strikes_dropped"] >= 0
    assert 0.0 <= p["inferred_pct"] <= 1.0
    assert p["inferred_pct"] + p["assumed_pct"] == pytest.approx(1.0)


def test_provenance_lists_the_expiries_used():
    p = get()["provenance"]
    assert p["expiries"] == get()["expiries"]


def test_concentration_is_reported():
    p = get()["provenance"]
    assert 0.0 <= p["max_strike_share"] <= 1.0
    assert isinstance(p["concentrated"], bool)


# ── The prime directive, as an executable assertion ───────────────────────────
FORECAST_KEYS = {"bias", "verdict", "regime", "target", "signal", "score",
                 "prediction", "direction", "recommendation", "outlook"}


def test_no_forecast_shaped_key_in_the_response():
    """
    The endpoint measures dealer inventory. It does not say where price is
    going, and no key may imply that it does. This is the module's prime
    directive made executable so it survives future edits.
    """
    def walk(o, path="root"):
        if isinstance(o, dict):
            for k, v in o.items():
                assert k.lower() not in FORECAST_KEYS, \
                    f"forecast-shaped key {k!r} at {path}"
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, x in enumerate(o):
                walk(x, f"{path}[{i}]")

    walk(get())


# ── Validation and failure modes ──────────────────────────────────────────────
def test_bad_ticker_is_rejected():
    assert client.get("/api/gex?symbol=not a ticker").status_code == 400


def test_no_spot_price_is_a_503_not_a_zeroed_surface(monkeypatch):
    class _Dead:
        options = EXPS
        class fast_info:
            last_price = 0.0
    monkeypatch.setattr(webapp, "_yf", lambda s: _Dead())
    assert client.get("/api/gex?symbol=SPX").status_code == 503


def test_no_expiries_is_a_503(monkeypatch):
    class _Empty:
        options = []
        fast_info = _FakeInfo()
    monkeypatch.setattr(webapp, "_yf", lambda s: _Empty())
    assert client.get("/api/gex?symbol=SPX").status_code == 503


def test_past_expiries_are_ignored(monkeypatch):
    class _Stale:
        options = ["2020-01-17"] + EXPS
        fast_info = _FakeInfo()
    monkeypatch.setattr(webapp, "_yf", lambda s: _Stale())
    assert "2020-01-17" not in get()["expiries"]


def test_endpoint_is_pin_protected():
    """Same posture as its neighbours; test_security.py covers the mechanism."""
    import inspect
    src = inspect.getsource(webapp.api_gex)
    assert "_check_pin" in src and "_check_rate" in src


# ── UI wiring ─────────────────────────────────────────────────────────────────
def test_index_serves_the_gex_tab():
    html = client.get("/").text
    assert 'id="tab-gex"' in html
    assert "showTab('gex'" in html


def test_app_js_ships_the_gex_renderer():
    js = open("web/static/app.js").read()
    for fn in ("function loadGEX", "function renderGexChart", "function renderGexProv"):
        assert fn in js, fn


def test_ui_never_labels_open_interest_live():
    """Provenance must state staleness; the tab must not imply intraday OI."""
    js = open("web/static/app.js").read()
    assert "oi_asof" in js and "not intraday" in js
