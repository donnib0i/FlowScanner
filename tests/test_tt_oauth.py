"""
OAuth2 personal grant — the only auth path a container can use.

The password flow needs an SMS device challenge, so Railway can never complete
it and prod has always fallen back to delayed yfinance. A personal grant swaps
a non-expiring refresh token for a 15-minute access token with no challenge,
which is what makes unattended auth possible.

Two details that differ from the session flow and are easy to get wrong:
  - OAuth sends `Authorization: Bearer <token>`; the session flow sends the
    raw session token with no prefix.
  - Every request needs a User-Agent in product/version form.

Refreshing does not rotate the refresh token: none is returned, and the one
held stays valid for the life of the grant.
"""
import asyncio
import time

import pytest

from data import tt_flow


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}
        self.text = str(payload)

    def json(self):
        return self._payload


def token_ok(access="acc-1", expires_in=900):
    return FakeResponse(200, {"access_token": access, "token_type": "Bearer",
                              "expires_in": expires_in})


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(tt_flow, "_SESSION_PATH", str(tmp_path / "session.json"))
    for k in ("TT_CLIENT_SECRET", "TT_REFRESH_TOKEN", "TT_CLIENT_ID",
              "TT_USERNAME", "TT_PASSWORD", "TT_OTP"):
        monkeypatch.delenv(k, raising=False)
    tt_flow.invalidate_auth()
    yield
    tt_flow.invalidate_auth()


def with_grant(monkeypatch, secret="sec", refresh="ref", client_id=None):
    monkeypatch.setenv("TT_CLIENT_SECRET", secret)
    monkeypatch.setenv("TT_REFRESH_TOKEN", refresh)
    if client_id:
        monkeypatch.setenv("TT_CLIENT_ID", client_id)


def capture_token_posts(monkeypatch, responses):
    sent = []
    queue = list(responses)

    async def _post(self, payload):
        sent.append(payload)
        return queue.pop(0) if queue else FakeResponse(500)

    monkeypatch.setattr(tt_flow.TTAuth, "_post_oauth_token", _post)
    return sent


# ── Configuration ─────────────────────────────────────────────────────────────
def test_oauth_is_configured_when_both_values_are_present(monkeypatch):
    with_grant(monkeypatch)
    assert tt_flow.oauth_configured() is True


def test_a_refresh_token_alone_is_not_enough(monkeypatch):
    monkeypatch.setenv("TT_REFRESH_TOKEN", "ref")
    assert tt_flow.oauth_configured() is False


def test_a_client_secret_alone_is_not_enough(monkeypatch):
    monkeypatch.setenv("TT_CLIENT_SECRET", "sec")
    assert tt_flow.oauth_configured() is False


def test_nothing_configured_is_not_oauth():
    assert tt_flow.oauth_configured() is False


# ── The refresh exchange ──────────────────────────────────────────────────────
def test_refresh_token_is_exchanged_for_an_access_token(monkeypatch):
    with_grant(monkeypatch)
    sent = capture_token_posts(monkeypatch, [token_ok("acc-live")])
    auth = tt_flow.TTAuth("", "")
    assert asyncio.run(auth.login()) is True
    assert auth.access_tok == "acc-live"
    assert sent[0]["grant_type"] == "refresh_token"
    assert sent[0]["refresh_token"] == "ref"
    assert sent[0]["client_secret"] == "sec"


def test_client_id_is_sent_only_when_configured(monkeypatch):
    """The server infers it from the refresh token; sending a wrong one fails."""
    with_grant(monkeypatch)
    sent = capture_token_posts(monkeypatch, [token_ok()])
    asyncio.run(tt_flow.TTAuth("", "").login())
    assert "client_id" not in sent[0]

    tt_flow.invalidate_auth()
    with_grant(monkeypatch, client_id="cid-9")
    sent2 = capture_token_posts(monkeypatch, [token_ok()])
    asyncio.run(tt_flow.TTAuth("", "").login())
    assert sent2[0]["client_id"] == "cid-9"


def test_no_password_is_ever_sent_on_the_oauth_path(monkeypatch):
    with_grant(monkeypatch)
    sent = capture_token_posts(monkeypatch, [token_ok()])
    asyncio.run(tt_flow.TTAuth("user", "hunter2").login())
    assert "password" not in sent[0]
    assert "hunter2" not in str(sent[0])


def test_a_rejected_grant_reports_why(monkeypatch):
    with_grant(monkeypatch)
    capture_token_posts(monkeypatch, [FakeResponse(401, {"error": "invalid_grant"})])
    auth = tt_flow.TTAuth("", "")
    assert asyncio.run(auth.login()) is False
    assert "oauth" in tt_flow.last_error().lower()


def test_the_refresh_token_is_never_leaked_into_an_error(monkeypatch):
    """last_error() is served to the browser through /api/status."""
    with_grant(monkeypatch, secret="SECRETVAL", refresh="REFRESHVAL")
    capture_token_posts(monkeypatch, [FakeResponse(400, {"error": "bad"})])
    asyncio.run(tt_flow.TTAuth("", "").login())
    err = tt_flow.last_error()
    assert "SECRETVAL" not in err and "REFRESHVAL" not in err


# ── Token lifetime ────────────────────────────────────────────────────────────
def test_the_access_token_is_reused_until_it_nears_expiry(monkeypatch):
    with_grant(monkeypatch)
    sent = capture_token_posts(monkeypatch, [token_ok(), token_ok()])
    auth = tt_flow.TTAuth("", "")
    asyncio.run(auth.login())
    asyncio.run(auth.login())
    assert len(sent) == 1, "second login must reuse the live access token"


def test_an_expiring_token_is_refreshed_early(monkeypatch):
    """A token that dies mid-scan is worse than one already known to be dead."""
    with_grant(monkeypatch)
    sent = capture_token_posts(monkeypatch, [token_ok(expires_in=30), token_ok("acc-2")])
    auth = tt_flow.TTAuth("", "")
    asyncio.run(auth.login())
    asyncio.run(auth.login())
    assert len(sent) == 2
    assert auth.access_tok == "acc-2"


def test_refreshing_does_not_expect_a_new_refresh_token(monkeypatch):
    """The grant's refresh token is not rotated; treating it as such loses it."""
    with_grant(monkeypatch)
    capture_token_posts(monkeypatch, [token_ok()])
    asyncio.run(tt_flow.TTAuth("", "").login())
    assert tt_flow._load_oauth()[1] == "ref", "refresh token must be untouched"


# ── Headers ───────────────────────────────────────────────────────────────────
def test_oauth_requests_use_a_bearer_header(monkeypatch):
    with_grant(monkeypatch)
    capture_token_posts(monkeypatch, [token_ok("acc-x")])
    auth = tt_flow.TTAuth("", "")
    asyncio.run(auth.login())
    assert auth._headers()["Authorization"] == "Bearer acc-x"


def test_session_requests_send_the_raw_token(monkeypatch):
    """The session flow takes no Bearer prefix -- adding one breaks it."""
    auth = tt_flow.TTAuth("u", "p")
    auth.session_tok = "sess-raw"
    assert auth._headers()["Authorization"] == "sess-raw"


def test_every_request_carries_a_user_agent():
    auth = tt_flow.TTAuth("u", "p")
    auth.session_tok = "s"
    ua = auth._headers().get("User-Agent", "")
    assert "/" in ua, "User-Agent must be product/version"


# ── Precedence ────────────────────────────────────────────────────────────────
def test_oauth_is_preferred_over_a_cached_session(monkeypatch):
    """
    A grant needs no SMS and no stored session, so it is the better path
    whenever it is available.
    """
    with_grant(monkeypatch)
    tt_flow._save_session("sess-cached", "2099-01-01T00:00:00.000Z")
    capture_token_posts(monkeypatch, [token_ok("acc-pref")])
    auth = tt_flow.TTAuth("u", "p")
    asyncio.run(auth.login())
    assert auth.access_tok == "acc-pref"
    assert auth.session_tok == ""


def test_without_a_grant_the_session_path_still_works(monkeypatch):
    tt_flow._save_session("sess-cached", "2099-01-01T00:00:00.000Z")
    auth = tt_flow.TTAuth("u", "p")
    assert asyncio.run(auth.login()) is True
    assert auth.session_tok == "sess-cached"
    assert auth.access_tok == ""


def test_a_failing_grant_does_not_silently_fall_back_to_the_password(monkeypatch):
    """
    A broken grant is an operator error worth surfacing. Falling through to the
    password path would trigger an SMS nobody is waiting for, and on a container
    would just hang the scan instead of reporting the real problem.
    """
    with_grant(monkeypatch)
    capture_token_posts(monkeypatch, [FakeResponse(401, {"error": "invalid_grant"})])

    async def _boom(self):
        raise AssertionError("password login must not be attempted")

    monkeypatch.setattr(tt_flow.TTAuth, "_password_login", _boom)
    assert asyncio.run(tt_flow.TTAuth("u", "p").login()) is False


# ── Availability gate ─────────────────────────────────────────────────────────
def test_a_grant_alone_makes_the_flow_path_available(monkeypatch):
    """
    core/flow.py decides whether to attempt TastyTrade at all. A grant needs no
    username and no SMS, so requiring a username would keep prod on the yfinance
    fallback even when it is fully able to authenticate.
    """
    import importlib
    with_grant(monkeypatch)
    monkeypatch.delenv("TT_USERNAME", raising=False)
    monkeypatch.delenv("TT_PASSWORD", raising=False)
    monkeypatch.setattr(tt_flow, "load_credentials", lambda: ("", ""))
    monkeypatch.setattr(tt_flow, "_load_session", lambda: ("", ""))
    import core.flow as flow
    importlib.reload(flow)
    assert flow._TT_AVAILABLE is True


def test_the_scan_entrypoint_does_not_demand_a_username(monkeypatch):
    """scan_options_flow_tt bailed on a missing username before reaching auth."""
    import inspect
    src = inspect.getsource(tt_flow.scan_options_flow_tt)
    assert "oauth_configured()" in src, \
        "the credential gate must let an OAuth grant through"


# ── Credential file ───────────────────────────────────────────────────────────
def test_a_grant_can_live_in_a_file_instead_of_the_environment(tmp_path, monkeypatch):
    """
    The repo is public and a client secret is shown exactly once, so it needs a
    home outside both the repo and the shell history.
    """
    p = tmp_path / "oauth.json"
    monkeypatch.setattr(tt_flow, "_OAUTH_PATH", str(p))
    tt_flow.save_oauth("sec-file", "ref-file", "cid-file")
    assert tt_flow._load_oauth() == ("sec-file", "ref-file", "cid-file")
    assert tt_flow.oauth_configured() is True


def test_the_grant_file_is_owner_only(tmp_path, monkeypatch):
    import os as _os
    p = tmp_path / "oauth.json"
    monkeypatch.setattr(tt_flow, "_OAUTH_PATH", str(p))
    tt_flow.save_oauth("sec", "ref")
    assert _os.stat(p).st_mode & 0o777 == 0o600


def test_saving_merges_so_the_secret_survives_adding_the_token(tmp_path, monkeypatch):
    """
    The client secret is created before the grant exists and is displayed once.
    Saving the refresh token later must not wipe it.
    """
    p = tmp_path / "oauth.json"
    monkeypatch.setattr(tt_flow, "_OAUTH_PATH", str(p))
    tt_flow.save_oauth("sec-only")
    tt_flow.save_oauth("", "ref-added")
    assert tt_flow._load_oauth()[:2] == ("sec-only", "ref-added")


def test_the_environment_wins_over_the_file(tmp_path, monkeypatch):
    """Railway sets env vars; a stale local file must not override prod."""
    p = tmp_path / "oauth.json"
    monkeypatch.setattr(tt_flow, "_OAUTH_PATH", str(p))
    tt_flow.save_oauth("file-sec", "file-ref")
    monkeypatch.setenv("TT_CLIENT_SECRET", "env-sec")
    monkeypatch.setenv("TT_REFRESH_TOKEN", "env-ref")
    assert tt_flow._load_oauth()[:2] == ("env-sec", "env-ref")


def test_a_secret_without_a_token_is_still_not_configured(tmp_path, monkeypatch):
    """Half a grant cannot authenticate; it must not look ready."""
    p = tmp_path / "oauth.json"
    monkeypatch.setattr(tt_flow, "_OAUTH_PATH", str(p))
    tt_flow.save_oauth("sec-only")
    assert tt_flow.oauth_configured() is False


def test_a_missing_or_corrupt_file_is_not_fatal(tmp_path, monkeypatch):
    p = tmp_path / "oauth.json"
    monkeypatch.setattr(tt_flow, "_OAUTH_PATH", str(p))
    assert tt_flow._load_oauth() == ("", "", "")
    p.write_text("{not json")
    assert tt_flow._load_oauth() == ("", "", "")


def test_the_grant_file_is_gitignored():
    """This repo is public."""
    assert ".tt_oauth.json" in open(".gitignore").read()
