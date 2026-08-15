import os
os.environ.pop("SCANNER_PIN", None)  # disable auth for these tests

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from data import unusual_flow as uf

client = TestClient(webapp.app)


@pytest.fixture
def captured(monkeypatch):
    """Capture the ticker list /api/flow hands to the scan engine."""
    seen = {}

    def _fake_scan(tickers, show_progress=True, on_signal=None, on_progress=None):
        seen["tickers"] = list(tickers)
        return []

    monkeypatch.setattr(webapp, "scan_options_flow", _fake_scan)
    webapp._active_scan.clear()
    return seen


def test_flow_scan_drops_etfs_from_requested_tickers(captured):
    r = client.get("/api/flow?tickers=SPY,QQQ,NVDA,TQQQ,AMD")
    assert r.status_code == 200
    assert captured["tickers"] == ["NVDA", "AMD"]


def test_flow_scan_keeps_index_symbols(captured):
    # SPX is a cash index, not an ETF — it must survive the filter.
    r = client.get("/api/flow?tickers=SPX,SPY,NVDA")
    assert r.status_code == 200
    assert captured["tickers"] == ["SPX", "NVDA"]


def test_flow_scan_rejects_all_etf_request(captured):
    r = client.get("/api/flow?tickers=SPY,QQQ,IWM")
    assert r.status_code == 400
    assert "ETF" in r.json()["detail"]
    assert "tickers" not in captured  # engine never invoked


def test_default_flow_tickers_contain_no_etfs():
    from data.etf_filter import is_etf
    assert [t for t in webapp.DEFAULT_FLOW_TICKERS if is_etf(t)] == []


def test_insider_universe_excludes_etfs(monkeypatch):
    monkeypatch.setattr(webapp, "get_universe",
                        lambda: ["SPY", "NVDA", "ARKK", "AMD", "XLF"])
    assert webapp._get_insider_universe() == ["NVDA", "AMD"]


def test_sector_map_fallback_is_empty_not_etf_anchors(monkeypatch):
    # When every live source fails the map must stay empty — falling back to the
    # ETF anchor dict used to inject SPY/QQQ/XLK straight into the scan pool.
    monkeypatch.setattr(uf, "build_ticker_sector_map", lambda: {})
    monkeypatch.setitem(uf._POOL_CACHE, "ticker_sector", {})
    monkeypatch.setitem(uf._POOL_CACHE, "ts", 0.0)
    assert uf.get_ticker_sector_map(force=True) == {}
    assert uf.get_scan_pool() == []
