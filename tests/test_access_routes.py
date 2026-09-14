"""
The access routes end to end: /join, /auth, /api/me, and the admin list.

Two properties matter more than the happy path and are easy to lose in a
refactor: the join endpoint must not reveal whether an address is already a
member, and a member's session must never open the admin routes -- every member
holds a valid session, so an admin check that accepts one accepts everybody.
"""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_ACCOUNTS_DB", str(tmp_path / "accounts.db"))
    monkeypatch.setenv("SCANNER_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("SCANNER_PIN", "owner-pin")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    import web.app as webapp
    importlib.reload(webapp)
    yield webapp
    monkeypatch.delenv("SCANNER_PIN", raising=False)
    importlib.reload(webapp)


@pytest.fixture
def client(app):
    return TestClient(app.app)


def link_for(app, email):
    """The token the app would have mailed."""
    return app._accounts.request_access(email)["token"]


# ── Joining ───────────────────────────────────────────────────────────────────
def test_the_join_page_is_reachable_without_any_credential(client):
    r = client.get("/join")
    assert r.status_code == 200
    assert "Request access" in r.text


def test_joining_creates_an_account(client, app):
    r = client.post("/api/join", json={"email": "New@Example.com"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert app._accounts.get_user("new@example.com") is not None


def test_the_link_never_comes_back_in_the_response(client):
    """Returning it would let anyone sign in as any address they can spell."""
    body = client.post("/api/join", json={"email": "a@example.com"}).text
    assert "/auth?token=" not in body and "token" not in body.lower()


def test_a_known_and_an_unknown_address_are_answered_identically(client):
    first = client.post("/api/join", json={"email": "known@example.com"}).json()
    again = client.post("/api/join", json={"email": "known@example.com"}).json()
    fresh = client.post("/api/join", json={"email": "other@example.com"}).json()
    assert first == again == fresh


def test_a_bad_address_is_refused(client):
    assert client.post("/api/join", json={"email": "nope"}).status_code == 400


# ── Signing in ────────────────────────────────────────────────────────────────
def test_a_link_signs_you_in_and_the_api_then_answers(client, app):
    tok = link_for(app, "dante@example.com")
    r = client.get(f"/auth?token={tok}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.cookies.get("scanner_session")

    me = client.get("/api/me").json()
    assert me["signed_in"] is True and me["email"] == "dante@example.com"
    assert me["plan"] == "founder"


def test_a_spent_link_says_so_instead_of_failing_silently(client, app):
    tok = link_for(app, "dante@example.com")
    client.get(f"/auth?token={tok}")
    client.cookies.clear()
    r = client.get(f"/auth?token={tok}")
    assert r.status_code == 401
    assert "expired or has already been used" in r.text


def test_without_a_session_the_api_is_closed(client):
    assert client.get("/api/vix").status_code == 401


def test_signing_out_closes_it_again(client, app):
    client.get(f"/auth?token={link_for(app, 'dante@example.com')}")
    assert client.get("/api/me").json()["signed_in"] is True
    client.post("/api/signout")
    assert client.get("/api/me").json()["signed_in"] is False


def test_a_blocked_member_loses_access_on_their_next_request(client, app):
    client.get(f"/auth?token={link_for(app, 'spam@example.com')}")
    assert client.get("/api/me").json()["signed_in"] is True
    app._accounts.set_status("spam@example.com", "blocked")
    assert client.get("/api/me").json()["signed_in"] is False


# ── The owner's routes ────────────────────────────────────────────────────────
def test_a_members_session_cannot_read_the_membership(client, app):
    """The whole point of the separate owner check."""
    client.get(f"/auth?token={link_for(app, 'member@example.com')}")
    assert client.get("/api/vix").status_code != 401      # a real member
    assert client.get("/api/admin/users").status_code == 401


def test_the_pin_reads_the_list(client, app):
    client.post("/api/join", json={"email": "a@example.com"})
    client.post("/api/join", json={"email": "b@example.com"})
    r = client.get("/api/admin/users", headers={"x-pin": "owner-pin"})
    assert r.status_code == 200
    d = r.json()
    assert d["counts"]["total"] == 2
    assert [u["email"] for u in d["users"]] == ["a@example.com", "b@example.com"]
    assert all(u["plan"] == "founder" for u in d["users"])


def test_the_owner_can_change_a_plan(client, app):
    client.post("/api/join", json={"email": "a@example.com"})
    r = client.post("/api/admin/user",
                    headers={"x-pin": "owner-pin"},
                    json={"email": "a@example.com", "plan": "paid"})
    assert r.status_code == 200 and r.json()["user"]["plan"] == "paid"


# ── Unsubscribe ───────────────────────────────────────────────────────────────
def test_unsubscribing_needs_no_sign_in(client, app):
    """An unsubscribe link that demands a login is how a sender gets reported.
    It carries a signature instead -- see test_access_security for why a bare
    ?email= let anyone cut a stranger off from their own updates."""
    client.post("/api/join", json={"email": "a@example.com"})
    tok = app._accounts.unsubscribe_token("a@example.com")
    r = client.get(f"/unsubscribe?email=a@example.com&t={tok}")
    assert r.status_code == 200
    assert app._accounts.get_user("a@example.com")["subscribed"] == 0
    assert app._accounts.mailing_list() == []


def test_one_address_cannot_be_mail_bombed(client):
    """The endpoint sends email to whatever address it is given, so the abuse
    case is someone pointing it at a stranger's inbox, not at ours."""
    codes = [client.post("/api/join", json={"email": "victim@example.com"}).status_code
             for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]


def test_a_group_joining_from_one_network_is_not_cut_off(client):
    """A shared office or carrier NAT is one IP. A limit tuned so tight that a
    Discord link 429s half the room is a launch that quietly fails."""
    codes = [client.post("/api/join", json={"email": f"p{i}@example.com"}).status_code
             for i in range(10)]
    assert all(c == 200 for c in codes), codes


def test_a_test_signup_can_be_removed_so_it_stops_holding_a_founder_seat(client, app):
    """The five seats are a promise to real people. A test row sitting in seat
    one silently costs somebody theirs, and blocking does not give it back."""
    client.post("/api/join", json={"email": "test-signup@example.com"})
    assert app._accounts.counts()["founders"] == 1

    r = client.post("/api/admin/user/delete", headers={"x-pin": "owner-pin"},
                    json={"email": "test-signup@example.com"})
    assert r.status_code == 200 and r.json()["removed"] is True
    assert app._accounts.get_user("test-signup@example.com") is None
    assert app._accounts.counts()["founders"] == 0

    # And the seat genuinely goes to the next real person.
    client.post("/api/join", json={"email": "real@example.com"})
    assert app._accounts.get_user("real@example.com")["plan"] == "founder"


def test_a_members_session_cannot_delete_accounts(client, app):
    client.get(f"/auth?token={link_for(app, 'member@example.com')}")
    r = client.post("/api/admin/user/delete", json={"email": "member@example.com"})
    assert r.status_code == 401
    assert app._accounts.get_user("member@example.com") is not None
