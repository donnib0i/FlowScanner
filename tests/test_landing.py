"""
The front door.

/ is the landing page for a visitor. The scanner itself lives at /app. Someone
who already holds the PIN -- the owner, on the phone the PWA is installed on --
is sent from / to /app by the page's own head script before it paints, so the
installed icon, which points at /, still opens the scanner.
"""
import re

from fastapi.testclient import TestClient

import web.app as webapp

client = TestClient(webapp.app, raise_server_exceptions=False)
LANDING = open("web/templates/landing.html").read()


def test_root_is_the_landing_and_app_is_the_scanner():
    root = client.get("/").text
    app = client.get("/app").text
    assert "See where" in root and 'id="tab-flow"' not in root
    assert 'id="tab-flow"' in app and "See where" not in app


def test_a_stored_pin_skips_the_landing_before_paint():
    head = LANDING[: LANDING.index("</head>")]
    boot = head[: head.index("<style>")] if "<style>" in head else head
    assert "localStorage.getItem('scanner_pin')" in boot
    assert "location.replace('/app')" in boot
    # ...and it runs before the stylesheet, so nothing paints first
    assert head.index("location.replace('/app')") < head.index("<link href=")


def test_the_pwa_opens_the_scanner_not_the_landing():
    assert client.get("/manifest.json").json()["start_url"] == "/app"


def test_a_redeemed_magic_link_lands_in_the_scanner():
    src = open("web/app.py").read()
    assert 'headers={"Location": "/app"}' in src


def test_the_landing_pin_box_is_a_real_door_not_a_demo():
    # It checks the PIN against the API, in a header, and stores it the way
    # the app stores it -- so the app opens signed in.
    js = LANDING[LANDING.rindex("<script>"):]
    assert "fetch('/api/vix', { headers: { 'X-Pin': v } })" in js
    assert "localStorage.setItem('scanner_pin', v)" in js
    assert "location.href = '/app'" in js
    assert "?pin=" not in js and "pin=" not in js.split("X-Pin")[0][-80:], "the PIN must never travel in the URL"


def test_the_landing_reports_each_failure_honestly():
    js = LANDING[LANDING.rindex("<script>"):]
    for status in ("401", "429", "503"):
        assert f"r.status === {status}" in js, f"a {status} is not handled"
    assert "not right" in js and "Too many attempts" in js and "being updated" in js


def test_the_landing_never_claims_real_time():
    # The deployment serves delayed data and the app says so on every tab. A
    # front door that says "live" or "real-time" contradicts the product behind
    # it, which is the one thing this project never does.
    body = LANDING[LANDING.index("<body>"):LANDING.rindex("<script>")]
    text = re.sub(r"<[^>]+>", " ", body).lower()
    for phrase in ("real-time", "realtime", "streams in live", "live ·", " live "):
        assert phrase not in text, f"the landing claims {phrase!r}"


def test_the_sample_tape_says_it_is_sample():
    assert "SAMPLE DATA" in LANDING


def test_both_pages_are_served_no_store():
    for path in ("/", "/app"):
        cc = client.get(path).headers.get("cache-control", "")
        assert "no-store" in cc, f"{path} is cacheable: {cc!r}"


def test_the_landing_carries_the_security_headers_too():
    r = client.get("/")
    assert r.headers.get("x-frame-options") == "DENY"
    assert "content-security-policy" in r.headers
