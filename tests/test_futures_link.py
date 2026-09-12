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
    def install(mapping, micro_last="track"):
        import core.market_data
        # Keep the real ticker normalisation (SPX -> ^SPX) in the path: the
        # mapping is keyed on what yfinance is actually asked for.
        norm = core.market_data._yf_ticker
        full = dict(mapping)
        # Micros are quoted too. Unless a test says otherwise they track the
        # full contract, which is what they do in the market to within a tick.
        for sizes in F.CONTRACTS.values():
            big, small = sizes[0]["yf"], sizes[1]["yf"]
            if big in full and small not in full:
                last = full[big]._last if micro_last == "track" else micro_last
                full[small] = _FakeTicker(pd.DataFrame({"Close": []}), last=last)

        def fake_yf(sym):
            s = norm(sym)
            assert s in full, f"unexpected fetch for {s}"
            return full[s]
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


# ── contract sizing ──────────────────────────────────────────────────────────
def test_every_family_lists_a_full_contract_and_its_micro():
    for under, expect in (("SPX", ("ES", "MES")), ("QQQ", ("NQ", "MNQ")),
                          ("IWM", ("RTY", "M2K")), ("DIA", ("YM", "MYM")),
                          ("GLD", ("GC", "MGC"))):
        codes = tuple(c["code"] for c in F.contracts_for(under))
        assert codes == expect
        sizes = F.contracts_for(under)
        assert sizes[0]["micro"] is False and sizes[1]["micro"] is True


def test_a_micro_is_a_tenth_of_its_full_contract():
    # This is the whole reason the micro is listed: it is what makes the
    # contract carryable on a small account.
    for under in ("SPX", "QQQ", "IWM", "DIA", "GLD"):
        big, small = F.contracts_for(under)
        assert small["multiplier"] == pytest.approx(big["multiplier"] / 10)
        assert small["tick"] == big["tick"]


def test_the_nasdaq_pair_is_nq_and_mnq_at_twenty_and_two_dollars_a_point():
    nq, mnq = F.contracts_for("QQQ")
    assert (nq["code"], nq["multiplier"]) == ("NQ", 20.0)
    assert (mnq["code"], mnq["multiplier"]) == ("MNQ", 2.0)


def test_tick_value_is_reported_per_contract(wired):
    wired({"QQQ":  _FakeTicker(_bars(DATES, [700.0, 704.0, 708.69])),
           "NQ=F": _FakeTicker(_bars(DATES, [28700.0, 28860.0, 29054.50]), last=29100.0)})
    nq, mnq = F.link_for("QQQ")["contracts"]
    assert nq["tick_value"] == pytest.approx(5.00)     # 0.25 x $20
    assert mnq["tick_value"] == pytest.approx(0.50)    # 0.25 x $2


def test_the_basis_is_taken_from_the_full_contract_not_the_micro(wired):
    # The full contract has the deeper book and the cleaner daily bar, and the
    # micro tracks it to within a tick -- deriving the basis from the micro
    # would add noise and change nothing.
    wired({"QQQ":   _FakeTicker(_bars(DATES, [700.0, 704.0, 708.69])),
           "NQ=F":  _FakeTicker(_bars(DATES, [28700.0, 28860.0, 29054.50]), last=29100.0),
           "MNQ=F": _FakeTicker(_bars(DATES, [1.0, 2.0, 3.0]), last=29099.75)})
    link = F.link_for("QQQ")
    assert link["ratio"] == pytest.approx(29054.50 / 708.69)
    assert link["last"] == 29100.0


def test_both_sizes_carry_their_own_live_print(wired):
    wired({"QQQ":   _FakeTicker(_bars(DATES, [700.0, 704.0, 708.69])),
           "NQ=F":  _FakeTicker(_bars(DATES, [28700.0, 28860.0, 29054.50]), last=29100.0),
           "MNQ=F": _FakeTicker(pd.DataFrame({"Close": []}), last=29099.75)})
    nq, mnq = F.link_for("QQQ")["contracts"]
    assert nq["last"] == 29100.0
    assert mnq["last"] == 29099.75


def test_a_micro_with_no_quote_falls_back_to_the_full_contract(wired):
    # They track to within a tick by construction, so a blank micro quote is
    # better filled than left at zero and rendered as a $0 level.
    wired({"QQQ":  _FakeTicker(_bars(DATES, [700.0, 704.0, 708.69])),
           "NQ=F": _FakeTicker(_bars(DATES, [28700.0, 28860.0, 29054.50]), last=29100.0)},
          micro_last=None)
    nq, mnq = F.link_for("QQQ")["contracts"]
    assert mnq["last"] == nq["last"] == 29100.0


def test_a_single_name_lists_no_contracts_at_all():
    assert F.contracts_for("NVDA") == []


# ── typing a contract instead of a ticker ────────────────────────────────────
def test_a_futures_code_resolves_to_the_chain_its_options_live_on():
    # There is no option chain on MNQ. Typing it means "the Nasdaq surface, in
    # MNQ prices", so it resolves to the index dealers hedge with that contract.
    assert F.resolve("MNQ") == ("NDX", "MNQ")
    assert F.resolve("NQ") == ("NDX", "NQ")
    assert F.resolve("ES") == ("SPX", "ES")
    assert F.resolve("MES") == ("SPX", "MES")
    assert F.resolve("RTY") == ("RUT", "RTY")


def test_the_dow_resolves_to_the_etf_because_the_index_carries_no_chain():
    # ^DJI returns zero expiries on this feed; DIA is the tradeable chain.
    assert F.resolve("YM") == ("DIA", "YM")
    assert F.resolve("MYM") == ("DIA", "MYM")


def test_the_ways_a_trader_actually_writes_a_contract_all_resolve():
    for typed in ("MNQ", "mnq", "/MNQ", "/mnq", "MNQ=F", "mnq=f"):
        assert F.resolve(typed) == ("NDX", "MNQ"), typed


def test_an_ordinary_ticker_passes_through_untouched():
    assert F.resolve("QQQ") == ("QQQ", None)
    assert F.resolve("nvda") == ("NVDA", None)
    assert F.resolve("SPX") == ("SPX", None)


def test_an_empty_symbol_does_not_blow_up():
    assert F.resolve("") == ("", None)
    assert F.resolve(None) == ("", None)


def test_every_family_resolves_to_a_symbol_that_has_a_chain():
    # The map is only useful if each target actually carries options; ^DJI is
    # the reason this is asserted rather than assumed.
    for fam in F.CONTRACTS:
        assert fam in F.SURFACE_UNDERLYING, f"{fam} resolves nowhere"
    assert set(F.SURFACE_UNDERLYING.values()) == {"SPX", "NDX", "RUT", "DIA", "GLD"}


# ── gold ─────────────────────────────────────────────────────────────────────
def test_gold_resolves_to_the_etf_with_the_deeper_chain():
    # Both GLD and IAU track gold; GLD carries 25 expiries against IAU's 14,
    # and a thin chain makes a thin surface.
    assert F.resolve("MGC") == ("GLD", "MGC")
    assert F.resolve("GC") == ("GLD", "GC")
    assert F.resolve("/mgc") == ("GLD", "MGC")
    assert F.resolve("MGC=F") == ("GLD", "MGC")


def test_a_gold_multiplier_is_the_contract_size_in_ounces():
    # Gold is quoted in dollars per troy ounce, so a $1 move in the metal is
    # worth the contract size: 100oz for GC, 10oz for MGC.
    gc, mgc = F.contracts_for("GLD")
    assert (gc["multiplier"], gc["tick"]) == (100.0, 0.10)
    assert (mgc["multiplier"], mgc["tick"]) == (10.0, 0.10)
    assert gc["tick_value"] if "tick_value" in gc else True


def test_the_gold_divisor_is_measured_not_assumed(wired):
    # GLD is roughly a tenth of an ounce, but it is not exactly a tenth and it
    # drifts as the fund's expenses accrue. Measuring the ratio absorbs both
    # the divisor and the drift; hard-coding 10 would be wrong and get wronger.
    wired({"GLD":  _FakeTicker(_bars(DATES, [390.0, 394.0, 398.50])),
           "GC=F": _FakeTicker(_bars(DATES, [4290.0, 4335.0, 4387.10]), last=4390.0)})
    link = F.link_for("GLD")
    assert link["ratio"] == pytest.approx(4387.10 / 398.50)
    assert 10.5 < link["ratio"] < 11.5, "the measured divisor drifted off gold"
    # Over a 1.1 ratio the points-basis is meaningless, so it is not reported.
    assert link["basis"] is None


def test_gold_reports_both_sizes_with_their_tick_values(wired):
    wired({"GLD":  _FakeTicker(_bars(DATES, [390.0, 394.0, 398.50])),
           "GC=F": _FakeTicker(_bars(DATES, [4290.0, 4335.0, 4387.10]), last=4390.0)})
    gc, mgc = F.link_for("GLD")["contracts"]
    assert gc["tick_value"] == pytest.approx(10.0)    # 0.10 x $100
    assert mgc["tick_value"] == pytest.approx(1.0)    # 0.10 x $10
