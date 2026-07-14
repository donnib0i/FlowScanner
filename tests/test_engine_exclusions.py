from data import unusual_flow as uf
from core import universe


def test_score_contract_builds_signal_with_baseline_fields():
    contract = {
        "volume": 18000, "open_interest": 3000, "bid": 1.0, "ask": 1.2,
        "last": 1.18, "mid": 1.1, "strike": 145.0, "iv": 0.6,
        "expiry": "2026-06-13", "dte": 2, "type": "call", "source": "test",
    }
    sig = uf.score_contract(
        contract, price=142.0, sector="Technology",
        chain_volumes=[100, 120, 18000],
        oi_vs_avg=6.1, ticker_optvol_rvol=3.5, dampen=False, ticker="NVDA",
    )
    assert sig is not None
    assert sig["ticker_oi_vs_avg"] == 6.1
    assert sig["volume"] == 18000 and sig["open_interest"] == 3000  # shown separately
    assert sig["score"] >= 50


def test_score_contract_filters_tiny_volume():
    contract = {"volume": 10, "open_interest": 5, "mid": 1.0, "strike": 145.0,
                "expiry": "2026-06-13", "dte": 2, "type": "call"}
    assert uf.score_contract(contract, 142.0, "Tech", [10], None, None, False) is None


def test_apply_pool_exclusions_drops_etfs():
    pool = ["SPY", "AAPL", "QQQ", "NVDA", "TQQQ"]
    assert uf.apply_pool_exclusions(pool, exclude_etfs=True) == ["AAPL", "NVDA"]


def test_apply_pool_exclusions_off_keeps_everything():
    pool = ["SPY", "AAPL", "QQQ"]
    assert uf.apply_pool_exclusions(pool, exclude_etfs=False) == ["SPY", "AAPL", "QQQ"]


def test_universe_finalize_strips_etfs_keeps_megacap_stocks():
    raw = ["SPY", "AAPL", "QQQ", "NVDA", "spy", "MSFT"]
    out = universe.finalize_universe(raw, exclude_etfs=True)
    assert "SPY" not in out and "QQQ" not in out
    assert "AAPL" in out and "NVDA" in out and "MSFT" in out
