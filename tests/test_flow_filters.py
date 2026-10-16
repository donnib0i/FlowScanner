"""
Post-scan filter chips on the FLOW tab.

The chips above the SCAN button decide what gets scanned — changing one costs a
full rescan, and a scan is rate limited to three a minute. These chips decide
what stays on screen out of a scan already paid for: sweeps only, 0DTE vs
swing, a premium floor, whale grade. Sweep is worth filtering on as of today's
fix — it used to fire on 72% of contracts, so "sweeps only" filtered nothing.

The predicates are sliced out of app.js and executed under node, so this tests
what the browser will actually run rather than that the source mentions it.
"""
import json
import shutil
import subprocess

import pytest

from web.app import _serialize_flow

JS = open("web/static/app.js").read()
HTML = open("web/templates/index.html").read()


# ── the predicates, actually executed ─────────────────────────────────────────
def _run(state, signal):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed — predicate behaviour unverified")
    # The predicates read the swing window constants declared just above them.
    consts = "".join(l + "\n" for l in JS.splitlines() if l.startswith("const SWING_DTE_"))
    block = consts + "const FLOW_FILTERS={" + JS.split("const FLOW_FILTERS={")[1].split("\n};")[0] + "\n};"
    fn    = "function flowPasses(s){" + JS.split("function flowPasses(s){")[1].split("\n}")[0] + "\n}"
    src = (f"const S={json.dumps(state)};\n{block}\n{fn}\n"
           f"process.stdout.write(String(flowPasses({json.dumps(signal)})));")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip() == "true"


DEFAULTS = {"fSweeps": False, "flowWin": "all", "fMin": 0, "fWhale": False}


def _state(**kw):
    s = dict(DEFAULTS)
    s.update(kw)
    return s


def _s(**kw):
    sig = {"ticker": "NVDA", "total": 5e6, "score": 45, "tier": "block",
           "dte": 4, "has_sweep": False, "golden": False}
    sig.update(kw)
    return sig


def test_nothing_is_filtered_by_default():
    assert _run(_state(), _s())


def test_sweeps_only_drops_a_signal_with_no_sweep():
    assert not _run(_state(fSweeps=True), _s(has_sweep=False))
    assert _run(_state(fSweeps=True), _s(has_sweep=True))


def test_a_golden_sweep_counts_as_a_sweep():
    assert _run(_state(fSweeps=True), _s(has_sweep=False, golden=True))


def test_overall_shows_every_expiry():
    for dte in (0, 3, 7, 21, 45):
        assert _run(_state(flowWin="all"), _s(dte=dte))


def test_0dte_window_is_today_or_a_real_share_of_todays_flow():
    """A signal is one ticker across every expiry. Its headline contract can be a
    monthly while a quarter of its money went into today's expiry -- that name
    belongs in the 0DTE view. A sliver of 0DTE flow does not."""
    assert _run(_state(flowWin="0dte"), _s(dte=0))
    assert _run(_state(flowWin="0dte"), _s(dte=21, total=4e6, dte0_raw=1e6))
    assert not _run(_state(flowWin="0dte"), _s(dte=21, total=4e6, dte0_raw=1e5))
    assert not _run(_state(flowWin="0dte"), _s(dte=1))


def test_weekly_window_is_seven_days_including_today():
    for dte in (0, 1, 7):
        assert _run(_state(flowWin="weekly"), _s(dte=dte)), f"{dte}DTE is weekly"
    assert not _run(_state(flowWin="weekly"), _s(dte=21))
    assert _run(_state(flowWin="weekly"), _s(dte=21, total=4e6, dte1_7_raw=8e5, dte0_raw=3e5))


def test_min_premium_is_a_floor_not_a_ceiling():
    assert _run(_state(fMin=1e6), _s(total=5e6))
    assert not _run(_state(fMin=1e7), _s(total=5e6))


def test_whale_grade_accepts_score_or_tier():
    assert _run(_state(fWhale=True), _s(score=72, tier="retail"))
    assert _run(_state(fWhale=True), _s(score=10, tier="whale"))
    assert not _run(_state(fWhale=True), _s(score=10, tier="retail"))


def test_filters_stack_rather_than_override():
    """Two chips on means both must pass — an OR here would show noise."""
    assert not _run(_state(fSweeps=True, fMin=1e6), _s(has_sweep=False, total=5e6))
    assert not _run(_state(fSweeps=True, fMin=1e7), _s(has_sweep=True, total=5e6))
    assert _run(_state(fSweeps=True, fMin=1e6), _s(has_sweep=True, total=5e6))


# ── what the card is handed ───────────────────────────────────────────────────
def _sig(contracts):
    return {"ticker": "NVDA", "flow_bias": "call", "call_flow": 1.0, "put_flow": 0.0,
            "total_flow": 1.0, "pc_ratio": 0.5, "whale_score": 50, "spot": 100.0,
            "call_contracts": contracts, "put_contracts": [],
            "top_contract": contracts[0] if contracts else None}


def _c(**kw):
    c = {"strike": 100.0, "type": "call", "flow": 1e6, "exp": "2026-08-28",
         "dte": 4, "sweep": False, "golden_sweep": False}
    c.update(kw)
    return c


def test_sweep_is_reported_per_signal_so_the_chip_can_filter_on_it():
    assert _serialize_flow(_sig([_c(sweep=True), _c(strike=105.0)]))["has_sweep"] is True
    assert _serialize_flow(_sig([_c(), _c(strike=105.0)]))["has_sweep"] is False


def test_sweep_count_spans_contracts_the_chips_never_show():
    """has_sweep must see the whole signal, not just the four ranked chips."""
    contracts = [_c(strike=100.0 + i, flow=(9 - i) * 1e5) for i in range(9)]
    contracts[8]["sweep"] = True   # ranked last, well outside the top four
    d = _serialize_flow(_sig(contracts))
    assert d["has_sweep"] is True
    assert d["sweep_n"] == 1


# ── UI wiring ─────────────────────────────────────────────────────────────────
def test_the_chips_are_on_the_flow_tab():
    bar = HTML.split('id="flow-filter-bar"')[1].split("</div>")[0]
    for cid in ("f-sweeps", "f-min", "f-whale"):
        assert f'id="{cid}"' in bar, f"{cid} is missing from the flow filter bar"
    assert bar.count('class="chip"') == 3, "chips must reuse the existing .chip style"


def test_the_expiry_window_is_one_control_with_three_choices():
    for w in ("0dte", "weekly", "all"):
        assert f"setFlowWin('{w}')" in HTML, f"{w} button missing"
    # The scan reads every expiry; the window is applied on the client.
    assert "&dte=all&" in JS


def test_the_filter_bar_hides_until_there_is_something_to_filter():
    bar = HTML.split('id="flow-filter-bar"')[0].rsplit("<div", 1)[1]
    assert "display:none" in HTML.split('id="flow-filter-bar"')[1].split(">")[0] or \
           "display:none" in bar


def test_the_feed_renderer_applies_the_filters():
    body = JS.split("function sortFlowFeed(){")[1].split("\n}")[0]
    assert "flowPasses" in body, "sortFlowFeed renders every signal regardless of chips"


def test_streamed_cards_are_gated_too():
    """
    Signals arrive one SSE message at a time and render as they land. Without
    the same gate on that path, a chip set mid-scan filters what is already on
    screen and then lets every later signal through behind it.
    """
    body = JS.split("if(m.__signal__){")[1].split("\n    }")[0]
    assert "flowPasses" in body


def test_filtering_says_how_much_it_hid():
    """A filter that silently empties the feed reads as a broken scan."""
    assert "flow-filter-count" in JS
