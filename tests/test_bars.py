"""
Today's session for the chart beside the flow feed.

Two things are pinned here. The freshness is measured, never asserted: the
response carries the timestamp of its last bar and the minutes since, so the
chart can say "last bar 14:32, 3 min ago" instead of "live" on a feed that
does not promise a latency. And a futures code charts the future -- the gamma
surface has to resolve MNQ to the NDX chain because there is no chain on the
contract, but the chart has no such constraint and the contract trades 23
hours a day.
"""
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core import bars as B
from core.market_data import _yf_ticker


# ── what gets fetched ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("typed,fetched,label", [
    ("SPX", "SPX", "SPX"),          # index map is applied later by _yf
    ("mnq", "MNQ=F", "MNQ"),
    ("/ES", "ES=F", "ES"),
    ("ES=F", "ES=F", "ES"),
    ("NVDA", "NVDA", "NVDA"),
    ("GC", "GC=F", "GC"),
])
def test_a_futures_code_charts_the_contract_not_the_chain(typed, fetched, label):
    assert B.chart_symbol(typed) == (fetched, label)


# ── the session, from stubbed bars ───────────────────────────────────────────
class _T:
    def __init__(self, df): self._df = df
    def history(self, period=None, interval=None): return self._df


def _frame(rows):
    """rows: [(iso_ts, o, h, l, c, v)] -> the frame yfinance would return."""
    idx = pd.to_datetime([r[0] for r in rows]).tz_localize("America/New_York")
    return pd.DataFrame({"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
                         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
                         "Volume": [r[5] for r in rows]}, index=idx)


@pytest.fixture
def wired(monkeypatch):
    def install(df):
        import core.market_data
        monkeypatch.setattr(core.market_data, "_yf", lambda s: _T(df))
    return install


TWO_DAYS = _frame([
    ("2026-09-18 15:58", 100.0, 100.5, 99.8, 100.2, 1000),
    ("2026-09-18 15:59", 100.2, 100.4, 100.0, 100.4, 1200),   # prior close
    ("2026-09-19 09:30", 101.0, 101.5, 100.8, 101.2, 3000),
    ("2026-09-19 09:31", 101.2, 101.8, 101.1, 101.7, 2000),
    ("2026-09-19 09:32", 101.7, 101.9, 101.3, 101.5, 1000),
])


def test_only_the_latest_session_is_drawn(wired):
    wired(TWO_DAYS)
    d = B.session_bars("SPX")
    assert d["session"] == "2026-09-19"
    assert len(d["bars"]) == 3
    assert d["bars"][0]["o"] == 101.0 and d["bars"][-1]["c"] == 101.5


def test_the_prior_close_is_the_last_bar_of_the_session_before(wired):
    wired(TWO_DAYS)
    assert B.session_bars("SPX")["prev_close"] == 100.4


def test_vwap_is_volume_weighted_typical_price(wired):
    wired(TWO_DAYS)
    tp = lambda h, l, c: (h + l + c) / 3
    expect = (tp(101.5, 100.8, 101.2) * 3000 + tp(101.8, 101.1, 101.7) * 2000
              + tp(101.9, 101.3, 101.5) * 1000) / 6000
    assert B.session_bars("SPX")["vwap"] == pytest.approx(expect)


def test_freshness_is_measured_from_the_last_bar_not_asserted(wired):
    # The last bar here is 2026-09-19 09:32 ET, which is a long time ago by
    # now; the lag has to say so rather than the chart claiming "live".
    wired(TWO_DAYS)
    d = B.session_bars("SPX")
    last = datetime.fromisoformat(d["asof"])
    expect = (datetime.now(timezone.utc) - last).total_seconds() / 60
    assert d["lag_min"] == pytest.approx(expect, abs=1.0)
    assert d["lag_min"] > 60


def test_a_first_session_with_no_prior_day_has_no_prior_close(wired):
    wired(_frame([("2026-09-19 09:30", 101.0, 101.5, 100.8, 101.2, 3000)]))
    assert B.session_bars("SPX")["prev_close"] is None


def test_no_bars_is_an_error_not_an_empty_chart(wired):
    wired(pd.DataFrame({"Open": [], "High": [], "Low": [], "Close": [], "Volume": []}))
    with pytest.raises(ValueError, match="No intraday bars"):
        B.session_bars("SPX")


# ── the level overlay, executed ──────────────────────────────────────────────
JS = open("web/static/app.js").read()


def _fn(name):
    head = f"function {name}("
    assert head in JS, f"{name} moved -- re-point this test"
    return head + JS.split(head)[1].split("\n}\n")[0] + "\n}"


def _levels_for(sym, gex):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed — overlay logic unverified")
    src = ("let _gexData=" + json.dumps(gex) + ";\n" + _fn("chartLevelsFor")
           + f"\nprocess.stdout.write(JSON.stringify(chartLevelsFor({json.dumps(sym)})));")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


GEX_NDX = {"symbol": "NDX", "requested": "MNQ", "call_wall": 29275.0, "put_wall": 29130.0,
           "flip": 29126.13, "provenance": {"flip_stable": True},
           "futures": {"ratio": 1.0115, "contracts": [{"code": "NQ"}, {"code": "MNQ"}]}}


def test_levels_draw_only_for_the_symbol_the_surface_measured():
    # An SPX surface on an NVDA chart would be lines that mean nothing.
    assert _levels_for("NVDA", GEX_NDX) is None
    assert _levels_for("NDX", GEX_NDX) is not None


def test_a_futures_chart_gets_the_levels_in_contract_prices():
    # The surface is on NDX strikes; MNQ is the same map through the ratio.
    lv = _levels_for("MNQ", GEX_NDX)
    assert lv["call_wall"] == pytest.approx(29275.0 * 1.0115)
    assert lv["put_wall"] == pytest.approx(29130.0 * 1.0115)


def test_the_index_chart_gets_the_levels_unconverted():
    lv = _levels_for("NDX", GEX_NDX)
    assert lv["call_wall"] == 29275.0


def test_an_unstable_flip_is_flagged_so_the_chart_draws_it_faint():
    g = dict(GEX_NDX, provenance={"flip_stable": False})
    assert _levels_for("NDX", g)["flip_stable"] is False
    assert 'unstable' in JS and "ch-flip.unstable" in open("web/static/app.css").read()


def test_the_chart_never_claims_to_be_live():
    body = _fn("chartFreshness")
    assert "'live'" not in body.lower() and '"live"' not in body.lower()
    assert "last bar" in body
