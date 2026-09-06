"""
Ladder row selection — the CALLS vs PUTS table.

Regression basis: a live NVDA 2DTE chain ranked strike 207.5C (delta 0.908,
OI 293, mid $17.68) above the actual whale strike 225C (46,805 vol, 60k OI),
and floated a 240P with OI=1 into the top 3. Cause: rows were sorted by
dollar_flow = vol x mid x 100, which rewards expensive deep-ITM contracts,
with no liquidity floor and a fake delta proxy (0.5 + log(S/K)*5).
"""
import pytest

from core.scanner import ladder_rows


def _row(strike, volume, oi, bid=0.0, ask=0.0, last=0.0, iv=0.30):
    return {"strike": strike, "volume": volume, "openInterest": oi,
            "bid": bid, "ask": ask, "lastPrice": last, "impliedVolatility": iv}


PRICE = 225.16
DTE = 2


def test_deep_itm_does_not_outrank_atm_whale_strike():
    rows = ladder_rows([
        _row(207.5, 6966, 293, bid=17.60, ask=17.76),   # deep ITM, expensive
        _row(225.0, 46805, 6260, bid=1.77, ask=1.81),   # ATM, real activity
    ], "call", PRICE, DTE)
    assert rows, "expected at least the ATM strike to survive"
    assert rows[0]["strike"] == 225.0


def test_illiquid_strike_is_dropped():
    rows = ladder_rows([
        _row(240.0, 1997, 1, bid=15.20, ask=15.30),     # OI=1 -> noise
        _row(222.0, 7862, 562, bid=3.00, ask=3.04),
    ], "put", PRICE, DTE)
    assert [r["strike"] for r in rows] == [222.0]


def test_low_volume_strike_is_dropped():
    rows = ladder_rows([
        _row(225.0, 3, 9000, bid=1.77, ask=1.81),       # stale OI, no flow today
        _row(227.5, 7862, 562, bid=3.00, ask=3.04),
    ], "call", PRICE, DTE)
    assert [r["strike"] for r in rows] == [227.5]


def test_delta_is_black_scholes_not_moneyness_proxy():
    # The old proxy returned 0.908 for 207.5C at 2DTE. Real BS delta with the
    # chain's own IV is far higher for a strike that deep, and ATM sits near .5.
    atm = ladder_rows([_row(225.0, 46805, 6260, bid=1.77, ask=1.81, iv=0.21)],
                      "call", PRICE, DTE)[0]
    assert 0.45 <= atm["delta"] <= 0.60


def test_delta_band_excludes_lotto_and_deep_itm():
    rows = ladder_rows([
        _row(300.0, 9000, 4000, bid=0.01, ask=0.03),    # ~0 delta lotto
        _row(150.0, 9000, 4000, bid=75.0, ask=75.4),    # ~1.0 delta deep ITM
        _row(228.0, 9000, 4000, bid=1.50, ask=1.60),    # tradeable
    ], "call", PRICE, DTE)
    assert [r["strike"] for r in rows] == [228.0]


def test_after_hours_falls_back_to_last_price():
    # Outside RTH yfinance returns bid=ask=0; mid must come from lastPrice or
    # every row collapses to zero flow and the whole ladder empties out.
    rows = ladder_rows([_row(225.0, 46805, 6260, bid=0, ask=0, last=1.79)],
                       "call", PRICE, DTE)
    assert rows[0]["mid"] == 1.79
    assert rows[0]["dollar_flow"] == pytest.approx(46805 * 1.79 * 100)


def test_ddoi_uses_real_delta():
    r = ladder_rows([_row(225.0, 46805, 6260, bid=1.77, ask=1.81, iv=0.21)],
                    "call", PRICE, DTE)[0]
    assert r["ddoi"] == pytest.approx(abs(r["delta"]) * 6260, rel=0.01)


def test_puts_report_absolute_delta():
    r = ladder_rows([_row(222.0, 8000, 3000, bid=1.40, ask=1.50)],
                    "put", PRICE, DTE)[0]
    assert r["delta"] > 0


def test_handles_nan_and_missing_fields():
    rows = ladder_rows([
        {"strike": float("nan"), "volume": 100, "openInterest": 100},
        {"strike": 225.0},
        _row(226.0, 9000, 4000, bid=1.50, ask=1.60),
    ], "call", PRICE, DTE)
    assert [r["strike"] for r in rows] == [226.0]


def test_respects_top_n():
    raw = [_row(220.0 + i, 9000 + i, 4000, bid=1.50, ask=1.60) for i in range(12)]
    assert len(ladder_rows(raw, "call", PRICE, DTE, top_n=5)) == 5


def test_empty_chain_returns_empty():
    assert ladder_rows([], "call", PRICE, DTE) == []


def test_strike_not_being_accumulated_is_dropped():
    """Volume must clear VOL_OI_ACTIVE x OI: size alone is not today's buying."""
    rows = ladder_rows([
        _row(226.0, 5000, 4000, bid=1.50, ask=1.60),    # 1.25x -- already held
        _row(227.0, 9000, 4000, bid=1.50, ask=1.60),    # 2.25x -- bought today
    ], "call", PRICE, DTE)
    assert [r["strike"] for r in rows] == [227.0]
    assert rows[0]["vol_oi"] == pytest.approx(2.25)


def test_at_the_money_strike_survives_the_delta_cap():
    """A 0.50 cap drops the ATM row (delta ~.52) -- the one row that must stay."""
    rows = ladder_rows([_row(225.0, 46805, 6260, bid=1.77, ask=1.81)],
                       "call", PRICE, DTE)
    assert [r["strike"] for r in rows] == [225.0]
