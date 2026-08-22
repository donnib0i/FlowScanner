import datetime as dt
import os
from zoneinfo import ZoneInfo

os.environ.pop("SCANNER_PIN", None)

# The fixture is meant to be "a 2DTE expiry". Hardcoding the date made it 2DTE
# only during the week it was written; by the next Monday the chain was five
# days expired and three tests failed for reasons unrelated to what they test.
_EXP = (dt.datetime.now(ZoneInfo("America/New_York")).date()
        + dt.timedelta(days=2)).isoformat()

from fastapi.testclient import TestClient

import web.app as webapp
import core.scanner as scanner

client = TestClient(webapp.app)


class _Chain:
    def __init__(self, calls, puts):
        self.calls, self.puts = calls, puts


class _FakeTicker:
    """Minimal yfinance stand-in: one 2DTE expiry, two strikes a side."""
    options = [_EXP]

    class fast_info:
        last_price = 225.16

    def option_chain(self, exp):
        import pandas as pd
        calls = pd.DataFrame([
            {"strike": 207.5, "volume": 6966, "openInterest": 293,
             "bid": 17.60, "ask": 17.76, "lastPrice": 17.68, "impliedVolatility": 0.636},
            {"strike": 225.0, "volume": 46805, "openInterest": 6260,
             "bid": 1.77, "ask": 1.81, "lastPrice": 1.79, "impliedVolatility": 0.212},
        ])
        puts = pd.DataFrame([
            {"strike": 240.0, "volume": 1997, "openInterest": 1,
             "bid": 15.20, "ask": 15.30, "lastPrice": 15.25, "impliedVolatility": 0.51},
            {"strike": 225.0, "volume": 37842, "openInterest": 2309,
             "bid": 1.55, "ask": 1.59, "lastPrice": 1.57, "impliedVolatility": 0.204},
        ])
        return _Chain(calls, puts)


def _stub(monkeypatch):
    monkeypatch.setattr(scanner, "_yf", lambda t: _FakeTicker())
    monkeypatch.setattr(webapp, "fetch_vix", lambda: 15.0)


def _best(direction):
    return {"exp": _EXP, "dte": 2, "strike": 225.0,
            "type": "call" if direction == "up" else "put",
            "mid": 1.79, "delta": 0.5, "score": 71.4, "roi": 150.0}


def test_ladder_returns_best_contract_per_side(monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(webapp, "get_best_contract",
                        lambda tk, direction, *a, **k: [_best(direction)])
    d = client.get("/api/find/both?ticker=NVDA&dte_mode=all").json()
    assert d["best_call"]["type"] == "call"
    assert d["best_put"]["type"] == "put"
    assert d["best_call"]["score"] == 71.4


def test_ladder_best_is_null_when_engine_finds_nothing(monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(webapp, "get_best_contract", lambda *a, **k: None)
    d = client.get("/api/find/both?ticker=NVDA&dte_mode=all").json()
    assert d["best_call"] is None and d["best_put"] is None
    assert d["calls"], "ladder itself must still render"


def test_ladder_drops_deep_itm_and_illiquid_strikes(monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(webapp, "get_best_contract", lambda *a, **k: None)
    d = client.get("/api/find/both?ticker=NVDA&dte_mode=all").json()
    assert [r["strike"] for r in d["calls"]] == [225.0]   # 207.5 deep ITM gone
    assert [r["strike"] for r in d["puts"]] == [225.0]    # 240 OI=1 gone


def test_ladder_totals_match_returned_rows(monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(webapp, "get_best_contract", lambda *a, **k: None)
    d = client.get("/api/find/both?ticker=NVDA&dte_mode=all").json()
    assert d["call_totals"]["volume"] == sum(r["vol"] for r in d["calls"])
    assert d["put_totals"]["oi"] == sum(r["oi"] for r in d["puts"])


def test_ladder_reports_dte_fallback(monkeypatch):
    # Same silent-widening bug already fixed on /api/find: 0DTE asked, 2DTE served.
    _stub(monkeypatch)
    monkeypatch.setattr(webapp, "get_best_contract", lambda *a, **k: None)
    d = client.get("/api/find/both?ticker=NVDA&dte_mode=0dte").json()
    assert d["dte_note"] and "2DTE" in d["dte_note"]
