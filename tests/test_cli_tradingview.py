"""
The --tradingview flag: the user's own screener export as the universe.

The behaviour worth locking down at the CLI layer is that a bad file fails
loudly. Falling back to the built-in universe here would scan a list the user
never asked for while looking like it worked.
"""
import os

import pytest

from core.scanner import build_parser, load_tv_universe, print_tv_drops

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def test_parser_accepts_tradingview_path_and_limit():
    ns = build_parser().parse_args(["--tradingview", "list.csv", "--tv-limit", "40"])
    assert ns.tradingview == "list.csv"
    assert ns.tv_limit == 40


def test_tv_short_flag_is_the_same_option():
    ns = build_parser().parse_args(["--tv", "list.csv"])
    assert ns.tradingview == "list.csv"


def test_tradingview_defaults_off_and_capped():
    ns = build_parser().parse_args([])
    assert ns.tradingview is None
    assert ns.tv_limit > 0


def test_load_uses_the_export_in_its_own_order():
    tv = load_tv_universe(fixture("tv_screener_clean.csv"))
    assert tv.symbols == ["NVDA", "AMD", "TSLA", "SOFI", "HIMS"]


def test_load_honours_the_limit():
    tv = load_tv_universe(fixture("tv_screener_clean.csv"), cap=2)
    assert tv.symbols == ["NVDA", "AMD"]


def test_zero_limit_means_no_cap():
    tv = load_tv_universe(fixture("tv_screener_clean.csv"), cap=0)
    assert len(tv.symbols) == 5


def test_missing_file_exits_rather_than_falling_back(capsys):
    with pytest.raises(SystemExit):
        load_tv_universe(fixture("does_not_exist.csv"))
    assert "Could not read TradingView CSV" in capsys.readouterr().out


def test_file_with_no_symbols_exits_with_a_usable_hint(tmp_path, capsys):
    path = tmp_path / "wrong.csv"
    path.write_text("Sector,Description\nFinance,Some Bank\n")
    with pytest.raises(SystemExit):
        load_tv_universe(str(path))
    out = capsys.readouterr().out
    assert "No symbols found" in out
    assert "'Symbol' or 'Ticker' column" in out


def test_drops_are_printed_when_the_data_layer_loses_symbols(capsys):
    tv = load_tv_universe(fixture("tv_screener_clean.csv"))
    print_tv_drops(tv, tv.symbols, ["NVDA", "AMD", "TSLA"], "no options chain")
    out = capsys.readouterr().out
    assert "2 of 5 symbols dropped" in out
    assert "no options chain" in out
    assert "SOFI" in out and "HIMS" in out


def test_nothing_is_printed_when_nothing_was_dropped(capsys):
    tv = load_tv_universe(fixture("tv_screener_clean.csv"))
    capsys.readouterr()
    print_tv_drops(tv, tv.symbols, tv.symbols, "no options chain")
    assert capsys.readouterr().out == ""


def test_drop_reporting_is_inert_for_the_built_in_universe(capsys):
    print_tv_drops(None, ["AAPL", "MSFT"], ["AAPL"], "no price data")
    assert capsys.readouterr().out == ""
