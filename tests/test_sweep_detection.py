"""
Sweep detection, pinned against what the live OPRA feed actually sends.

Measured on 2026-09-08 during RTH: 73% of SPY 0DTE prints carry OPRA's 'I'
(Intermarket Sweep Order) condition. The previous rule returned True for any
single ISO print, so it flagged 72% of contracts -- and its time-based branch
never fired at all, because it compared the first and last print across the
whole collection window rather than sliding a window through them.

A sweep is one order split across exchanges and filled fast. These tests pin
that: ISO prints, clustered in time.
"""
import pytest

from data import tt_flow
from data.tt_flow import SWEEP_MIN_ISO_PRINTS as N, SWEEP_WINDOW_MS as W


def p(ts, conditions="I"):
    return tt_flow.DXPrint(symbol="X", price=1.0, size=1, bid=0.9, ask=1.1,
                           aggressor="buy", exchange="C", conditions=conditions,
                           spread_leg=False, ts=ts)


def test_clustered_iso_prints_are_a_sweep():
    assert tt_flow._is_sweep([p(1000 + i * 10) for i in range(N)]) is True


def test_a_single_iso_print_is_not_a_sweep():
    """The old rule's core failure: one ISO print flagged the whole contract."""
    assert tt_flow._is_sweep([p(1000)]) is False


def test_iso_prints_spread_over_time_are_not_a_sweep():
    """Same count, spread across seconds — that is ordinary activity."""
    assert tt_flow._is_sweep([p(1000 + i * W * 2) for i in range(N * 3)]) is False


def test_non_iso_prints_never_qualify():
    assert tt_flow._is_sweep([p(1000 + i, conditions="a") for i in range(N * 4)]) is False


def test_one_below_the_threshold_is_not_a_sweep():
    assert tt_flow._is_sweep([p(1000 + i * 10) for i in range(N - 1)]) is False


def test_a_burst_is_found_anywhere_in_the_window():
    """
    The old check compared first vs last across everything, so a genuine burst
    inside a long quiet stretch was missed. A sliding window finds it.
    """
    quiet = [p(i * 100_000) for i in range(6)]
    burst = [p(500_000 + i * 10) for i in range(N)]
    assert tt_flow._is_sweep(quiet + burst) is True


def test_prints_without_timestamps_are_ignored():
    assert tt_flow._is_sweep([p(0) for _ in range(N * 2)]) is False


def test_empty_input_is_not_a_sweep():
    assert tt_flow._is_sweep([]) is False


def test_missing_conditions_do_not_raise():
    assert tt_flow._is_sweep([p(1000 + i, conditions=None) for i in range(N)]) is False


def test_golden_sweep_is_independent_of_this_rule():
    """
    is_golden keys off premium and trade side, not _is_sweep, so tightening
    sweep detection must not silently change which contracts are golden.
    """
    import inspect
    src = inspect.getsource(tt_flow.aggregate_flow)
    assert "is_golden = (max_single_premium" in src
    assert "is_sweep" not in src.split("is_golden = (")[1].split("\n")[0]


# ── Contract stats coercion ───────────────────────────────────────────────────
def test_nan_string_from_the_feed_is_rejected():
    """
    dxFeed sends dayVolume as the literal string "NaN" for illiquid contracts.
    int("NaN") raises, and float("NaN") silently becomes a number that poisons
    any ratio computed from it.
    """
    assert tt_flow._num_or_none("NaN") is None
    assert tt_flow._num_or_none(float("nan")) is None
    assert tt_flow._num_or_none(None) is None
    assert tt_flow._num_or_none("junk") is None


def test_real_numbers_survive_coercion():
    assert tt_flow._num_or_none(1967.0) == 1967.0
    assert tt_flow._num_or_none(0) == 0.0
    assert tt_flow._num_or_none("413") == 413.0


def test_booleans_are_not_treated_as_numbers():
    assert tt_flow._num_or_none(True) is None


def test_feed_config_reads_each_event_separately():
    """One FEED_CONFIG can describe several events; each needs its own layout."""
    msg = {"eventFields": {"Summary": ["eventSymbol", "openInterest"],
                           "Trade": ["eventSymbol", "dayVolume"]}}
    assert tt_flow.parse_feed_config(msg, "Summary") == {"eventSymbol": 0, "openInterest": 1}
    assert tt_flow.parse_feed_config(msg, "Trade") == {"eventSymbol": 0, "dayVolume": 1}
    assert tt_flow.parse_feed_config(msg, "TimeAndSale") == {}
