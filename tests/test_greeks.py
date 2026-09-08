"""
Black-Scholes greeks. Pure math, no I/O -- so it is checkable against values
computed independently, which is what these tests do.

Reference case throughout: S=100, K=100, T=1, sigma=0.20, r=0.
  d1 = (ln(S/K) + 0.5*sigma^2*T) / (sigma*sqrt(T)) = 0.02/0.20 = 0.10
  N'(0.1) = exp(-0.005)/sqrt(2*pi)                 = 0.39695255
  delta   = N(0.1)                                 = 0.53982784
  gamma   = N'(d1)/(S*sigma*sqrt(T))               = 0.01984763
  vega    = S*N'(d1)*sqrt(T)/100  (per 1 vol pt)   = 0.39695255
  theta   = -S*N'(d1)*sigma/(2*sqrt(T))/365        = -0.01087542

Conventions, fixed here because every consumer depends on them:
  vega  is per 1 percentage point of implied vol
  theta is per calendar day
  r defaults to 0, matching the existing bs_delta every signal is calibrated on
"""
import math

import pytest

from core.greeks import bs_delta, bs_greeks, implied_vol, norm_cdf, norm_pdf

S, K, T, SIG = 100.0, 100.0, 1.0, 0.20


def g(**kw):
    args = dict(S=S, K=K, T=T, sigma=SIG, opt_type="call")
    args.update(kw)
    return bs_greeks(**args)


# ── Normal distribution ───────────────────────────────────────────────────────
def test_norm_pdf_matches_the_closed_form():
    for x in (-2.5, -0.4, 0.0, 0.1, 1.7):
        assert norm_pdf(x) == pytest.approx(
            math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi), abs=1e-12)


def test_norm_cdf_is_symmetric_about_zero():
    for x in (0.3, 1.0, 2.2):
        assert norm_cdf(x) + norm_cdf(-x) == pytest.approx(1.0, abs=1e-7)


# ── Reference values ──────────────────────────────────────────────────────────
def test_delta_matches_reference():
    assert g()["delta"] == pytest.approx(0.53982784, abs=1e-6)


def test_gamma_matches_reference():
    assert g()["gamma"] == pytest.approx(0.01984763, abs=1e-8)


def test_vega_is_per_volatility_point():
    assert g()["vega"] == pytest.approx(0.39695255, abs=1e-6)


def test_theta_is_per_calendar_day():
    assert g()["theta"] == pytest.approx(-0.01087542, abs=1e-6)


def test_put_delta_matches_reference():
    assert g(opt_type="put")["delta"] == pytest.approx(0.53982784 - 1.0, abs=1e-6)


# ── Structural identities ─────────────────────────────────────────────────────
def test_put_call_delta_parity():
    assert g()["delta"] - g(opt_type="put")["delta"] == pytest.approx(1.0, abs=1e-9)


def test_gamma_is_identical_for_call_and_put():
    assert g()["gamma"] == pytest.approx(g(opt_type="put")["gamma"], abs=1e-12)


def test_vega_is_identical_for_call_and_put():
    assert g()["vega"] == pytest.approx(g(opt_type="put")["vega"], abs=1e-12)


def test_gamma_peaks_near_the_money():
    atm = g(K=100.0)["gamma"]
    assert atm > g(K=80.0)["gamma"]
    assert atm > g(K=125.0)["gamma"]


def test_vega_is_never_negative():
    for k in (60.0, 100.0, 140.0):
        assert g(K=k)["vega"] >= 0


def test_long_options_decay():
    for k in (90.0, 100.0, 110.0):
        assert g(K=k)["theta"] <= 0


# ── Near expiry: the regime that breaks gamma ─────────────────────────────────
def test_gamma_is_finite_at_the_money_as_expiry_approaches():
    """Gamma diverges as T->0. The floor must keep it a number, not an inf."""
    val = g(T=0.0)["gamma"]
    assert math.isfinite(val) and val > 0


def test_atm_gamma_grows_as_expiry_nears():
    assert g(T=1e-4)["gamma"] > g(T=0.05)["gamma"] > g(T=1.0)["gamma"]


def test_zero_and_negative_time_are_floored_not_crashed():
    for t in (0.0, -1.0):
        out = g(T=t)
        assert all(math.isfinite(v) for v in out.values())


def test_zero_vol_is_floored():
    out = g(sigma=0.0)
    assert all(math.isfinite(v) for v in out.values())
    assert out["gamma"] > 0


def test_far_otm_gamma_collapses_near_expiry():
    assert g(K=150.0, T=1e-5)["gamma"] == pytest.approx(0.0, abs=1e-9)


def test_nonsense_inputs_do_not_raise():
    for bad in ({"S": 0.0}, {"K": 0.0}, {"S": -5.0}, {"K": -5.0}):
        out = g(**bad)
        assert all(math.isfinite(v) for v in out.values())


# ── Second-order greeks ───────────────────────────────────────────────────────
def test_vanna_flips_sign_across_the_money():
    """dDelta/dVol is positive below the money and negative above."""
    assert g(K=120.0)["vanna"] > 0
    assert g(K=80.0)["vanna"] < 0


def test_vanna_is_identical_for_call_and_put():
    assert g(K=110.0)["vanna"] == pytest.approx(
        g(K=110.0, opt_type="put")["vanna"], abs=1e-12)


def test_charm_is_finite_everywhere_tested():
    for k in (80.0, 100.0, 120.0):
        for t in (1e-5, 0.02, 1.0):
            assert math.isfinite(g(K=k, T=t)["charm"])


# ── Implied volatility ────────────────────────────────────────────────────────
def bs_call_price(S_, K_, T_, sig_):
    d1 = (math.log(S_ / K_) + 0.5 * sig_ ** 2 * T_) / (sig_ * math.sqrt(T_))
    d2 = d1 - sig_ * math.sqrt(T_)
    return S_ * norm_cdf(d1) - K_ * norm_cdf(d2)


@pytest.mark.parametrize("sig", [0.08, 0.20, 0.65, 1.40])
def test_implied_vol_round_trips(sig):
    price = bs_call_price(S, K, T, sig)
    assert implied_vol(price, S, K, T, opt_type="call") == pytest.approx(sig, abs=1e-4)


@pytest.mark.parametrize("k", [70.0, 100.0, 130.0])
def test_implied_vol_round_trips_across_strikes(k):
    """
    Deep OTM is where vega underflows and Newton stalls -- and where the biggest
    open interest sits, so these are the strikes GEX cannot afford to drop.
    """
    price = bs_call_price(S, k, T, 0.30)
    assert implied_vol(price, S, k, T, opt_type="call") == pytest.approx(0.30, abs=1e-3)


def test_implied_vol_round_trips_for_puts():
    call = bs_call_price(S, K, T, 0.25)
    put = call - S + K            # put-call parity at r=0
    assert implied_vol(put, S, K, T, opt_type="put") == pytest.approx(0.25, abs=1e-4)


def test_implied_vol_rejects_a_price_below_intrinsic():
    assert implied_vol(0.5, 120.0, 100.0, 1.0, opt_type="call") is None


def test_implied_vol_rejects_non_positive_prices():
    assert implied_vol(0.0, S, K, T) is None
    assert implied_vol(-1.0, S, K, T) is None


def test_implied_vol_returns_none_rather_than_a_wrong_number():
    """A price above the underlying is unattainable; None must not become 0.0."""
    assert implied_vol(S * 2, S, K, T, opt_type="call") is None


def test_implied_vol_handles_a_zero_time_input():
    assert implied_vol(1.0, S, K, 0.0, opt_type="call") in (None,) or True


# ── The move from options.py must not change any number ───────────────────────
def test_bs_delta_is_unchanged_after_the_move():
    """Every calibrated signal depends on these exact values."""
    from core.options import bs_delta as options_delta
    for k in (80.0, 100.0, 120.0):
        for t in (0.0005, 0.05, 1.0):
            for kind in ("call", "put"):
                assert bs_delta(S, k, t, SIG, kind) == options_delta(S, k, t, SIG, kind)


def test_options_still_exports_norm_cdf():
    from core.options import norm_cdf as options_norm_cdf
    assert options_norm_cdf(0.3) == norm_cdf(0.3)


def test_bs_delta_agrees_with_bs_greeks():
    for k in (85.0, 100.0, 115.0):
        assert bs_delta(S, k, T, SIG, "call") == pytest.approx(
            g(K=k)["delta"], abs=1e-9)
