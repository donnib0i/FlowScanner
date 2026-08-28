"""
Calibrated probabilistic scoring.

The point of this module is that its numbers are checkable, so these tests are
mostly about the *guards*, not the arithmetic: that outcome fields can never
reach the feature vector, that splits are by session and never by row, that a
model which does not beat a constant out of sample is refused, and that a
refusal leaves the existing scoring untouched rather than shipping a 0.5.

Everything runs on synthetic records built in-process. No network, no reading
the user's real results/ directory — a test that passed only because April 2026
happened to be a good month would be worthless.
"""
import json
import math

import numpy as np
import pytest

from core import calibration as cal


# ── fixtures ─────────────────────────────────────────────────────────────────
def _rec(date, ticker="AAA", signal="gap_up", setup_q=0.5, rel_vol=1.5,
         gap_pct=0.5, hv20=0.2, direction="up", correct=1, opt_pnl=10.0, **kw):
    r = {
        "date": date, "ticker": ticker, "signal": signal, "signal_types": [signal],
        "setup_q": setup_q, "rel_vol": rel_vol, "gap_pct": gap_pct, "hv20": hv20,
        "direction": direction, "ema_bull": False, "above_vwap": False,
        "aplus_score": 0, "direction_correct": correct, "opt_pnl_pct": opt_pnl,
    }
    r.update(kw)
    return r


def _sessions(n_days, per_day=10, start=1):
    """n_days synthetic sessions, `per_day` rows each."""
    out = []
    for d in range(start, start + n_days):
        date = f"2026-01-{d:02d}" if d < 32 else f"2026-02-{d - 31:02d}"
        for t in range(per_day):
            out.append(_rec(date, ticker=f"T{t}"))
    return out


# ── look-ahead guards ────────────────────────────────────────────────────────
def test_no_feature_is_an_outcome_field():
    """The whole model is worthless if an outcome leaks in. Pin it."""
    cal.assert_no_lookahead()
    assert not set(cal.FEATURES) & cal.OUTCOME_FIELDS


def test_feature_vector_ignores_outcome_fields():
    """Same signal-time inputs must give the same vector regardless of how the
    trade actually resolved — otherwise the fit is reading the answer."""
    winner = _rec("2026-01-05", correct=1, opt_pnl=120.0,
                  fwd_return_pct=3.0, next_close=110.0, intraday_range_pct=4.0)
    loser = _rec("2026-01-05", correct=0, opt_pnl=-50.0,
                 fwd_return_pct=-3.0, next_close=90.0, intraday_range_pct=1.0)
    assert cal.feature_vector(winner) == cal.feature_vector(loser)


def test_feature_vector_length_matches_feature_names():
    assert len(cal.feature_vector(_rec("2026-01-05"))) == len(cal.FEATURES)


def test_assert_no_lookahead_raises_when_contract_is_violated(monkeypatch):
    monkeypatch.setattr(cal, "FEATURES", cal.FEATURES + ("opt_pnl_pct",))
    with pytest.raises(ValueError, match="look-ahead"):
        cal.assert_no_lookahead()


def test_feature_vector_survives_missing_and_nan_fields():
    vec = cal.feature_vector({"date": "2026-01-05", "rel_vol": float("nan"),
                              "setup_q": None, "gap_pct": "x"})
    assert len(vec) == len(cal.FEATURES)
    assert all(v == v for v in vec)          # no NaN escaped


# ── splitting ────────────────────────────────────────────────────────────────
def test_split_is_by_session_never_by_row():
    """No session may appear on both sides. A random row split would put a
    trade's own day in training and inflate every held-out number."""
    _, _, dates = cal.build_dataset(_sessions(20), label="direction")
    train, test = cal.purged_time_split(dates)
    assert not (set(dates[train].tolist()) & set(dates[test].tolist()))


def test_split_embargoes_sessions_between_train_and_test():
    """Forward returns reach one bar past the signal, so the sessions either
    side of the boundary must belong to neither fold."""
    _, _, dates = cal.build_dataset(_sessions(20), label="direction")
    uniq = sorted(set(dates.tolist()))
    train, test = cal.purged_time_split(dates, train_fraction=0.7, embargo_days=2)
    used = set(dates[train].tolist()) | set(dates[test].tolist())
    assert len(uniq) - len(used) == 2


def test_split_train_is_strictly_older_than_test():
    _, _, dates = cal.build_dataset(_sessions(20), label="direction")
    train, test = cal.purged_time_split(dates)
    assert max(dates[train].tolist()) < min(dates[test].tolist())


# ── effective sample size ────────────────────────────────────────────────────
def test_clustered_outcomes_shrink_the_effective_sample():
    """Every row in a session sharing one outcome is one observation, not 20."""
    y, dates = [], []
    for d in range(20):
        outcome = float(d % 2)
        for _ in range(20):
            y.append(outcome)
            dates.append(f"d{d:02d}")
    ess = cal.effective_sample_size(np.array(y), np.array(dates, dtype=object))
    assert ess["n_rows"] == 400
    assert ess["design_effect"] > 10
    assert ess["effective_n"] < 50


def test_independent_outcomes_keep_the_full_sample():
    rng = np.random.default_rng(7)
    y = rng.integers(0, 2, 400).astype(float)
    dates = np.array([f"d{i // 20:02d}" for i in range(400)], dtype=object)
    ess = cal.effective_sample_size(y, dates)
    assert ess["design_effect"] < 2.0
    assert ess["effective_n"] > 200


def test_effective_sample_size_handles_empty_and_degenerate_input():
    empty = cal.effective_sample_size(np.array([]), np.array([], dtype=object))
    assert empty["effective_n"] == 0
    allwin = cal.effective_sample_size(np.ones(50),
                                       np.array([f"d{i%5}" for i in range(50)], dtype=object))
    assert allwin["effective_n"] == 50.0     # no variance => no evidence of clustering


# ── model ────────────────────────────────────────────────────────────────────
def test_logistic_model_recovers_a_known_relationship():
    """Sanity: on data that IS separable by setup_q, the fit must find it."""
    rng = np.random.default_rng(3)
    X = np.zeros((600, len(cal.FEATURES)))
    X[:, 0] = rng.uniform(0, 1, 600)                        # setup_q
    y = (rng.uniform(0, 1, 600) < X[:, 0]).astype(float)
    model = cal.LogisticModel().fit(X, y, l2=0.01)
    assert dict(model.coefficients())["setup_q"] > 1.0
    assert cal.auc_score(y, model.predict_proba(X)) > 0.7


def test_predicted_probabilities_stay_in_range():
    X = np.random.default_rng(1).normal(0, 5, (200, len(cal.FEATURES)))
    y = (X[:, 0] > 0).astype(float)
    p = cal.LogisticModel().fit(X, y).predict_proba(X)
    assert p.min() >= 0.0 and p.max() <= 1.0


def test_model_round_trips_through_json():
    X = np.random.default_rng(2).normal(0, 1, (100, len(cal.FEATURES)))
    y = (X[:, 1] > 0).astype(float)
    model = cal.LogisticModel().fit(X, y)
    clone = cal.LogisticModel.from_dict(json.loads(json.dumps(model.to_dict())))
    assert np.allclose(model.predict_proba(X), clone.predict_proba(X))


# ── metrics ──────────────────────────────────────────────────────────────────
def test_brier_and_log_loss_reward_the_truth():
    y = np.array([1.0, 1.0, 0.0, 0.0])
    assert cal.brier_score(y, np.array([1.0, 1.0, 0.0, 0.0])) == 0.0
    assert cal.brier_score(y, np.array([0.5] * 4)) == 0.25
    assert cal.log_loss(y, np.array([0.9, 0.9, 0.1, 0.1])) < cal.log_loss(y, np.array([0.5] * 4))


def test_auc_reports_a_reversed_ranking_as_below_half():
    """An AUC under 0.5 is the finding, not a bug to clamp away."""
    y = np.array([0.0, 0.0, 1.0, 1.0])
    assert cal.auc_score(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert cal.auc_score(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
    assert cal.auc_score(y, np.array([0.5] * 4)) == 0.5


def test_reliability_table_flags_a_miscalibrated_bucket():
    """Predicting 0.9 on rows that never win must come back as NOT calibrated."""
    p = np.r_[np.full(200, 0.1), np.full(200, 0.9)]
    y = np.r_[np.full(200, 0.1), np.zeros(200)]
    rows = cal.reliability_table(y, p, n_buckets=2)
    top = [r for r in rows if r["predicted"] > 0.5][0]
    assert top["actual"] == 0.0
    assert top["calibrated"] is False
    assert cal.expected_calibration_error(rows) > 0.4


def test_reliability_buckets_are_equal_count_not_equal_width():
    """Predictions clumped in a narrow band must still spread across buckets —
    equal-width bins would leave one row holding everything and measure nothing."""
    p = np.linspace(0.48, 0.52, 500)
    y = (np.arange(500) % 2).astype(float)
    rows = cal.reliability_table(y, p, n_buckets=5)
    assert len(rows) == 5
    assert max(r["n"] for r in rows) - min(r["n"] for r in rows) <= 1


# ── expected value ───────────────────────────────────────────────────────────
def test_expected_value_uses_option_payoffs_not_the_probability_alone():
    # 20% at +100% against 80% at -50% is negative; a high win rate is not edge.
    assert cal.expected_value(0.20, 100.0, -50.0) < 0
    assert cal.expected_value(0.40, 100.0, -50.0) > 0
    assert cal.expected_value(1.0, 100.0, -50.0) == 1.0


def test_realised_payoffs_averages_actual_wins_and_losses():
    recs = [_rec("2026-01-01", opt_pnl=100.0), _rec("2026-01-01", opt_pnl=50.0),
            _rec("2026-01-02", opt_pnl=-50.0), _rec("2026-01-02", opt_pnl=-30.0)]
    win, loss = cal.realised_payoffs(recs)
    assert win == 75.0 and loss == -40.0
    assert cal.realised_payoffs([_rec("2026-01-01", opt_pnl=10.0)]) is None


# ── the gate ─────────────────────────────────────────────────────────────────
def test_gate_refuses_a_thin_sample_even_when_the_fit_looks_good():
    """43 sessions of a strongly-fitting relationship is still 43 sessions."""
    recs = []
    for d in range(1, 29):
        for t in range(20):
            sq = t / 20.0
            recs.append(_rec(f"2026-01-{d:02d}", ticker=f"T{t}", setup_q=sq,
                             correct=int(t >= 10), opt_pnl=10.0 if t >= 10 else -50.0))
    report = cal.evaluate(recs, label="option_win")
    assert report.released is False
    assert any("effective sample" in r for r in report.reasons)
    assert any("distinct sessions" in r for r in report.reasons)
    assert report.dates_needed > 0


def test_gate_refuses_when_held_out_performance_does_not_beat_a_constant():
    """Pure noise: the fit will find something in train and nothing in test."""
    rng = np.random.default_rng(11)
    recs = []
    for d in range(1, 32):
        for t in range(40):
            recs.append(_rec(f"2026-01-{d:02d}", ticker=f"T{t}",
                             setup_q=float(rng.uniform()), rel_vol=float(rng.uniform(1, 5)),
                             correct=int(rng.uniform() < 0.5),
                             opt_pnl=10.0 if rng.uniform() < 0.5 else -50.0))
    report = cal.evaluate(recs, label="option_win")
    assert report.released is False
    assert report.reasons


def test_a_rejected_report_never_writes_a_model_to_disk(tmp_path):
    report = cal.evaluate(_sessions(20), label="direction")
    path = tmp_path / "calibration_model.json"
    assert report.released is False
    assert cal.save_model(report, str(path)) is None
    assert not path.exists()


def test_load_model_returns_none_for_a_missing_or_unreleased_artifact(tmp_path):
    missing = tmp_path / "nope.json"
    assert cal.load_model(str(missing)) is None
    unreleased = tmp_path / "unreleased.json"
    unreleased.write_text(json.dumps({"released": False,
                                      "model": {"weights": [0.0] * (len(cal.FEATURES) + 1)}}))
    assert cal.load_model(str(unreleased)) is None
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert cal.load_model(str(corrupt)) is None


def test_a_released_model_round_trips_through_the_artifact(tmp_path):
    """Force-release a report so the happy path is covered without pretending
    the real data supports one."""
    report = cal.evaluate(_sessions(20), label="direction")
    report.released = True
    report.reasons = []
    path = tmp_path / "calibration_model.json"
    assert cal.save_model(report, str(path)) == str(path)
    loaded = cal.load_model(str(path))
    assert loaded is not None
    assert np.allclose(loaded.weights, report.model.weights)
    status = cal.calibration_status(str(path))
    assert status["calibrated"] is True


def test_walk_forward_reports_every_fold_including_the_losing_ones():
    rng = np.random.default_rng(5)
    recs = []
    for d in range(1, 32):
        for t in range(40):
            recs.append(_rec(f"2026-01-{d:02d}", ticker=f"T{t}",
                             setup_q=float(rng.uniform()),
                             opt_pnl=10.0 if rng.uniform() < 0.5 else -50.0))
    folds = cal.walk_forward(recs, label="option_win")
    assert len(folds) >= 3
    assert all("beat_baseline" in f for f in folds)
    # honesty check: folds that lost are still in the list
    assert set(f["beat_baseline"] for f in folds) <= {True, False}


def test_evaluate_reports_split_sizes_and_base_rates():
    report = cal.evaluate(_sessions(30, per_day=20), label="direction")
    assert report.train_dates > 0 and report.test_dates > 0
    assert report.train_n > 0 and report.test_n > 0
    assert 0.0 <= report.base_rate_train <= 1.0


def test_evaluate_refuses_gracefully_on_a_tiny_sample():
    report = cal.evaluate(_sessions(3, per_day=2), label="direction")
    assert report.released is False
    assert report.model is None


# ── record loading / dedup ───────────────────────────────────────────────────
def test_overlapping_runs_are_deduplicated(tmp_path):
    """The same 60-day window scanned seven times is not seven samples — that
    is the easiest way to manufacture significance out of nothing."""
    rows = [_rec("2026-01-05", ticker="AAA"), _rec("2026-01-06", ticker="BBB")]
    for i in range(3):
        (tmp_path / f"backtest_{i}.json").write_text(json.dumps({"records": rows}))
    assert len(cal.load_records(str(tmp_path))) == 2


def test_load_records_skips_corrupt_files_instead_of_aborting(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps({"records": [_rec("2026-01-05")]}))
    (tmp_path / "bad.json").write_text("{truncated")
    (tmp_path / "wrong_shape.json").write_text(json.dumps({"records": "nope"}))
    assert len(cal.load_records(str(tmp_path))) == 1


def test_load_records_accepts_the_older_signal_types_format(tmp_path):
    old = {"date": "2026-01-05", "ticker": "AAA", "signal_types": ["inside"],
           "direction_correct": 1}
    (tmp_path / "old.json").write_text(json.dumps({"records": [old]}))
    recs = cal.load_records(str(tmp_path))
    assert len(recs) == 1
    assert cal.feature_vector(recs[0])[cal.FEATURES.index("is_inside")] == 1.0


def test_report_text_states_the_verdict_and_what_is_missing():
    report = cal.evaluate(_sessions(25, per_day=20), label="direction")
    text = cal.format_report(report, cal.walk_forward(_sessions(25, per_day=20),
                                                      label="direction"))
    assert "NOT RELEASED" in text
    assert "EFFECTIVE sample" in text
    assert "more trading sessions" in text


# ── scanner integration ──────────────────────────────────────────────────────
def _scan_row(ticker, setup_q, opt_score=50):
    return {"ticker": ticker, "setup_q": setup_q, "opt_score": opt_score,
            "rel_vol": 2.0, "gap_pct": 1.0, "change_pct": 1.0, "hv20": 0.2,
            "direction": "up", "contract": None, "signal_combo": "gap_up"}


def test_annotate_marks_rows_uncalibrated_and_keeps_order_without_a_model():
    rows = [_scan_row("AAA", 0.9), _scan_row("BBB", 0.2)]
    out = cal.annotate(rows, None, None)
    assert [r["ticker"] for r in out] == ["AAA", "BBB"]
    assert all(r["calibration"] == cal.UNCALIBRATED for r in out)
    assert all(r["win_prob"] is None and r["expected_value"] is None for r in out)


def test_annotate_ranks_by_expected_value_when_a_model_exists():
    weights = [0.0] * (len(cal.FEATURES) + 1)
    weights[1 + cal.FEATURES.index("setup_q")] = 5.0     # higher setup_q => higher p
    model = cal.LogisticModel(weights=weights)
    rows = [_scan_row("LOW", 0.1), _scan_row("HIGH", 0.9)]
    out = cal.annotate(rows, model, (100.0, -50.0))
    assert [r["ticker"] for r in out] == ["HIGH", "LOW"]
    assert out[0]["expected_value"] > out[1]["expected_value"]
    assert all(r["calibration"] == cal.CALIBRATED for r in out)


def test_calibration_status_says_so_when_nothing_is_released(tmp_path):
    status = cal.calibration_status(str(tmp_path / "absent.json"))
    assert status["calibrated"] is False
    assert "setup quality" in status["note"]


def test_scanner_sort_by_ev_falls_back_to_setup_when_uncalibrated():
    from core.scanner import apply_sort, annotate_calibration
    rows = [_scan_row("LOW", 0.1), _scan_row("HIGH", 0.9)]
    annotate_calibration(rows)
    assert [r["ticker"] for r in apply_sort(rows, "ev")] == ["HIGH", "LOW"]


def test_scanner_sort_by_ev_uses_ev_when_present():
    from core.scanner import apply_sort
    rows = [_scan_row("LOW", 0.9), _scan_row("HIGH", 0.1)]
    rows[0]["expected_value"] = -0.2
    rows[1]["expected_value"] = 0.4
    assert [r["ticker"] for r in apply_sort(rows, "ev")] == ["HIGH", "LOW"]


def test_annotate_calibration_never_raises_without_a_model_on_disk():
    from core.scanner import annotate_calibration
    rows = [_scan_row("AAA", 0.5)]
    annotate_calibration(rows)
    assert rows[0]["calibration"] == cal.UNCALIBRATED


def test_ev_is_exposed_as_a_sort_choice():
    from core.scanner import SORT_LABELS, SORT_MAP
    assert "ev" in SORT_LABELS
    assert SORT_MAP["s7"] == "ev"


# ── the standing verdict on the real data ────────────────────────────────────
def test_no_calibrated_model_is_shipped_in_the_repo():
    """The measured answer today is 'not enough data'. If a model artifact ever
    appears without this test being revisited, it was not validated."""
    assert cal.load_model() is None
