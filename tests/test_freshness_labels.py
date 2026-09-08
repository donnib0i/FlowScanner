"""
Every tab must state what its data actually is.

The deployed app serves delayed quotes and a 15-minute flow snapshot. A badge
claiming "real-time" on any of that would be a lie the user sizes a position
against, so these tests pin that nothing overstates its freshness and that the
one genuinely-live path is the only thing allowed to say LIVE.
"""
import re

JS = open("web/static/app.js").read()
HTML = open("web/templates/index.html").read()

TABS = ["flow", "scan", "sectors", "find", "uoa", "gex", "intel"]


def test_every_tab_has_a_freshness_entry():
    block = JS.split("const FRESHNESS = {")[1].split("};")[0]
    for tab in TABS:
        assert re.search(rf"\b{tab}:\s*\{{", block), f"{tab} has no freshness label"


def test_every_tab_pane_exists_for_its_label():
    for tab in TABS:
        assert f'id="tab-{tab}"' in HTML, f"no pane for {tab}"


def test_no_tab_claims_realtime_by_default():
    """Only the flow path can become live, and only from real provenance."""
    block = JS.split("const FRESHNESS = {")[1].split("};")[0]
    lowered = block.lower()
    for claim in ("real-time", "realtime", "live opra"):
        assert claim not in lowered, f"a default label claims {claim!r}"


def test_defaults_are_all_marked_lagged():
    block = JS.split("const FRESHNESS = {")[1].split("};")[0]
    assert block.count("is-lagged") == len(TABS)
    assert "is-live" not in block


def test_flow_label_upgrades_only_on_real_provenance():
    """`live` comes from get_flow_source(), which records what actually ran."""
    fn = JS.split("function _updateFlowFreshness")[1].split("\n}")[0]
    assert "d.live" in fn
    assert "is-live" in fn and "LIVE" in fn


def test_flow_label_falls_back_when_not_live():
    fn = JS.split("function _updateFlowFreshness")[1].split("\n}")[0]
    assert "FRESHNESS.flow.t" in fn, "must revert to the delayed text"


def test_gex_label_states_open_interest_is_settled():
    block = JS.split("const FRESHNESS = {")[1].split("};")[0]
    gex = [l for l in block.splitlines() if l.strip().startswith("gex:")][0]
    assert "prior-session" in gex and "Not intraday" in gex


def test_intel_label_does_not_call_the_proxy_a_dark_pool_feed():
    """
    finra_darkpool is a volume-based proxy off yfinance, not off-exchange
    prints. Calling it a dark pool feed would overstate what it knows.
    """
    block = JS.split("const FRESHNESS = {")[1].split("};")[0]
    intel = [l for l in block.splitlines() if l.strip().startswith("intel:")][0]
    assert "proxy" in intel
    assert "not off-exchange prints" in intel


def test_labels_are_mounted_into_the_panes():
    assert "_mountFreshness()" in JS
    assert "pane.insertBefore" in JS
