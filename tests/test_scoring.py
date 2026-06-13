from data.unusual_flow import (
    intra_chain_z,
    baseline_multiplier,
    adjusted_score,
)


def test_intra_chain_z_flags_outlier():
    assert intra_chain_z(100, [1, 1, 1, 100]) > 1.0


def test_intra_chain_z_flat_chain_is_zero():
    assert intra_chain_z(5, [5, 5, 5, 5]) == 0.0
    assert intra_chain_z(1, []) == 0.0


def test_baseline_multiplier_boosts_spike_and_is_neutral_without_history():
    assert baseline_multiplier(None, None, dampen=False) == 1.0
    assert baseline_multiplier(6.1, 3.5, dampen=False) == 1.5   # clamped high
    assert baseline_multiplier(2.0, None, dampen=False) == 1.15


def test_baseline_multiplier_dampens_perennial_megacap():
    assert baseline_multiplier(None, None, dampen=True) == 0.6


def test_adjusted_score_clamps():
    assert adjusted_score(80, 1.5) == 100
    assert adjusted_score(50, 0.6) == 30
    assert adjusted_score(40, 1.0) == 40
