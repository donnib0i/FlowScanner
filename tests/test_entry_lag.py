"""
Tests for core/entry_lag.py — the entry-lag decay analysis.

No network. Intraday frames are constructed in-memory with known geometry so the
assertions are about arithmetic we can verify by hand, not about market data.

The load-bearing tests are the look-ahead ones. Everything else in this module is
descriptive statistics; if `entry_index_for_lag` is wrong, every number the study
produces is wrong in a way that flatters the result.
"""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from core import entry_lag as el
from core.entry_lag import (
    LagConfig, entry_index_for_lag, session_frames, session_minute, tod_bucket,
    detect_intraday_signals, evaluate_entry_lags, cell_stats, aggregate,
    decay_verdict, strip_ansi, MIN_CELL_N, THIN_CELL_N,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────
def make_session(day="2026-08-24", bars=78, interval_min=5, start_price=100.0,
                 drift=0.0, vol=1_000_000.0, seed=None):
    """
    One session of intraday OHLCV, stamped by bar START time in UTC — the exact
    shape yfinance returns, so the code under test sees production geometry.
    """
    idx = pd.date_range(f"{day} 13:30:00", periods=bars, freq=f"{interval_min}min", tz="UTC")
    rng = np.random.default_rng(seed) if seed is not None else None
    closes, opens, highs, lows = [], [], [], []
    p = start_price
    for i in range(bars):
        o = p
        step = drift + (float(rng.normal(0, 0.05)) if rng is not None else 0.0)
        c = o + step
        opens.append(o)
        closes.append(c)
        highs.append(max(o, c) + 0.02)
        lows.append(min(o, c) - 0.02)
        p = c
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes,
         "Volume": [vol] * bars},
        index=idx,
    )


def make_multi_session(n_sessions=8, **kw):
    frames = []
    for k in range(n_sessions):
        d = (pd.Timestamp("2026-08-03") + pd.Timedelta(days=k)).strftime("%Y-%m-%d")
        frames.append(make_session(day=d, **kw))
    return pd.concat(frames)


# ── Config validation: the refusal to interpolate ──────────────────────────────
def test_lag_finer_than_bar_is_rejected_not_interpolated():
    # A +1min lag on 5min bars has no measurable entry price. Inventing one would
    # fabricate the quantity under test, so this must raise, not warn.
    with pytest.raises(ValueError, match="not measurable"):
        LagConfig(interval="5m", lags=[0, 1, 2, 5]).validate()


def test_lag_ladder_valid_on_matching_interval():
    LagConfig(interval="5m", lags=[0, 5, 10, 30]).validate()
    LagConfig(interval="1m", lags=[0, 1, 2, 5]).validate()
    LagConfig(interval="15m", lags=[0, 15, 30]).validate()


def test_default_lookback_tracks_the_interval_cap():
    # A fixed default would make --interval 1m fail on arrival, or throw away
    # eight weeks of 5m history. It must follow the interval.
    assert LagConfig(interval="5m").lookback_days == 59
    assert LagConfig(interval="1m").lookback_days == 7
    LagConfig(interval="1m").validate()
    assert LagConfig(interval="1m", lookback_days=3).lookback_days == 3


def test_lookback_beyond_provider_cap_is_rejected():
    # Yahoo returns an EMPTY frame past the cap, which would read as "no signals"
    # rather than "no data". Fail loudly at config time instead.
    with pytest.raises(ValueError, match="capped at"):
        LagConfig(interval="1m", lookback_days=60).validate()
    with pytest.raises(ValueError, match="capped at"):
        LagConfig(interval="5m", lookback_days=120).validate()


def test_hold_must_be_whole_bars_and_ladder_nonempty():
    with pytest.raises(ValueError, match="whole number"):
        LagConfig(interval="5m", hold_minutes=7).validate()
    with pytest.raises(ValueError, match="empty"):
        LagConfig(interval="5m", lags=[]).validate()


def test_unknown_interval_rejected():
    with pytest.raises(ValueError, match="not supported"):
        LagConfig(interval="3m").validate()


# ── Look-ahead guarantee ───────────────────────────────────────────────────────
def test_entry_at_lag_zero_is_the_bar_after_the_signal():
    # Bar i is only KNOWN once it closes, at ts[i] + interval. The soonest
    # transactable price is therefore the open of bar i+1 — never bar i's close.
    ts = make_session(bars=20).index
    assert entry_index_for_lag(ts, 5, 0, 5) == 6


def test_entry_index_advances_one_bar_per_bar_of_lag():
    ts = make_session(bars=40).index
    for lag, expected in [(0, 11), (5, 12), (10, 13), (15, 14), (30, 17), (60, 23)]:
        assert entry_index_for_lag(ts, 10, lag, 5) == expected


def test_entry_timestamp_never_precedes_decision_plus_lag():
    # The invariant the whole study rests on, checked exhaustively over a session.
    ts = make_session(bars=78).index
    bar_min = 5
    for i in range(0, 78):
        for lag in (0, 5, 10, 15, 30, 60):
            ei = entry_index_for_lag(ts, i, lag, bar_min)
            if ei is None:
                continue
            decision = ts[i] + pd.Timedelta(minutes=bar_min)
            assert ts[ei] >= decision + pd.Timedelta(minutes=lag)
            assert ei > i


def test_entry_returns_none_when_lag_runs_past_the_session():
    # No such trade exists. Returning a nearby bar instead would silently smuggle
    # a different trade into the sample.
    ts = make_session(bars=78).index
    assert entry_index_for_lag(ts, 77, 0, 5) is None
    assert entry_index_for_lag(ts, 75, 60, 5) is None
    assert entry_index_for_lag(ts, -1, 0, 5) is None


def test_entry_price_uses_the_open_not_the_close():
    # An open is transactable at the instant the bar begins; a close embeds the
    # whole bar's information. Using the close would leak the future.
    bars = make_session(bars=40)
    bars.loc[bars.index[12], "Open"] = 999.0
    bars.loc[bars.index[12], "Close"] = 111.0
    sessions = session_frames(bars)
    cfg = LagConfig(interval="5m", lags=[0], hold_minutes=10, balanced=False)
    sig = [{
        "session_date": sessions[0][0].isoformat(), "bar_index": 11,
        "ts": sessions[0][1].index[11].isoformat(), "session_minute": 55,
        "tod": "open", "signal": "test", "signal_types": ["test"],
        "direction": "up", "signal_price": 100.0, "rel_vol": 2.0,
        "gap_pct": 0.0, "setup_q": 0.9, "aplus_score": 4, "ema_bull": True,
    }]
    recs = evaluate_entry_lags("T", sig, sessions, cfg, {})
    assert len(recs) == 1
    assert recs[0]["entry_price"] == pytest.approx(999.0)


# ── Session handling ───────────────────────────────────────────────────────────
def test_sessions_split_by_day_and_converted_to_market_time():
    bars = make_multi_session(n_sessions=3, bars=10)
    sessions = session_frames(bars)
    assert len(sessions) == 3
    assert [d for d, _ in sessions] == sorted(d for d, _ in sessions)
    # 13:30 UTC is the 09:30 ET open.
    assert sessions[0][1].index[0].hour == 9
    assert sessions[0][1].index[0].minute == 30


def test_session_frames_handles_empty_and_naive_index():
    assert session_frames(pd.DataFrame()) == []
    assert session_frames(None) == []
    naive = make_session(bars=5)
    naive.index = naive.index.tz_localize(None)
    assert len(session_frames(naive)) == 1


def test_session_minute_and_tod_buckets():
    assert session_minute(pd.Timestamp("2026-08-24 09:30", tz=el.MARKET_TZ)) == 0
    assert session_minute(pd.Timestamp("2026-08-24 14:00", tz=el.MARKET_TZ)) == 270
    assert tod_bucket(0) == "open"
    assert tod_bucket(59) == "open"
    assert tod_bucket(60) == "midday"
    assert tod_bucket(269) == "midday"
    assert tod_bucket(270) == "close"
    assert tod_bucket(389) == "close"


# ── Trade evaluation arithmetic ────────────────────────────────────────────────
def test_signed_return_flips_with_direction():
    # A rising tape is a win for "up" and a loss for "down" of equal magnitude.
    bars = make_session(bars=40, drift=0.10)
    sessions = session_frames(bars)
    cfg = LagConfig(interval="5m", lags=[0], hold_minutes=25, balanced=False,
                    stop_pct=99.0, target_pct=99.0)

    def sig(direction):
        return [{
            "session_date": sessions[0][0].isoformat(), "bar_index": 10,
            "ts": sessions[0][1].index[10].isoformat(), "session_minute": 50,
            "tod": "open", "signal": "t", "signal_types": ["t"],
            "direction": direction, "signal_price": 100.0, "rel_vol": 2.0,
            "gap_pct": 0.0, "setup_q": 0.9, "aplus_score": 4, "ema_bull": True,
        }]

    up = evaluate_entry_lags("T", sig("up"), sessions, cfg, {})[0]
    dn = evaluate_entry_lags("T", sig("down"), sessions, cfg, {})[0]
    assert up["signed_return_pct"] > 0
    assert dn["signed_return_pct"] == pytest.approx(-up["signed_return_pct"])


def test_bracket_records_stop_when_a_bar_touches_both_levels():
    # Within one bar we cannot know which printed first. Recording the stop is the
    # conservative resolution; the opposite convention would flatter every number.
    h = np.array([110.0, 110.0]); l = np.array([90.0, 90.0]); c = np.array([100.0, 100.0])
    ret, reason = el._bracketed_return(h, l, c, 0, 1, 100.0, "up",
                                       stop_pct=1.0, target_pct=1.0)
    assert reason == "stop"
    assert ret == pytest.approx(-1.0)


def test_bracket_time_exit_when_neither_level_is_touched():
    h = np.array([100.1, 100.2]); l = np.array([99.9, 99.9]); c = np.array([100.0, 100.15])
    ret, reason = el._bracketed_return(h, l, c, 0, 1, 100.0, "up",
                                       stop_pct=5.0, target_pct=5.0)
    assert reason == "time"
    assert ret == pytest.approx(0.15, abs=1e-6)


def test_balanced_panel_keeps_population_identical_across_the_ladder():
    # If late-day signals dropped out rung by rung, a composition shift would read
    # as decay. Balanced mode must yield exactly one row per signal per lag.
    bars = make_session(bars=78, seed=1)
    sessions = session_frames(bars)
    cfg = LagConfig(interval="5m", lags=[0, 5, 30, 60], hold_minutes=60, balanced=True)
    sigs = [{
        "session_date": sessions[0][0].isoformat(), "bar_index": i,
        "ts": sessions[0][1].index[i].isoformat(), "session_minute": i * 5,
        "tod": tod_bucket(i * 5), "signal": "t", "signal_types": ["t"],
        "direction": "up", "signal_price": 100.0, "rel_vol": 2.0,
        "gap_pct": 0.0, "setup_q": 0.9, "aplus_score": 4, "ema_bull": True,
    } for i in range(2, 77)]
    recs = evaluate_entry_lags("T", sigs, sessions, cfg, {})
    counts = {lag: sum(1 for r in recs if r["lag_min"] == lag) for lag in cfg.lags}
    assert len(set(counts.values())) == 1, f"unbalanced ladder: {counts}"
    assert counts[0] > 0


def test_unbalanced_mode_lets_late_signals_drop_out_down_the_ladder():
    bars = make_session(bars=78, seed=2)
    sessions = session_frames(bars)
    cfg = LagConfig(interval="5m", lags=[0, 60], hold_minutes=60, balanced=False)
    sigs = [{
        "session_date": sessions[0][0].isoformat(), "bar_index": i,
        "ts": sessions[0][1].index[i].isoformat(), "session_minute": i * 5,
        "tod": tod_bucket(i * 5), "signal": "t", "signal_types": ["t"],
        "direction": "up", "signal_price": 100.0, "rel_vol": 2.0,
        "gap_pct": 0.0, "setup_q": 0.9, "aplus_score": 4, "ema_bull": True,
    } for i in range(2, 77)]
    recs = evaluate_entry_lags("T", sigs, sessions, cfg, {})
    n0 = sum(1 for r in recs if r["lag_min"] == 0)
    n60 = sum(1 for r in recs if r["lag_min"] == 60)
    assert n0 > n60 > 0


def test_strike_is_fixed_at_signal_time_across_all_lags():
    # The scanner picks the contract when it fires; a tailer buys THAT contract
    # later at a worse price. Re-picking per lag would repair the entry and erase
    # the effect under test.
    bars = make_session(bars=78, drift=0.05)
    sessions = session_frames(bars)
    cfg = LagConfig(interval="5m", lags=[0, 30], hold_minutes=30, balanced=True)
    sigs = [{
        "session_date": sessions[0][0].isoformat(), "bar_index": 5,
        "ts": sessions[0][1].index[5].isoformat(), "session_minute": 25,
        "tod": "open", "signal": "t", "signal_types": ["t"],
        "direction": "up", "signal_price": float(sessions[0][1]["Close"].iloc[5]),
        "rel_vol": 2.0, "gap_pct": 0.0, "setup_q": 0.9,
        "aplus_score": 4, "ema_bull": True,
    }]
    recs = evaluate_entry_lags("T", sigs, sessions, cfg, {"hv20": 0.35})
    strikes = {r["sim_strike"] for r in recs}
    assert len(strikes) == 1 and strikes != {None}


# ── Signal detection ───────────────────────────────────────────────────────────
def test_detector_emits_intraday_signals_with_required_fields():
    bars = make_multi_session(n_sessions=10, bars=78, seed=7)
    sessions = session_frames(bars)
    sigs = detect_intraday_signals(sessions, LagConfig(interval="5m"))
    assert sigs, "detector produced nothing on a full synthetic tape"
    for s in sigs[:20]:
        assert s["direction"] in ("up", "down")
        assert 0.0 <= s["setup_q"] <= 1.0
        assert s["tod"] in ("open", "midday", "close")
        assert s["signal_types"]


def test_relative_volume_baseline_uses_only_prior_sessions():
    # The first session has no prior same-slot history, so no rel-vol-dependent
    # signal may fire in it. If one does, the baseline is leaking the present.
    bars = make_multi_session(n_sessions=8, bars=40, seed=3)
    sessions = session_frames(bars)
    sigs = detect_intraday_signals(sessions, LagConfig(interval="5m"))
    first_day = sessions[0][0].isoformat()
    vol_dependent = {"highvol", "breakout", "orb_break"}
    for s in sigs:
        if s["session_date"] == first_day:
            assert not (vol_dependent & set(s["signal_types"])), \
                f"volume signal fired with no prior-session baseline: {s}"
            assert s["rel_vol"] is None


def test_volume_spike_registers_only_after_baseline_exists():
    # Same slot, same volume for 6 sessions, then a 5x spike on session 7.
    frames = []
    for k in range(7):
        d = (pd.Timestamp("2026-08-03") + pd.Timedelta(days=k)).strftime("%Y-%m-%d")
        f = make_session(day=d, bars=40, drift=0.03)
        if k == 6:
            f.iloc[20, f.columns.get_loc("Volume")] = 5_000_000.0
        frames.append(f)
    sessions = session_frames(pd.concat(frames))
    sigs = detect_intraday_signals(sessions, LagConfig(interval="5m"))
    spike = [s for s in sigs
             if s["session_date"] == sessions[6][0].isoformat() and s["bar_index"] == 20]
    assert spike and spike[0]["rel_vol"] == pytest.approx(5.0, rel=0.01)
    assert "highvol" in spike[0]["signal_types"]


def test_detector_survives_a_short_session():
    # Half days are real; they must not raise.
    sessions = session_frames(make_session(bars=6))
    assert detect_intraday_signals(sessions, LagConfig(interval="5m")) is not None


# ── Sample-size discipline ─────────────────────────────────────────────────────
def test_cell_below_minimum_reports_n_and_refuses_a_statistic():
    rows = [{"signed_return_pct": 1.0, "bracketed_pct": 1.0, "adverse_slip_pct": 0.0,
             "clock_return_pct": 1.0} for _ in range(3)]
    c = cell_stats(rows, min_n=MIN_CELL_N)
    assert c["n"] == 3
    assert c["sufficient"] is False
    # The point of the exercise: a win rate off 3 trades must not exist at all.
    assert "win_rate" not in c
    assert "avg_return_pct" not in c
    assert "expectancy_pct" not in c


def test_cell_at_the_minimum_reports_and_is_flagged_thin():
    rows = [{"signed_return_pct": 0.5, "bracketed_pct": 0.5, "adverse_slip_pct": 0.1,
             "clock_return_pct": 0.4} for _ in range(MIN_CELL_N)]
    c = cell_stats(rows, min_n=MIN_CELL_N)
    assert c["sufficient"] is True
    assert c["thin"] is True
    assert c["win_rate"] == 1.0


def test_large_cell_is_not_flagged_thin():
    rng = np.random.default_rng(11)
    rows = [{"signed_return_pct": float(rng.normal(0.2, 1.0)), "bracketed_pct": 0.1,
             "adverse_slip_pct": 0.0, "clock_return_pct": 0.1}
            for _ in range(THIN_CELL_N + 50)]
    c = cell_stats(rows, min_n=MIN_CELL_N)
    assert c["thin"] is False
    assert c["stderr_pct"] > 0 and c["t_stat"] is not None


def test_cell_stats_standard_error_shrinks_with_sample_size():
    rng = np.random.default_rng(5)

    def cell(n):
        return cell_stats([{"signed_return_pct": float(rng.normal(0, 1)),
                            "bracketed_pct": 0.0, "adverse_slip_pct": 0.0,
                            "clock_return_pct": 0.0} for _ in range(n)])

    assert cell(1000)["stderr_pct"] < cell(50)["stderr_pct"]


def test_aggregate_keys_end_in_the_lag_and_split_by_dimension():
    recs = [{"lag_min": lag, "grade": g, "tod": "open", "signed_return_pct": 0.1,
             "bracketed_pct": 0.1, "adverse_slip_pct": 0.0, "clock_return_pct": 0.1}
            for lag in (0, 5) for g in ("A", "B") for _ in range(MIN_CELL_N)]
    flat = aggregate(recs, LagConfig(interval="5m", lags=[0, 5]))
    assert set(flat) == {"0", "5"}
    assert flat["0"]["n"] == 2 * MIN_CELL_N

    by_grade = aggregate(recs, LagConfig(interval="5m", lags=[0, 5]), dims=("grade",))
    assert set(by_grade) == {"A|0", "A|5", "B|0", "B|5"}
    assert by_grade["A|0"]["n"] == MIN_CELL_N


# ── Verdict discipline ─────────────────────────────────────────────────────────
def _ladder_from(returns_by_lag, n=400, seed=0):
    rng = np.random.default_rng(seed)
    recs = []
    for lag, mu in returns_by_lag.items():
        for _ in range(n):
            r = float(rng.normal(mu, 0.5))
            recs.append({"lag_min": lag, "signed_return_pct": r, "bracketed_pct": r,
                         "adverse_slip_pct": 0.0, "clock_return_pct": r})
    cfg = LagConfig(interval="5m", lags=sorted(returns_by_lag))
    return aggregate(recs, cfg), cfg


def test_verdict_refuses_when_endpoint_cells_are_too_small():
    ladder, cfg = _ladder_from({0: 0.3, 30: 0.0}, n=5)
    assert decay_verdict(ladder, cfg)["verdict"] == "INSUFFICIENT_DATA"


def test_verdict_says_no_edge_when_lag_zero_is_indistinguishable_from_zero():
    # Nothing decays because nothing was there — that is explanation (1), and the
    # module must not dress it up as decay.
    ladder, cfg = _ladder_from({0: 0.0, 30: 0.0}, n=400, seed=2)
    v = decay_verdict(ladder, cfg)
    assert v["verdict"] == "NO_EDGE_AT_EMIT"
    assert "explanation (1), not (2)" in v["detail"]


def test_verdict_detects_a_genuine_decay():
    ladder, cfg = _ladder_from({0: 0.40, 30: 0.02}, n=600, seed=3)
    v = decay_verdict(ladder, cfg)
    assert v["verdict"] == "EDGE_DECAYS"
    assert v["retained_frac"] < 0.3


def test_verdict_calls_a_flat_curve_durable():
    ladder, cfg = _ladder_from({0: 0.35, 30: 0.35}, n=600, seed=4)
    assert decay_verdict(ladder, cfg)["verdict"] == "EDGE_DURABLE"


def test_verdict_detects_delay_being_better():
    ladder, cfg = _ladder_from({0: 0.10, 30: 0.50}, n=600, seed=6)
    assert decay_verdict(ladder, cfg)["verdict"] == "EDGE_IMPROVES_WITH_DELAY"


# ── Grading + plumbing ─────────────────────────────────────────────────────────
def test_grade_is_the_scanners_own_letter_stripped_of_colour():
    from core.scanner import trade_grade
    for q, s, hc in [(0.95, 90, True), (0.60, 40, True), (0.30, 10, False), (0.05, 0, False)]:
        assert strip_ansi(trade_grade(q, s, hc)) in {"A", "B", "C", "D"}


def test_strip_ansi_removes_colour_codes():
    assert strip_ansi("\x1b[32m\x1b[1mA\x1b[0m") == "A"


def test_save_writes_csv_and_json_with_suppressed_cells_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(el, "RESULTS_DIR", tmp_path)
    cfg = LagConfig(interval="5m", lags=[0, 5])
    recs = [{"ticker": "T", "session_date": "2026-08-24", "ts": "x", "session_minute": 10,
             "tod": "open", "signal": "t", "signal_types": ["t"], "direction": "up",
             "setup_q": 0.9, "rel_vol": 2.0, "grade": "A", "lag_min": lag,
             "signal_price": 100.0, "entry_price": 100.0, "exit_price": 100.1,
             "adverse_slip_pct": 0.0, "signed_return_pct": 0.1, "bracketed_pct": 0.1,
             "clock_return_pct": 0.1, "exit_reason": "time", "opt_pnl_pct": None,
             "sim_strike": None}
            for lag in (0, 5) for _ in range(4)]
    path = el.save_lag_results(recs, cfg, {"sessions": 1})
    import json
    payload = json.loads(open(path).read())
    # n=4 is under the floor, so the persisted cell must carry no statistic —
    # a JSON consumer must not be able to chart a win rate off four trades.
    assert payload["ladder"]["0"]["sufficient"] is False
    assert "win_rate" not in payload["ladder"]["0"]
    assert payload["verdict"]["verdict"] == "INSUFFICIENT_DATA"
    assert list(tmp_path.glob("entrylag_*.csv"))
    # The JSON must reference the CSV, not re-embed 200k rows: results/ is a
    # committed directory and the duplicate would be a ~136MB file.
    assert "records" not in payload
    assert payload["n_records"] == 8
    assert payload["records_csv"].endswith(".csv")
    assert (tmp_path / payload["records_csv"]).exists()
    assert (tmp_path / f"{payload['records_csv'][:-4]}.json").stat().st_size < 100_000


def test_run_lag_analysis_reports_empty_data_as_a_finding_not_a_crash(monkeypatch):
    # "The data cannot support this" is a valid, valuable outcome. It must come
    # back as a stated finding, never as a fabricated ladder.
    monkeypatch.setattr(el.yf, "download", lambda *a, **k: pd.DataFrame())
    records, meta = run_empty = el.run_lag_analysis(LagConfig(interval="5m", tickers=["NVDA"]))
    assert records == []
    assert "fatal" in meta
    assert "daily bars cannot express" in meta["fatal"]


def test_run_lag_analysis_reports_download_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(el.yf, "download", boom)
    records, meta = el.run_lag_analysis(LagConfig(interval="5m", tickers=["NVDA"]))
    assert records == []
    assert meta["fatal"] == "intraday download failed"


def test_end_to_end_on_stubbed_intraday_data(monkeypatch, tmp_path):
    """Full pipeline with no network: download -> detect -> ladder -> report -> save."""
    bars = make_multi_session(n_sessions=12, bars=78, seed=21)
    monkeypatch.setattr(el.yf, "download", lambda *a, **k: bars)
    monkeypatch.setattr(el, "fetch_ticker_meta",
                        lambda t, d: {x: {"hv20": 0.3, "avg_vol": 5e7,
                                          "opt_score": 70, "has_contract": True} for x in t})
    monkeypatch.setattr(el, "RESULTS_DIR", tmp_path)

    cfg = LagConfig(tickers=["NVDA"], interval="5m", lookback_days=20,
                    lags=[0, 5, 15, 30], hold_minutes=30)
    records, meta = el.run_lag_analysis(cfg)
    assert records, "pipeline produced no records on a full synthetic tape"
    assert meta["sessions"] == 12

    counts = {lag: sum(1 for r in records if r["lag_min"] == lag) for lag in cfg.lags}
    assert len(set(counts.values())) == 1, f"balanced panel violated: {counts}"

    for r in records:
        assert r["grade"] in {"A", "B", "C", "D"}
        assert r["exit_reason"] in {"stop", "target", "time"}

    el.print_lag_report(records, cfg, meta)   # must not raise
    path = el.save_lag_results(records, cfg, meta)
    assert path.endswith(".json")


def test_report_names_time_of_day_buckets_the_panel_cannot_reach(capsys):
    # A balanced panel amputates the end of the session. If that deletes a whole
    # bucket, the report must say "not measured" rather than leave a silent gap a
    # reader would take for "measured and flat".
    cfg = LagConfig(interval="5m", lags=[0, 60], hold_minutes=60)
    recs = [{"ticker": "T", "session_date": "2026-08-24", "ts": "x",
             "session_minute": 60, "tod": "midday", "signal": "t",
             "signal_types": ["t"], "direction": "up", "setup_q": 0.9,
             "rel_vol": 2.0, "grade": "A", "lag_min": lag, "signal_price": 100.0,
             "entry_price": 100.0, "exit_price": 100.1, "adverse_slip_pct": 0.0,
             "signed_return_pct": 0.1, "bracketed_pct": 0.1, "clock_return_pct": 0.1,
             "exit_reason": "time", "opt_pnl_pct": None, "sim_strike": None}
            for lag in (0, 60) for _ in range(MIN_CELL_N)]
    el.print_lag_report(recs, cfg, {})
    out = strip_ansi(capsys.readouterr().out)
    assert "BLIND SPOT" in out
    assert "close" in out
    assert "NOT measured-and-flat" in out


def test_report_has_no_blind_spot_warning_when_the_panel_reaches_the_close(capsys):
    cfg = LagConfig(interval="5m", lags=[0, 5], hold_minutes=15)
    el.print_lag_report([], cfg, {})
    assert "BLIND SPOT" not in strip_ansi(capsys.readouterr().out)


# ── Null control ───────────────────────────────────────────────────────────────
def test_random_control_signals_are_evenly_spread_and_directionless():
    sessions = session_frames(make_multi_session(n_sessions=6, bars=78))
    sigs = el.random_control_signals(sessions, per_session=20, seed=1)
    assert len(sigs) == 6 * 20
    assert {s["signal"] for s in sigs} == {"control"}
    # Coin-flip direction: neither side should dominate a 120-signal draw.
    ups = sum(1 for s in sigs if s["direction"] == "up")
    assert 0.3 < ups / len(sigs) < 0.7
    assert all(2 <= s["bar_index"] < 77 for s in sigs)


def test_control_signals_never_repeat_a_bar_within_a_session():
    sessions = session_frames(make_session(bars=78))
    sigs = el.random_control_signals(sessions, per_session=30, seed=2)
    idxs = [s["bar_index"] for s in sigs]
    assert len(idxs) == len(set(idxs))


def test_control_handles_a_session_shorter_than_the_request():
    sessions = session_frames(make_session(bars=12))
    sigs = el.random_control_signals(sessions, per_session=50, seed=3)
    assert 0 < len(sigs) <= 12


def test_control_mode_bypasses_the_detector(monkeypatch):
    bars = make_multi_session(n_sessions=8, bars=78, seed=31)
    monkeypatch.setattr(el.yf, "download", lambda *a, **k: bars)
    monkeypatch.setattr(el, "fetch_ticker_meta", lambda t, d: {x: {} for x in t})

    def explode(*a, **k):
        raise AssertionError("detector must not run in null-control mode")
    monkeypatch.setattr(el, "detect_intraday_signals", explode)

    cfg = LagConfig(tickers=["NVDA"], interval="5m", lookback_days=20,
                    lags=[0, 15], hold_minutes=30, null_control=True)
    records, meta = el.run_lag_analysis(cfg)
    assert records and all(r["signal"] == "control" for r in records)


def test_harness_recovers_an_edge_that_is_actually_there(monkeypatch):
    """
    The instrument's own calibration: on a tape with a KNOWN drift, and control
    signals all pointing the right way, the ladder must report a clearly positive
    lag-0 return. If this fails, a 'no edge' verdict from the real study would be
    worthless — it could just be the harness eating the signal.
    """
    bars = make_multi_session(n_sessions=10, bars=78, drift=0.02)  # steady uptrend
    monkeypatch.setattr(el.yf, "download", lambda *a, **k: bars)
    monkeypatch.setattr(el, "fetch_ticker_meta", lambda t, d: {x: {} for x in t})
    monkeypatch.setattr(
        el, "detect_intraday_signals",
        lambda sessions, cfg: [dict(s, direction="up")
                               for s in el.random_control_signals(sessions, 20, seed=9)],
    )
    cfg = LagConfig(tickers=["NVDA"], interval="5m", lookback_days=20,
                    lags=[0, 15], hold_minutes=30, stop_pct=99.0, target_pct=99.0)
    records, _ = el.run_lag_analysis(cfg)
    ladder = aggregate(records, cfg)
    assert ladder["0"]["sufficient"]
    assert ladder["0"]["avg_return_pct"] > 0.05
    assert ladder["0"]["t_stat"] > 5
    assert decay_verdict(ladder, cfg)["verdict"] != "NO_EDGE_AT_EMIT"


def test_no_edge_verdict_bounds_the_edge_rather_than_just_calling_it_insignificant():
    # "Not significant" is easy to misread as "we learned nothing". The CI upper
    # bound turns it into a usable statement: the largest edge still consistent
    # with the data.
    ladder, cfg = _ladder_from({0: 0.0, 30: 0.0}, n=800, seed=12)
    v = decay_verdict(ladder, cfg)
    assert v["verdict"] == "NO_EDGE_AT_EMIT"
    assert v["edge_ci95_low"] < v["edge_ci95_high"]
    assert v["edge_ci95_high"] < 0.1        # a tight bound at n=800
    assert "95% CI upper bound" in v["detail"]
