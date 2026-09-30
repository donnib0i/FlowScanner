from data import cboe_chain as C


def _opts():
    return [
        {"option": "SPX261016C07600000", "open_interest": 100, "volume": 5, "iv": 0.15, "bid": 1, "ask": 2},
        {"option": "SPXW261016C07600000", "open_interest": 40, "volume": 1, "iv": 0.16, "bid": 1.1, "ask": 2.1},
        {"option": "SPXW261016P07500000", "open_interest": 7, "volume": 0, "iv": 0.2, "bid": 3, "ask": 4},
        {"option": "SPXW261023C07600000", "open_interest": 9, "volume": 0, "iv": 0.2, "bid": 3, "ask": 4},
        {"option": "garbage", "open_interest": 1},
    ]


def test_parses_occ_sums_roots_and_filters_expiries(monkeypatch):
    monkeypatch.setattr(C, "_fetch_raw", lambda s: _opts())
    out = C.fetch_chain_stats("SPX", ["2026-10-16"])
    assert set(out) == {("2026-10-16", 7600.0, "call"), ("2026-10-16", 7500.0, "put")}
    c = out[("2026-10-16", 7600.0, "call")]
    assert c["oi"] == 140 and c["day_volume"] == 6
    assert c["iv"] == 0.15, "quotes are not additive"


def test_failure_is_empty_not_an_exception(monkeypatch):
    def boom(s):
        raise RuntimeError("down")
    monkeypatch.setattr(C, "_fetch_raw", boom)
    assert C.fetch_chain_stats("SPX", ["2026-10-16"]) == {}


def test_index_symbols_get_cboes_underscore():
    assert C._cboe_symbol("SPX") == "_SPX"
    assert C._cboe_symbol("^NDX") == "_NDX"
    assert C._cboe_symbol("QQQ") == "QQQ"
