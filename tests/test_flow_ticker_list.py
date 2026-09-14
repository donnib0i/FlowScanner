"""
The default flow list, and what happens when a scan asks for more names than
the endpoint will take.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("SCANNER_PIN", raising=False)
    monkeypatch.delenv("SCANNER_REQUIRE_PIN", raising=False)
    import web.app as webapp
    importlib.reload(webapp)
    webapp._active_scan.clear()
    yield webapp
    webapp._active_scan.clear()


def test_the_default_list_is_the_curated_universe(app):
    """Twenty names was a sampler. The scan is only as good as the names it
    looks at, and the universe is already curated and liquid."""
    assert len(app.DEFAULT_FLOW_TICKERS) >= 180


def test_the_default_list_carries_no_funds(app):
    from data.etf_filter import is_etf
    assert [t for t in app.DEFAULT_FLOW_TICKERS if is_etf(t)] == []


def test_the_default_list_leads_with_the_names_worth_seeing_first(app):
    """Cards stream in scan order, so the first screenful decides whether a
    long scan feels useful or random."""
    head = app.DEFAULT_FLOW_TICKERS[:10]
    for t in ("SPX", "NVDA", "TSLA", "AAPL"):
        assert t in head, (t, head)


def test_the_default_list_fits_the_endpoints_own_cap(app):
    """The cap silently dropped everything past 150, so the app could ask for
    its own default list and not get it."""
    assert len(app.DEFAULT_FLOW_TICKERS) <= app.MAX_SCAN_TICKERS


def test_asking_for_more_names_than_the_cap_says_so(app):
    """Truncation used to be invisible: FULL claimed 671 names and scanned 150,
    and nothing anywhere said which 150."""
    client = TestClient(app.app)
    many = ",".join(f"AA{i:02d}" for i in range(app.MAX_SCAN_TICKERS + 40))
    with client.stream("GET", f"/api/flow?tickers={many}") as r:
        first = ""
        for line in r.iter_lines():
            if line.startswith("data: "):
                first = line[6:]
                break
    assert "__notice__" in first or "__error__" in first
    assert str(app.MAX_SCAN_TICKERS) in first
