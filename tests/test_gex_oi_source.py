"""
Where the gamma surface's open interest comes from.

Outside market hours yfinance reports zero open interest across SPX/SPY/QQQ
(measured 2026-09-07 Sunday; intraday it is fully populated -- see the spec's
2026-09-08 correction). OI is the entire input to a gamma profile, so the
surface simply refused to render pre-market and after the close -- which is
when the morning plan is actually written. dxFeed's Summary event carries the
same settled number (measured 2026-09-08, 300/300 SPY symbols), so it can fill
the gap. These tests pin the merge: it fills holes, never overwrites a real
yfinance number, and a feed failure costs the caller nothing it had before.
"""
import pytest

from core import gex as G


def row(strike, opt_type="call", oi=0, expiry="2026-09-18"):
    return {"strike": strike, "type": opt_type, "oi": oi, "iv": 0.20,
            "volume": 100, "expiry": expiry, "bid": 1.0, "ask": 1.1,
            "last": 1.05}


# ── The merge ─────────────────────────────────────────────────────────────────
def test_a_zero_oi_row_is_filled_from_the_live_reading():
    rows = [row(100.0)]
    filled = G.merge_open_interest(rows, {("2026-09-18", 100.0, "call"): {"oi": 4200}})
    assert filled == 1
    assert rows[0]["oi"] == 4200


def test_an_existing_oi_is_never_overwritten():
    rows = [row(100.0, oi=7)]
    filled = G.merge_open_interest(rows, {("2026-09-18", 100.0, "call"): {"oi": 4200}})
    assert filled == 0
    assert rows[0]["oi"] == 7


def test_strikes_match_across_float_representations():
    rows = [row(452.50)]
    filled = G.merge_open_interest(
        rows, {("2026-09-18", 452.5, "call"): {"oi": 900}})
    assert filled == 1
    assert rows[0]["oi"] == 900


def test_calls_and_puts_at_one_strike_stay_separate():
    rows = [row(100.0, "call"), row(100.0, "put")]
    G.merge_open_interest(rows, {
        ("2026-09-18", 100.0, "call"): {"oi": 10},
        ("2026-09-18", 100.0, "put"):  {"oi": 20},
    })
    assert [r["oi"] for r in rows] == [10, 20]


def test_a_strike_the_feed_never_returned_is_left_alone():
    rows = [row(100.0)]
    assert G.merge_open_interest(rows, {("2026-09-18", 105.0, "call"): {"oi": 1}}) == 0
    assert rows[0]["oi"] == 0


def test_day_volume_fills_an_empty_volume_but_not_a_populated_one():
    rows = [row(100.0), row(105.0)]
    rows[0]["volume"] = 0
    G.merge_open_interest(rows, {
        ("2026-09-18", 100.0, "call"): {"oi": 5, "day_volume": 3000},
        ("2026-09-18", 105.0, "call"): {"oi": 5, "day_volume": 3000},
    })
    assert rows[0]["volume"] == 3000     # was empty
    assert rows[1]["volume"] == 100      # yfinance had a real number


def test_a_zero_from_the_feed_is_not_treated_as_a_reading():
    rows = [row(100.0)]
    assert G.merge_open_interest(rows, {("2026-09-18", 100.0, "call"): {"oi": 0}}) == 0


# ── surface_for wiring ────────────────────────────────────────────────────────
@pytest.fixture
def surface(monkeypatch):
    """A yfinance chain with no open interest, plus a controllable OI feed."""
    import datetime as dt

    from core import market_calendar, market_data

    EXPS = ["2026-09-09", "2026-09-10", "2026-09-11", "2026-09-18"]

    class _Info:
        last_price = 100.0

    class _Ticker:
        options = EXPS
        fast_info = _Info()

    monkeypatch.setattr(market_calendar, "exchange_today", lambda: dt.date(2026, 9, 9))
    monkeypatch.setattr(market_data, "_yf", lambda s: _Ticker())

    state = {"oi": 0, "sparse": False, "calls": []}

    def chain(symbol, expiries):
        rows = [{"expiry": e, "type": kind, "strike": k, "oi": state["oi"],
                 "volume": 600, "iv": 0.20, "bid": 1.0, "ask": 2.0, "last": 1.95}
                for e in expiries for k in (90.0, 95.0, 100.0, 105.0, 110.0)
                for kind in ("call", "put")]
        for r in rows:
            if state["sparse"] and (r["expiry"], r["strike"], r["type"]) == \
                    (expiries[0], 100.0, "call"):
                r["oi"] = 250
        return rows

    monkeypatch.setattr(market_data, "full_chain", chain)
    return state


def _feed(state, monkeypatch, result=None, boom=False):
    def fetch(symbol, expiries, spot=0.0):
        state["calls"].append((symbol, tuple(expiries)))
        if boom:
            raise RuntimeError("dxfeed down")
        if result is not None:
            return result
        return {(e, k, kind): {"oi": 1000, "day_volume": 5000}
                for e in expiries for k in (90.0, 95.0, 100.0, 105.0, 110.0)
                for kind in ("call", "put")}
    monkeypatch.setattr(G, "_fetch_live_oi", fetch)


def test_an_empty_yfinance_chain_is_backfilled_from_the_live_feed(surface, monkeypatch):
    _feed(surface, monkeypatch)
    out = G.surface_for("SPY")
    assert surface["calls"], "the live feed was never consulted"
    assert out["provenance"]["oi_usable"] is True
    assert out["provenance"]["oi_source"] == "dxfeed"
    assert out["provenance"]["oi_total"] > 0


def test_a_populated_yfinance_chain_does_not_touch_the_feed(surface, monkeypatch):
    surface["oi"] = 1500
    _feed(surface, monkeypatch)
    out = G.surface_for("SPY")
    assert surface["calls"] == []
    assert out["provenance"]["oi_source"] == "yfinance"


def test_a_chain_both_feeds_contributed_to_names_both(surface, monkeypatch):
    """yfinance answered for one strike -- too few to render, but its numbers
    are still in the profile, so the label must not claim the surface is
    purely dxFeed's."""
    surface["sparse"] = True
    _feed(surface, monkeypatch)
    out = G.surface_for("SPY")
    assert out["provenance"]["oi_source"] == "yfinance+dxfeed"
    assert out["provenance"]["oi_usable"] is True


def test_a_backfill_that_fills_nothing_stays_labelled_yfinance(surface, monkeypatch):
    _feed(surface, monkeypatch, result={})
    out = G.surface_for("SPY")
    assert out["provenance"]["oi_source"] == "yfinance"
    assert out["provenance"]["oi_usable"] is False


def test_a_feed_failure_leaves_the_surface_exactly_as_it_was(surface, monkeypatch):
    _feed(surface, monkeypatch, boom=True)
    out = G.surface_for("SPY")
    assert out["provenance"]["oi_source"] == "yfinance"
    assert out["provenance"]["oi_usable"] is False
    assert out["flip"] is None


def test_the_feed_is_asked_only_for_the_expiries_being_rendered(surface, monkeypatch):
    _feed(surface, monkeypatch)
    out = G.surface_for("SPY")
    assert surface["calls"][0][1] == tuple(out["expiries"])


# ── What the OI fetch is allowed to do ────────────────────────────────────────
# The password login sends the SMS *before* it can discover the host has no way
# to answer it (data/tt_flow.py: _post_device_challenge precedes the tty check).
# /api/gex is user-triggered and rate-limited at 10/min, so an unguarded fetch
# would text Dante's phone ten times a minute and never authenticate once.
def test_a_dead_session_does_not_start_a_login_that_texts_a_phone(monkeypatch):
    from data import tt_flow as T

    T._OI_CACHE.clear()
    monkeypatch.setattr(T, "oauth_configured", lambda: False)
    monkeypatch.setattr(T, "_load_session", lambda: ("", ""))
    monkeypatch.setattr(T, "load_credentials", lambda: ("user", "pass"))
    monkeypatch.setattr(T.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.delenv("TT_OTP", raising=False)

    attempts = []

    async def fake(*a, **k):
        attempts.append(a)
        return {}
    monkeypatch.setattr(T, "_async_open_interest", fake)

    assert T.fetch_open_interest("SPY", ["2026-09-18"]) == {}
    assert attempts == [], "the fetch tried to authenticate anyway"


def test_a_cached_session_is_enough_to_try(monkeypatch):
    from data import tt_flow as T

    T._OI_CACHE.clear()
    monkeypatch.setattr(T, "oauth_configured", lambda: False)
    monkeypatch.setattr(T, "_load_session", lambda: ("tok", "2099-01-01T00:00:00"))
    monkeypatch.setattr(T, "load_credentials", lambda: ("user", "pass"))

    called = []

    async def fake(*a, **k):
        called.append(a)
        return {("2026-09-18", 100.0, "call"): {"oi": 5}}
    monkeypatch.setattr(T, "_async_open_interest", fake)

    assert T.fetch_open_interest("SPY", ["2026-09-18"]) != {}
    assert called


def test_a_reading_is_cached_and_an_empty_result_is_not(monkeypatch):
    from data import tt_flow as T

    T._OI_CACHE.clear()
    monkeypatch.setattr(T, "oauth_configured", lambda: True)
    calls = []

    async def fake(*a, **k):
        calls.append(1)
        return {("2026-09-18", 100.0, "call"): {"oi": 5}} if len(calls) > 1 else {}
    monkeypatch.setattr(T, "_async_open_interest", fake)

    assert T.fetch_open_interest("SPY", ["2026-09-18"]) == {}     # not cached
    assert T.fetch_open_interest("SPY", ["2026-09-18"]) != {}     # retried
    assert T.fetch_open_interest("SPY", ["2026-09-18"]) != {}     # served from cache
    assert len(calls) == 2
