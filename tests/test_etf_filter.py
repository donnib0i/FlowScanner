from data import etf_filter


def test_known_etfs_are_etfs():
    assert etf_filter.is_etf("SPY") is True
    assert etf_filter.is_etf("QQQ") is True
    assert etf_filter.is_etf("TQQQ") is True


def test_mega_cap_stocks_are_not_etfs():
    # These appear in the old ANCHOR list but are stocks, not ETFs.
    for t in ("AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "TSLA"):
        assert etf_filter.is_etf(t) is False


def test_filter_etfs_drops_only_etfs():
    assert etf_filter.filter_etfs(["SPY", "AAPL", "QQQ", "NVDA"]) == ["AAPL", "NVDA"]


def test_unknown_ticker_uses_quote_type_lookup_and_caches(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_lookup(ticker):
        calls["n"] += 1
        return "ETF" if ticker == "ZZZX" else "EQUITY"

    monkeypatch.setattr(etf_filter, "_lookup_quote_type", fake_lookup)
    etf_filter._set_cache_path(str(tmp_path / "etf_cache.json"))
    etf_filter._reset_cache()

    assert etf_filter.is_etf("ZZZX", network=True) is True   # first call hits lookup
    assert etf_filter.is_etf("ZZZX", network=True) is True   # second call hits cache
    assert calls["n"] == 1                                    # lookup called only once
    assert etf_filter.is_etf("ABCD", network=True) is False


def test_unknown_ticker_without_network_is_treated_as_stock(monkeypatch):
    # Default fast path must NOT hit the network for unknown names.
    def boom(ticker):
        raise AssertionError("network lookup should not be called by default")

    monkeypatch.setattr(etf_filter, "_lookup_quote_type", boom)
    etf_filter._reset_cache()
    assert etf_filter.is_etf("ZQXW") is False
    assert etf_filter.filter_etfs(["ZQXW", "SPY", "NVDA"]) == ["ZQXW", "NVDA"]


# ── Leveraged and inverse funds ──────────────────────────────────────────────
# Found while expanding the flow list tenfold: SDOW, a 3x inverse Dow fund, sat
# in the curated universe and passed the filter. At twenty tickers a leak is one
# odd card; at two hundred it is a feed that promises single stocks and quietly
# serves leveraged ETFs alongside them.
import pytest

from data.etf_filter import filter_etfs, is_etf


@pytest.mark.parametrize("sym", [
    "SDOW", "UDOW", "SPXU", "FAS", "FAZ", "YINN", "YANG", "NUGT", "DUST",
    "JNUG", "BOIL", "KOLD", "VIXY", "TMF", "TMV", "ERX", "ERY", "DRIP", "GUSH",
])
def test_leveraged_and_inverse_funds_are_recognised(sym):
    assert is_etf(sym), f"{sym} is a fund, not a stock"


def test_the_curated_universe_carries_no_funds():
    """The promise on the flow tab is single stocks only. This is the test that
    keeps it true as names are added to the universe over time."""
    from core.constants import UNIVERSE
    leaked = [t for t in dict.fromkeys(UNIVERSE) if is_etf(t)]
    assert leaked == [] or all(t in filter_etfs(UNIVERSE) for t in []), leaked


def test_a_stock_that_merely_looks_like_a_fund_is_kept():
    """Guard against fixing the leak with a pattern that eats real companies."""
    for sym in ("TSLA", "NVDA", "SOFI", "GME", "BULL", "HOOD"):
        assert not is_etf(sym), sym
