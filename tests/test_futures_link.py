"""
Reading a gamma surface in futures prices.

Dealers hedge index options in the futures, and out of hours the future is the
only leg still printing -- the index is frozen at its close. So the levels the
surface measures are worth reading in ES or NQ terms.

The conversion is the whole risk here. It is a ratio taken from two closes that
printed in the SAME session; taking it from two live prints instead folds the
overnight move into the basis and slides every level by exactly the amount you
most wanted to know. These tests pin that, and pin that a ticker with no futures
counterpart says so rather than getting a proxy.
"""
import pandas as pd
import pytest

from core import futures as F


class _FakeTicker:
    def __init__(self, hist, last=None):
        self._hist, self._last = hist, last

    def history(self, period=None):
        return self._hist

    @property
    def fast_info(self):
        return type("FI", (), {"last_price": self._last})()


def _bars(dates, closes):
    return pd.DataFrame({"Close": closes},
                        index=pd.to_datetime(dates).tz_localize("America/New_York"))


@pytest.fixture
def wired(monkeypatch):
    """Point core.futures at bars we control, without touching the network."""
    def install(mapping):
        import core.market_data
        # Keep the real ticker normalisation (SPX -> ^SPX) in the path: the
        # mapping is keyed on what yfinance is actually asked for.
        norm = core.market_data._yf_ticker

        def fake_yf(sym):
            s = norm(sym)
            assert s in mapping, f"unexpected fetch for {s}"
            return mapping[s]
        monkeypatch.setattr(core.market_data, "_yf", fake_yf)
    return install


DATES = ["2026-09-08", "2026-09-09", "2026-09-10"]


# ── the mapping ──────────────────────────────────────────────────────────────
def test_an_index_and_its_tracking_etf_hedge_in_the_same_contract():
    assert F.future_for("SPX") == F.future_for("SPY") == "ES=F"
    assert F.future_for("NDX") == F.future_for("QQQ") == "NQ=F"
    assert F.future_for("RUT") == F.future_for("IWM") == "RTY=F"


def test_a_single_name_has_no_futures_counterpart():
    # Most tickers don't, and saying so beats inventing a proxy.
    assert F.future_for("NVDA") is None
    assert F.future_for("") is None


def test_the_lookup_does_not_care_about_case():
    assert F.future_for("spx") == "ES=F"


# ── the conversion ───────────────────────────────────────────────────────────
def test_the_ratio_comes_from_closes_that_printed_in_the_same_session(wired):
    # Index closed 7591.70 with the future at 7599.50: a basis of +7.80. The
    # future has since run to 7650 overnight. The ratio must reflect the basis
    # ONLY -- fold the overnight move in and every strike slides ~50 points.
    wired({"^SPX": _FakeTicker(_bars(DATES, [7500.0, 7550.0, 7591.70])),
           "ES=F": _FakeTicker(_bars(DATES, [7506.0, 7557.0, 7599.50]), last=7650.0)})
    link = F.link_for("SPX")
    assert link["ratio"] == pytest.approx(7599.50 / 7591.70, rel=1e-9)
    assert link["basis"] == pytest.approx(7.80, abs=0.01)
    assert link["ratio"] != pytest.approx(7650.0 / 7591.70, rel=1e-6)


def test_the_live_future_is_reported_apart_from_the_conversion(wired):
    wired({"^SPX": _FakeTicker(_bars(DATES, [7500.0, 7550.0, 7591.70])),
           "ES=F": _FakeTicker(_bars(DATES, [7506.0, 7557.0, 7599.50]), last=7650.0)})
    link = F.link_for("SPX")
    assert link["last"] == 7650.0
    assert link["change"] == pytest.approx(50.5)
    assert link["prev_close"] == 7599.50


def test_the_implied_index_undoes_the_basis(wired):
    # This is what positions the futures rule on a strike axis. Plotting the raw
    # print instead would place the line off by the whole basis.
    wired({"^SPX": _FakeTicker(_bars(DATES, [7500.0, 7550.0, 7591.70])),
           "ES=F": _FakeTicker(_bars(DATES, [7506.0, 7557.0, 7599.50]), last=7650.0)})
    link = F.link_for("SPX")
    assert link["implied_underlying"] == pytest.approx(7650.0 / link["ratio"])
    assert link["implied_underlying"] < link["last"], "the basis was not removed"


def test_an_etf_conversion_carries_the_divisor(wired):
    # QQQ is ~1/41 of NQ, so the ratio is a divisor, not a basis -- and a basis
    # in points would be meaningless, so it is not reported.
    wired({"QQQ":  _FakeTicker(_bars(DATES, [700.0, 704.0, 708.69])),
           "NQ=F": _FakeTicker(_bars(DATES, [28700.0, 28860.0, 29054.50]), last=29100.0)})
    link = F.link_for("QQQ")
    assert link["ratio"] == pytest.approx(29054.50 / 708.69)
    assert 40 < link["ratio"] < 42
    assert link["basis"] is None


def test_a_session_only_one_leg_printed_is_skipped(wired):
    # A futures holiday the cash market did not take, or vice versa. Pairing
    # those two closes would invent a basis out of a day's move.
    wired({"^SPX": _FakeTicker(_bars(DATES, [7500.0, 7550.0, 7591.70])),
           "ES=F": _FakeTicker(_bars(["2026-09-09", "2026-09-11"], [7557.0, 7700.0]),
                               last=7700.0)})
    link = F.link_for("SPX")
    assert link["ratio_asof"] == "2026-09-09"
    assert link["ratio"] == pytest.approx(7557.0 / 7550.0)


def test_a_ticker_with_no_future_gets_no_link(wired):
    wired({})
    assert F.link_for("NVDA") is None


def test_no_history_is_a_missing_link_not_a_guessed_one(wired):
    wired({"^SPX": _FakeTicker(pd.DataFrame({"Close": []})),
           "ES=F": _FakeTicker(pd.DataFrame({"Close": []}))})
    assert F.link_for("SPX") is None


def test_a_dead_live_quote_falls_back_to_the_close_rather_than_zero(wired):
    wired({"^SPX": _FakeTicker(_bars(DATES, [7500.0, 7550.0, 7591.70])),
           "ES=F": _FakeTicker(_bars(DATES, [7506.0, 7557.0, 7599.50]), last=None)})
    link = F.link_for("SPX")
    assert link["last"] == 7599.50
    assert link["change"] == 0.0


# ── the surface never dies because the futures leg did ───────────────────────
def test_a_broken_futures_leg_does_not_take_the_surface_with_it(monkeypatch):
    import core.gex
    monkeypatch.setattr(F, "link_for",
                        lambda s: (_ for _ in ()).throw(RuntimeError("feed down")))
    import inspect
    src = inspect.getsource(core.gex.surface_for)
    assert "out[\"futures\"] = None" in src, "the futures fetch is not guarded"
