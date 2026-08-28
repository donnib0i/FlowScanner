"""
Option chain de-duplication within a scan pass.

One pass fetched the same expiry up to three times: scan_options_flow walks the
near expiries, enrich_contracts fetches them again for the overlapping top-N,
and the IV-vs-HV adjustment fetches the front expiry once more. Same URL,
seconds apart, on a rate-limited source.

The cache exists to collapse those duplicates and nothing else, so the tests
that matter are the ones proving it expires well before the live refresh
interval — a chain cache that outlives a refresh is a stale-data bug, not an
optimisation. Everything here uses a counting stub; no network.
"""
import pytest

from core import scanner


class _Chain:
    def __init__(self, tag):
        self.calls = tag
        self.puts = tag


class _StubTicker:
    """Counts fetches so a saved round trip is observable, not assumed."""

    def __init__(self):
        self.fetches = []

    def option_chain(self, exp):
        self.fetches.append(exp)
        return _Chain(f"{exp}#{len(self.fetches)}")


@pytest.fixture(autouse=True)
def _clean_cache():
    scanner.clear_chain_cache()
    yield
    scanner.clear_chain_cache()


def test_repeat_fetch_in_the_same_pass_hits_the_network_once():
    t = _StubTicker()
    a = scanner._option_chain(t, "NVDA", "2026-09-05")
    b = scanner._option_chain(t, "NVDA", "2026-09-05")
    assert t.fetches == ["2026-09-05"]
    assert a is b


def test_different_expiries_and_tickers_are_cached_separately():
    t = _StubTicker()
    scanner._option_chain(t, "NVDA", "2026-09-05")
    scanner._option_chain(t, "NVDA", "2026-09-12")
    scanner._option_chain(t, "AMD", "2026-09-05")
    assert len(t.fetches) == 3


def test_cache_expires_and_refetches():
    t = _StubTicker()
    scanner._option_chain(t, "NVDA", "2026-09-05", ttl=0.0)
    scanner._option_chain(t, "NVDA", "2026-09-05", ttl=0.0)
    assert len(t.fetches) == 2


def test_ttl_is_well_inside_the_live_refresh_interval():
    """45s is the --interval default. A TTL at or above it would let a refresh
    redraw the screen from the previous pass's chain."""
    assert scanner._CHAIN_TTL_S < 45.0 / 2


def test_symbol_aliases_share_one_cache_entry():
    """SPX maps to ^SPX for yfinance. Two spellings of one instrument must not
    cost two fetches — or, worse, return two different chains."""
    t = _StubTicker()
    scanner._option_chain(t, "SPX", "2026-09-05")
    scanner._option_chain(t, "spx", "2026-09-05")
    assert len(t.fetches) == 1


def test_cache_stays_bounded_in_a_long_live_session():
    t = _StubTicker()
    for i in range(scanner._CHAIN_CACHE_MAX + 40):
        scanner._option_chain(t, f"T{i}", "2026-09-05")
    assert len(scanner._chain_cache) <= scanner._CHAIN_CACHE_MAX


def test_a_fetch_failure_is_not_cached():
    """Caching an exception would poison the whole pass for that expiry."""
    class _Flaky(_StubTicker):
        def option_chain(self, exp):
            self.fetches.append(exp)
            if len(self.fetches) == 1:
                raise RuntimeError("chain unavailable")
            return _Chain("ok")

    t = _Flaky()
    with pytest.raises(RuntimeError):
        scanner._option_chain(t, "NVDA", "2026-09-05")
    assert scanner._option_chain(t, "NVDA", "2026-09-05").calls == "ok"


def test_clear_chain_cache_empties_it():
    t = _StubTicker()
    scanner._option_chain(t, "NVDA", "2026-09-05")
    assert scanner._chain_cache
    scanner.clear_chain_cache()
    assert not scanner._chain_cache
