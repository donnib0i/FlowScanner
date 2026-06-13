from data.baseline import BaselineStore


def _store(tmp_path):
    return BaselineStore(db_path=str(tmp_path / "baseline.db"))


def test_contract_oi_vs_avg_uses_prior_days_only(tmp_path):
    s = _store(tmp_path)
    s.record_contract("2026-06-09", "ABCD", "call", 30.0, "2026-06-20", oi=1000, volume=500)
    s.record_contract("2026-06-10", "ABCD", "call", 30.0, "2026-06-20", oi=2000, volume=900)
    # today's OI 6000 vs avg of prior (1000, 2000) = 1500 -> 4.0x
    ratio = s.contract_oi_vs_avg("ABCD", "call", 30.0, "2026-06-20", today_oi=6000)
    assert ratio == 4.0


def test_contract_oi_vs_avg_none_without_history(tmp_path):
    s = _store(tmp_path)
    assert s.contract_oi_vs_avg("ABCD", "call", 30.0, "2026-06-20", today_oi=6000) is None


def test_record_contract_is_upsert_last_write_wins(tmp_path):
    s = _store(tmp_path)
    s.record_contract("2026-06-09", "ABCD", "call", 30.0, "2026-06-20", oi=1000, volume=500)
    s.record_contract("2026-06-09", "ABCD", "call", 30.0, "2026-06-20", oi=1500, volume=700)
    # only one prior row; today 3000 vs avg 1500 -> 2.0x
    assert s.contract_oi_vs_avg("ABCD", "call", 30.0, "2026-06-20", today_oi=3000) == 2.0


def test_ticker_optvol_rvol(tmp_path):
    s = _store(tmp_path)
    s.record_ticker("2026-06-09", "ABCD", total_opt_vol=100, total_oi=10, equity_vol=1)
    s.record_ticker("2026-06-10", "ABCD", total_opt_vol=300, total_oi=20, equity_vol=2)
    # today 800 vs avg(100,300)=200 -> 4.0x
    assert s.ticker_optvol_rvol("ABCD", today_opt_vol=800) == 4.0
    assert s.ticker_optvol_rvol("ZZZZ", today_opt_vol=800) is None
