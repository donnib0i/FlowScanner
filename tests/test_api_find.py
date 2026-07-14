import os
os.environ.pop("SCANNER_PIN", None)  # disable auth for these tests
from fastapi.testclient import TestClient
import web.app as webapp
import core.scanner as scanner

client = TestClient(webapp.app)


class _FakeTicker:
    """No expirations -> _fetch() returns None -> endpoint 404 (never touches network)."""
    options = []


def test_find_both_import_path_resolves(monkeypatch):
    # Regression: /api/find/both used `from scanner import ...` (module is
    # core.scanner), raising ModuleNotFoundError -> 500. With the correct
    # import, a no-data chain yields 404, not a 500.
    monkeypatch.setattr(scanner, "_yf", lambda t: _FakeTicker())
    r = client.get("/api/find/both?ticker=AMD&dte_mode=all")
    assert r.status_code == 404
