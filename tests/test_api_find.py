import os
os.environ.pop("SCANNER_PIN", None)  # disable auth for these tests
from fastapi.testclient import TestClient
import web.app as webapp
import core.scanner as scanner

client = TestClient(webapp.app)


class _FakeTicker:
    """No expirations -> _fetch() returns None -> endpoint 404 (never touches network)."""
    options = []


def test_find_both_import_path_resolves(monkeypatch):
    # Regression: /api/find/both used `from scanner import ...` (module is
    # core.scanner), raising ModuleNotFoundError -> 500. With the correct
    # import, a no-data chain yields 404, not a 500.
    monkeypatch.setattr(scanner, "_yf", lambda t: _FakeTicker())
    r = client.get("/api/find/both?ticker=AMD&dte_mode=all")
    assert r.status_code == 404


def _stub_find(monkeypatch, dte):
    """Make /api/find return one contract at the given DTE, no network."""
    monkeypatch.setattr(webapp, "fetch_vix", lambda: 15.0)
    monkeypatch.setattr(
        webapp, "get_best_contract",
        lambda *a, **k: [{"exp": "2026-08-14", "dte": dte, "strike": 225.0,
                          "type": "call", "mid": 2.0, "score": 60.0}],
    )


def test_find_flags_dte_fallback(monkeypatch):
    # 0DTE was asked for but the nearest expiry is 2 days out — the UI must be
    # told, instead of silently labelling a 2DTE contract "0DTE / Today only".
    _stub_find(monkeypatch, dte=2)
    d = client.get("/api/find?ticker=NVDA&direction=up&dte_mode=0dte").json()
    assert d["dte_note"]
    assert "0DTE" in d["dte_note"] and "2DTE" in d["dte_note"]


def test_find_no_note_when_dte_mode_honored(monkeypatch):
    _stub_find(monkeypatch, dte=0)
    d = client.get("/api/find?ticker=NVDA&direction=up&dte_mode=0dte").json()
    assert d["dte_note"] is None


def test_find_weekly_window_honored(monkeypatch):
    _stub_find(monkeypatch, dte=5)
    d = client.get("/api/find?ticker=NVDA&direction=up&dte_mode=weekly").json()
    assert d["dte_note"] is None


def test_find_weekly_outside_window_flagged(monkeypatch):
    _stub_find(monkeypatch, dte=21)
    d = client.get("/api/find?ticker=NVDA&direction=up&dte_mode=weekly").json()
    assert d["dte_note"] and "21DTE" in d["dte_note"]


def test_find_all_mode_never_notes(monkeypatch):
    _stub_find(monkeypatch, dte=30)
    d = client.get("/api/find?ticker=NVDA&direction=up&dte_mode=all").json()
    assert d["dte_note"] is None
