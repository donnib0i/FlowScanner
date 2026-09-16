"""
What the GEX tab does when a lookup goes wrong, and what it says about futures.

Three things made a working tab look broken. A failed lookup blanked the chart
that was already on screen, so a typo cost you the surface you were reading. A
transient 502 out of the chain provider was reported as final. And the raw
server detail was the whole error message, which for an unknown symbol read
"no spot price for ZZZZZ" -- true, and no help.

The futures note is here because it is a claim about a model, and a claim about
a model should not drift silently.
"""
import re

JS = open("web/static/app.js").read()


def _load_gex():
    assert "async function loadGEX(" in JS, "loadGEX moved -- re-point this test"
    return "async function loadGEX(" + JS.split("async function loadGEX(")[1].split("\n}\n")[0]


# ── failure handling ─────────────────────────────────────────────────────────
def test_a_failed_lookup_does_not_wipe_the_surface_already_on_screen():
    body = _load_gex()
    clear = body.index("gex-chart').innerHTML=''")
    got_data = body.index("_gexData=d")
    assert clear < got_data, "the panes are cleared somewhere other than on success"
    # and nothing clears them before the request goes out
    assert "gex-chart').innerHTML=''" not in body[:body.index("await fetch")]


def test_a_transient_server_error_is_retried():
    body = _load_gex()
    m = re.search(r"if\(r\.status>=500\s*&&\s*attempt<(\d+)\)", body)
    assert m, "5xx is not retried"
    assert int(m[1]) >= 1


def test_a_client_error_is_not_retried():
    # A 4xx is the request being wrong; it will be just as wrong next time.
    body = _load_gex()
    guard = re.search(r"if\(r\.status>=500[^)]*\)\s*\{", body)
    assert guard, "the retry is not gated on the status class"


def test_a_network_failure_is_retried_before_it_is_reported():
    body = _load_gex()
    m = re.search(r"\}catch\(e\)\{([^}]*)\}", body)
    assert m and "loadGEX(attempt+1)" in m[1], "a dropped connection gives up at once"


def test_the_retry_is_visible_rather_than_a_silent_pause():
    body = _load_gex()
    assert "Retrying" in body or "retry " in body


def test_the_error_text_names_the_symbol():
    # "Request failed (503)" told the reader nothing about what they asked for.
    body = _load_gex()
    tail = body[body.index("if(!r.ok)"):]
    assert "+sym+" in tail, "the failure message does not mention the symbol"


# ── the futures greeks claim ─────────────────────────────────────────────────
def test_the_note_says_which_model_the_greeks_come_from():
    assert "Black&ndash;Scholes" in JS
    assert "Black&ndash;76" in JS


def test_the_note_gives_the_size_of_the_difference_not_just_its_name():
    # Measured: at 0-4 DTE the two differ by under 0.12%. A note that only said
    # "these are different models" would leave the reader unable to judge it.
    m = re.search(r"less than ([\d.]+)% at the", JS)
    assert m, "the note does not quantify the Black-76 difference"
    assert 0 < float(m[1]) < 1


def test_the_note_names_the_difference_that_actually_matters():
    # The models agree at these tenors; the contract multiplier does not.
    assert "100 shares" in JS
    assert "notional per lot" in JS


def test_net_gamma_is_offered_in_contracts_of_the_selected_size():
    # Dollars of delta is the measurement; lots is the unit the hedge is placed
    # in, and it is the only figure that depends on the multiplier.
    m = re.search(r"const notional=\(sel\.last\|\|f\.last\)\*\(sel\.multiplier\|\|0\)", JS)
    assert m, "the lot conversion is gone"
    assert "Math.abs(d.net_gex)/notional" in JS


def test_the_lot_figure_is_skipped_when_there_is_no_multiplier():
    assert re.search(r"if\(notional>0\)\{", JS), \
        "a missing multiplier would divide by zero and print Infinity lots"
