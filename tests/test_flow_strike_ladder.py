"""
The strike ladder on the FLOW card.

The card shows four contracts a side, ranked on premium. That answers "what is
the best contract" but not "where is the money stacked" — a ticker with six
strikes bought in a row around spot is a different trade from one with a single
lotto strike 8% out, and the four-chip view renders them identically. The
ladder aggregates every contract in the signal by strike, so the shape of the
positioning is visible, with spot marked to say which side of the money it is on.
"""
from web.app import _serialize_flow, _strike_ladder, LADDER_STRIKES


def _c(strike, otype, flow, **kw):
    c = {
        "ticker": "NVDA", "exp": "2026-08-28", "dte": 4,
        "strike": strike, "type": otype, "vol": 5000, "oi": 2000,
        "vol_oi": 2.5, "mid": 5.0, "bid": 4.9, "ask": 5.1, "flow": flow,
        "sweep": False, "golden_sweep": False, "trade_side": "ask",
        "premium_tier": "whale", "n_prints": 3,
    }
    c.update(kw)
    return c


def _sig(**kw):
    sig = {
        "ticker": "NVDA", "flow_bias": "call",
        "call_flow": 5e7, "put_flow": 3e7, "total_flow": 8e7,
        "pc_ratio": 0.6, "trade_side": "ask", "iv_skew": -0.06,
        "stacked_flow": True, "golden_sweep": True, "premium_tier": "whale",
        "whale_score": 60, "dte0_flow": 0.0, "dte1_7_flow": 8e7, "dte8p_flow": 0.0,
        "call_contracts": [_c(210, "call", 4e6), _c(215, "call", 2e6)],
        "put_contracts": [_c(200, "put", 1e6)],
        "top_call": None, "top_put": None, "top_contract": None,
        "spot": 208.48, "filtered_n": 0, "filtered_premium": 0.0,
        "filtered_reasons": [],
    }
    sig.update(kw)
    return sig


def test_ladder_is_sorted_by_strike_not_by_premium():
    """It is a price axis. Ranking it by size would destroy the shape."""
    rows = _strike_ladder(_sig())["rows"]
    assert [r["strike"] for r in rows] == [200.0, 210.0, 215.0]


def test_premium_at_one_strike_is_summed_across_expiries():
    """Two expiries at the same strike are one level of interest, not two."""
    sig = _sig(call_contracts=[_c(210, "call", 4e6, exp="2026-08-28"),
                               _c(210, "call", 3e6, exp="2026-09-05")])
    rows = _strike_ladder(sig)["rows"]
    assert len(rows) == 2
    assert next(r for r in rows if r["strike"] == 210.0)["call"] == 7e6


def test_calls_and_puts_at_one_strike_stay_separable():
    """A strike bought on both sides is a straddle, not conviction — show both."""
    sig = _sig(call_contracts=[_c(210, "call", 4e6)],
               put_contracts=[_c(210, "put", 3e6)])
    row = _strike_ladder(sig)["rows"][0]
    assert row["call"] == 4e6 and row["put"] == 3e6 and row["total"] == 7e6


def test_rows_carry_share_of_the_largest_strike():
    """The bar length is drawn from pct, so the biggest strike must be 100."""
    rows = _strike_ladder(_sig())["rows"]
    top = max(rows, key=lambda r: r["total"])
    assert top["pct"] == 100.0
    assert next(r for r in rows if r["strike"] == 200.0)["pct"] == 25.0


def test_spot_side_is_marked_on_every_row():
    """Which side of the money the stack sits on is the whole read."""
    rows = _strike_ladder(_sig())["rows"]
    assert [r["above_spot"] for r in rows] == [False, True, True]


def test_ladder_keeps_only_the_biggest_strikes():
    """A 40-strike chain rendered in full is a wall, not a signal."""
    sig = _sig(call_contracts=[_c(200 + i, "call", (i + 1) * 1e5) for i in range(30)],
               put_contracts=[])
    rows = _strike_ladder(sig)["rows"]
    assert len(rows) == LADDER_STRIKES
    # The survivors are the largest, still in strike order.
    assert [r["strike"] for r in rows] == sorted(r["strike"] for r in rows)
    assert min(r["total"] for r in rows) == (30 - LADDER_STRIKES + 1) * 1e5


def test_spot_rides_along_for_the_marker():
    assert _strike_ladder(_sig())["spot"] == 208.48


def test_no_contracts_means_no_ladder():
    assert _strike_ladder(_sig(call_contracts=[], put_contracts=[]))["rows"] == []


def test_missing_spot_still_produces_a_ladder():
    """Without spot the bars are still true; only the marker is unavailable."""
    d = _strike_ladder(_sig(spot=0))
    assert d["rows"] and d["spot"] == 0
    assert all(r["above_spot"] is False for r in d["rows"])


def test_zero_and_missing_strikes_are_skipped():
    sig = _sig(call_contracts=[_c(0, "call", 9e6), {"type": "call", "flow": 5e6},
                               _c(210, "call", 1e6)],
               put_contracts=[])
    assert [r["strike"] for r in _strike_ladder(sig)["rows"]] == [210.0]


def test_ladder_reaches_the_card():
    d = _serialize_flow(_sig())
    assert d["ladder"]["rows"], "the card was handed no ladder"
    assert d["ladder"]["spot"] == 208.48


def test_ladder_sees_contracts_the_four_chips_never_show():
    """The chips cap at CARD_CONTRACTS a side; the ladder must not."""
    sig = _sig(call_contracts=[_c(200 + i, "call", 1e6) for i in range(6)],
               put_contracts=[])
    assert len(_serialize_flow(sig)["ladder"]["rows"]) == 6


def test_app_js_ships_the_ladder_renderer():
    js = open("web/static/app.js").read()
    assert "function renderFlowLadder" in js
    assert "<svg" in js.split("function renderFlowLadder")[1][:1800], \
        "the ladder must be hand-rolled SVG like every other chart here"
