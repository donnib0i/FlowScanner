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

    assert etf_filter.is_etf("ZZZX") is True     # first call hits lookup
    assert etf_filter.is_etf("ZZZX") is True     # second call hits cache
    assert calls["n"] == 1                        # lookup called only once
    assert etf_filter.is_etf("ABCD") is False
