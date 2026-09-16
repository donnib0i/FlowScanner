"""
Sector lookup.

The scan reported "Other" for 522 of 612 names, which is the sector column not
working: grouping, laggard ranking and the heatmap all read this. The cause was
a hand-maintained table of ~134 names behind a universe of ~670 that is rebuilt
from live screeners every fifteen minutes.
"""
import json

import pytest

from core.constants import TICKER_SECTOR
from data import sector_map as S


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_SECTOR_CACHE", str(tmp_path / "sector_cache.json"))
    monkeypatch.setattr(S, "_cache", None)
    yield
    monkeypatch.setattr(S, "_cache", None)


def test_the_curated_table_wins(monkeypatch):
    ticker, sector = next(iter(TICKER_SECTOR.items()))
    assert S.sector_for(ticker) == sector


def test_the_constituent_table_covers_names_the_curated_one_misses():
    """478 names arrive free from a table the app already ships; the scan was
    simply not reading it."""
    from data.sector_constituents import STATIC_CONSTITUENTS
    extra = [t for ts in STATIC_CONSTITUENTS.values() for t in ts
             if t not in TICKER_SECTOR]
    assert extra, "expected the constituent table to add names"
    assert S.sector_for(extra[0]) is not None


def test_an_unknown_name_is_none_not_other(monkeypatch):
    """None lets the caller decide what to show. "Other" asserts a sector that
    does not exist, which is what made 85% of the table look classified."""
    monkeypatch.setattr(S, "_lookup", lambda t: None)
    assert S.sector_for("ZZZZ-NOT-REAL") is None


def test_a_scan_never_reaches_the_network(monkeypatch):
    """A scan that stops to make 300 HTTP requests is a scan nobody runs twice."""
    def _boom(ticker):
        raise AssertionError("network lookup during a scan")
    monkeypatch.setattr(S, "_lookup", _boom)
    assert S.sector_for("ZZZZ-NOT-REAL") is None          # default network=False


def test_a_resolved_answer_is_remembered(monkeypatch):
    calls = []

    def _fake(ticker):
        calls.append(ticker)
        return "Technology"

    monkeypatch.setattr(S, "_lookup", _fake)
    assert S.sector_for("NEWCO", network=True) == "Technology"
    assert S.sector_for("NEWCO") == "Technology"          # no network this time
    assert calls == ["NEWCO"]


def test_the_cache_survives_a_restart(monkeypatch):
    monkeypatch.setattr(S, "_lookup", lambda t: "Energy")
    S.sector_for("NEWCO", network=True)

    monkeypatch.setattr(S, "_cache", None)                # as a fresh process
    assert S.sector_for("NEWCO") == "Energy"


def test_warming_reports_what_it_did(monkeypatch):
    monkeypatch.setattr(S, "_lookup", lambda t: "Healthcare" if t == "AAA" else None)
    out = S.warm(["AAA", "BBB"])
    assert out["looked_up"] == 2 and out["resolved"] == 1
    assert S.sector_for("AAA") == "Healthcare"
    assert S.sector_for("BBB") is None


def test_warming_skips_names_already_known(monkeypatch):
    looked = []
    monkeypatch.setattr(S, "_lookup", lambda t: looked.append(t) or "Technology")
    known = next(iter(TICKER_SECTOR))
    S.warm([known])
    assert looked == []


def test_a_corrupt_cache_file_does_not_break_the_scan(monkeypatch, tmp_path):
    path = tmp_path / "sector_cache.json"
    path.write_text("{not json")
    monkeypatch.setenv("SCANNER_SECTOR_CACHE", str(path))
    monkeypatch.setattr(S, "_cache", None)
    assert S.sector_for("ZZZZ-NOT-REAL") is None          # no exception
