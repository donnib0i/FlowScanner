"""
What the FLOW card is handed.

The card used to receive `top3`: three contracts, ranked on premium, taken only
from the bias side. If a ticker was call-biased you never saw a put it traded,
and the ranking had no idea whether a contract was tradeable. Both sides are
now serialized, each carrying what it costs to enter and what it needs to pay.
"""
from web.app import _serialize_flow


def _c(strike, otype, flow, **kw):
    c = {
        "ticker": "NVDA", "exp": "2026-08-28", "dte": 4,
        "strike": strike, "type": otype, "vol": 5000, "oi": 2000,
        "vol_oi": 2.5, "mid": 5.0, "bid": 4.9, "ask": 5.1, "flow": flow,
        "sweep": False, "golden_sweep": False, "trade_side": "ask",
        "premium_tier": "whale", "breakeven": strike + 5.0,
        "pct_to_breakeven": 3.45, "moneyness_pct": 0.73,
        "spread_pct": 4.0, "wide_spread": False,
    }
    c.update(kw)
    return c


def _sig(**kw):
    calls = [_c(210 + i, "call", 9_000_000 - i * 1_000_000) for i in range(6)]
    puts  = [_c(200 - i, "put",  8_000_000 - i * 1_000_000) for i in range(6)]
    sig = {
        "ticker": "NVDA", "flow_bias": "call",
        "call_flow": 5e7, "put_flow": 3e7, "total_flow": 8e7,
        "pc_ratio": 0.6, "trade_side": "ask", "iv_skew": -0.06,
        "stacked_flow": True, "golden_sweep": True, "premium_tier": "whale",
        "whale_score": 60, "dte0_flow": 0.0, "dte1_7_flow": 8e7, "dte8p_flow": 0.0,
        "call_contracts": calls, "put_contracts": puts,
        "top_call": calls[0], "top_put": puts[0], "top_contract": calls[0],
        "spot": 208.48, "filtered_n": 0, "filtered_premium": 0.0,
        "filtered_reasons": [],
    }
    sig.update(kw)
    return sig


def test_both_sides_are_serialized():
    d = _serialize_flow(_sig())
    assert [c["type"] for c in d["top_calls"]] == ["call"] * 4
    assert [c["type"] for c in d["top_puts"]] == ["put"] * 4


def test_each_side_is_capped_at_four_and_ranked_by_premium():
    d = _serialize_flow(_sig())
    assert len(d["top_calls"]) == 4
    flows = [c["flow_raw"] for c in d["top_calls"]]
    assert flows == sorted(flows, reverse=True)


def test_put_side_survives_a_call_biased_signal():
    """The bug: a call-biased ticker showed no puts at all."""
    d = _serialize_flow(_sig(flow_bias="call"))
    assert d["top_puts"], "call-biased signal dropped every put contract"


def test_contracts_carry_entry_economics():
    d = _serialize_flow(_sig())
    c = d["top_calls"][0]
    for f in ("breakeven", "pct_to_breakeven", "moneyness_pct",
              "spread_pct", "wide_spread", "bid", "ask"):
        assert f in c, f"missing {f}"


def test_wide_spread_flag_reaches_the_card():
    sig = _sig()
    sig["call_contracts"][0].update(spread_pct=18.02, wide_spread=True)
    d = _serialize_flow(sig)
    assert d["top_calls"][0]["wide_spread"] is True
    assert d["top_calls"][0]["spread_pct"] == 18.02


def test_filtered_contracts_are_reported_not_hidden():
    """A silent filter reads as 'there was nothing there'."""
    d = _serialize_flow(_sig(
        filtered_n=3, filtered_premium=2_400_000.0,
        filtered_reasons=["deep ITM (26%) on 160 lots", "illiquid"]))
    assert d["filtered_n"] == 3
    assert d["filtered_fmt"] == "$2.4M"
    assert "deep ITM" in " ".join(d["filtered_reasons"])


def test_spot_reaches_the_card():
    assert _serialize_flow(_sig())["spot"] == 208.48


def test_empty_contract_lists_do_not_explode():
    d = _serialize_flow(_sig(call_contracts=[], put_contracts=[],
                             top_call=None, top_put=None, top_contract=None))
    assert d["top_calls"] == [] and d["top_puts"] == []
