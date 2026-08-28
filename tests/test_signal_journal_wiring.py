"""The scanner side of the signal journal: grading, the non-fatal recorder,
the history dump, and the CLI flags.

No network: every test either builds result dicts by hand or hands the recorder
an explicit temp-path journal.
"""
from data.signal_journal import SignalJournal
from core.scanner import (
    build_parser,
    grade_letter,
    grade_score,
    merge_whale_scores,
    print_signal_history,
    record_scan_signals,
    trade_grade,
)


def _journal(tmp_path):
    return SignalJournal(db_path=str(tmp_path / "signals.db"))


def _result(ticker="NVDA", **over):
    r = {
        "ticker": ticker, "direction": "up", "price": 182.5,
        "setup_q": 0.8, "opt_score": 70, "signal_combo": "GU+HV",
        "rel_vol": 1.8, "change_pct": 2.3, "gap_pct": 0.9,
        "contract": {"exp": "2026-09-05", "dte": 8, "strike": 185.0,
                     "type": "call", "delta": 0.41, "iv": 0.38,
                     "oi": 12000, "vol": 4300, "bid": 3.1, "ask": 3.3, "mid": 3.2},
    }
    r.update(over)
    return r


# ── grading ──────────────────────────────────────────────────────────────────
def test_grade_letter_is_plain_and_matches_coloured_grade():
    for q, s, c in [(1.0, 100, True), (0.8, 60, True), (0.5, 40, False), (0.0, 0, False)]:
        letter = grade_letter(q, s, c)
        assert letter in ("A", "B", "C", "D")
        # No ANSI in the plain letter; the coloured one still wraps the same one.
        assert "\x1b" not in letter
        assert letter in trade_grade(q, s, c)


def test_grade_score_matches_a_grade_filter_weighting():
    assert grade_score(0.8, 70, True) == 0.8 * 50 + 70 * 0.30 + 20


def test_grade_boundaries():
    assert grade_letter(1.0, 100, True) == "A"     # 100
    assert grade_letter(0.7, 60, False) == "C"     # 53
    assert grade_letter(0.8, 50, False) == "B"     # 55 — exactly on the B floor
    assert grade_letter(1.0, 0, False) == "C"      # 50 — just under it
    assert grade_letter(1.0, 20, True) == "A"      # 76
    assert grade_letter(0.7, 0, False) == "C"      # 35 — exactly on the C floor
    assert grade_letter(0.6, 10, False) == "D"     # 33
    assert grade_letter(0.0, 0, False) == "D"      # 0


# ── merge keeps the whale score visible ──────────────────────────────────────
def test_merge_whale_scores_records_raw_score_on_result():
    results = [_result("NVDA"), _result("AMD")]
    merge_whale_scores(results, [{"ticker": "NVDA", "whale_score": 72}])
    assert results[0]["whale_score"] == 72
    assert results[1]["whale_score"] == 0
    # and the existing boost behaviour is unchanged
    assert results[0]["opt_score"] == 85


# ── the recorder ─────────────────────────────────────────────────────────────
def test_record_scan_signals_writes_graded_rows(tmp_path, capsys):
    j = _journal(tmp_path)
    rid = record_scan_signals([_result("NVDA"), _result("AMD", setup_q=0.1, opt_score=5,
                                                        contract=None)],
                              params={"enrich_top": 20}, journal=j)
    assert rid
    rows = {r["symbol"]: r for r in j.query()}
    assert rows["NVDA"]["grade"] == "A"
    assert rows["AMD"]["grade"] == "D"
    assert rows["AMD"]["contract_key"] == ""
    assert "Journaled 2 signals" in capsys.readouterr().out


def test_record_scan_signals_is_idempotent_within_a_run(tmp_path):
    j = _journal(tmp_path)
    rid = record_scan_signals([_result()], params={}, journal=j)
    record_scan_signals([_result()], params={}, journal=j, run_id=rid)
    assert j.count() == 1


def test_record_scan_signals_never_raises_when_the_write_fails(tmp_path, capsys):
    """A full disk must cost a log line, not the trading day."""
    class Exploding:
        def start_run(self, *a, **k):
            raise OSError("disk I/O error")

    assert record_scan_signals([_result()], params={}, journal=Exploding()) is None
    out = capsys.readouterr().out
    assert "Signal journal write failed" in out
    assert "scan continues" in out


def test_record_scan_signals_survives_a_malformed_result(tmp_path, capsys):
    j = _journal(tmp_path)
    # setup_q as a non-numeric string would blow up grading if it were unguarded.
    assert record_scan_signals([{"ticker": "???"}], params={}, journal=j) is not None
    assert j.count() == 1


# ── the history dump ─────────────────────────────────────────────────────────
def test_print_signal_history_renders_rows(tmp_path, capsys):
    j = _journal(tmp_path)
    record_scan_signals([_result("NVDA")], params={}, journal=j)
    print_signal_history(journal=j)
    out = capsys.readouterr().out
    assert "NVDA" in out
    assert "09-05 $185C" in out
    assert "1 signal(s)" in out


def test_print_signal_history_filters_by_symbol_and_grade(tmp_path, capsys):
    j = _journal(tmp_path)
    record_scan_signals([_result("NVDA"), _result("AMD", setup_q=0.1, opt_score=5,
                                                  contract=None)],
                        params={}, journal=j)
    print_signal_history(symbol="AMD", journal=j)
    out = capsys.readouterr().out
    assert "AMD" in out and "NVDA" not in out

    print_signal_history(grade="Z", journal=j)
    assert "No signals recorded" in capsys.readouterr().out


def test_print_signal_history_reports_a_read_failure_without_raising(capsys):
    class Exploding:
        def query(self, **k):
            raise OSError("database is locked")

    print_signal_history(journal=Exploding())
    assert "Could not read the signal journal" in capsys.readouterr().out


# ── CLI ──────────────────────────────────────────────────────────────────────
def test_parser_has_signal_history_flags():
    ns = build_parser().parse_args([
        "--signals", "--signals-since", "2026-08-01", "--signals-until", "2026-08-28",
        "--signals-symbol", "NVDA", "--signals-grade", "A", "--signals-limit", "10",
    ])
    assert ns.signals is True
    assert ns.signals_since == "2026-08-01"
    assert ns.signals_until == "2026-08-28"
    assert ns.signals_symbol == "NVDA"
    assert ns.signals_grade == "A"
    assert ns.signals_limit == 10


def test_journalling_is_on_by_default_and_can_be_disabled():
    assert build_parser().parse_args([]).no_journal is False
    assert build_parser().parse_args(["--no-journal"]).no_journal is True


def test_signals_flag_defaults_off_and_does_not_disturb_a_normal_scan():
    ns = build_parser().parse_args(["--tickers", "NVDA"])
    assert ns.signals is False
    assert ns.signals_limit == 50
    assert ns.tickers == ["NVDA"]
