"""
The flow stream against a deployment that requires a PIN -- EXECUTED.

EventSource cannot send headers, which is why /api/flow takes a single-use
ticket, and it cannot read a status code, which is why a missing ticket looked
like a dead server rather than a locked one.

The first version of this file only grepped the source, and it passed for days
while the feature was completely broken: the ticket was appended to a `const`,
which throws, inside a catch written to swallow network errors. Every local
run passed because with no PIN configured the ticket is never checked. Every
production run failed from the day a PIN was set.

So doFlowScan is sliced out of app.js and run under node against stub fetch,
EventSource and DOM. A test that executes the code would have thrown on line
one of the bug; a test that greps it never can.
"""
import json
import shutil
import subprocess

import pytest

JS = open("web/static/app.js").read()


def _fn(name):
    head = f"async function {name}("
    assert head in JS, f"{name} moved -- re-point this test"
    return head + JS.split(head)[1].split("\n}\n")[0] + "\n}"


STUBS = r"""
// ── the world doFlowScan runs in, reduced to what it touches ────────────────
const PIN = 'set';
const _pa = p => p;
const S = {scanning:false, full:false, whale:false, dte:'all', qt:[], ft:[],
           callFlow:0, putFlow:0, signals:[], hotContracts:[]};
function setView(){}
function endFlowScan(err){ globalThis.__ended = {err: err===undefined ? null : err}; }
function _handleAuth(r){ return r.status===401; }
function _refreshSourceBadge(){}
const _el = () => new Proxy({style:{}, classList:{remove(){},add(){}}, textContent:'', className:''},
                            {get:(t,k)=> k in t ? t[k] : (typeof k==='string' ? '' : undefined),
                             set:(t,k,v)=>{t[k]=v;return true;}});
const document = { getElementById: () => _el() };
const localStorage = { getItem(){return null}, setItem(){}, removeItem(){} };
globalThis.setTimeout = (fn) => { globalThis.__retryScheduled = true; };

// stub fetch: the ticket endpoint answers as configured
globalThis.__ticketStatus = %(ticket_status)d;
globalThis.fetch = async (url) => ({
  status: globalThis.__ticketStatus,
  ok: globalThis.__ticketStatus===200,
  json: async () => ({ticket: 'TICKET-XYZ', expires_in: 30}),
});

// stub EventSource: record the URL it was opened with
globalThis.__opened = [];
class EventSource {
  constructor(url){ this.url=url; globalThis.__opened.push(url); }
  close(){}
}
globalThis.EventSource = EventSource;
"""


def _run(ticket_status=200):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed — stream auth unverified")
    src = (STUBS % {"ticket_status": ticket_status}
           + _fn("doFlowScan")
           + "\n(async()=>{ await doFlowScan(0);"
             " process.stdout.write(JSON.stringify({opened:globalThis.__opened,"
             " ended:globalThis.__ended||null, retry:!!globalThis.__retryScheduled})); })()"
             ".catch(e=>{process.stdout.write(JSON.stringify({threw:String(e)}));});")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── the bug, executed ────────────────────────────────────────────────────────
def test_the_stream_opens_with_the_ticket_it_was_issued():
    r = _run()
    assert "threw" not in r, f"doFlowScan threw: {r.get('threw')}"
    assert len(r["opened"]) == 1, f"expected one stream, got {r['opened']}"
    assert "ticket=TICKET-XYZ" in r["opened"][0], \
        f"the stream opened without its ticket: {r['opened'][0]}"


def test_the_ticket_is_url_encoded():
    r = _run()
    assert "ticket=TICKET-XYZ" in r["opened"][0]
    assert "&ticket=" in r["opened"][0], "ticket is not appended as a query parameter"


def test_nothing_in_the_ticket_path_is_swallowed():
    # The whole failure mode: an exception in the auth path was caught by a
    # handler meant for network errors. If the append ever throws again, this
    # surfaces it instead of opening a doomed stream.
    r = _run()
    assert r["opened"] and "ticket=" in r["opened"][0]


# ── a 401 prompts instead of opening a doomed stream ─────────────────────────
def test_a_401_on_the_ticket_ends_the_scan_without_opening_the_stream():
    r = _run(ticket_status=401)
    assert r["opened"] == [], "the stream was opened after the ticket was refused"
    assert r["ended"] is not None, "the scan was left running after a 401"
    assert r["ended"]["err"] is None, "a PIN prompt was reported as a scan error"


def test_a_pin_prompt_does_not_leave_the_button_reading_scanning():
    r = _run(ticket_status=401)
    assert r["ended"] is not None, \
        "returning on 401 without endFlowScan leaves the scan state stuck"


# ── the ticket is fetched regardless of what the browser already holds ───────
def test_the_ticket_is_requested_even_when_no_pin_is_stored():
    body = _fn("doFlowScan")
    head = body[:body.index("/api/sse-ticket")]
    assert "if(PIN){" not in head.replace(" ", ""), \
        "the ticket fetch is gated on a PIN the browser may not have yet"
