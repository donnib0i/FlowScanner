"""
Dealer gamma exposure. Measurement, not prediction: every assertion here is
about a quantity with units and a derivation, and there is deliberately nothing
to test about a "verdict" because the module does not produce one.

The convention under test, stated once:
    gamma_notional(K) = gamma(K) * OI(K) * 100 * S^2 * 0.01
    -> dollars of delta dealers must hedge per 1% move in spot
    -> positive = dealers long gamma at that strike
"""
import math

import pytest

from core import gex as G
from core.greeks import bs_greeks


SPOT = 100.0


def row(strike, opt_type="call", oi=1000, iv=0.20, volume=0, expiry="2026-09-18"):
    return {"strike": strike, "type": opt_type, "oi": oi, "iv": iv,
            "volume": volume, "expiry": expiry, "bid": 1.0, "ask": 1.1,
            "last": 1.05}


def notional(strike, opt_type, oi, iv, spot=SPOT, T=0.03):
    g = bs_greeks(spot, strike, T, iv, opt_type=opt_type)["gamma"]
    return g * oi * 100 * spot ** 2 * 0.01


# ── The convention ────────────────────────────────────────────────────────────
def test_gamma_notional_matches_the_stated_formula():
    g = bs_greeks(SPOT, 100.0, 0.03, 0.20, opt_type="call")["gamma"]
    expected = g * 1000 * 100 * SPOT ** 2 * 0.01
    assert G.gamma_notional(g, 1000, SPOT) == pytest.approx(expected, rel=1e-12)


def test_gamma_notional_scales_linearly_with_open_interest():
    g = 0.02
    assert G.gamma_notional(g, 2000, SPOT) == pytest.approx(
        2 * G.gamma_notional(g, 1000, SPOT), rel=1e-12)


def test_zero_open_interest_contributes_nothing():
    assert G.gamma_notional(0.02, 0, SPOT) == 0.0


# ── Naive dealer sign ─────────────────────────────────────────────────────────
def test_assumed_sign_is_long_calls_short_puts():
    prof = G.build_profile([row(100.0, "call"), row(100.0, "put")], SPOT, 0.03)
    by = {(r["strike"], r["type"]): r for r in prof}
    assert by[(100.0, "call")]["gamma_notional"] > 0
    assert by[(100.0, "put")]["gamma_notional"] < 0
    assert all(r["src"] == "assumed" for r in prof)


def test_net_gex_sums_the_signed_notionals():
    rows = [row(95.0, "call", oi=500), row(105.0, "put", oi=800)]
    prof = G.build_profile(rows, SPOT, 0.03)
    assert G.net_gex(prof) == pytest.approx(
        sum(r["gamma_notional"] for r in prof), rel=1e-12)


# ── Flow-inferred sign ────────────────────────────────────────────────────────
def flow(strike, ask=0, bid=0, opt_type="call"):
    """Classified volume at a strike: ask-side = customer bought."""
    return {"strike": strike, "type": opt_type, "ask": ask, "bid": bid}


def test_ask_heavy_flow_makes_dealers_short_gamma():
    """Customer bought -> dealer sold -> dealer is short gamma there."""
    prof = G.build_profile([row(100.0, "call", volume=1000)], SPOT, 0.03,
                           flow={(100.0, "call"): {"ask": 900, "bid": 100}})
    r = prof[0]
    assert r["src"] == "inferred"
    assert r["gamma_notional"] < 0


def test_bid_heavy_flow_makes_dealers_long_gamma():
    prof = G.build_profile([row(100.0, "call", volume=1000)], SPOT, 0.03,
                           flow={(100.0, "call"): {"ask": 100, "bid": 900}})
    assert prof[0]["src"] == "inferred"
    assert prof[0]["gamma_notional"] > 0


def test_inference_reports_its_confidence():
    prof = G.build_profile([row(100.0, "call", volume=1000)], SPOT, 0.03,
                           flow={(100.0, "call"): {"ask": 800, "bid": 200}})
    assert prof[0]["confidence"] == pytest.approx(0.8, abs=1e-9)


def test_low_contract_count_falls_back_to_assumed():
    """3 contracts all on the ask is 100% lopsided and means nothing."""
    prof = G.build_profile([row(100.0, "call", volume=3)], SPOT, 0.03,
                           flow={(100.0, "call"): {"ask": 3, "bid": 0}})
    assert prof[0]["src"] == "assumed"


def test_a_split_share_falls_back_to_assumed():
    """High volume, but 55/45 says nothing about who is holding the gamma."""
    prof = G.build_profile([row(100.0, "call", volume=10_000)], SPOT, 0.03,
                           flow={(100.0, "call"): {"ask": 5500, "bid": 4500}})
    assert prof[0]["src"] == "assumed"


def test_both_gates_must_pass_independently():
    lots_but_split = G.build_profile([row(100.0, "call", volume=10_000)], SPOT, 0.03,
                                     flow={(100.0, "call"): {"ask": 5100, "bid": 4900}})
    lopsided_but_tiny = G.build_profile([row(100.0, "call", volume=10)], SPOT, 0.03,
                                        flow={(100.0, "call"): {"ask": 10, "bid": 0}})
    assert lots_but_split[0]["src"] == "assumed"
    assert lopsided_but_tiny[0]["src"] == "assumed"


def test_mid_volume_is_discarded_not_split():
    """Mid prints say nothing about aggressor; counting them dilutes the share."""
    prof = G.build_profile([row(100.0, "call", volume=10_000)], SPOT, 0.03,
                           flow={(100.0, "call"): {"ask": 900, "bid": 100, "mid": 9000}})
    assert prof[0]["src"] == "inferred"


# ── Zero-gamma flip: grid method ──────────────────────────────────────────────
def balanced_chain():
    """Calls above, puts below: net gamma crosses zero somewhere between."""
    return ([row(k, "call", oi=1000) for k in (105.0, 110.0)]
            + [row(k, "put", oi=1000) for k in (90.0, 95.0)])


def test_flip_is_found_and_lies_within_the_grid():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    assert out["flip"] is not None
    assert SPOT * 0.95 <= out["flip"] <= SPOT * 1.05


def test_net_gex_changes_sign_across_the_flip():
    """The defining property: it is the root of net gamma as a function of spot."""
    chain = balanced_chain()
    out = G.compute(chain, SPOT, 0.03)
    flip = out["flip"]
    below = G.net_gex(G.build_profile(chain, flip - 2.0, 0.03))
    above = G.net_gex(G.build_profile(chain, flip + 2.0, 0.03))
    assert below * above < 0, "net gamma must change sign across the flip"


def test_flip_is_recomputed_at_each_spot_not_read_off_the_bars():
    """
    The naive method interpolates between the last positive and first negative
    strike bar. That conflates 'gamma contributed by strike K' with 'net gamma
    when spot is at K'. On this chain the two answers differ.
    """
    chain = balanced_chain()
    out = G.compute(chain, SPOT, 0.03)
    prof = sorted(G.build_profile(chain, SPOT, 0.03), key=lambda r: r["strike"])
    naive = None
    for a, b in zip(prof, prof[1:]):
        if a["gamma_notional"] < 0 <= b["gamma_notional"]:
            naive = a["strike"]
            break
    assert naive is not None
    assert abs(out["flip"] - naive) > G.FLIP_TOLERANCE


def test_flip_is_none_when_the_profile_never_crosses():
    """All calls, all long gamma. Inventing a flip is worse than reporting none."""
    out = G.compute([row(k, "call", oi=1000) for k in (95.0, 100.0, 105.0)],
                    SPOT, 0.03)
    assert out["flip"] is None
    assert out["flips"] == []


def test_all_roots_are_reported_and_flip_is_the_nearest():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    assert out["flip"] in out["flips"]
    nearest = min(out["flips"], key=lambda f: abs(f - SPOT))
    assert out["flip"] == nearest


def test_flip_precision_is_within_tolerance():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    net_at_flip = G.net_gex(G.build_profile(balanced_chain(), out["flip"], 0.03))
    scale = sum(abs(r["gamma_notional"])
                for r in G.build_profile(balanced_chain(), SPOT, 0.03))
    assert abs(net_at_flip) < scale * 0.01


# ── Walls ─────────────────────────────────────────────────────────────────────
def test_call_wall_is_the_largest_positive_strike():
    chain = [row(105.0, "call", oi=5000), row(110.0, "call", oi=500),
             row(95.0, "put", oi=800)]
    out = G.compute(chain, SPOT, 0.03)
    assert out["call_wall"] == 105.0


def test_put_wall_is_the_largest_negative_strike():
    chain = [row(105.0, "call", oi=500), row(90.0, "put", oi=9000),
             row(95.0, "put", oi=100)]
    out = G.compute(chain, SPOT, 0.03)
    assert out["put_wall"] == 90.0


def test_walls_are_none_on_an_empty_side():
    out = G.compute([row(105.0, "call", oi=500)], SPOT, 0.03)
    assert out["call_wall"] == 105.0
    assert out["put_wall"] is None


# ── Missing IV and near-expiry clamping ───────────────────────────────────────
def test_strike_with_no_usable_iv_is_excluded_and_counted():
    chain = [row(100.0, "call", iv=0.0), row(105.0, "call", iv=0.20)]
    # No bid/ask/last to solve from either.
    chain[0].update(bid=0, ask=0, last=0)
    out = G.compute(chain, SPOT, 0.03)
    assert out["provenance"]["strikes_dropped"] == 1
    assert all(r["strike"] != 100.0 for r in out["profile"])


def test_missing_iv_is_recovered_from_the_mid_price():
    """Wing OI must not vanish just because the feed omitted a vol."""
    from core.greeks import _bs_price
    price = _bs_price(SPOT, 115.0, 0.03, 0.35, 0.0, "call")
    r = row(115.0, "call", iv=0.0)
    r.update(bid=price - 0.01, ask=price + 0.01, last=price)
    out = G.compute([r], SPOT, 0.03)
    assert out["provenance"]["strikes_dropped"] == 0
    assert out["profile"][0]["iv"] == pytest.approx(0.35, abs=1e-2)


def test_near_expiry_concentration_is_measured_not_suppressed():
    """
    Gamma diverges at the money as T -> 0, so on a 0DTE chain one strike really
    does carry the surface. That is the shape of the book, not an artifact:
    the module reports the concentration rather than clamping the value, which
    would be it editorialising its own input.
    """
    chain = [row(100.0, "call", oi=1000)] + [
        row(k, "call", oi=1000) for k in (98.0, 99.0, 101.0, 102.0)]
    out = G.compute(chain, SPOT, T=G.MIN_T)
    assert out["provenance"]["max_strike_share"] > 0.9
    assert out["provenance"]["concentrated"] is True


def test_a_balanced_chain_is_not_flagged_concentrated():
    chain = [row(k, "call", oi=1000) for k in (95.0, 100.0, 105.0)]
    out = G.compute(chain, SPOT, 0.25)
    assert out["provenance"]["max_strike_share"] < 0.50
    assert out["provenance"]["concentrated"] is False


def test_dominant_strike_keeps_its_true_notional():
    """The ATM value must survive intact; nothing silently rewrites it."""
    chain = [row(100.0, "call", oi=1000), row(105.0, "call", oi=1000)]
    out = G.compute(chain, SPOT, T=G.MIN_T)
    atm = next(r for r in out["profile"] if r["strike"] == 100.0)
    expected = notional(100.0, "call", 1000, 0.20, T=G.MIN_T)
    assert atm["gamma_notional"] == pytest.approx(expected, rel=1e-9)


# ── Provenance ────────────────────────────────────────────────────────────────
def test_inferred_share_is_weighted_by_notional_not_strike_count():
    """A handful of high-OI strikes dominate the surface; counting rows lies."""
    chain = [row(100.0, "call", oi=100_000, volume=10_000),
             row(105.0, "call", oi=10, volume=0),
             row(110.0, "call", oi=10, volume=0)]
    out = G.compute(chain, SPOT, 0.03,
                    flow={(100.0, "call"): {"ask": 9000, "bid": 1000}})
    assert out["provenance"]["inferred_pct"] > 0.9, "one huge strike must dominate"


def test_inferred_and_assumed_shares_sum_to_one():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    p = out["provenance"]
    assert p["inferred_pct"] + p["assumed_pct"] == pytest.approx(1.0, abs=1e-9)


def test_provenance_marks_open_interest_stale():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    assert out["provenance"]["oi_stale"] is True
    assert out["provenance"]["oi_asof"]


def test_strike_totals_are_reported():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    assert out["provenance"]["strikes_total"] == 4


# ── The prime directive ───────────────────────────────────────────────────────
FORECAST_KEYS = {"bias", "verdict", "regime", "target", "signal", "score",
                 "prediction", "direction", "recommendation"}


def test_output_contains_no_forecast_shaped_key():
    """
    The module measures dealer inventory. It does not say where price is going,
    and no key may imply that it does.
    """
    out = G.compute(balanced_chain(), SPOT, 0.03)

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                assert k.lower() not in FORECAST_KEYS, f"forecast-shaped key: {k}"
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(out)


# ── Degenerate inputs ─────────────────────────────────────────────────────────
def test_empty_chain_returns_a_well_formed_empty_result():
    out = G.compute([], SPOT, 0.03)
    assert out["profile"] == [] and out["net_gex"] == 0.0
    assert out["flip"] is None and out["call_wall"] is None


def test_zero_spot_does_not_raise():
    out = G.compute(balanced_chain(), 0.0, 0.03)
    assert math.isfinite(out["net_gex"])


def test_rows_missing_fields_are_skipped_not_fatal():
    chain = balanced_chain() + [{"strike": None, "type": "call"}, {}]
    out = G.compute(chain, SPOT, 0.03)
    assert math.isfinite(out["net_gex"])


# ── Multi-expiry chains ───────────────────────────────────────────────────────
def test_a_rows_own_time_to_expiry_wins_over_the_default():
    """Pricing a 30DTE strike as 0DTE overstates its gamma by orders of magnitude."""
    near = row(100.0, "call", oi=1000); near["T"] = 1.0 / 365
    far  = row(100.0, "call", oi=1000); far["T"] = 30.0 / 365
    prof = G.build_profile([near, far], SPOT, T=1.0 / 365)
    assert prof[0]["gamma_notional"] > prof[1]["gamma_notional"] * 3


def test_rows_without_a_t_fall_back_to_the_default():
    prof = G.build_profile([row(100.0, "call")], SPOT, T=0.03)
    assert prof[0]["T"] == pytest.approx(0.03)


# ── No open interest: the feed failure that looks like an answer ──────────────
def zero_oi_chain():
    return [row(k, "call", oi=0) for k in (95.0, 100.0, 105.0)] + \
           [row(k, "put", oi=0) for k in (95.0, 100.0, 105.0)]


def test_a_chain_with_no_open_interest_is_reported_unusable():
    """
    Observed 2026-09-07: yfinance returns zero OI across SPX/SPY/QQQ near
    expiries. OI is the entire input, so the result is noise -- and noise shaped
    like an answer is worse than no answer.
    """
    out = G.compute(zero_oi_chain(), SPOT, 0.03)
    assert out["provenance"]["oi_usable"] is False
    assert out["provenance"]["oi_total"] == 0


def test_no_walls_are_invented_without_open_interest():
    out = G.compute(zero_oi_chain(), SPOT, 0.03)
    assert out["call_wall"] is None and out["put_wall"] is None


def test_no_flip_is_invented_without_open_interest():
    out = G.compute(zero_oi_chain(), SPOT, 0.03)
    assert out["flip"] is None and out["flips"] == []


def test_a_handful_of_stray_contracts_do_not_become_walls():
    """
    The real SPX chain on 2026-09-07: 8 contracts of open interest spread over
    four strikes out of 1,070. That summed to "more than zero" and produced a
    call wall 24% above spot. Coverage is the test, not the total.
    """
    chain = [row(90.0 + i, "call", oi=0) for i in range(40)] + [row(150.0, "call", oi=1)]
    out = G.compute(chain, SPOT, 0.03)
    assert out["provenance"]["oi_usable"] is False
    assert out["call_wall"] is None


def test_coverage_is_reported():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    assert out["provenance"]["oi_coverage"] == pytest.approx(1.0)


def test_real_open_interest_is_still_reported_usable():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    assert out["provenance"]["oi_usable"] is True
    assert out["provenance"]["oi_total"] == 4000


# ── Time to expiry ────────────────────────────────────────────────────────────
def test_zero_dte_uses_the_intraday_clock():
    """At 15:55 a 0DTE contract has five minutes of life, not a full day."""
    five_min = G.years_to_expiry(0, minutes_left=5)
    full_day = G.years_to_expiry(0, minutes_left=390)
    assert five_min < full_day
    assert five_min == pytest.approx(5 / (365 * 24 * 60), rel=1e-9)


def test_time_to_expiry_adds_the_remaining_session():
    """A 2DTE contract still has today's remaining hours on top of two days."""
    assert G.years_to_expiry(2, minutes_left=390) == pytest.approx(
        2 / 365 + 390 / (365 * 24 * 60), rel=1e-9)


def test_time_to_expiry_never_goes_below_the_floor():
    assert G.years_to_expiry(0, minutes_left=0) == pytest.approx(G.MIN_T)


def test_shorter_time_means_more_atm_gamma():
    """Gamma scales as 1/sqrt(T); understating T understates the whole surface."""
    near = G.build_profile([row(100.0, "call", oi=1000)], SPOT,
                           G.years_to_expiry(0, minutes_left=30))
    far = G.build_profile([row(100.0, "call", oi=1000)], SPOT,
                          G.years_to_expiry(0, minutes_left=390))
    assert near[0]["gamma_notional"] > far[0]["gamma_notional"] * 2


# ── Deriving the sign from the chain ──────────────────────────────────────────
def chain_row(strike, opt_type, bid, ask, last, volume):
    return {"strike": strike, "type": opt_type, "bid": bid, "ask": ask,
            "last": last, "volume": volume, "oi": 1000, "iv": 0.20}


def test_last_near_the_ask_counts_as_customer_buying():
    f = G.flow_from_chain([chain_row(100.0, "call", 1.00, 2.00, 1.95, 500)])
    assert f[(100.0, "call")]["ask"] == 500


def test_last_near_the_bid_counts_as_customer_selling():
    f = G.flow_from_chain([chain_row(100.0, "call", 1.00, 2.00, 1.05, 500)])
    assert f[(100.0, "call")]["bid"] == 500


def test_mid_prints_are_not_recorded():
    f = G.flow_from_chain([chain_row(100.0, "call", 1.00, 2.00, 1.50, 500)])
    assert (100.0, "call") not in f


def test_zero_volume_rows_are_ignored():
    f = G.flow_from_chain([chain_row(100.0, "call", 1.00, 2.00, 1.95, 0)])
    assert f == {}


def test_volume_accumulates_across_expiries_at_one_strike():
    f = G.flow_from_chain([chain_row(100.0, "call", 1.0, 2.0, 1.95, 300),
                           chain_row(100.0, "call", 1.0, 2.0, 1.95, 400)])
    assert f[(100.0, "call")]["ask"] == 700


def test_derived_flow_actually_flips_a_dealer_sign():
    """The end of the chain: derived flow must reach the profile, not sit unused."""
    rows = [chain_row(100.0, "call", 1.00, 2.00, 1.95, 5000)]
    naive = G.build_profile(rows, SPOT, 0.03)
    inferred = G.build_profile(rows, SPOT, 0.03, flow=G.flow_from_chain(rows))
    assert naive[0]["src"] == "assumed" and naive[0]["gamma_notional"] > 0
    assert inferred[0]["src"] == "inferred" and inferred[0]["gamma_notional"] < 0


# ── Flip stability ────────────────────────────────────────────────────────────
def test_a_single_clean_crossing_is_reported_stable():
    out = G.compute(balanced_chain(), SPOT, 0.03)
    assert out["provenance"]["flip_roots"] == 1
    assert out["provenance"]["flip_stable"] is True


def test_a_profile_crossing_repeatedly_is_reported_unstable():
    """
    Observed on live SPX: seven roots within +/-5%. Alternating dealer signs on
    adjacent strikes make net gamma oscillate through zero as spot passes each
    one, so the nearest root is an artifact of where spot happens to sit -- and
    it moved 90 points between two fetches seconds apart.
    """
    strikes = [96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0]
    chain, flow = [], {}
    for i, k in enumerate(strikes):
        chain.append(row(k, "call", oi=5000, volume=10_000))
        # Alternate observed sign strike by strike.
        flow[(k, "call")] = ({"ask": 9000, "bid": 1000} if i % 2
                             else {"ask": 1000, "bid": 9000})
    out = G.compute(chain, SPOT, T=1.0 / 365, flow=flow)
    assert out["provenance"]["flip_roots"] > 1, out["flips"]
    assert out["provenance"]["flip_stable"] is False


def test_no_flip_is_not_reported_as_stable():
    out = G.compute([row(k, "call", oi=1000) for k in (95.0, 100.0, 105.0)], SPOT, 0.03)
    assert out["flip"] is None
    assert out["provenance"]["flip_stable"] is False
