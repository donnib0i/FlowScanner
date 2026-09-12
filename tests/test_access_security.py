"""
The three ways this access system could be turned against its users.

Found by review of the first commit, and the first one is the serious one:
every magic link was built from the request's own Host header. An attacker
POSTs /api/join for a victim's address with X-Forwarded-Host: evil.example.com,
the victim receives a genuine email from the real sender, and the link inside
it hands a valid one-time token to the attacker. Verified against the running
server before the fix.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    def _build(**env):
        monkeypatch.setenv("SCANNER_ACCOUNTS_DB", str(tmp_path / "accounts.db"))
        monkeypatch.setenv("SCANNER_SESSION_SECRET", "test-secret")
        monkeypatch.setenv("SCANNER_PIN", "owner-pin")
        for k in ("SCANNER_PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN", "SCANNER_ALLOWED_HOSTS"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        import web.app as webapp
        importlib.reload(webapp)
        return webapp
    yield _build
    for k in ("SCANNER_PIN", "SCANNER_PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN"):
        monkeypatch.delenv(k, raising=False)
    import web.app as webapp
    importlib.reload(webapp)


# ── 1. The link's host must not come from the request ────────────────────────
def test_a_spoofed_host_header_cannot_redirect_a_magic_link(make_app):
    app = make_app(SCANNER_PUBLIC_URL="https://scanner.example.com")
    client = TestClient(app.app)
    sent = {}
    app._send_magic_link = lambda email, link, unsub='': sent.update(email=email, link=link, unsub=unsub) or True

    client.post("/api/join", json={"email": "victim@example.com"},
                headers={"X-Forwarded-Host": "evil.example.com",
                         "X-Forwarded-Proto": "https",
                         "Host": "evil.example.com"})
    assert sent["link"].startswith("https://scanner.example.com/auth?token=")
    assert "evil.example.com" not in sent["link"]


def test_railways_own_domain_is_trusted_when_nothing_else_is_configured(make_app):
    app = make_app(RAILWAY_PUBLIC_DOMAIN="flowscanner-production.up.railway.app")
    client = TestClient(app.app,
                        base_url="https://flowscanner-production.up.railway.app")
    sent = {}
    app._send_magic_link = lambda email, link, unsub='': sent.update(link=link, unsub=unsub) or True
    r = client.post("/api/join", json={"email": "a@example.com"})
    assert r.status_code == 200
    assert sent["link"].startswith("https://flowscanner-production.up.railway.app/auth?")


def test_a_spoofed_host_is_also_stopped_at_the_middleware(make_app):
    """Defence in depth, and worth pinning: once any allowed host is configured
    -- which Railway does for itself automatically -- a request claiming to be
    another host never reaches the route at all. The link-building fix is what
    covers deployments where that middleware is off, such as a tunnel with no
    SCANNER_ALLOWED_HOSTS set."""
    app = make_app(RAILWAY_PUBLIC_DOMAIN="flowscanner-production.up.railway.app")
    client = TestClient(app.app)
    r = client.post("/api/join", json={"email": "a@example.com"},
                    headers={"X-Forwarded-Host": "evil.example.com",
                             "Host": "evil.example.com"})
    assert r.status_code == 400
    assert "host" in r.text.lower()


def test_local_development_still_works_with_no_configuration(make_app):
    app = make_app()
    client = TestClient(app.app)   # TestClient's host is testserver
    sent = {}
    app._send_magic_link = lambda email, link, unsub='': sent.update(link=link, unsub=unsub) or True
    r = client.post("/api/join", json={"email": "a@example.com"})
    assert r.status_code == 200
    assert sent["link"].startswith("http://testserver/auth?token=")


def test_an_unrecognised_host_refuses_rather_than_guessing(make_app):
    """With no configuration to work from and a host we have no reason to
    trust, building a link is a guess. Failing loudly beats a deployment that
    quietly mails links to whatever host the last request claimed to be."""
    app = make_app(SCANNER_ALLOWED_HOSTS="scanner.example.com")

    class _FakeURL:
        netloc = "evil.example.com"
        hostname = "evil.example.com"
        scheme = "https"

    class _FakeReq:
        url = _FakeURL()
        headers = {"x-forwarded-proto": "https"}

    with pytest.raises(Exception) as e:
        app._public_base(_FakeReq())
    assert "SCANNER_PUBLIC_URL" in str(getattr(e.value, "detail", e.value))


# ── 2. Unsubscribing somebody else ───────────────────────────────────────────
def test_a_stranger_cannot_unsubscribe_an_address_they_know(make_app):
    app = make_app(SCANNER_PUBLIC_URL="https://scanner.example.com")
    client = TestClient(app.app)
    client.post("/api/join", json={"email": "member@example.com"})

    assert client.get("/unsubscribe?email=member@example.com").status_code == 400
    assert client.get("/unsubscribe?email=member@example.com&t=guess").status_code == 400
    assert app._accounts.get_user("member@example.com")["subscribed"] == 1


def test_the_link_from_the_email_unsubscribes_without_a_sign_in(make_app):
    app = make_app(SCANNER_PUBLIC_URL="https://scanner.example.com")
    client = TestClient(app.app)
    client.post("/api/join", json={"email": "member@example.com"})

    tok = app._accounts.unsubscribe_token("member@example.com")
    r = client.get(f"/unsubscribe?email=member@example.com&t={tok}")
    assert r.status_code == 200
    assert app._accounts.get_user("member@example.com")["subscribed"] == 0


def test_one_members_unsubscribe_token_does_not_work_on_another(make_app):
    app = make_app(SCANNER_PUBLIC_URL="https://scanner.example.com")
    client = TestClient(app.app)
    client.post("/api/join", json={"email": "a@example.com"})
    client.post("/api/join", json={"email": "b@example.com"})

    tok_a = app._accounts.unsubscribe_token("a@example.com")
    assert client.get(f"/unsubscribe?email=b@example.com&t={tok_a}").status_code == 400
    assert app._accounts.get_user("b@example.com")["subscribed"] == 1


# ── 3. The cookie's Secure flag ──────────────────────────────────────────────
def _cookie_header(resp):
    return resp.headers.get("set-cookie", "")


def test_the_cookie_is_secure_behind_a_proxy_that_terminates_tls(make_app):
    app = make_app(SCANNER_PUBLIC_URL="https://scanner.example.com")
    client = TestClient(app.app)
    tok = app._accounts.request_access("a@example.com")["token"]
    r = client.get(f"/auth?token={tok}", headers={"X-Forwarded-Proto": "https"},
                   follow_redirects=False)
    assert "secure" in _cookie_header(r).lower()


def test_the_cookie_is_secure_on_a_direct_tls_connection(make_app):
    """No proxy, no forwarded header -- the scheme on the request is the only
    signal, and the previous version ignored it and shipped the cookie in the
    clear."""
    app = make_app(SCANNER_PUBLIC_URL="https://scanner.example.com")
    client = TestClient(app.app, base_url="https://scanner.example.com")
    tok = app._accounts.request_access("a@example.com")["token"]
    r = client.get(f"/auth?token={tok}", follow_redirects=False)
    assert "secure" in _cookie_header(r).lower()


def test_the_cookie_is_not_secure_on_plain_local_http(make_app):
    """Otherwise the browser drops it and nobody can sign in on localhost."""
    app = make_app()
    client = TestClient(app.app, base_url="http://localhost:8765")
    tok = app._accounts.request_access("a@example.com")["token"]
    r = client.get(f"/auth?token={tok}", follow_redirects=False)
    assert "secure" not in _cookie_header(r).lower()


def test_the_cookie_is_always_httponly_and_lax(make_app):
    app = make_app()
    client = TestClient(app.app)
    tok = app._accounts.request_access("a@example.com")["token"]
    c = _cookie_header(client.get(f"/auth?token={tok}", follow_redirects=False)).lower()
    assert "httponly" in c and "samesite=lax" in c
