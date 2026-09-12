"""
The flow stream against a deployment that requires a PIN.

EventSource cannot send headers, which is why /api/flow takes a single-use
ticket. It also cannot read a status code, and that is the part that bit: on a
PIN-protected deployment the stream just fails, which the tab could not tell
apart from the server being down. It spent two retries and then reported
"Server unavailable - try again" at a server that was answering every request
correctly -- and because the ticket fetch was gated on a PIN the browser did
not have yet, the PIN prompt that would have fixed it was never reached.

Every other tab uses fetch, gets a clean 401, and prompts. FLOW is the default
tab and the only one that could not.
"""
import re

import pytest

JS = open("web/static/app.js").read()


def _do_flow_scan():
    assert "async function doFlowScan(" in JS, "doFlowScan moved -- re-point this test"
    return "async function doFlowScan(" + JS.split("async function doFlowScan(")[1].split("\n}\n")[0]


def test_the_stream_asks_for_a_ticket_before_it_opens():
    body = _do_flow_scan()
    ticket = body.index("/api/sse-ticket")
    stream = body.index("new EventSource(")
    assert ticket < stream, "the stream opens before it authenticates"


def test_the_ticket_is_not_gated_on_already_holding_a_pin():
    # This was the bug. With no stored PIN the ticket call was skipped, the
    # stream opened unauthenticated, and a 401 arrived somewhere EventSource
    # cannot read it.
    body = _do_flow_scan()
    head = body[:body.index("/api/sse-ticket")]
    # the last conditional opened before the ticket fetch must not be `if(PIN)`
    assert not re.search(r"if\s*\(\s*PIN\s*\)[^\n]*\n?\s*(?:\{)?\s*$",
                         head.rsplit("try", 1)[0][-200:]), \
        "the ticket fetch is still behind `if(PIN)`"
    assert "if(PIN){" not in head.replace(" ", ""), \
        "the ticket fetch is still behind `if(PIN)`"


def test_a_401_on_the_ticket_prompts_instead_of_opening_a_doomed_stream():
    body = _do_flow_scan()
    m = re.search(r"if\s*\(\s*_handleAuth\(tr\)\s*\)\s*\{([^}]*)\}", body)
    assert m, "the ticket response is no longer checked for auth"
    assert "return" in m[1], "a 401 falls through and opens the stream anyway"


def test_a_pin_prompt_does_not_leave_the_button_reading_scanning():
    # The other half: the tab looked hung rather than locked, because the
    # early return skipped every reset endFlowScan does.
    body = _do_flow_scan()
    m = re.search(r"if\s*\(\s*_handleAuth\(tr\)\s*\)\s*\{([^}]*)\}", body)
    assert "endFlowScan(" in m[1], \
        "returning on 401 without endFlowScan leaves the scan state stuck"


def test_a_network_failure_still_falls_through_to_the_stream():
    # Only auth short-circuits. A flaky ticket call should not stop a scan that
    # would otherwise work -- on a deployment with no PIN there is nothing to
    # authenticate against in the first place.
    body = _do_flow_scan()
    m = re.search(r"catch\s*\(\s*e\s*\)\s*\{([^}]*)\}", body)
    assert m and "return" not in m[1], "a network blip now aborts the scan"
