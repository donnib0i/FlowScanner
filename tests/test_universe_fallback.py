"""
Universe must not collapse to today's screeners when Wikipedia is unreachable.

Production evidence (2026-08-15): the live universe was 227 tickers vs 682
locally, and 224 of those 227 were exactly the most-active/gainers/losers
screener set. Both Wikipedia fetches (S&P 500 and Nasdaq-100) fail from
Railway's IPs, so the entire index-coverage tier was silently missing and the
scanner only ever saw names that were already moving today.
"""
from core import universe


def _no_wikipedia(monkeypatch):
    monkeypatch.setattr(universe, "fetch_sp500", lambda: [])
    monkeypatch.setattr(universe, "fetch_nasdaq100", lambda: [])


def _no_screeners(monkeypatch):
    monkeypatch.setattr(universe, "fetch_most_active", lambda n=100: [])
    monkeypatch.setattr(universe, "fetch_day_gainers", lambda n=75: [])
    monkeypatch.setattr(universe, "fetch_day_losers", lambda n=75: [])
    monkeypatch.setattr(universe, "fetch_high_relvol",
                        lambda c, min_relvol=1.5, top_n=100: [])
    monkeypatch.setattr(universe, "_get_sector_pool", lambda: [])


def test_universe_survives_wikipedia_outage(monkeypatch):
    _no_wikipedia(monkeypatch)
    _no_screeners(monkeypatch)
    u = universe.build_universe()
    assert len(u) >= 300, f"index coverage collapsed to {len(u)} tickers"
    for t in ("AAPL", "JPM", "XOM", "JNJ", "CAT"):
        assert t in u, f"{t} missing — sector breadth is gone"


def test_fallback_is_etf_free(monkeypatch):
    from data.etf_filter import is_etf
    _no_wikipedia(monkeypatch)
    _no_screeners(monkeypatch)
    assert [t for t in universe.build_universe() if is_etf(t)] == []


def test_live_sp500_is_preferred_over_static(monkeypatch):
    monkeypatch.setattr(universe, "fetch_sp500", lambda: ["ZZTOP", "AAPL"])
    monkeypatch.setattr(universe, "fetch_nasdaq100", lambda: [])
    _no_screeners(monkeypatch)
    u = universe.build_universe()
    assert "ZZTOP" in u, "live S&P 500 result was discarded"


def test_no_duplicates(monkeypatch):
    _no_wikipedia(monkeypatch)
    _no_screeners(monkeypatch)
    u = universe.build_universe()
    assert len(u) == len(set(u))
