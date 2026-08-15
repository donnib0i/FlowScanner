import importlib, os, re
import pytest
from fastapi.testclient import TestClient


def _reload(env):
    for k in ["SCANNER_PIN", "SCANNER_REQUIRE_PIN", "SCANNER_ALLOW_PIN_QUERY",
              "SCANNER_PROXY_HOPS", "SCANNER_ALLOWED_HOSTS", "SCANNER_FORCE_HTTPS"]:
        os.environ.pop(k, None)
    os.environ.update(env)
    import web.app as webapp
    return importlib.reload(webapp)


# ─── Task 6: auth ──────────────────────────────────────────────────────────────
def test_require_pin_fails_closed_when_unset():
    webapp = _reload({"SCANNER_REQUIRE_PIN": "1"})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    assert c.get("/api/status").status_code == 503


def test_header_pin_accepted():
    webapp = _reload({"SCANNER_PIN": "secret1"})
    c = TestClient(webapp.app)
    assert c.get("/api/status", headers={"X-Pin": "secret1"}).status_code == 200


def test_query_pin_rejected_by_default():
    webapp = _reload({"SCANNER_PIN": "secret1"})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    assert c.get("/api/status?pin=secret1").status_code in (401, 403)


def test_query_pin_allowed_with_flag():
    webapp = _reload({"SCANNER_PIN": "secret1", "SCANNER_ALLOW_PIN_QUERY": "1"})
    c = TestClient(webapp.app)
    assert c.get("/api/status?pin=secret1").status_code == 200


# ─── Task 7: XFF ─────────────────────────────────────────────────────────────
def test_client_ip_uses_trusted_hop():
    webapp = _reload({"SCANNER_PROXY_HOPS": "1"})

    class Req:
        def __init__(self, xff):
            self.headers = {"x-forwarded-for": xff}
            self.client = None
    assert webapp._client_ip(Req("1.1.1.1, 2.2.2.2, 9.9.9.9")) == "9.9.9.9"


# ─── Task 8: security headers ────────────────────────────────────────────────
def test_security_headers_present():
    webapp = _reload({})
    c = TestClient(webapp.app)
    r = c.get("/")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert "no-referrer" in r.headers.get("referrer-policy", "")
    assert "content-security-policy" in r.headers


@pytest.mark.skip(reason="Strict CSP nonces require refactoring 31 inline onclick "
                         "handlers + 88 inline style attrs; pending user decision.")
def test_strict_csp_nonce():
    webapp = _reload({})
    c = TestClient(webapp.app)
    r = c.get("/")
    csp = r.headers.get("content-security-policy", "")
    m = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
    assert m, csp
    nonce = m.group(1)
    scripts = re.findall(r"<script(?![^>]*src=)([^>]*)>", r.text)
    assert scripts and all(('nonce="%s"' % nonce) in s for s in scripts)


# ─── Task 9: trusted host ────────────────────────────────────────────────────
def test_trusted_host_rejects_bad_host():
    webapp = _reload({"SCANNER_ALLOWED_HOSTS": "good.example.com"})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    r = c.get("/api/status", headers={"host": "evil.com"})
    assert r.status_code == 400


def test_trusted_host_allows_listed():
    webapp = _reload({"SCANNER_ALLOWED_HOSTS": "good.example.com"})
    c = TestClient(webapp.app)
    r = c.get("/api/status", headers={"host": "good.example.com"})
    assert r.status_code == 200


# ─── Task 10: surface reduction ──────────────────────────────────────────────
def test_openapi_disabled():
    webapp = _reload({})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    assert c.get("/openapi.json").status_code == 404


def test_generic_500():
    webapp = _reload({})

    @webapp.app.get("/_boom")
    async def boom():
        raise RuntimeError("leaky internal detail")
    c = TestClient(webapp.app, raise_server_exceptions=False)
    r = c.get("/_boom")
    assert r.status_code == 500
    assert "leaky internal detail" not in r.text          # no traceback/detail leak
    assert r.json() == {"detail": "Internal server error"}


# ─── Task 11: status rate-limit ──────────────────────────────────────────────
def test_status_rate_limited():
    webapp = _reload({})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    codes = [c.get("/api/status").status_code for _ in range(35)]
    assert 429 in codes


# ─── Deploy safety: host filtering must be opt-in ─────────────────────────────
def test_unset_allowed_hosts_does_not_reject_deploy_domain():
    # The default used to be "localhost,127.0.0.1,testserver", which meant
    # deploying to Railway without setting SCANNER_ALLOWED_HOSTS made
    # TrustedHostMiddleware 400 every single request to the public domain.
    # Host filtering is a hardening step you opt into, like the PIN.
    webapp = _reload({})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    r = c.get("/api/status", headers={"Host": "flowscanner-production.up.railway.app"})
    assert r.status_code == 200


def test_explicit_allowed_hosts_still_filters():
    webapp = _reload({"SCANNER_ALLOWED_HOSTS": "good.example.com"})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    assert c.get("/api/status", headers={"Host": "evil.example.com"}).status_code == 400
    assert c.get("/api/status", headers={"Host": "good.example.com"}).status_code == 200
