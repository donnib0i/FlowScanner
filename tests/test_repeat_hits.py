"""
"DAY N" — the same contract printing across more than one session.

A single day's unusual print is one decision. The same strike and expiry
showing up again tomorrow, and again the day after, is a position being built:
a far stronger read, and one the scanner could never make because it never
wrote a flow contract down. These tests pin the persistence half of it — the
recording hook, the session count, and the two failure modes that matter
(a corrupt store, and a store that grows forever).
"""
import datetime as dt

import pytest

import core.flow as scflow
import core.repeat_hits as rh
from data.baseline import BaselineStore


def _store(tmp_path):
    return BaselineStore(db_path=str(tmp_path / "baseline.db"))


def _c(strike=210.0, otype="call", exp="2026-09-18", **kw):
    c = {"ticker": "NVDA", "exp": exp, "dte": 10, "strike": strike, "type": otype,
         "vol": 5000, "oi": 2000, "flow": 1_000_000.0}
    c.update(kw)
    return c


def _sig(calls=None, puts=None):
    calls = [_c()] if calls is None else calls
    puts = [] if puts is None else puts
    sig = {"ticker": "NVDA", "flow_bias": "call", "total_flow": 1_000_000.0,
           "call_contracts": calls, "put_contracts": puts}
    sig["top_contract"] = calls[0] if calls else None
    return sig


# ── the session count ────────────────────────────────────────────────────────
def test_contract_sessions_is_zero_before_any_observation(tmp_path):
    s = _store(tmp_path)
    assert s.contract_sessions("NVDA", "call", 210.0, "2026-09-18") == 0


def test_contract_sessions_counts_distinct_days_not_writes(tmp_path):
    """Forty scans on Tuesday is still DAY 1 — the signal is accumulation."""
    s = _store(tmp_path)
    for _ in range(40):
        s.record_contract("2026-09-08", "NVDA", "call", 210.0, "2026-09-18", oi=2000, volume=5000)
    assert s.contract_sessions("NVDA", "call", 210.0, "2026-09-18") == 1

    s.record_contract("2026-09-09", "NVDA", "call", 210.0, "2026-09-18", oi=2600, volume=4100)
    assert s.contract_sessions("NVDA", "call", 210.0, "2026-09-18") == 2


def test_contract_sessions_does_not_bleed_across_contracts(tmp_path):
    s = _store(tmp_path)
    s.record_contract("2026-09-08", "NVDA", "call", 210.0, "2026-09-18", oi=1, volume=1)
    s.record_contract("2026-09-09", "NVDA", "call", 215.0, "2026-09-18", oi=1, volume=1)
    s.record_contract("2026-09-09", "NVDA", "put", 210.0, "2026-09-18", oi=1, volume=1)
    s.record_contract("2026-09-09", "NVDA", "call", 210.0, "2026-09-25", oi=1, volume=1)
    s.record_contract("2026-09-09", "AMD", "call", 210.0, "2026-09-18", oi=1, volume=1)
    assert s.contract_sessions("NVDA", "call", 210.0, "2026-09-18") == 1


# ── bounded storage ──────────────────────────────────────────────────────────
def test_prune_drops_observations_past_the_retention_window(tmp_path):
    s = _store(tmp_path)
    s.record_contract("2026-01-02", "NVDA", "call", 210.0, "2026-01-16", oi=1, volume=1)
    s.record_contract("2026-09-08", "NVDA", "call", 210.0, "2026-09-18", oi=1, volume=1)
    s.record_ticker("2026-01-02", "NVDA", 1, 1, 1)
    s.record_ticker("2026-09-08", "NVDA", 1, 1, 1)

    s.prune(today=dt.date(2026, 9, 8), max_age_days=30)

    assert s.contract_sessions("NVDA", "call", 210.0, "2026-01-16") == 0
    assert s.contract_sessions("NVDA", "call", 210.0, "2026-09-18") == 1
    assert s.count_contract_obs() == 1
    assert s.count_ticker_obs() == 1


def test_prune_enforces_a_hard_row_ceiling_oldest_first(tmp_path):
    s = _store(tmp_path)
    for day in range(1, 11):
        s.record_contract(f"2026-09-{day:02d}", "NVDA", "call", 210.0, "2026-12-18",
                          oi=1, volume=1)

    s.prune(today=dt.date(2026, 9, 10), max_age_days=3650, max_rows=4)

    assert s.count_contract_obs() == 4
    # The ceiling keeps the most recent sessions, which are the ones a DAY N
    # read is about.
    rows = s.conn.execute("SELECT MIN(obs_date) FROM contract_obs").fetchone()
    assert rows[0] == "2026-09-07"


# ── the tracker ──────────────────────────────────────────────────────────────
def test_tracker_stamps_day_one_on_a_first_sighting(tmp_path):
    t = rh.RepeatHitTracker(store=_store(tmp_path), session_date=dt.date(2026, 9, 8))
    sig = _sig()
    t.annotate(sig)
    assert sig["call_contracts"][0]["repeat_days"] == 1


def test_tracker_counts_up_across_sessions_not_scans(tmp_path):
    store = _store(tmp_path)
    for _ in range(5):
        rh.RepeatHitTracker(store=store, session_date=dt.date(2026, 9, 8)).annotate(_sig())
    sig = _sig()
    rh.RepeatHitTracker(store=store, session_date=dt.date(2026, 9, 9)).annotate(sig)
    assert sig["call_contracts"][0]["repeat_days"] == 2

    sig = _sig()
    rh.RepeatHitTracker(store=store, session_date=dt.date(2026, 9, 10)).annotate(sig)
    assert sig["call_contracts"][0]["repeat_days"] == 3


def test_tracker_annotates_both_sides_and_the_top_contract(tmp_path):
    calls, puts = [_c(210.0, "call")], [_c(200.0, "put")]
    sig = _sig(calls, puts)
    rh.RepeatHitTracker(store=_store(tmp_path), session_date=dt.date(2026, 9, 8)).annotate(sig)
    assert sig["put_contracts"][0]["repeat_days"] == 1
    # top_contract is the same dict object, so it carries the stamp for free.
    assert sig["top_contract"]["repeat_days"] == 1


def test_tracker_survives_a_broken_store_without_costing_the_flow(tmp_path):
    """History is a bonus. A scan that loses it must still return its prints."""
    class Corrupt:
        def prune(self, *a, **k):
            raise OSError("database disk image is malformed")

        def record_contract(self, *a, **k):
            raise OSError("database disk image is malformed")

        def contract_sessions(self, *a, **k):
            raise OSError("database disk image is malformed")

    sig = _sig()
    rh.RepeatHitTracker(store=Corrupt(), session_date=dt.date(2026, 9, 8)).annotate(sig)
    assert sig["call_contracts"][0]["repeat_days"] == 1
    assert sig["call_contracts"][0]["flow"] == 1_000_000.0


def test_tracker_skips_a_contract_it_cannot_key(tmp_path):
    sig = _sig(calls=[{"ticker": "NVDA", "type": "call"}])
    rh.RepeatHitTracker(store=_store(tmp_path), session_date=dt.date(2026, 9, 8)).annotate(sig)
    assert sig["call_contracts"][0]["repeat_days"] == 1


def test_tracker_never_raises_when_the_store_cannot_be_opened(monkeypatch):
    monkeypatch.setattr(rh, "BaselineStore",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))
    sig = _sig()
    rh.RepeatHitTracker(session_date=dt.date(2026, 9, 8)).annotate(sig)
    assert sig["call_contracts"][0]["repeat_days"] == 1


# ── wiring into the scan ─────────────────────────────────────────────────────
def test_scan_records_flow_contracts_once_per_session(monkeypatch, tmp_path):
    """The whole point: history has to accrue from an ordinary scan."""
    db = str(tmp_path / "baseline.db")
    monkeypatch.setattr(rh, "BaselineStore", lambda *a, **k: BaselineStore(db_path=db))
    monkeypatch.setattr(scflow, "_TT_AVAILABLE", True)

    def _scan(day):
        monkeypatch.setattr(rh, "exchange_today", lambda: day)
        out = _sig()
        monkeypatch.setattr(scflow, "scan_options_flow_tt", lambda *a, **k: [out])
        scflow.scan_options_flow(["NVDA"], show_progress=False)
        return out["call_contracts"][0]["repeat_days"]

    assert _scan(dt.date(2026, 9, 8)) == 1
    assert _scan(dt.date(2026, 9, 8)) == 1
    assert _scan(dt.date(2026, 9, 9)) == 2


def test_yf_path_annotates_before_it_streams_the_signal():
    """The web scan serializes each signal the moment on_signal fires.

    Annotating after the ticker loop would therefore ship DAY 1 to the browser
    forever, however much history the store had.
    """
    import inspect
    src = inspect.getsource(scflow._scan_options_flow_yf)
    assert src.index("tracker.annotate(signal)") < src.index("on_signal(signal)")


def test_serialized_contract_carries_the_day_count():
    from web.app import _serialize_flow
    sig = _sig(calls=[_c(repeat_days=3)])
    sig.update({"whale_score": 60, "premium_tier": "whale", "call_flow": 1.0,
                "put_flow": 0.0, "pc_ratio": 0.0, "iv_skew": 0.0})
    assert _serialize_flow(sig)["top_calls"][0]["repeat_days"] == 3


def test_serialized_contract_defaults_to_day_one_when_history_is_missing():
    from web.app import _serialize_flow
    sig = _sig()
    sig.update({"whale_score": 60, "premium_tier": "whale", "call_flow": 1.0,
                "put_flow": 0.0, "pc_ratio": 0.0, "iv_skew": 0.0})
    assert _serialize_flow(sig)["top_calls"][0]["repeat_days"] == 1
