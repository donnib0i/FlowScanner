"""
TradingView screener CSV as a universe source.

The user trusts his own TradingView screener more than the scanner's universe
builder, so the contract these tests defend is narrow and strict: whatever he
exported comes through, in his order, and anything we could not use is reported
rather than silently missing. All fixtures are on disk — no network.
"""
import os

import pytest

from core import tv_universe as tv

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


# ── Symbol normalization ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("AAPL", "AAPL"),
    ("  aapl  ", "AAPL"),
    ("NASDAQ:AAPL", "AAPL"),
    ("NYSE:BRK.B", "BRK-B"),          # dot class -> hyphen, as yfinance wants
    ("AMEX:SPY", "SPY"),
    ('"NASDAQ:NVDA"', "NVDA"),
    ("﻿NVDA", "NVDA"),           # BOM glued to the first cell
])
def test_normalize_symbol_accepts(raw, expected):
    assert tv.normalize_symbol(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", None, "Showing 4 of 4", "Electronic Technology",
    "NVIDIA Corporation", "182.35", "—", "TOOLONGSYMBOL",
])
def test_normalize_symbol_rejects(raw):
    assert tv.normalize_symbol(raw) is None


# ── Number parsing ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("182.35", 182.35),
    ("+2.41%", 2.41),
    ("-1.12%", -1.12),
    ("−0.32%", -0.32),           # Unicode minus
    ("214,830,000", 214830000.0),
    ("214.83M", 214830000.0),
    ("4.42T", 4.42e12),
    ("265.8B", 265.8e9),
    ("12.5K", 12500.0),
    ("$671.44", 671.44),
])
def test_parse_number(raw, expected):
    assert tv.parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "—", "N/A", "Strong Buy", "Technology", None])
def test_parse_number_refuses_non_numbers(raw):
    assert tv.parse_number(raw) is None


def test_decimal_comma_only_applied_for_semicolon_exports():
    assert tv.parse_number("182,35", decimal_comma=True) == pytest.approx(182.35)
    assert tv.parse_number("1.234,56", decimal_comma=True) == pytest.approx(1234.56)
    assert tv.parse_number("1,234.56") == pytest.approx(1234.56)


# ── The straightforward export ────────────────────────────────────────────────

def test_clean_export_preserves_screener_order():
    u = tv.load_tradingview_csv(fixture("tv_screener_clean.csv"))
    assert u.symbols == ["NVDA", "AMD", "TSLA", "SOFI", "HIMS"]


def test_clean_export_carries_identified_metadata():
    u = tv.load_tradingview_csv(fixture("tv_screener_clean.csv"))
    nvda = u.metadata["NVDA"]
    assert nvda["price"] == pytest.approx(182.35)
    assert nvda["change_pct"] == pytest.approx(2.41)
    assert nvda["volume"] == pytest.approx(214830000)
    assert nvda["relative_volume"] == pytest.approx(1.87)
    assert nvda["market_cap"] == pytest.approx(4.42e12)


def test_unrecognized_columns_are_reported_not_guessed():
    u = tv.load_tradingview_csv(fixture("tv_screener_clean.csv"))
    assert "Sector" in u.ignored_columns
    assert "sector" not in u.metadata["NVDA"]


# ── The messy realistic export ────────────────────────────────────────────────

def test_messy_export_survives_bom_prefixes_blanks_and_junk():
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    assert u.symbols == ["NVDA", "BRK-B", "AMD", "SPY"]


def test_messy_export_dedupes_and_records_it():
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    assert u.duplicates == ["NVDA"]
    assert u.symbols.count("NVDA") == 1


def test_messy_export_records_the_row_it_could_not_read():
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    assert [value for _, value in u.unparsed] == ["Showing 4 of 4"]


def test_messy_export_counts_blank_rows_separately():
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    assert u.blank_rows == 2          # the all-empty row and the trailing newline


def test_messy_export_maps_a_different_column_set():
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    assert u.metadata_columns["price"] == "Last"
    assert u.metadata_columns["volume"] == "Vol"
    assert u.metadata_columns["change_pct"] == "Chg %"
    assert u.metadata_columns["relative_volume"] == "Rel Volume (10 day)"
    assert u.metadata["NVDA"]["volume"] == pytest.approx(214830000)


def test_messy_export_ignores_columns_it_cannot_interpret():
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    for label in ("Description", "Perf % 1M", "Analyst Rating"):
        assert label in u.ignored_columns


def test_etfs_are_kept_because_the_screener_chose_them():
    # build_universe() strips ETFs; a TradingView list is the user's own call.
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    assert "SPY" in u.symbols


def test_quoted_commas_inside_a_field_do_not_shift_columns():
    # "Berkshire Hathaway Inc., Class B" contains the delimiter.
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    assert u.metadata["BRK-B"]["price"] == pytest.approx(478.10)


# ── Locale variants ───────────────────────────────────────────────────────────

def test_semicolon_locale_export_with_decimal_commas():
    u = tv.load_tradingview_csv(fixture("tv_screener_euro.csv"))
    assert u.symbols == ["NVDA", "AMD"]
    assert u.metadata["NVDA"]["price"] == pytest.approx(182.35)
    assert u.metadata["AMD"]["change_pct"] == pytest.approx(-1.12)


def test_unlabeled_headers_fall_back_to_content_detection():
    u = tv.load_tradingview_csv(fixture("tv_screener_unlabeled.csv"))
    assert u.symbols == ["NVDA", "AMD", "SOFI"]
    assert u.ticker_column == "Kolom B"


def test_a_ticker_named_column_still_has_to_contain_tickers():
    # A localized export can reuse a word we mapped; content wins over the label.
    text = ("Symbol,Ticker\n"
            "NVIDIA Corporation,NVDA\n"
            "Advanced Micro Devices,AMD\n")
    u = tv.parse_tradingview_csv(text)
    assert u.symbols == ["NVDA", "AMD"]


def test_description_first_column_does_not_hijack_the_ticker_column():
    text = ("Description,Symbol\n"
            "NVIDIA Corporation,NASDAQ:NVDA\n")
    u = tv.parse_tradingview_csv(text)
    assert u.ticker_column == "Symbol"
    assert u.symbols == ["NVDA"]


# ── Degenerate input ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["", "   \n\n", "Sector,Description\nFinance,Some Bank\n"])
def test_unusable_input_yields_an_empty_universe_not_an_exception(text):
    assert tv.parse_tradingview_csv(text).symbols == []


def test_header_only_file_is_empty():
    assert tv.parse_tradingview_csv("Symbol,Price\n").symbols == []


def test_single_column_export_needs_no_delimiter():
    u = tv.parse_tradingview_csv("Symbol\nNASDAQ:NVDA\nAMD\n")
    assert u.symbols == ["NVDA", "AMD"]


# ── Cap ───────────────────────────────────────────────────────────────────────

def test_cap_keeps_the_top_of_the_screener_ordering():
    text = "Symbol\n" + "".join(f"AA{i}\n" for i in range(10))
    u = tv.parse_tradingview_csv(text, cap=3)
    assert u.symbols == ["AA0", "AA1", "AA2"]
    assert u.capped_from == 10


def test_cap_none_takes_everything():
    text = "Symbol\n" + "".join(f"AA{i}\n" for i in range(10))
    u = tv.parse_tradingview_csv(text, cap=None)
    assert len(u.symbols) == 10
    assert u.capped_from == 0


def test_cap_drops_metadata_for_the_symbols_it_removes():
    text = "Symbol,Price\nAAA,1\nBBB,2\nCCC,3\n"
    u = tv.parse_tradingview_csv(text, cap=2)
    assert set(u.metadata) == {"AAA", "BBB"}


# ── Reporting ─────────────────────────────────────────────────────────────────

def test_drop_report_names_the_count_the_total_and_the_reason():
    note = tv.drop_report(["A", "B", "C"], ["A"], "no options chain")
    assert "2 of 3 symbols dropped" in note
    assert "no options chain" in note
    assert "B" in note and "C" in note


def test_drop_report_is_empty_when_nothing_was_lost():
    assert tv.drop_report(["A", "B"], ["A", "B"], "no options chain") == ""


def test_drop_report_truncates_a_long_list_but_keeps_the_count():
    requested = [f"AA{i}" for i in range(30)]
    note = tv.drop_report(requested, [], "no price data")
    assert "30 of 30 symbols dropped" in note
    assert "+18 more" in note


def test_summary_mentions_the_things_a_user_would_otherwise_miss():
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    summary = u.summary()
    assert "4 symbols" in summary
    assert "duplicate" in summary
    assert "unreadable" in summary


def test_detail_lines_surface_ignored_columns():
    u = tv.load_tradingview_csv(fixture("tv_screener_messy.csv"))
    joined = " ".join(u.detail_lines())
    assert "Analyst Rating" in joined
    assert "Symbol" in joined
