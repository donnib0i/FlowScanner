"""
Owner-only mode: the PIN is the only key, sign-ups close, and the door says
"being updated" rather than "private".

Changing the PIN alone did not make the scanner Dante's alone. Another path in
exists -- /join mints a magic link for any address, and the resulting session
cookie bypasses the PIN entirely, with no approval step. This switch closes
that path without deleting anything, so it can be reopened later.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_in(monkeypatch, tmp_path):
    """A fresh web.app imported under the given env, so module-level flags
    (the PIN, the mode) are read the way the deployed process reads them."""
    def build(owner_only, pin="213085"):
        # Never the tracked local store: these tests create accounts.
        monkeypatch.setenv("SCANNER_ACCOUNTS_DB", str(tmp_path / "accounts.db"))
        monkeypatch.setenv("SCANNER_PIN", pin)
        monkeypatch.setenv("SCANNER_REQUIRE_PIN", "1")
        monkeypatch.setenv("SCANNER_OWNER_ONLY", "1" if owner_only else "")
        import web.app as m
        importlib.reload(m)
        m._rl._windows.clear()
        return m, TestClient(m.app, raise_server_exceptions=False)
    yield build
    import web.app as m
    monkeypatch.delenv("SCANNER_OWNER_ONLY", raising=False)
    importlib.reload(m)


# ── the PIN is the only key ──────────────────────────────────────────────────
def test_the_new_pin_gets_in(app_in):
    m, c = app_in(owner_only=True)
    assert c.get("/api/vix", headers={"X-Pin": "213085"}).status_code != 401


def test_the_old_pin_does_not(app_in):
    m, c = app_in(owner_only=True)
    assert c.get("/api/vix", headers={"X-Pin": "4321"}).status_code == 401


def _active_session(m, email="someone@example.com"):
    """A cookie for a real, active account -- the way /join produces one."""
    if m._accounts is None:
        pytest.skip("accounts store unavailable in this environment")
    tok = m._accounts.request_access(email)["token"]
    assert m._accounts.redeem(tok) == email        # activates the account
    return m._accounts.issue_session(email)


def test_a_valid_account_session_no_longer_bypasses_the_pin(app_in):
    # This is the whole point: a signed cookie from /join used to be a way in
    # that the PIN knew nothing about.
    m, c = app_in(owner_only=True)
    cookie = _active_session(m)
    assert m._accounts.read_session(cookie) is not None, "fixture: session not valid"
    r = c.get("/api/vix", cookies={m.SESSION_COOKIE: cookie})
    assert r.status_code == 401, "an account session still bypasses the PIN"


def test_with_the_switch_off_a_session_still_works(app_in):
    # Nothing is deleted; the accounts table just stops being consulted.
    m, c = app_in(owner_only=False)
    cookie = _active_session(m)
    assert c.get("/api/vix", cookies={m.SESSION_COOKIE: cookie}).status_code != 401


# ── sign-ups close ───────────────────────────────────────────────────────────
def test_sign_up_is_refused_before_anything_is_minted(app_in):
    m, c = app_in(owner_only=True)
    r = c.post("/api/join", json={"email": "stranger@example.com"})
    assert r.status_code == 503
    assert "updated" in r.json()["detail"].lower()
    if m._accounts is not None:
        assert m._accounts.get_user("stranger@example.com") is None, \
            "a refused sign-up still created an account"


def test_the_join_page_shows_the_notice_and_not_the_form(app_in):
    m, c = app_in(owner_only=True)
    html = c.get("/join").text
    assert "being updated" in html
    assert "#f,h1,.card>p{display:none}" in html, "the sign-up form is still shown"


def test_the_join_page_is_normal_with_the_switch_off(app_in):
    m, c = app_in(owner_only=False)
    html = c.get("/join").text
    assert "being updated" not in html


# ── the door says what is happening ──────────────────────────────────────────
def test_the_app_page_carries_the_mode_to_the_gate(app_in):
    m, c = app_in(owner_only=True)
    assert "window.__OWNER_ONLY = true;" in c.get("/").text
    m, c = app_in(owner_only=False)
    assert "window.__OWNER_ONLY = false;" in c.get("/").text


def test_the_gate_says_updating_and_hides_sign_up_when_owner_only():
    js = open("web/static/app.js").read()
    body = js.split("function _promptPin(msg){")[1].split("\n}\n")[0]
    assert "window.__OWNER_ONLY" in body
    assert "being updated" in body
    # sign-up link is conditional on NOT updating
    assert "(updating?'':'<a class=\"gate-go\" href=\"/join\">" in body
    # owner sign-in is unconditional -- Dante still needs a way in
    assert "'<button class=\"gate-alt\" id=\"gate-pin\">Owner sign-in</button>'" in body


# ── a visitor who has not signed in is not a brute-forcer ────────────────────
def test_a_missing_pin_does_not_burn_the_brute_force_allowance(app_in):
    # The page polls several endpoints on load. Counting a MISSING pin as a
    # failed guess locked every visitor out within seconds of arriving, and
    # the lockout is a 429 the gate did not draw itself over -- so instead of
    # "being updated" they saw a dead page.
    m, c = app_in(owner_only=True)
    for _ in range(25):
        assert c.get("/api/vix").status_code == 401, "a missing pin was rate-limited"


def test_a_wrong_pin_still_is(app_in):
    m, c = app_in(owner_only=True)
    codes = [c.get("/api/vix", headers={"X-Pin": "000000"}).status_code for _ in range(12)]
    assert 429 in codes, "wrong-pin guesses are no longer throttled"


def test_the_right_pin_works_during_someone_elses_lockout(app_in):
    # The limiter must never be a way to lock the owner out.
    m, c = app_in(owner_only=True)
    for _ in range(12):
        c.get("/api/vix", headers={"X-Pin": "000000"})
    assert c.get("/api/vix", headers={"X-Pin": "213085"}).status_code != 401


def test_the_gate_is_drawn_over_a_lockout_too():
    js = open("web/static/app.js").read()
    body = js.split("function _handleAuth(resp){")[1].split("\n}\n")[0]
    assert "429" in body and "_promptPin(" in body.split("429")[1], \
        "a 429 on auth leaves the visitor with a dead page"
