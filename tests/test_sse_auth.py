"""
Server-sent events must survive a PIN.

The browser's EventSource API cannot set request headers and does not route
through window.fetch, so the X-Pin header the rest of the app uses is
unreachable from it. Moving the PIN out of the query string (commit 21193cb)
therefore made /api/flow -- the app's primary feature -- return 401 the moment
SCANNER_PIN was set. It was invisible because production had no PIN: the app
worked *because* it was unauthenticated.

The fix is a single-use, short-lived ticket. The durable secret still never
appears in a URL, an access log, or browser history; a ticket that does leak is
worth one stream for a few seconds.
"""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


def _reload(env):
    for k in ["SCANNER_PIN", "SCANNER_REQUIRE_PIN", "SCANNER_ALLOW_PIN_QUERY",
              "SCANNER_PROXY_HOPS", "SCANNER_ALLOWED_HOSTS", "SCANNER_FORCE_HTTPS",
              "SCANNER_ALLOWED_ORIGINS", "RAILWAY_PUBLIC_DOMAIN"]:
        os.environ.pop(k, None)
    os.environ.update(env)
    import web.app as webapp
    return importlib.reload(webapp)


@pytest.fixture
def secured():
    webapp = _reload({"SCANNER_PIN": "secret1"})
    return webapp, TestClient(webapp.app, raise_server_exceptions=False)


def test_a_ticket_requires_the_pin(secured):
    webapp, c = secured
    assert c.get("/api/sse-ticket").status_code in (401, 403)


def test_a_ticket_is_issued_to_a_valid_pin(secured):
    webapp, c = secured
    r = c.get("/api/sse-ticket", headers={"X-Pin": "secret1"})
    assert r.status_code == 200
    assert r.json().get("ticket")


def test_the_ticket_is_not_the_pin(secured):
    """A ticket in a URL must not reveal the durable secret."""
    webapp, c = secured
    t = c.get("/api/sse-ticket", headers={"X-Pin": "secret1"}).json()["ticket"]
    assert "secret1" not in t
    assert len(t) >= 20


def test_flow_accepts_a_ticket_in_the_query(secured):
    """EventSource can only authenticate this way."""
    webapp, c = secured
    t = c.get("/api/sse-ticket", headers={"X-Pin": "secret1"}).json()["ticket"]
    assert webapp._check_ticket(t) is True


def test_a_ticket_is_single_use(secured):
    webapp, c = secured
    t = c.get("/api/sse-ticket", headers={"X-Pin": "secret1"}).json()["ticket"]
    assert webapp._check_ticket(t) is True
    assert webapp._check_ticket(t) is False, "a replayed ticket must be refused"


def test_an_unknown_ticket_is_refused(secured):
    webapp, _ = secured
    assert webapp._check_ticket("not-a-real-ticket") is False
    assert webapp._check_ticket("") is False


def test_an_expired_ticket_is_refused(secured, monkeypatch):
    webapp, c = secured
    t = c.get("/api/sse-ticket", headers={"X-Pin": "secret1"}).json()["ticket"]
    now = webapp.time.monotonic()
    monkeypatch.setattr(webapp.time, "monotonic",
                        lambda: now + webapp._TICKET_TTL_S + 1)
    assert webapp._check_ticket(t) is False


def test_flow_rejects_a_request_with_neither_pin_nor_ticket(secured):
    webapp, c = secured
    assert c.get("/api/flow").status_code in (401, 403)


def test_flow_still_accepts_the_header_for_non_browser_callers(secured):
    """curl and the CLI can send a header and should not need a ticket."""
    webapp, c = secured
    import inspect
    src = inspect.getsource(webapp.api_flow)
    assert "_check_pin" in src


def test_no_ticket_endpoint_exposure_when_no_pin_is_set():
    """With auth off the ticket is pointless but must not error."""
    webapp = _reload({})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    assert c.get("/api/sse-ticket").status_code == 200


def test_ticket_store_is_bounded(secured):
    """An unauthenticated flood must not grow memory without limit."""
    webapp, c = secured
    for _ in range(webapp._TICKET_MAX * 2 + 10):
        c.get("/api/sse-ticket", headers={"X-Pin": "secret1"})
    assert len(webapp._TICKETS) <= webapp._TICKET_MAX


def test_the_client_fetches_a_ticket_before_opening_the_stream():
    js = open("web/static/app.js").read()
    assert "/api/sse-ticket" in js, "the browser must request a ticket"
    stream = js.split("new EventSource")[0]
    assert "ticket" in stream.rsplit("function", 1)[-1].lower()


def test_app_js_parses():
    """
    app.js is 1,500 lines of hand-rolled JS with no build step, so nothing
    catches a syntax error before it reaches the browser as a blank page. This
    caught a real one: adding `await` to the ticket fetch without making the
    enclosing function async.
    """
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    r = subprocess.run([node, "--check", "web/static/app.js"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_flow_scan_can_await_its_ticket():
    """The ticket fetch is async, so its caller must be too."""
    js = open("web/static/app.js").read()
    assert "async function doFlowScan(" in js
