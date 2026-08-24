"""
Contract selection quality for the FLOW tab.

top3 was ranked on raw premium alone, so a contract's *price* could buy it a
top slot. On 2026-08-24 AMD's number-two contract was a 575P — 25.9% in the
money, 34 open interest, 160 lots — which cleared $1.9M of "flow" purely
because a nearly-all-intrinsic contract costs $118. That is a closing trade or
a roll, not directional flow.

Every fixture here is a real contract, cross-checked field by field against the
Yahoo chain on 2026-08-24. The obvious filter — extrinsic value ratio — is the
reason for that: it rejects AMD's 575P correctly and TSLA's 355P *incorrectly*,
because TSLA's stale mid (5.55) sits below its intrinsic (6.05). Distance plus
liquidity is the rule that survives real data.
"""
import pytest

from core.scanner import contract_quality, contract_economics

SPOT = {"NVDA": 208.48, "AMD": 456.74, "TSLA": 348.95}


def _c(**kw):
    base = dict(strike=0.0, type="call", vol=0, oi=0, mid=0.0,
                bid=0.0, ask=0.0, dte=1)
    base.update(kw)
    return base


# ── the junk that prompted this ───────────────────────────────────────────────
def test_deep_itm_thin_contract_is_rejected():
    amd_575p = _c(strike=575.0, type="put", vol=160, oi=34,
                  mid=118.45, bid=117.35, ask=119.55, dte=4)
    ok, reason = contract_quality(amd_575p, SPOT["AMD"])
    assert ok is False
    assert "deep itm" in reason.lower()


def test_hot_near_money_contract_survives_despite_mid_below_intrinsic():
    """TSLA 355P: 1.7% ITM, 178,840 lots. An extrinsic-ratio filter kills this."""
    tsla_355p = _c(strike=355.0, type="put", vol=178840, oi=2903,
                   mid=5.55, bid=5.05, ask=6.05, dte=0)
    ok, reason = contract_quality(tsla_355p, SPOT["TSLA"], market_open=True)
    assert ok is True, reason


def test_deep_itm_but_heavily_traded_survives():
    """Depth alone is not disqualifying — depth plus no volume is."""
    heavy = _c(strike=575.0, type="put", vol=40000, oi=9000,
               mid=118.45, bid=117.35, ask=119.55, dte=4)
    ok, _ = contract_quality(heavy, SPOT["AMD"])
    assert ok is True


# ── the other gates ───────────────────────────────────────────────────────────
def test_illiquid_contract_is_rejected():
    ok, reason = contract_quality(
        _c(strike=210.0, vol=12, oi=8, mid=1.20, bid=1.10, ask=1.30),
        SPOT["NVDA"])
    assert ok is False
    assert "illiquid" in reason.lower()


def test_dead_premium_is_rejected():
    ok, reason = contract_quality(
        _c(strike=300.0, vol=5000, oi=4000, mid=0.03, bid=0.02, ask=0.04),
        SPOT["NVDA"])
    assert ok is False
    assert "premium" in reason.lower()


def test_expired_0dte_is_rejected_once_the_session_is_shut():
    """At 18:20 ET the 0DTE contract topping the card stopped existing at 16:00."""
    c = _c(strike=355.0, type="put", vol=178840, oi=2903,
           mid=5.55, bid=5.05, ask=6.05, dte=0)
    assert contract_quality(c, SPOT["TSLA"], market_open=True)[0] is True
    ok, reason = contract_quality(c, SPOT["TSLA"], market_open=False)
    assert ok is False
    assert "expired" in reason.lower()


def test_unknown_spot_never_drops_a_contract():
    """No spot means no judgement to make — dropping would hide real flow."""
    ok, _ = contract_quality(
        _c(strike=575.0, type="put", vol=160, oi=34, mid=118.45), 0.0)
    assert ok is True


def test_good_contracts_pass_clean():
    for tk, c in [
        ("NVDA", _c(strike=210.0, type="call", vol=52381, oi=9479,
                    mid=5.68, bid=5.65, ask=5.70, dte=4)),
        ("AMD",  _c(strike=450.0, type="put", vol=3892, oi=317,
                    mid=4.10, bid=4.00, ask=4.20, dte=2)),
    ]:
        ok, reason = contract_quality(c, SPOT[tk])
        assert ok is True, f"{tk} rejected: {reason}"


# ── actionability ─────────────────────────────────────────────────────────────
def test_call_breakeven_and_move_needed():
    e = contract_economics(
        _c(strike=210.0, type="call", vol=52381, oi=9479,
           mid=5.68, bid=5.65, ask=5.70), SPOT["NVDA"])
    assert e["breakeven"] == pytest.approx(215.68, abs=0.01)
    assert e["pct_to_breakeven"] == pytest.approx(3.45, abs=0.02)
    assert e["moneyness_pct"] == pytest.approx(0.73, abs=0.02)
    assert e["spread_pct"] == pytest.approx(0.88, abs=0.05)


def test_put_breakeven_is_below_the_strike():
    e = contract_economics(
        _c(strike=355.0, type="put", mid=5.55, bid=5.05, ask=6.05),
        SPOT["TSLA"])
    assert e["breakeven"] == pytest.approx(349.45, abs=0.01)
    # Spot is already through it — a put 0.14% in profit, not one needing a fall.
    assert e["pct_to_breakeven"] == pytest.approx(-0.14, abs=0.02)


def test_wide_spread_is_flagged_not_rejected():
    """18% wide is your fill, not a reason to hide the contract."""
    c = _c(strike=355.0, type="put", vol=178840, oi=2903,
           mid=5.55, bid=5.05, ask=6.05, dte=0)
    e = contract_economics(c, SPOT["TSLA"])
    assert e["spread_pct"] == pytest.approx(18.02, abs=0.1)
    assert e["wide_spread"] is True
    assert contract_quality(c, SPOT["TSLA"], market_open=True)[0] is True


def test_economics_survive_missing_bid_ask():
    e = contract_economics(_c(strike=210.0, type="call", mid=5.68), SPOT["NVDA"])
    assert e["spread_pct"] is None
    assert e["wide_spread"] is False
    assert e["breakeven"] == pytest.approx(215.68, abs=0.01)
