"""
Getting from what a person types to what yfinance answers to.

Two ways a ticker used to produce no surface at all:

Class shares. Every human writes BRK.B; yfinance answers to BRK-B. The dotted
form does not fail cleanly either -- fast_info raises a KeyError out of its
internals -- so the whole thing surfaced as "no spot price for BRK.B" and the
tab stayed empty for one of the largest companies on the exchange.

Futures codes outside the web endpoint. Resolution lived in the API handler
only, so `scanner --gex mnq` went looking for an equity called MNQ and died.
Worse for the two codes that ARE real equities: ES is Eversource Energy and MGC
is the Vanguard Mega Cap ETF, both with live option chains, so the CLI silently
measured the wrong instrument rather than failing.
"""
import pytest

from core.market_data import _yf_ticker
from core import futures as F


# ── class shares ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("typed,sent", [
    ("BRK.B", "BRK-B"), ("BF.B", "BF-B"), ("HEI.A", "HEI-A"),
    ("brk.b", "BRK-B"),
])
def test_a_dotted_class_share_is_sent_the_way_yfinance_spells_it(typed, sent):
    assert _yf_ticker(typed) == sent


@pytest.mark.parametrize("sym", ["NVDA", "SPY", "QQQ", "F", "A"])
def test_an_ordinary_ticker_is_untouched(sym):
    assert _yf_ticker(sym) == sym


@pytest.mark.parametrize("sym", ["SHOP.TO", "BMW.DE", "BHP.AX", "VOD.L"])
def test_an_exchange_suffix_keeps_its_dot(sym):
    # .TO/.DE/.AX are how yfinance spells a listing venue, not a share class.
    # Rewriting those would break the symbol; none carries a US option chain
    # anyway, so there is nothing to gain by guessing.
    assert _yf_ticker(sym) == sym


def test_the_index_map_still_wins():
    assert _yf_ticker("SPX") == "^SPX"
    assert _yf_ticker("VIX") == "^VIX"


# ── futures codes resolve for every caller, not just the web endpoint ────────
def test_surface_for_resolves_a_futures_code_itself():
    import inspect
    from core import gex
    src = inspect.getsource(gex.surface_for)
    assert "_resolve_future" in src, \
        "surface_for does not resolve -- the CLI and the API will disagree"


def test_resolution_is_idempotent_so_a_double_resolve_is_harmless():
    # The endpoint resolves before calling, so surface_for resolves again.
    once, c1 = F.resolve("mnq")
    twice, c2 = F.resolve(once)
    assert once == "NDX" and twice == "NDX"
    assert c1 == "MNQ" and c2 is None


def test_the_codes_that_collide_with_real_equities_are_known():
    # ES (Eversource) and MGC (Vanguard Mega Cap) are live tickers with their
    # own option chains. The futures reading wins because this is a futures
    # scanner and the screen says which instrument it measured -- but the
    # collision is deliberate, not accidental, and is pinned here so it cannot
    # be reintroduced silently for a third code.
    colliding = {"ES", "MGC"}
    assert {c for c in F._CODE_TO_FAMILY} & colliding == colliding
    for code in colliding:
        under, contract = F.resolve(code)
        assert contract == code
        assert under in ("SPX", "GLD")
