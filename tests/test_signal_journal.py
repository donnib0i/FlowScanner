"""Signal-journal storage, idempotency, and read API.

Every test drives an on-disk temp DB and hand-built result dicts. Nothing here
touches yfinance, TastyTrade, or any other network source.
"""
import json


from data.signal_journal import (
    GRADES,
    SignalJournal,
    contract_key,
    new_run_id,
    utc_now_iso,
)


def _journal(tmp_path):
    return SignalJournal(db_path=str(tmp_path / "signals.db"))


def _result(ticker="NVDA", direction="up", contract=True, **over):
    r = {
        "ticker": ticker,
        "direction": direction,
        "price": 182.50,
        "setup_q": 0.72,
        "opt_score": 68,
        "whale_score": 55,
        "signal_combo": "GU+HV",
        "rel_vol": 1.84,
        "change_pct": 2.31,
        "gap_pct": 0.9,
        "gap_flag": True,
        "inside_day": False,
        "high_vol": True,
        "breakout": "up",
        "is_laggard": False,
        "rsi14": 61.2,
        "hv_regime": "NORMAL",
        "contract": {
            "exp": "2026-09-05", "dte": 8, "strike": 185.0, "type": "call",
            "delta": 0.41, "iv": 0.38, "oi": 12000, "vol": 4300,
            "bid": 3.10, "ask": 3.30, "mid": 3.20, "stale": False,
        } if contract else None,
    }
    r.update(over)
    return r


# ── schema / write ───────────────────────────────────────────────────────────
def test_records_run_and_signal_with_full_snapshot(tmp_path):
    j = _journal(tmp_path)
    rid = new_run_id()
    j.start_run(rid, "scan", {"enrich_top": 20, "vix": 15.2})
    j.record_signals(rid, [_result()], grade_fn=lambda r: "A")

    rows = j.query()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "NVDA"
    assert row["direction"] == "up"
    assert row["grade"] == "A"
    assert row["setup_q"] == 0.72
    assert row["opt_score"] == 68
    assert row["whale_score"] == 55
    assert row["underlying_px"] == 182.50
    assert row["contract_strike"] == 185.0
    assert row["contract_expiry"] == "2026-09-05"
    assert row["contract_type"] == "call"
    assert row["contract_bid"] == 3.10
    assert row["contract_ask"] == 3.30
    assert row["contract_mid"] == 3.20
    assert row["contract_dte"] == 8
    # The run's parameters ride along on the read so attribution can tell a
    # grade-A picked from 5 candidates from one picked from 50.
    assert json.loads(row["run_params"])["enrich_top"] == 20
    assert row["scan_kind"] == "scan"


def test_missing_scores_are_null_not_zero(tmp_path):
    """A field the scanner never computed must not read back as a real zero."""
    j = _journal(tmp_path)
    rid = j.start_run(new_run_id(), "scan", {})
    j.record_signal(rid, {"ticker": "SPY", "direction": "down"})

    row = j.query()[0]
    assert row["setup_q"] is None
    assert row["opt_score"] is None
    assert row["whale_score"] is None
    assert row["underlying_px"] is None
    assert row["contract_strike"] is None
    assert row["contract_key"] == ""


def test_emitted_at_is_utc_and_timezone_explicit(tmp_path):
    j = _journal(tmp_path)
    rid = j.start_run(new_run_id(), "scan", {})
    j.record_signals(rid, [_result()])
    stamp = j.query()[0]["emitted_at"]
    assert stamp.endswith("+00:00")
    assert "T" in stamp


# ── idempotency: natural key (run_id, symbol, direction, contract_key) ───────
def test_reemitting_same_signal_in_same_run_does_not_duplicate(tmp_path):
    j = _journal(tmp_path)
    rid = j.start_run(new_run_id(), "scan", {})
    for _ in range(3):
        j.record_signals(rid, [_result()], grade_fn=lambda r: "A")
    assert j.count() == 1


def test_reemit_updates_in_place_last_write_wins(tmp_path):
    j = _journal(tmp_path)
    rid = j.start_run(new_run_id(), "scan", {})
    j.record_signals(rid, [_result(setup_q=0.60)], grade_fn=lambda r: "B")
    j.record_signals(rid, [_result(setup_q=0.85)], grade_fn=lambda r: "A")
    rows = j.query()
    assert len(rows) == 1
    assert rows[0]["setup_q"] == 0.85
    assert rows[0]["grade"] == "A"


def test_new_run_of_same_ticker_is_a_new_row(tmp_path):
    """The scanner changing its mind during the day is history worth keeping."""
    j = _journal(tmp_path)
    for _ in range(2):
        rid = j.start_run(new_run_id(), "scan", {})
        j.record_signals(rid, [_result()])
    assert j.count() == 2


def test_different_contract_in_same_run_is_a_distinct_signal(tmp_path):
    j = _journal(tmp_path)
    rid = j.start_run(new_run_id(), "scan", {})
    a = _result()
    b = _result()
    b["contract"] = dict(b["contract"], strike=190.0)
    j.record_signals(rid, [a, b])
    assert j.count() == 2


def test_different_direction_in_same_run_is_a_distinct_signal(tmp_path):
    j = _journal(tmp_path)
    rid = j.start_run(new_run_id(), "scan", {})
    j.record_signals(rid, [_result(direction="up"), _result(direction="down")])
    assert j.count() == 2


def test_contract_key_shape(tmp_path):
    assert contract_key(None) == ""
    assert contract_key({"exp": "2026-09-05", "strike": 185.0, "type": "call"}) == \
        "2026-09-05|185.0|call"


def test_run_ids_are_unique_and_their_timestamp_prefix_is_monotonic():
    ids = [new_run_id() for _ in range(5)]
    assert len(set(ids)) == 5        # the random suffix breaks same-second ties
    stamps = [i.split("-")[0] for i in ids]
    assert stamps == sorted(stamps)  # so ORDER BY run_id is ORDER BY time


# ── read API ─────────────────────────────────────────────────────────────────
def _seed(j):
    rid = j.start_run(new_run_id(), "scan", {})
    j.record_signals(rid, [_result("NVDA")], grade_fn=lambda r: "A",
                     emitted_at="2026-08-20T14:00:00+00:00")
    j.record_signals(rid, [_result("AMD")], grade_fn=lambda r: "B",
                     emitted_at="2026-08-25T14:00:00+00:00")
    j.record_signals(rid, [_result("TSLA")], grade_fn=lambda r: "A",
                     emitted_at="2026-08-28T20:30:00+00:00")
    return rid


def test_query_by_symbol(tmp_path):
    j = _journal(tmp_path)
    _seed(j)
    rows = j.by_symbol("amd")   # case-insensitive
    assert [r["symbol"] for r in rows] == ["AMD"]


def test_query_by_grade(tmp_path):
    j = _journal(tmp_path)
    _seed(j)
    assert sorted(r["symbol"] for r in j.by_grade("A")) == ["NVDA", "TSLA"]
    assert [r["symbol"] for r in j.by_grade("d")] == []


def test_query_by_date_range_bare_end_date_includes_that_whole_day(tmp_path):
    j = _journal(tmp_path)
    _seed(j)
    rows = j.by_date_range("2026-08-25", "2026-08-28")
    # TSLA was emitted at 20:30 UTC on the 28th; a naive `<= "2026-08-28"`
    # would have cut it off at midnight and silently lost the day's signals.
    assert sorted(r["symbol"] for r in rows) == ["AMD", "TSLA"]


def test_query_orders_newest_first_and_honours_limit(tmp_path):
    j = _journal(tmp_path)
    _seed(j)
    rows = j.query(limit=2)
    assert [r["symbol"] for r in rows] == ["TSLA", "AMD"]


def test_query_filters_combine(tmp_path):
    j = _journal(tmp_path)
    _seed(j)
    assert j.query(grade="A", start="2026-08-26") == j.by_symbol("TSLA")


def test_query_by_min_setup_q(tmp_path):
    j = _journal(tmp_path)
    rid = j.start_run(new_run_id(), "scan", {})
    j.record_signals(rid, [_result("NVDA", setup_q=0.9), _result("AMD", setup_q=0.2)])
    assert [r["symbol"] for r in j.query(min_setup_q=0.5)] == ["NVDA"]


def test_runs_listing_counts_signals(tmp_path):
    j = _journal(tmp_path)
    rid = _seed(j)
    runs = j.runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == rid
    assert runs[0]["n_signals"] == 3


def test_journal_reopens_existing_db(tmp_path):
    """History has to survive the process that wrote it — that is the point."""
    path = str(tmp_path / "signals.db")
    j1 = SignalJournal(db_path=path)
    rid = j1.start_run(new_run_id(), "scan", {})
    j1.record_signals(rid, [_result()])
    j1.close()

    j2 = SignalJournal(db_path=path)
    assert j2.count() == 1


def test_grades_constant_matches_scanner_letters():
    from core.scanner import grade_letter
    assert set(GRADES) == {grade_letter(q, s, c)
                           for q in (0.0, 0.5, 0.8, 1.0)
                           for s in (0, 50, 100)
                           for c in (True, False)}


def test_utc_now_iso_is_seconds_precision():
    assert utc_now_iso().endswith("+00:00")
    assert "." not in utc_now_iso()
