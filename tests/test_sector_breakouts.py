from core.scanner import classify_breakout, rank_breakout_constituents
import core.scanner as sc
# The sector scans resolve these names in core.sectors; patch them there.
import core.sectors as scsec


# ─── classify_breakout ────────────────────────────────────────────────────────
def test_classify_breakout_up():
    rs, b = classify_breakout(change_pct=1.2, spy_chg=0.3, rel_vol=1.5)
    assert rs == 0.9 and b == "up"


def test_classify_breakout_down():
    rs, b = classify_breakout(change_pct=-1.0, spy_chg=0.2, rel_vol=1.3)
    assert rs == -1.2 and b == "down"


def test_classify_breakout_below_rs_threshold():
    _, b = classify_breakout(change_pct=0.4, spy_chg=0.1, rel_vol=2.0)
    assert b == "none"  # rs 0.3 < 0.5


def test_classify_breakout_low_volume():
    _, b = classify_breakout(change_pct=1.2, spy_chg=0.0, rel_vol=1.0)
    assert b == "none"  # rel_vol 1.0 < 1.1


def test_classify_breakout_spy_zero_fallback():
    rs, b = classify_breakout(change_pct=0.8, spy_chg=0.0, rel_vol=1.2)
    assert rs == 0.8 and b == "up"  # rs falls back to change_pct


# ─── rank_breakout_constituents ────────────────────────────────────────────────
def _q(ch):
    return {"change_pct": ch, "dollar_vol": 1e6, "price": 10.0}


def test_rank_up_breakout_laggards_then_leaders():
    quotes = {"AAA": _q(2.0), "BBB": _q(0.1), "CCC": _q(1.8), "DDD": _q(-0.5)}
    out = rank_breakout_constituents(sector_chg=1.5, breakout="up",
                                     quotes=quotes, n_laggards=2, n_leaders=1)
    roles = [(t, r) for t, r, _, _ in out]
    # laggards = biggest positive lag (sector_chg - ticker_chg): DDD(2.0), BBB(1.4)
    assert roles[0] == ("DDD", "laggard")
    assert roles[1] == ("BBB", "laggard")
    # leader = strongest mover not already chosen: AAA(2.0)
    assert roles[2] == ("AAA", "leader")
    assert len({t for t, _ in roles}) == 3  # no overlap


def test_rank_down_breakout():
    quotes = {"AAA": _q(-2.0), "BBB": _q(0.2), "CCC": _q(-1.9)}
    out = rank_breakout_constituents(sector_chg=-1.5, breakout="down",
                                     quotes=quotes, n_laggards=1, n_leaders=1)
    roles = [(t, r) for t, r, _, _ in out]
    # laggard = most-positive (least-down) lag magnitude: BBB lags a falling sector
    assert roles[0][1] == "laggard" and roles[0][0] == "BBB"
    # leader = strongest downside mover not chosen: AAA(-2.0)
    assert roles[1] == ("AAA", "leader")


def test_rank_empty_when_no_breakout():
    assert rank_breakout_constituents(1.0, "none", {"AAA": _q(0.0)}) == []


# ─── sector_breakout_plays ─────────────────────────────────────────────────────
def test_breakout_plays_none_skips_network(monkeypatch):
    called = {"q": 0, "c": 0}
    monkeypatch.setattr(scsec, "_quotes_for", lambda *a, **k: called.__setitem__("q", called["q"] + 1) or {})
    monkeypatch.setattr(scsec, "get_best_contract", lambda *a, **k: called.__setitem__("c", called["c"] + 1))
    out = sc.sector_breakout_plays("Technology", {"Technology": {"change_pct": 0.1, "breakout": "none"}})
    assert out == {"sector": "Technology", "breakout": "none", "plays": []}
    assert called == {"q": 0, "c": 0}


def test_breakout_plays_builds_contracts(monkeypatch):
    sc._PLAYS_CACHE.clear()
    sd = {"Technology": {"change_pct": 1.5, "breakout": "up"}}
    monkeypatch.setattr(scsec, "constituents_for", lambda *a, **k: ["AAA", "BBB"])
    monkeypatch.setattr(scsec, "_quotes_for", lambda *a, **k: {
        "AAA": {"change_pct": 0.1, "dollar_vol": 1e6, "price": 10.0},
        "BBB": {"change_pct": 1.8, "dollar_vol": 2e6, "price": 20.0}})
    monkeypatch.setattr(scsec, "get_best_contract",
                        lambda tk, d, *a, **k: {"label": f"{tk} {d}", "strike": 1})
    out = sc.sector_breakout_plays("Technology", sd, dte_mode="0dte", n_laggards=1, n_leaders=1)
    assert out["breakout"] == "up"
    assert out["plays"][0]["ticker"] == "AAA" and out["plays"][0]["role"] == "laggard"
    assert out["plays"][0]["contract"]["label"] == "AAA up"
    assert any(p["role"] == "leader" for p in out["plays"])
