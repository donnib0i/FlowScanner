from core.live_flow import sparkline, aggregate_by_ticker, net_flow, fmt_compact

SIGNALS = [
    {"ticker": "NVDA", "sector": "Tech", "type": "call",
     "volume": 18000, "open_interest": 3000, "notional": 3_200_000},
    {"ticker": "NVDA", "sector": "Tech", "type": "put",
     "volume": 2000, "open_interest": 1000, "notional": 400_000},
    {"ticker": "PLTR", "sector": "Tech", "type": "call",
     "volume": 9000, "open_interest": 2000, "notional": 1_100_000},
]


def test_sparkline_shape():
    sp = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(sp) == 8
    assert sp[0] == "▁" and sp[-1] == "█"
    assert sparkline([5, 5, 5]) == "▁▁▁"
    assert sparkline([]) == ""


def test_aggregate_by_ticker_rolls_up_vol_oi_and_flow():
    agg = aggregate_by_ticker(SIGNALS)
    assert agg["NVDA"]["opt_vol"] == 20000
    assert agg["NVDA"]["opt_oi"] == 4000
    assert agg["NVDA"]["call_notional"] == 3_200_000
    assert agg["NVDA"]["put_notional"] == 400_000
    assert agg["PLTR"]["opt_vol"] == 9000


def test_net_flow_sums_calls_minus_puts():
    call, put = net_flow(SIGNALS)
    assert call == 4_300_000
    assert put == 400_000


def test_fmt_compact():
    assert fmt_compact(3_200_000) == "3.2M"
    assert fmt_compact(214_000) == "214.0K"
    assert fmt_compact(950) == "950"
