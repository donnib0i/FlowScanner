"""
Authentication decides whether the FLOW tab is live or delayed, and the flow it
has to implement is the one TastyTrade actually serves -- verified against the
live API on 2026-09-06:

  1. POST /sessions            -> 403 device_challenge_required, and the
                                  challenge token arrives in the
                                  `x-tastyworks-challenge-token` RESPONSE HEADER
  2. POST /device-challenge    -> this is what sends the OTP, by SMS
  3. POST /sessions + both     -> 201 with a session-token

The previous implementation read the challenge token from an env var or
~/.tt_challenge.txt, never called /device-challenge, and told the user to check
their email. All three were wrong: nothing is emailed, and nothing is sent at
all until step 2 runs. These tests pin the real flow.

TastyTrade also issues NO remember-token on this account -- `remember-me: true`
is ignored -- so a session lives ~8 hours and that is the whole story. The
session is cached to disk so one OTP covers a trading day across processes.
"""
import asyncio
import json
import os
import time

import pytest

from data import tt_flow


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def ok(session="sess-1", expires=None):
    """A 201 from /sessions. TastyTrade returns no remember-token."""
    data = {"session-token": session}
    if expires:
        data["session-expiration"] = expires
    return FakeResponse(201, {"data": data})


def challenge(token="chal-from-header"):
    """The real 403: the token comes back in a response header."""
    return FakeResponse(
        403,
        {"error": {"code": "device_challenge_required",
                   "redirect": {"url": "/device-challenge", "method": "POST"}}},
        headers={"x-tastyworks-challenge-token": token},
    )


def otp_sent(phone="********2217"):
    return FakeResponse(200, {"data": {"phone": phone, "step": "otp_verification"}})


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.tt_* files, and never reuse a cache."""
    monkeypatch.setattr(tt_flow, "_SESSION_PATH", str(tmp_path / "session.json"))
    monkeypatch.delenv("TT_OTP", raising=False)
    monkeypatch.delenv("TT_USERNAME", raising=False)
    monkeypatch.delenv("TT_PASSWORD", raising=False)
    tt_flow.invalidate_auth()
    yield
    tt_flow.invalidate_auth()


def script(monkeypatch, sessions, challenges=None):
    """
    Stub both endpoints with scripted responses, capturing every request.
    `sessions` feeds POST /sessions; `challenges` feeds POST /device-challenge.
    """
    sent = {"sessions": [], "challenge": []}
    sq, cq = list(sessions), list(challenges or [])

    async def _post_session(self, payload, challenge_token="", otp=""):
        sent["sessions"].append({"payload": payload,
                                 "challenge": challenge_token, "otp": otp})
        return sq.pop(0) if sq else FakeResponse(500)

    async def _post_device_challenge(self, challenge_token):
        sent["challenge"].append(challenge_token)
        return cq.pop(0) if cq else FakeResponse(500)

    monkeypatch.setattr(tt_flow.TTAuth, "_post_session", _post_session)
    monkeypatch.setattr(tt_flow.TTAuth, "_post_device_challenge", _post_device_challenge)
    return sent


def login(user="u", pw="p"):
    auth = tt_flow.TTAuth(user, pw)
    return auth, asyncio.run(auth.login())


# ── The three-step challenge flow ─────────────────────────────────────────────
def test_challenge_token_is_read_from_the_response_header(monkeypatch):
    """The token is never user-supplied; it comes back on the 403."""
    monkeypatch.setenv("TT_OTP", "123456")
    sent = script(monkeypatch, [challenge("hdr-tok"), ok()], [otp_sent()])
    _, result = login()
    assert result is True
    assert sent["challenge"] == ["hdr-tok"]
    assert sent["sessions"][1]["challenge"] == "hdr-tok"


def test_device_challenge_is_called_before_the_otp_retry(monkeypatch):
    """
    Nothing is sent to the user until /device-challenge runs. Skipping it was
    the reason no verification code ever arrived.
    """
    monkeypatch.setenv("TT_OTP", "123456")
    sent = script(monkeypatch, [challenge(), ok()], [otp_sent()])
    login()
    assert len(sent["challenge"]) == 1, "/device-challenge must be called"
    assert sent["sessions"][1]["otp"] == "123456"


def test_otp_and_challenge_token_are_sent_together_on_the_retry(monkeypatch):
    monkeypatch.setenv("TT_OTP", "654321")
    sent = script(monkeypatch, [challenge("tok-9"), ok()], [otp_sent()])
    login()
    retry = sent["sessions"][1]
    assert retry["challenge"] == "tok-9" and retry["otp"] == "654321"


def test_first_session_post_carries_no_challenge_headers(monkeypatch):
    monkeypatch.setenv("TT_OTP", "123456")
    sent = script(monkeypatch, [challenge(), ok()], [otp_sent()])
    login()
    assert sent["sessions"][0]["challenge"] == ""
    assert sent["sessions"][0]["otp"] == ""


def test_device_challenge_failure_is_reported_not_retried(monkeypatch):
    monkeypatch.setenv("TT_OTP", "123456")
    sent = script(monkeypatch, [challenge()], [FakeResponse(500)])
    _, result = login()
    assert result is False
    assert len(sent["sessions"]) == 1, "must not retry /sessions blindly"
    assert "device-challenge" in tt_flow.last_error()


def test_a_403_with_no_challenge_header_fails_clearly(monkeypatch):
    """Defensive: the documented flow guarantees the header, so say so if absent."""
    bare = FakeResponse(403, {"error": {"code": "device_challenge_required"}})
    script(monkeypatch, [bare], [otp_sent()])
    _, result = login()
    assert result is False
    assert "challenge token" in tt_flow.last_error().lower()


def test_other_403_is_not_treated_as_a_challenge(monkeypatch):
    denied = FakeResponse(403, {"error": {"code": "invalid_credentials"}})
    sent = script(monkeypatch, [denied], [otp_sent()])
    _, result = login()
    assert result is False
    assert sent["challenge"] == [], "must not start a device challenge"
    assert "device_challenge" not in tt_flow.last_error()


def test_wrong_otp_fails_without_looping(monkeypatch):
    monkeypatch.setenv("TT_OTP", "000000")
    rejected = FakeResponse(401, {"error": {"code": "invalid_otp"}})
    sent = script(monkeypatch, [challenge(), rejected], [otp_sent()])
    _, result = login()
    assert result is False
    assert len(sent["sessions"]) == 2, "one retry only, no OTP loop"


# ── OTP acquisition ───────────────────────────────────────────────────────────
def test_otp_env_var_is_used_without_prompting(monkeypatch):
    monkeypatch.setenv("TT_OTP", "424242")
    monkeypatch.setattr(tt_flow, "_prompt_for_otp",
                        lambda *a, **k: pytest.fail("must not prompt when TT_OTP is set"))
    script(monkeypatch, [challenge(), ok()], [otp_sent()])
    _, result = login()
    assert result is True


def test_non_interactive_host_fails_instead_of_hanging(monkeypatch):
    """
    Railway has no TTY and no phone. It must fail fast with an explanation
    rather than block a worker forever on a prompt nobody can answer.
    """
    monkeypatch.setattr(tt_flow.sys.stdin, "isatty", lambda: False, raising=False)
    script(monkeypatch, [challenge()], [otp_sent()])
    _, result = login()
    assert result is False
    err = tt_flow.last_error().lower()
    assert "otp" in err and ("non-interactive" in err or "tty" in err)


def test_otp_prompt_is_used_when_interactive(monkeypatch):
    monkeypatch.setattr(tt_flow.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(tt_flow, "_prompt_for_otp", lambda phone: "777777")
    sent = script(monkeypatch, [challenge(), ok()], [otp_sent()])
    _, result = login()
    assert result is True
    assert sent["sessions"][1]["otp"] == "777777"


# ── Session persistence: one OTP covers the trading day ───────────────────────
def test_session_is_persisted_with_its_expiry(monkeypatch):
    monkeypatch.setenv("TT_OTP", "123456")
    exp = "2099-01-01T00:00:00.000Z"
    script(monkeypatch, [challenge(), ok(session="sess-live", expires=exp)], [otp_sent()])
    login()
    stored = json.loads(open(tt_flow._SESSION_PATH).read())
    assert stored["session-token"] == "sess-live"
    assert stored["expires-at"] == exp


def test_stored_session_is_reused_without_any_network_call(monkeypatch):
    """The point of the cache: a second process must not need a new OTP."""
    tt_flow._save_session("sess-cached", "2099-01-01T00:00:00.000Z")
    sent = script(monkeypatch, [ok()], [otp_sent()])
    auth, result = login()
    assert result is True
    assert auth.session_tok == "sess-cached"
    assert sent["sessions"] == [], "must not hit /sessions with a valid session"


def test_expired_session_is_discarded_and_re_authenticated(monkeypatch):
    monkeypatch.setenv("TT_OTP", "123456")
    tt_flow._save_session("sess-old", "2000-01-01T00:00:00.000Z")
    sent = script(monkeypatch, [challenge(), ok(session="sess-fresh")], [otp_sent()])
    auth, result = login()
    assert result is True
    assert auth.session_tok == "sess-fresh"
    assert len(sent["sessions"]) == 2


def test_session_near_expiry_is_treated_as_expired(monkeypatch):
    """A token expiring mid-scan is worse than one already gone."""
    monkeypatch.setenv("TT_OTP", "123456")
    soon = time.time() + 60
    tt_flow._save_session("sess-edge", tt_flow._iso_from_epoch(soon))
    sent = script(monkeypatch, [challenge(), ok(session="sess-new")], [otp_sent()])
    auth, _ = login()
    assert auth.session_tok == "sess-new"
    assert len(sent["sessions"]) == 2


def test_corrupt_session_file_does_not_crash_login(monkeypatch):
    monkeypatch.setenv("TT_OTP", "123456")
    open(tt_flow._SESSION_PATH, "w").write("{not json")
    script(monkeypatch, [challenge(), ok()], [otp_sent()])
    _, result = login()
    assert result is True


def test_session_without_expiry_is_not_trusted(monkeypatch):
    """No expiry means unknown age. Re-auth rather than use a possibly dead token."""
    monkeypatch.setenv("TT_OTP", "123456")
    open(tt_flow._SESSION_PATH, "w").write(json.dumps({"session-token": "sess-x"}))
    sent = script(monkeypatch, [challenge(), ok(session="sess-y")], [otp_sent()])
    auth, _ = login()
    assert auth.session_tok == "sess-y"
    assert len(sent["sessions"]) == 2


# ── Security ──────────────────────────────────────────────────────────────────
def test_session_file_is_owner_only(monkeypatch):
    tt_flow._save_session("sess-secret", "2099-01-01T00:00:00.000Z")
    mode = os.stat(tt_flow._SESSION_PATH).st_mode & 0o777
    assert mode == 0o600, f"session file is {oct(mode)}, must be 0600"


def test_credentials_file_is_owner_only(tmp_path):
    path = tmp_path / "creds.json"
    tt_flow.save_credentials("u", "p", path=str(path))
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_tokens_never_appear_in_the_error_string(monkeypatch):
    """last_error() is surfaced through /api/status to the browser."""
    monkeypatch.setenv("TT_OTP", "999888")
    rejected = FakeResponse(401, {"error": {"code": "invalid_otp"},
                                  "session-token": "SUPERSECRET"})
    script(monkeypatch, [challenge("CHALSECRET"), rejected], [otp_sent()])
    login()
    err = tt_flow.last_error()
    assert "CHALSECRET" not in err
    assert "SUPERSECRET" not in err
    assert "999888" not in err


def test_error_text_never_mentions_email(monkeypatch):
    """Nothing is emailed. The old message sent the user hunting for hours."""
    monkeypatch.setattr(tt_flow.sys.stdin, "isatty", lambda: False, raising=False)
    script(monkeypatch, [challenge()], [otp_sent()])
    login()
    assert "email" not in tt_flow.last_error().lower()


def test_password_is_never_written_to_the_session_file(monkeypatch):
    monkeypatch.setenv("TT_OTP", "123456")
    script(monkeypatch, [challenge(), ok()], [otp_sent()])
    login(pw="hunter2")
    assert "hunter2" not in open(tt_flow._SESSION_PATH).read()


def test_no_password_fails_before_any_network_call(monkeypatch):
    sent = script(monkeypatch, [ok()], [otp_sent()])
    _, result = login(pw="")
    assert result is False
    assert sent["sessions"] == []


# ── The removed model must stay removed ───────────────────────────────────────
def test_challenge_token_env_and_file_are_gone(monkeypatch):
    """
    TT_CHALLENGE_TOKEN and ~/.tt_challenge.txt encoded a flow that does not
    exist. Leaving them behind invites someone to 'fix' auth by setting them.
    """
    for gone in ("_load_challenge_token", "_clear_challenge_token", "_CHALLENGE_PATH"):
        assert not hasattr(tt_flow, gone), f"{gone} should have been removed"


def test_remember_token_seeding_is_gone(monkeypatch):
    """TastyTrade issues none, so TT_REMEMBER_TOKEN could never work."""
    assert not hasattr(tt_flow, "_load_remember_token")
    assert not hasattr(tt_flow, "_save_remember_token")


# ── Auth caching across scans ─────────────────────────────────────────────────
def test_auth_is_reused_across_scans(monkeypatch):
    monkeypatch.setenv("TT_OTP", "123456")
    sent = script(monkeypatch, [challenge(), ok()], [otp_sent()])

    async def _tokens(self):
        self.dx_token, self.dx_url = "dx", "wss://x/realtime"
        return True

    monkeypatch.setattr(tt_flow.TTAuth, "get_quote_tokens", _tokens)
    asyncio.run(tt_flow._get_auth("u", "p"))
    asyncio.run(tt_flow._get_auth("u", "p"))
    assert len(sent["sessions"]) == 2, "second scan must reuse the cached auth"


def test_failed_auth_is_not_cached(monkeypatch):
    monkeypatch.setattr(tt_flow.sys.stdin, "isatty", lambda: False, raising=False)
    script(monkeypatch, [challenge(), challenge()], [otp_sent(), otp_sent()])
    assert asyncio.run(tt_flow._get_auth("u", "p")) is None
    assert tt_flow._AUTH_CACHE is None


# ── The SMS is only sent when it can be answered ──────────────────────────────
# Production proved this the hard way: Railway has a username and password, no
# tty and no OAuth grant, so every flow scan POSTed /device-challenge, texted
# Dante's phone, and then discovered it could not read the code. Observed live
# 2026-09-09 in /api/status: "device challenge sent an OTP by SMS to ...2217,
# but this host is non-interactive". With no PIN set, anyone with the URL could
# trigger that.
@pytest.mark.asyncio
async def test_no_sms_is_sent_when_the_host_cannot_answer_it(monkeypatch):
    from data import tt_flow as T

    a = T.TTAuth("user", "pass")
    sent = []

    async def _post(challenge_token):
        sent.append(challenge_token)
        raise AssertionError("SMS triggered on a host that cannot answer")
    monkeypatch.setattr(a, "_post_device_challenge", _post)
    monkeypatch.setattr(T.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.delenv("TT_OTP", raising=False)

    class _R403:
        headers = {"x-tastyworks-challenge-token": "tok"}

    assert await a._device_challenge({}, _R403()) is False
    assert sent == []
    assert "non-interactive" in T.last_error().lower()


@pytest.mark.asyncio
async def test_the_sms_is_still_sent_when_a_tty_can_answer(monkeypatch):
    from data import tt_flow as T

    a = T.TTAuth("user", "pass")
    sent = []

    class _C:
        status_code = 500          # stop the flow right after the send
        def json(self): return {}

    async def _post(challenge_token):
        sent.append(challenge_token)
        return _C()
    monkeypatch.setattr(a, "_post_device_challenge", _post)
    monkeypatch.setattr(T.sys.stdin, "isatty", lambda: True, raising=False)

    class _R403:
        headers = {"x-tastyworks-challenge-token": "tok"}

    assert await a._device_challenge({}, _R403()) is False
    assert sent == ["tok"]
