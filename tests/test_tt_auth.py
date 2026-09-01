"""
Authentication is what decides whether the FLOW tab is live or delayed, and it
is the one path with no coverage: prod has been falling back to yfinance
because a password login triggers a device challenge a container cannot answer.

These tests pin the logic that fixes that -- remember-token before password,
env-var tokens for hosts with no writable home, and no infinite retry.
"""
import asyncio
import json

import pytest

from data import tt_flow


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def ok(session="sess-1", remember="rem-new"):
    return FakeResponse(201, {"data": {"session-token": session,
                                       "remember-token": remember}})


CHALLENGE = FakeResponse(403, {"error": {"code": "device_challenge_required"}})


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.tt_* files, and never reuse a cache."""
    monkeypatch.setattr(tt_flow, "_SESSION_PATH", str(tmp_path / "session.json"))
    monkeypatch.setattr(tt_flow, "_CHALLENGE_PATH", str(tmp_path / "challenge.txt"))
    monkeypatch.delenv("TT_CHALLENGE_TOKEN", raising=False)
    monkeypatch.delenv("TT_REMEMBER_TOKEN", raising=False)
    tt_flow.invalidate_auth()
    yield
    tt_flow.invalidate_auth()


def record_posts(monkeypatch, responses):
    """Stub /sessions with a scripted sequence, capturing each request."""
    sent = []
    queue = list(responses)

    async def _post(self, payload, challenge_token=""):
        sent.append({"payload": payload, "challenge": challenge_token})
        return queue.pop(0) if queue else FakeResponse(500)

    monkeypatch.setattr(tt_flow.TTAuth, "_post_session", _post)
    return sent


# ── Challenge token source ────────────────────────────────────────────────────
def test_challenge_token_prefers_env_over_file(monkeypatch, tmp_path):
    open(tt_flow._CHALLENGE_PATH, "w").write("from-file")
    monkeypatch.setenv("TT_CHALLENGE_TOKEN", "from-env")
    assert tt_flow._load_challenge_token() == "from-env"


def test_challenge_token_falls_back_to_file():
    open(tt_flow._CHALLENGE_PATH, "w").write("  from-file\n")
    assert tt_flow._load_challenge_token() == "from-file"


def test_clearing_challenge_leaves_env_alone(monkeypatch):
    monkeypatch.setenv("TT_CHALLENGE_TOKEN", "from-env")
    tt_flow._clear_challenge_token()
    assert tt_flow._load_challenge_token() == "from-env"


# ── Remember-token login ──────────────────────────────────────────────────────
def test_remember_token_login_never_sends_the_password(monkeypatch):
    tt_flow._save_remember_token("rem-old")
    sent = record_posts(monkeypatch, [ok()])

    auth = tt_flow.TTAuth("dante", "hunter2")
    assert asyncio.run(auth.login()) is True

    assert len(sent) == 1
    assert sent[0]["payload"]["remember-token"] == "rem-old"
    assert "password" not in sent[0]["payload"]


def test_successful_login_persists_the_rotated_remember_token(monkeypatch):
    record_posts(monkeypatch, [ok(remember="rem-rotated")])

    auth = tt_flow.TTAuth("dante", "hunter2")
    assert asyncio.run(auth.login()) is True

    stored = json.loads(open(tt_flow._SESSION_PATH).read())
    assert stored["remember-token"] == "rem-rotated"
    assert tt_flow._load_remember_token() == "rem-rotated"


def test_stale_remember_token_is_dropped_and_password_used(monkeypatch):
    """A consumed token must not be retried forever on every future login."""
    tt_flow._save_remember_token("rem-dead")
    sent = record_posts(monkeypatch, [FakeResponse(401), ok()])

    auth = tt_flow.TTAuth("dante", "hunter2")
    assert asyncio.run(auth.login()) is True

    assert len(sent) == 2
    assert sent[0]["payload"].get("remember-token") == "rem-dead"
    assert sent[1]["payload"].get("password") == "hunter2"


def test_env_remember_token_seeds_a_host_with_no_writable_home(monkeypatch):
    monkeypatch.setenv("TT_REMEMBER_TOKEN", "rem-from-railway")
    sent = record_posts(monkeypatch, [ok()])

    auth = tt_flow.TTAuth("dante", "")
    assert asyncio.run(auth.login()) is True
    assert sent[0]["payload"]["remember-token"] == "rem-from-railway"


def test_no_password_and_no_remember_token_fails_clearly(monkeypatch):
    record_posts(monkeypatch, [])
    auth = tt_flow.TTAuth("dante", "")
    assert asyncio.run(auth.login()) is False
    assert "TT_PASSWORD" in tt_flow.last_error()


# ── Device challenge ──────────────────────────────────────────────────────────
def test_challenge_is_retried_with_the_env_token(monkeypatch):
    monkeypatch.setenv("TT_CHALLENGE_TOKEN", "chal-123")
    sent = record_posts(monkeypatch, [CHALLENGE, ok()])

    auth = tt_flow.TTAuth("dante", "hunter2")
    assert asyncio.run(auth.login()) is True

    assert sent[0]["challenge"] == ""
    assert sent[1]["challenge"] == "chal-123"


def test_challenge_retry_does_not_recurse_forever(monkeypatch):
    """Re-reading the token after a rejection would resend the same dead value."""
    monkeypatch.setenv("TT_CHALLENGE_TOKEN", "chal-dead")
    sent = record_posts(monkeypatch, [CHALLENGE, CHALLENGE])

    auth = tt_flow.TTAuth("dante", "hunter2")
    assert asyncio.run(auth.login()) is False
    assert len(sent) == 2
    assert "device_challenge_required" in tt_flow.last_error()


def test_challenge_without_a_token_reports_why(monkeypatch):
    record_posts(monkeypatch, [CHALLENGE])
    auth = tt_flow.TTAuth("dante", "hunter2")
    assert asyncio.run(auth.login()) is False
    assert "TT_CHALLENGE_TOKEN" in tt_flow.last_error()


def test_other_403_is_not_reported_as_a_challenge(monkeypatch):
    record_posts(monkeypatch, [FakeResponse(403, {"error": {"code": "invalid_credentials"}})])
    auth = tt_flow.TTAuth("dante", "hunter2")
    assert asyncio.run(auth.login()) is False
    assert "invalid_credentials" in tt_flow.last_error()
    assert "device_challenge" not in tt_flow.last_error()


# ── Session cache ─────────────────────────────────────────────────────────────
def test_session_is_reused_across_scans(monkeypatch):
    """--live re-scans every 45s; one login must cover them all."""
    calls = []

    async def _setup(self):
        calls.append(self.username)
        self.session_tok = "sess"
        self.dx_url = "wss://dx"
        return True

    monkeypatch.setattr(tt_flow.TTAuth, "setup", _setup)

    async def run():
        a = await tt_flow._get_auth("dante", "hunter2")
        b = await tt_flow._get_auth("dante", "hunter2")
        return a, b

    a, b = asyncio.run(run())
    assert a is b
    assert len(calls) == 1


def test_failed_auth_is_not_cached(monkeypatch):
    async def _setup(self):
        return False

    monkeypatch.setattr(tt_flow.TTAuth, "setup", _setup)
    assert asyncio.run(tt_flow._get_auth("dante", "hunter2")) is None
    assert tt_flow._AUTH_CACHE is None


def test_expired_session_re_authenticates(monkeypatch):
    calls = []

    async def _setup(self):
        calls.append(1)
        self.session_tok = "sess"
        return True

    monkeypatch.setattr(tt_flow.TTAuth, "setup", _setup)
    asyncio.run(tt_flow._get_auth("dante", "hunter2"))
    monkeypatch.setattr(tt_flow, "_AUTH_EXPIRES", 0.0)
    asyncio.run(tt_flow._get_auth("dante", "hunter2"))
    assert len(calls) == 2
