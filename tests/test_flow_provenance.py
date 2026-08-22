"""
The flow source badge must describe the data actually served.

`/api/status` derived `flow_source` from `_TT_AVAILABLE` — which is only
`bool(username and password)`, i.e. whether credentials *exist*, never whether
the TastyTrade session authenticated or whether any print was collected. When
TT login fails (it currently does: the account requires a device challenge
token that a Railway container cannot receive), `scan_options_flow` silently
falls through to the 15-minute-delayed yfinance path while the UI keeps
showing "● LIVE — TastyTrade OPRA feed".

Provenance is therefore recorded by the scan itself, not inferred from config.
"""
import pytest

import core.scanner as sc


@pytest.fixture(autouse=True)
def _reset_provenance():
    sc.reset_flow_source()
    yield
    sc.reset_flow_source()


def test_no_scan_yet_reports_unknown_not_live():
    src = sc.get_flow_source()
    assert src["source"] is None
    assert src["live"] is False


def test_tastytrade_prints_record_live(monkeypatch):
    monkeypatch.setattr(sc, "_TT_AVAILABLE", True)
    monkeypatch.setattr(sc, "scan_options_flow_tt",
                        lambda *a, **k: [{"ticker": "NVDA", "whale_score": 50}])
    out = sc.scan_options_flow(["NVDA"], show_progress=False)
    assert out
    src = sc.get_flow_source()
    assert src["source"] == "tastytrade-live"
    assert src["live"] is True


def test_tastytrade_auth_failure_records_delayed(monkeypatch):
    """No prints + a login failure must not be reported as live."""
    monkeypatch.setattr(sc, "_TT_AVAILABLE", True)
    monkeypatch.setattr(sc, "scan_options_flow_tt", lambda *a, **k: [])
    monkeypatch.setattr(sc, "_tt_last_error", lambda: "device_challenge_required")
    monkeypatch.setattr(sc, "_scan_options_flow_yf", lambda *a, **k: [])

    sc.scan_options_flow(["NVDA"], show_progress=False)
    src = sc.get_flow_source()
    assert src["source"] == "yfinance-delayed"
    assert src["live"] is False
    assert "device_challenge_required" in src["reason"]


def test_tastytrade_exception_records_delayed(monkeypatch):
    monkeypatch.setattr(sc, "_TT_AVAILABLE", True)
    def _boom(*a, **k):
        raise RuntimeError("websocket died")
    monkeypatch.setattr(sc, "scan_options_flow_tt", _boom)
    monkeypatch.setattr(sc, "_scan_options_flow_yf", lambda *a, **k: [])

    sc.scan_options_flow(["NVDA"], show_progress=False)
    src = sc.get_flow_source()
    assert src["source"] == "yfinance-delayed"
    assert "websocket died" in src["reason"]


def test_status_endpoint_reports_recorded_source(monkeypatch):
    import os
    os.environ.pop("SCANNER_PIN", None)
    from fastapi.testclient import TestClient
    import web.app as webapp

    monkeypatch.setattr(sc, "_TT_AVAILABLE", True)
    monkeypatch.setattr(webapp, "_TT_AVAILABLE", True)
    monkeypatch.setattr(sc, "scan_options_flow_tt", lambda *a, **k: [])
    monkeypatch.setattr(sc, "_tt_last_error", lambda: "device_challenge_required")
    monkeypatch.setattr(sc, "_scan_options_flow_yf", lambda *a, **k: [])
    sc.scan_options_flow(["NVDA"], show_progress=False)

    body = TestClient(webapp.app).get("/api/status").json()
    # Credentials are configured, but the served flow is delayed. Both facts,
    # kept apart — `live` describes the data, `tt_configured` the config.
    assert body["tt_configured"] is True
    assert body["live"] is False
    assert body["flow_source"] == "yfinance-delayed"
    assert "device_challenge_required" in body["flow_source_reason"]
