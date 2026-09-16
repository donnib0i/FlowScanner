"""
What the flow stream does when it cannot start.

EventSource cannot read an HTTP status code. Refusing a request with 503 is
therefore invisible to the browser -- it sees a connection that failed, and the
tab reported "Waking up... retry" at a server that was up and busy running the
very scan the user asked for. Any condition the user needs explained has to
travel *inside* the stream.
"""
import importlib
import os
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("SCANNER_PIN", raising=False)
    monkeypatch.delenv("SCANNER_REQUIRE_PIN", raising=False)
    import web.app as webapp
    importlib.reload(webapp)
    webapp._active_scan.clear()
    yield webapp
    webapp._active_scan.clear()


@pytest.fixture
def client(app):
    return TestClient(app.app)


def first_event(resp):
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            return line[len("data: "):]
    return ""


def test_a_second_scan_joins_rather_than_being_refused(app):
    """This endpoint used to answer a second request with 503, and before that
    with an in-stream error. Both were dead ends for the tab. It now joins the
    running scan -- see test_flow_scan_sharing -- so the only thing left to
    assert here is that nothing refuses it.
    """
    live = app._ScanSession(tickers=2)
    app._scan_session = live
    try:
        session, q, joined = app._attach_or_start(["NVDA"])
        assert joined is True and session is live
    finally:
        live.finish()


def test_a_wedged_scan_does_not_lock_the_endpoint_forever(client, app, monkeypatch):
    """The flag is set by a thread that can die without clearing it -- a stuck
    yfinance call, a killed worker. Without an expiry, one wedged scan means
    every later scan reports the same misleading failure until a redeploy."""
    app._active_scan.set()
    app._scan_started_at = time.monotonic() - (app.SCAN_STALE_AFTER_S + 5)

    started = {}
    monkeypatch.setattr(app, "scan_options_flow",
                        lambda *a, **k: started.setdefault("ran", True) or [])

    with client.stream("GET", "/api/flow?tickers=NVDA") as r:
        body = first_event(r)
    assert "already running" not in body


def test_a_fresh_scan_still_starts_normally(client, app, monkeypatch):
    monkeypatch.setattr(app, "scan_options_flow", lambda *a, **k: [])
    with client.stream("GET", "/api/flow?tickers=NVDA") as r:
        assert r.status_code == 200
        body = first_event(r)
    assert "already running" not in body
