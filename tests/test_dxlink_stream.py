"""
The dxFeed streaming path had no coverage at all, because authentication has
never succeeded -- so every test in the suite stopped at or above the auth
layer and this code has never run against a live feed.

That is exactly where the two bugs below were hiding:

  - the client never sent the opening SETUP. DXLink is client-initiated, so
    the server said nothing, the handshake deadline expired, and the scan
    returned {} -- which looks identical to a quiet tape.
  - KEEPALIVE was only ever sent in reply to the server's. On a long window
    the connection can be dropped for our silence.

These tests drive collect_prints against a scripted fake socket, so the
handshake and the COMPACT row decoding are pinned without a network.
"""
import asyncio
import json

import pytest

from data import tt_flow


class FakeWS:
    """A dxLink server that replays a script and records what we send."""

    def __init__(self, script):
        self.sent = []
        self._script = list(script)

    async def send_json(self, msg):
        self.sent.append(msg)

    async def receive_json(self):
        if self._script:
            return self._script.pop(0)
        # Nothing left to say: drop the connection. Idling instead would make
        # every test wait out the real 30s handshake deadline.
        raise ConnectionError("scripted socket exhausted")

    def types_sent(self):
        return [m.get("type") for m in self.sent]

    def first(self, mtype):
        return next(m for m in self.sent if m.get("type") == mtype)


def fake_connect(ws):
    class _Ctx:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, *a):
            return False

    def _connect(url, client=None):
        return _Ctx()

    return _connect


TAS = ["eventSymbol", "time", "price", "size", "aggressorSide",
       "exchangeCode", "exchangeSaleConditions", "spreadLeg"]


def feed_config(fields=TAS):
    """
    The shape the live dxLink server actually sends -- a mapping -- captured
    from the wire on 2026-09-07. The earlier fake here used dxFeed's documented
    list form, so these tests passed while the real feed raised AttributeError
    on the first config. Both shapes are now handled; both are tested.
    """
    return {"type": "FEED_CONFIG", "channel": 1,
            "eventFields": {"TimeAndSale": fields}}


def feed_config_list_form(fields=TAS):
    """dxFeed's documented shape, still accepted."""
    return {"type": "FEED_CONFIG", "channel": 1,
            "eventFields": [{"eventType": "TimeAndSale",
                             "eventFieldsList": fields}]}


def handshake(extra=None):
    return [
        {"type": "SETUP", "channel": 0},
        {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"},
        {"type": "CHANNEL_OPENED", "channel": 1},
        feed_config(),
    ] + (extra or [])


def run(ws, monkeypatch, symbols=(".SPXW260907C6500",), window=0.05):
    monkeypatch.setattr(tt_flow, "aconnect_ws", fake_connect(ws))
    auth = tt_flow.TTAuth("u", "p")
    auth.dx_token, auth.dx_url = "dx-tok", "wss://x/realtime"
    return asyncio.run(
        tt_flow.collect_prints(auth, list(symbols),
                               window_secs=window, show_progress=False)
    )


# ── Handshake ─────────────────────────────────────────────────────────────────
def test_client_sends_setup_first(monkeypatch):
    """DXLink is client-initiated. Without this the server never replies."""
    ws = FakeWS(handshake())
    run(ws, monkeypatch)
    assert ws.types_sent()[0] == "SETUP", \
        f"first frame must be SETUP, got {ws.types_sent()[:1]}"


def test_setup_declares_a_version_and_keepalive_timeout(monkeypatch):
    ws = FakeWS(handshake())
    run(ws, monkeypatch)
    setup = ws.first("SETUP")
    assert setup["version"] == tt_flow.DXLINK_VERSION
    assert setup["keepaliveTimeout"] > 0


def test_auth_is_sent_with_the_quote_token(monkeypatch):
    ws = FakeWS(handshake())
    run(ws, monkeypatch)
    assert ws.first("AUTH")["token"] == "dx-tok"


def test_channel_and_subscription_follow_authorization(monkeypatch):
    ws = FakeWS(handshake())
    run(ws, monkeypatch)
    order = [t for t in ws.types_sent()
             if t in ("SETUP", "AUTH", "CHANNEL_REQUEST", "FEED_SUBSCRIPTION")]
    assert order[:4] == ["SETUP", "AUTH", "CHANNEL_REQUEST", "FEED_SUBSCRIPTION"]


def test_every_symbol_is_subscribed(monkeypatch):
    syms = [f".SPXW260907C{i}" for i in range(450)]   # forces batching
    ws = FakeWS(handshake())
    run(ws, monkeypatch, symbols=syms)
    subbed = [e["symbol"] for m in ws.sent if m.get("type") == "FEED_SUBSCRIPTION"
              for e in m["add"]]
    assert sorted(subbed) == sorted(syms)


def test_keepalive_is_answered(monkeypatch):
    ws = FakeWS(handshake([{"type": "KEEPALIVE", "channel": 0}]))
    run(ws, monkeypatch)
    assert "KEEPALIVE" in ws.types_sent()


# ── COMPACT decoding ──────────────────────────────────────────────────────────
def tas_row(sym, ts, price, size, side="Buy"):
    return [sym, ts, price, size, side, "C", "", False]


def test_a_print_is_decoded_from_a_compact_row(monkeypatch):
    ws = FakeWS(handshake([
        {"type": "FEED_DATA", "channel": 1,
         "data": ["TimeAndSale", tas_row(".SPXW260907C6500", 1_700_000_000_000, 4.25, 30)]},
    ]))
    out = run(ws, monkeypatch)
    prints = out.get(".SPXW260907C6500", [])
    assert len(prints) == 1
    p = prints[0]
    assert p.price == 4.25 and p.size == 30 and p.aggressor == "buy"
    assert p.premium == 4.25 * 30 * 100


def test_multiple_events_in_one_frame_are_split_by_the_delivered_width(monkeypatch):
    """
    The row width is whatever FEED_CONFIG delivered, not what we asked for.
    Slicing by the requested width misaligns every event after the first.
    """
    flat = (tas_row(".SPXW260907C6500", 1_700_000_000_000, 1.0, 5)
            + tas_row(".SPXW260907C6500", 1_700_000_000_001, 2.0, 7))
    ws = FakeWS(handshake([
        {"type": "FEED_DATA", "channel": 1, "data": ["TimeAndSale", flat]},
    ]))
    prints = run(ws, monkeypatch).get(".SPXW260907C6500", [])
    assert [(p.price, p.size) for p in prints] == [(1.0, 5), (2.0, 7)]


def test_identical_prints_are_deduped(monkeypatch):
    row = tas_row(".SPXW260907C6500", 1_700_000_000_000, 4.25, 30)
    ws = FakeWS(handshake([
        {"type": "FEED_DATA", "channel": 1, "data": ["TimeAndSale", row]},
        {"type": "FEED_DATA", "channel": 1, "data": ["TimeAndSale", row]},
    ]))
    assert len(run(ws, monkeypatch).get(".SPXW260907C6500", [])) == 1


def test_zero_size_prints_are_dropped(monkeypatch):
    ws = FakeWS(handshake([
        {"type": "FEED_DATA", "channel": 1,
         "data": ["TimeAndSale", tas_row(".SPXW260907C6500", 1, 4.25, 0)]},
    ]))
    assert run(ws, monkeypatch) == {}


def test_data_before_feed_config_is_not_decoded(monkeypatch):
    """Without the field layout the row cannot be read; guessing invents prices."""
    ws = FakeWS([
        {"type": "SETUP", "channel": 0},
        {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"},
        {"type": "CHANNEL_OPENED", "channel": 1},
        {"type": "FEED_DATA", "channel": 1,
         "data": ["TimeAndSale", tas_row(".SPXW260907C6500", 1, 4.25, 30)]},
    ])
    assert run(ws, monkeypatch) == {}


def test_sell_side_is_classified_bearish(monkeypatch):
    ws = FakeWS(handshake([
        {"type": "FEED_DATA", "channel": 1,
         "data": ["TimeAndSale", tas_row(".SPXW260907C6500", 1, 4.25, 30, "Sell")]},
    ]))
    p = run(ws, monkeypatch)[".SPXW260907C6500"][0]
    assert p.aggressor == "sell"


def test_a_malformed_row_does_not_kill_the_scan(monkeypatch):
    ws = FakeWS(handshake([
        {"type": "FEED_DATA", "channel": 1, "data": ["TimeAndSale", ["junk"]]},
        {"type": "FEED_DATA", "channel": 1,
         "data": ["TimeAndSale", tas_row(".SPXW260907C6500", 1, 4.25, 30)]},
    ]))
    assert len(run(ws, monkeypatch).get(".SPXW260907C6500", [])) == 1


# ── FEED_CONFIG shapes ────────────────────────────────────────────────────────
def test_the_documented_list_shape_is_still_accepted(monkeypatch):
    ws = FakeWS([
        {"type": "SETUP", "channel": 0},
        {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"},
        {"type": "CHANNEL_OPENED", "channel": 1},
        feed_config_list_form(),
        {"type": "FEED_DATA", "channel": 1,
         "data": ["TimeAndSale", tas_row(".SPXW260907C6500", 1, 4.25, 30)]},
    ])
    assert len(run(ws, monkeypatch).get(".SPXW260907C6500", [])) == 1


def test_an_ack_config_without_fields_does_not_start_the_window(monkeypatch):
    """
    The server sends a bare FEED_CONFIG acknowledging setup, then a second with
    the fields. Accepting the first burns the collection window on an empty
    field layout, so every print that follows is discarded.
    """
    ws = FakeWS([
        {"type": "SETUP", "channel": 0},
        {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"},
        {"type": "CHANNEL_OPENED", "channel": 1},
        {"type": "FEED_CONFIG", "channel": 1, "dataFormat": "COMPACT"},   # no eventFields
        feed_config(),
        {"type": "FEED_DATA", "channel": 1,
         "data": ["TimeAndSale", tas_row(".SPXW260907C6500", 1, 4.25, 30)]},
    ])
    assert len(run(ws, monkeypatch).get(".SPXW260907C6500", [])) == 1
