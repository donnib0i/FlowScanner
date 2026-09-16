"""
When a filter empties the feed, the feed has to say so.

Measured on production at 18:11 ET: a 190-name scan found 47 signals and the
default 0DTE filter dropped all 47, because today's expiry died at the close.
The tab showed an empty feed and no reason, which is indistinguishable from a
broken scan -- and it would do that every day after 16:00 and all weekend.

The scan already knows the number. It just never said it.
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("SCANNER_PIN", raising=False)
    monkeypatch.delenv("SCANNER_REQUIRE_PIN", raising=False)
    import web.app as webapp
    importlib.reload(webapp)
    webapp._reset_scan_session()
    yield webapp
    webapp._reset_scan_session()


def sig(ticker="NVDA", dte=7, score=90, bias="call"):
    return {"ticker": ticker, "dte": dte, "score": score, "bias": bias,
            "institutional": False}


def run(app, monkeypatch, signals, query):
    monkeypatch.setattr(app, "_serialize_flow", lambda s: s)

    def _scan(tickers, show_progress=False, on_signal=None, on_progress=None):
        for s in signals:
            on_signal(s)
        return []
    monkeypatch.setattr(app, "scan_options_flow", _scan)

    client = TestClient(app.app)
    out = []
    with client.stream("GET", f"/api/flow?{query}") as r:
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            item = json.loads(line[6:])
            if item.get("__ping__"):
                continue
            out.append(item)
            if item.get("__done__") or item.get("__error__"):
                break
    return out


def done_event(events):
    return next(e for e in events if e.get("__done__"))


def test_the_done_event_counts_what_was_found_and_what_was_shown(app, monkeypatch):
    evs = run(app, monkeypatch, [sig(dte=7), sig(dte=7)], "tickers=NVDA&dte=0dte&min_score=0")
    d = done_event(evs)
    assert d["found"] == 2
    assert d["shown"] == 0


def test_the_filter_that_emptied_the_feed_is_named(app, monkeypatch):
    """"0 of 47" is the fact; which filter did it is the part that tells the
    user what to change."""
    evs = run(app, monkeypatch, [sig(dte=7) for _ in range(3)],
              "tickers=NVDA&dte=0dte&min_score=0")
    assert done_event(evs)["dropped"]["dte"] == 3


def test_a_score_floor_is_accounted_separately_from_expiry(app, monkeypatch):
    evs = run(app, monkeypatch, [sig(dte=0, score=10), sig(dte=0, score=95)],
              "tickers=NVDA&dte=0dte&min_score=50")
    d = done_event(evs)
    assert d["dropped"]["score"] == 1
    assert d["shown"] == 1


def test_a_bias_filter_is_accounted_too(app, monkeypatch):
    evs = run(app, monkeypatch, [sig(bias="put"), sig(bias="call")],
              "tickers=NVDA&bias=call&dte=all&min_score=0")
    d = done_event(evs)
    assert d["dropped"]["bias"] == 1
    assert d["shown"] == 1


def test_nothing_is_reported_dropped_when_everything_passes(app, monkeypatch):
    evs = run(app, monkeypatch, [sig(dte=0), sig(dte=0)],
              "tickers=NVDA&dte=0dte&min_score=0")
    d = done_event(evs)
    assert d["shown"] == 2 and d["found"] == 2
    assert sum(d["dropped"].values()) == 0
