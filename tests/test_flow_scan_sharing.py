"""
One scan, many viewers.

Refusing a second request was survivable when a scan took fifteen seconds. At
190 names it takes two minutes, and every tap, reload or second device inside
that window got an error it could not recover from -- reported as "error after
error". A scan is a property of the server, not of the tab that asked for it,
so a later request joins the one already running.

Note on shape: TestClient drives the app through a single event loop, so two
genuinely concurrent streams cannot be opened against it -- an earlier version
of this file tried, and the nested request simply waited for the first stream
to finish. These tests put a live session on the module the way a running scan
would, then make one request, which exercises the same branch without the race.
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("SCANNER_PIN", raising=False)
    monkeypatch.delenv("SCANNER_REQUIRE_PIN", raising=False)
    import web.app as webapp
    importlib.reload(webapp)
    webapp._reset_scan_session()
    yield webapp
    webapp._reset_scan_session()


def events(resp, limit=12):
    out = []
    for line in resp.iter_lines():
        if not line.startswith("data: "):
            continue
        item = json.loads(line[6:])
        if item.get("__ping__"):
            continue
        out.append(item)
        if item.get("__done__") or item.get("__error__") or len(out) >= limit:
            break
    return out


# ── The session object ────────────────────────────────────────────────────────
def test_a_late_subscriber_is_replayed_what_it_missed(app):
    """A viewer who joins and sees an empty feed cannot tell that from a broken
    scan."""
    s = app._ScanSession(tickers=2)
    s.publish({"__progress__": True, "i": 1, "n": 2})
    s.publish({"__signal__": True, "data": {"ticker": "NVDA"}})

    q = s.attach()
    got = [q.get_nowait() for _ in range(2)]
    assert [list(e)[0] for e in got] == ["__progress__", "__signal__"]


def test_every_subscriber_gets_events_published_after_it_joined(app):
    s = app._ScanSession(tickers=2)
    a, b = s.attach(), s.attach()
    s.publish({"__signal__": True, "data": {"ticker": "TSLA"}})
    assert a.get_nowait()["data"]["ticker"] == "TSLA"
    assert b.get_nowait()["data"]["ticker"] == "TSLA"


def test_finishing_closes_every_subscriber(app):
    s = app._ScanSession(tickers=1)
    a, b = s.attach(), s.attach()
    s.finish()
    assert a.get_nowait()["__done__"] is True
    assert b.get_nowait()["__done__"] is True
    assert not s.is_live()


def test_attaching_to_a_finished_scan_ends_immediately(app):
    s = app._ScanSession(tickers=1)
    s.publish({"__signal__": True, "data": {"ticker": "NVDA"}})
    s.finish()
    q = s.attach()
    assert q.get_nowait()["__signal__"] is True
    assert q.get_nowait()["__done__"] is True


def test_a_session_left_running_forever_goes_stale(app):
    """The publishing thread can die without finishing. Without an expiry, one
    wedged scan makes every later request join a session that will never emit
    another event."""
    s = app._ScanSession(tickers=1)
    s.started -= app.SCAN_STALE_AFTER_S + 1
    assert not s.is_live()


# ── Attach or start ───────────────────────────────────────────────────────────
# Tested at the helper rather than through an HTTP stream: TestClient runs the
# app on one event loop, so a request made while another stream is open simply
# waits for it, and a response is not closed until its generator returns. Both
# make a streaming test of concurrency a test of the harness.
def test_a_request_during_a_scan_joins_it_and_starts_no_new_work(app, monkeypatch):
    def _must_not_run(*a, **k):
        raise AssertionError("started a second scan instead of joining")
    monkeypatch.setattr(app, "scan_options_flow", _must_not_run)

    live = app._ScanSession(tickers=190)
    live.publish({"__signal__": True, "data": {"ticker": "NVDA"}})
    app._scan_session = live

    session, q, joined = app._attach_or_start(["NVDA"])
    assert joined is True
    assert session is live
    assert q.get_nowait()["data"]["ticker"] == "NVDA"   # replayed, not lost
    live.finish()


def test_the_joiner_knows_how_big_the_scan_it_joined_is(app, monkeypatch):
    """The notice says "joined the scan already running over N names", so the
    count has to come from the session rather than from this request."""
    monkeypatch.setattr(app, "scan_options_flow", lambda *a, **k: [])
    live = app._ScanSession(tickers=190)
    app._scan_session = live
    session, _, joined = app._attach_or_start(["NVDA"])
    assert joined and session.tickers == 190
    live.finish()


def test_a_stale_session_is_replaced_rather_than_joined(app, monkeypatch):
    """A scan whose thread died must not trap every later request in a session
    that will never emit another event."""
    ran = {}
    monkeypatch.setattr(app, "scan_options_flow",
                        lambda *a, **k: ran.setdefault("yes", True) or [])
    dead = app._ScanSession(tickers=5)
    dead.started -= app.SCAN_STALE_AFTER_S + 1
    app._scan_session = dead

    session, _, joined = app._attach_or_start(["NVDA"])
    assert joined is False
    assert session is not dead


def test_a_finished_scan_does_not_block_the_next_one(app, monkeypatch):
    monkeypatch.setattr(app, "scan_options_flow", lambda *a, **k: [])
    client = TestClient(app.app)
    for _ in range(2):
        with client.stream("GET", "/api/flow?tickers=NVDA") as r:
            evs = events(r)
        assert any(e.get("__done__") for e in evs), evs
        assert not any(e.get("__error__") for e in evs), evs


def test_the_rate_limit_leaves_room_for_a_reload(app, monkeypatch):
    """Joining starts no work, so the limit must not punish a user who tapped
    twice or reloaded the tab -- which is what produced 429s on top of the
    errors they were already seeing."""
    monkeypatch.setattr(app, "scan_options_flow", lambda *a, **k: [])
    client = TestClient(app.app)
    codes = []
    for _ in range(6):
        with client.stream("GET", "/api/flow?tickers=NVDA") as r:
            codes.append(r.status_code)
    assert codes.count(200) >= 5, codes
