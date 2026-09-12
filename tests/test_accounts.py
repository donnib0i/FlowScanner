"""
Accounts: email in, magic link out, signed cookie back.

The point of this layer is not secrecy -- it is knowing *who*. Dante needs a
verified list he can email about updates and pricing, founder access that
survives being forgotten, and the ability to cut someone off. A shared PIN
gives none of those.

What the tests pin down is the part that is easy to get subtly wrong: a link
works once, expires, cannot be forged, and cannot be replayed after the account
behind it is blocked.
"""
import time

import pytest

from data import accounts as A


@pytest.fixture
def store(tmp_path):
    return A.AccountStore(str(tmp_path / "accounts.db"), secret="test-secret")


# ── Signing up ────────────────────────────────────────────────────────────────
def test_an_email_is_stored_once_however_it_is_typed(store):
    store.request_access("  Dante@Example.COM ")
    store.request_access("dante@example.com")
    assert [u["email"] for u in store.list_users()] == ["dante@example.com"]


def test_something_that_is_not_an_email_is_refused(store):
    for bad in ("", "   ", "dante", "dante@", "@example.com", "a b@c.com", "x@y"):
        with pytest.raises(ValueError):
            store.request_access(bad)


def test_the_first_accounts_are_marked_founders(store):
    for i in range(7):
        store.request_access(f"u{i}@example.com")
    plans = [u["plan"] for u in store.list_users()]
    assert plans[:5] == ["founder"] * 5
    assert plans[5:] == ["free"] * 2


def test_a_founder_stays_a_founder_when_they_come_back(store):
    store.request_access("first@example.com")
    store.request_access("first@example.com")
    assert store.get_user("first@example.com")["plan"] == "founder"


# ── The link ──────────────────────────────────────────────────────────────────
def test_a_link_signs_its_holder_in(store):
    tok = store.request_access("dante@example.com")["token"]
    assert store.redeem(tok) == "dante@example.com"


def test_a_link_works_exactly_once(store):
    tok = store.request_access("dante@example.com")["token"]
    store.redeem(tok)
    assert store.redeem(tok) is None


def test_an_expired_link_is_refused(store):
    tok = store.request_access("dante@example.com", ttl_secs=-1)["token"]
    assert store.redeem(tok) is None


def test_an_invented_link_is_refused(store):
    assert store.redeem("not-a-real-token") is None
    assert store.redeem("") is None


def test_redeeming_marks_the_account_active(store):
    tok = store.request_access("dante@example.com")["token"]
    assert store.get_user("dante@example.com")["status"] == "pending"
    store.redeem(tok)
    assert store.get_user("dante@example.com")["status"] == "active"


# ── The cookie ────────────────────────────────────────────────────────────────
@pytest.fixture
def signed_up(store):
    """A cookie only means anything alongside an account, so the cookie tests
    start from one."""
    store.request_access("dante@example.com")
    return store


def test_a_session_cookie_round_trips(signed_up):
    c = signed_up.issue_session("dante@example.com")
    assert signed_up.read_session(c) == "dante@example.com"


def test_a_cookie_for_an_account_that_does_not_exist_is_refused(store):
    """Deleting an account has to actually lock its holder out, even though the
    signature on their cookie is still perfectly valid."""
    assert store.read_session(store.issue_session("ghost@example.com")) is None


def test_a_tampered_cookie_is_refused(signed_up):
    c = signed_up.issue_session("dante@example.com")
    # Swap the payload for someone else's address, keep the signature.
    body, sig = c.rsplit(".", 1)
    import base64
    forged = base64.urlsafe_b64encode(b"attacker@example.com|9999999999").decode().rstrip("=")
    assert signed_up.read_session(forged + "." + sig) is None
    assert signed_up.read_session(c[:-1]) is None
    assert signed_up.read_session("garbage") is None


def test_a_cookie_signed_with_another_secret_is_refused(signed_up, tmp_path):
    other = A.AccountStore(str(tmp_path / "other.db"), secret="different-secret")
    assert signed_up.read_session(other.issue_session("dante@example.com")) is None


def test_an_expired_cookie_is_refused(signed_up):
    assert signed_up.read_session(signed_up.issue_session("dante@example.com", ttl_secs=-1)) is None


# ── Cutting someone off ───────────────────────────────────────────────────────
def test_a_blocked_account_cannot_use_a_valid_cookie(store):
    c = store.issue_session("spammer@example.com")
    store.request_access("spammer@example.com")
    store.set_status("spammer@example.com", "blocked")
    assert store.read_session(c) is None


def test_a_blocked_account_cannot_redeem_a_link(store):
    tok = store.request_access("spammer@example.com")["token"]
    store.set_status("spammer@example.com", "blocked")
    assert store.redeem(tok) is None


def test_plans_can_be_changed_by_hand(store):
    store.request_access("dante@example.com")
    store.set_plan("dante@example.com", "paid")
    assert store.get_user("dante@example.com")["plan"] == "paid"
    with pytest.raises(ValueError):
        store.set_plan("dante@example.com", "platinum")


# ── The list he can actually email ────────────────────────────────────────────
def test_the_list_carries_what_an_update_email_needs(store):
    store.request_access("a@example.com")
    u = store.list_users()[0]
    assert set(u) >= {"email", "plan", "status", "created_at"}
    assert u["created_at"] <= time.time() + 1


def test_unsubscribing_is_recorded_and_survives_a_return_visit(store):
    store.request_access("a@example.com")
    store.set_subscribed("a@example.com", False)
    store.request_access("a@example.com")
    assert store.get_user("a@example.com")["subscribed"] == 0
    assert [u["email"] for u in store.mailing_list()] == []
