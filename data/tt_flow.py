#!/usr/bin/env python3
"""
tt_flow.py — Real options flow scanner via Tastytrade + dxFeed TimeAndSale streaming.

Why this beats yfinance:
  - yfinance: 15-min delayed daily snapshot (vol * stale_mid * 100 = fake flow)
  - This:     individual trade prints with exact price, size, exchange, and real
              aggressor_side (Buy/Sell from the exchange — no Lee-Ready guessing)

Data source: Tastytrade API → dxFeed OPRA feed
Credentials: env vars TT_USERNAME / TT_PASSWORD, or ~/.tt_creds.json
             A password login triggers a device challenge answered by an SMS
             code. Set TT_OTP to supply it non-interactively; otherwise the
             scan prompts. The session is cached ~8h in ~/.tt_session.json,
             so one code covers a trading day.
Check auth:  python3 -m data.tt_flow --check
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import math
from collections import defaultdict
import datetime as _dt
from datetime import datetime, date
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
from typing import List, Dict, Optional, Any

import httpx
from httpx_ws import aconnect_ws
import yfinance as yf

# ── Constants ─────────────────────────────────────────────────────────────────
TT_API         = "https://api.tastytrade.com"
DXLINK_VERSION = "0.1-DXF-JS/23.11.0"

# Sweep detection. OPRA marks Intermarket Sweep Orders with condition 'I', but
# ISO is ubiquitous rather than rare: measured live on 2026-09-08, 73% of SPY
# 0DTE prints carried it, and treating any ISO print as a sweep flagged 72% of
# contracts -- a badge that fires on three contracts in four says nothing.
# What the term actually denotes is one order split across exchanges and filled
# in rapid succession, so detection requires ISO prints *clustered in time*.
# At these thresholds the same sample flags 20%.
SWEEP_MIN_ISO_PRINTS = 5
SWEEP_WINDOW_MS      = 500

# TimeAndSale fields in the order model_fields defines them (must match from_stream)
TAS_FIELDS = [
    "eventSymbol", "eventTime",
    "index", "time", "timeNanoPart", "sequence", "exchangeCode",
    "price", "size", "bidPrice", "askPrice",
    "exchangeSaleConditions", "tradeThroughExempt",
    "aggressorSide", "spreadLeg", "extendedTradingHours", "validTick",
    "type", "buyer", "seller",
]


# ── Failure reporting ─────────────────────────────────────────────────────────
# A scan that returns [] is ambiguous: the market may simply be shut, or the
# session may have failed to authenticate. Callers label their data from this,
# so the difference has to survive the return.
_LAST_ERROR: str = ""


def last_error() -> str:
    """Why the most recent scan produced nothing, or "" if nothing went wrong."""
    return _LAST_ERROR


def _set_error(msg: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = msg


# ── Credentials ───────────────────────────────────────────────────────────────
def load_credentials() -> tuple[str, str]:
    """
    Load tastytrade credentials.
    Priority: env vars → ~/.tt_creds.json → scanner-dir/.tt_creds.json
    """
    u = os.environ.get("TT_USERNAME", "")
    p = os.environ.get("TT_PASSWORD", "")
    if u and p:
        return u, p

    for path in [
        os.path.expanduser("~/.tt_creds.json"),
        os.path.join(os.path.dirname(__file__), ".tt_creds.json"),
    ]:
        if os.path.exists(path):
            try:
                data = json.loads(open(path).read())
                return data.get("username", ""), data.get("password", "")
            except Exception:
                pass

    return "", ""


def save_credentials(username: str, password: str, path: Optional[str] = None) -> None:
    """Save credentials to ~/.tt_creds.json (outside project directory)."""
    if path is None:
        path = os.path.expanduser("~/.tt_creds.json")
    # Mode is set at creation: writing first and chmod'ing after leaves the
    # password world-readable for the moments in between.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"username": username, "password": password}, f)
    os.chmod(path, 0o600)  # owner read/write only


# ── Auth client ───────────────────────────────────────────────────────────────
# TastyTrade's device challenge is a three-step handshake, verified against the
# live API on 2026-09-06:
#
#   1. POST /sessions          -> 403 device_challenge_required. The challenge
#                                 token is in the `x-tastyworks-challenge-token`
#                                 RESPONSE HEADER -- it is issued, not supplied.
#   2. POST /device-challenge  -> this is what sends the OTP, by SMS. Until this
#                                 call is made, the user receives nothing.
#   3. POST /sessions          -> with X-Tastyworks-Challenge-Token and
#                                 X-Tastyworks-OTP, returns 201 + session-token.
#
# TastyTrade issues no remember-token on this account: `remember-me: true` is
# ignored and the session simply expires (~8h). So the session token is cached
# to disk with its expiry and one OTP covers a trading day across processes.
#
# A host with no TTY (Railway) cannot answer step 3 and must fail fast rather
# than block. Live flow in prod needs TastyTrade's OAuth flow, not this one.
_SESSION_PATH = os.path.expanduser("~/.tt_session.json")

# Re-authenticate this many seconds before the stated expiry. A token that dies
# mid-scan is worse than one already known to be dead.
_SESSION_MARGIN_S = 300


def _iso_from_epoch(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _epoch_from_iso(s: str) -> float:
    """Parse TastyTrade's expiry. Returns 0.0 on anything unparseable."""
    try:
        return _dt.datetime.fromisoformat(
            s.strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _save_session(token: str, expires_at: str) -> None:
    """Persist the session token 0600. Never holds the password."""
    if not token:
        return
    try:
        # Create with the right mode from the start; writing then chmod'ing
        # leaves the token world-readable for the moments in between.
        fd = os.open(_SESSION_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"session-token": token, "expires-at": expires_at}, f)
        os.chmod(_SESSION_PATH, 0o600)
    except Exception:
        pass


def _load_session() -> tuple[str, str]:
    """
    The cached session, or ("", "") when there is none worth using.

    A session with no stated expiry is discarded: unknown age is indistinguishable
    from expired, and retrying with a dead token costs a request and an OTP.
    """
    try:
        d = json.loads(open(_SESSION_PATH).read())
    except Exception:
        return "", ""
    tok = d.get("session-token", "")
    exp = d.get("expires-at", "")
    if not tok or not exp:
        return "", ""
    if _epoch_from_iso(exp) - _SESSION_MARGIN_S <= time.time():
        return "", ""
    return tok, exp


def _clear_session() -> None:
    try:
        if os.path.exists(_SESSION_PATH):
            os.remove(_SESSION_PATH)
    except Exception:
        pass


def _prompt_for_otp(phone: str) -> str:
    """Ask for the texted code. Separated so tests never block on stdin."""
    where = f" sent to {phone}" if phone else ""
    try:
        return input(f"TastyTrade code{where}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


class TTAuth:
    """
    Handles tastytrade authentication.
    Cached session first, then password + SMS device challenge.
    Then gets dxFeed streaming credentials from /api-quote-tokens.
    """

    def __init__(self, username: str, password: str):
        self.username    = username
        self.password    = password
        self.session_tok = ""
        self.dx_token    = ""
        self.dx_url      = ""

    def _headers(self) -> dict:
        return {
            "Authorization": self.session_tok,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    async def _post_session(self, payload: dict, challenge_token: str = "",
                            otp: str = "") -> Any:
        headers = {"Content-Type": "application/json"}
        if challenge_token:
            headers["X-Tastyworks-Challenge-Token"] = challenge_token
        if otp:
            headers["X-Tastyworks-OTP"] = otp
        async with httpx.AsyncClient(base_url=TT_API, timeout=15) as c:
            return await c.post("/sessions", json=payload, headers=headers)

    async def _post_device_challenge(self, challenge_token: str) -> Any:
        async with httpx.AsyncClient(base_url=TT_API, timeout=15) as c:
            return await c.post(
                "/device-challenge",
                headers={"X-Tastyworks-Challenge-Token": challenge_token,
                         "Content-Type": "application/json"},
            )

    def _consume(self, r) -> bool:
        """Record and persist the session from a 2xx /sessions."""
        data = r.json().get("data", {})
        self.session_tok = data.get("session-token", "")
        if not self.session_tok:
            return False
        _save_session(self.session_tok, data.get("session-expiration", ""))
        return True

    async def login(self) -> bool:
        """Cached session first, password + device challenge second."""
        tok, _ = _load_session()
        if tok:
            self.session_tok = tok
            return True
        return await self._password_login()

    async def _password_login(self) -> bool:
        if not self.password:
            _set_error("no password — set TT_PASSWORD")
            return False

        payload = {"login": self.username, "password": self.password,
                   "remember-me": True}
        r = await self._post_session(payload)

        if r.status_code == 403:
            try:
                code = r.json().get("error", {}).get("code")
            except Exception:
                code = None
            if code != "device_challenge_required":
                _set_error(f"login rejected (HTTP 403{f', {code}' if code else ''})")
                return False
            return await self._device_challenge(payload, r)

        if r.status_code not in (200, 201):
            _set_error(f"login rejected (HTTP {r.status_code})")
            return False
        if not self._consume(r):
            _set_error("login returned no session-token")
            return False
        return True

    async def _device_challenge(self, payload: dict, r403) -> bool:
        """Steps 2 and 3: trigger the SMS, then retry with the code."""
        challenge_token = ""
        try:
            challenge_token = r403.headers.get("x-tastyworks-challenge-token", "")
        except Exception:
            pass
        if not challenge_token:
            _set_error("device challenge required but no challenge token was "
                       "returned in the response header")
            return False

        # Nothing reaches the user until this call is made.
        c = await self._post_device_challenge(challenge_token)
        if c.status_code != 200:
            _set_error(f"device-challenge failed (HTTP {c.status_code})")
            return False
        try:
            phone = c.json().get("data", {}).get("phone", "")
        except Exception:
            phone = ""

        otp = os.environ.get("TT_OTP", "").strip()
        if not otp:
            if not sys.stdin.isatty():
                _set_error(
                    f"device challenge sent an OTP by SMS to {phone or 'your phone'}, "
                    "but this host is non-interactive (no tty) so it cannot be "
                    "entered. Set TT_OTP, or run the scan from a terminal."
                )
                return False
            print(f"TTAuth: TastyTrade texted a code to {phone or 'your phone'}.")
            otp = _prompt_for_otp(phone)
        if not otp:
            _set_error("no OTP supplied — device challenge cannot be completed")
            return False

        # One retry only. Re-prompting on rejection would loop against an SMS
        # the user has to wait for anyway.
        r = await self._post_session(payload, challenge_token=challenge_token,
                                     otp=otp)
        if r.status_code not in (200, 201):
            _set_error(f"OTP rejected (HTTP {r.status_code})")
            return False
        if not self._consume(r):
            _set_error("login returned no session-token")
            return False
        return True

    async def get_quote_tokens(self) -> bool:
        async with httpx.AsyncClient(base_url=TT_API, timeout=15) as c:
            r = await c.get("/api-quote-tokens", headers=self._headers())
            if r.status_code == 401:
                # The cached session died early. Drop it so the next attempt
                # re-authenticates instead of failing the same way forever.
                _clear_session()
                _set_error("session expired — re-run to authenticate again")
                return False
            if r.status_code != 200:
                _set_error(f"quote tokens unavailable (HTTP {r.status_code})")
                return False
            data = r.json().get("data", {})
            self.dx_token = data.get("token", "")
            self.dx_url   = data.get("dxlink-url", "")
            self.dx_level = data.get("level", "")
            return bool(self.dx_token and self.dx_url)

    def is_delayed(self) -> bool:
        """
        True when the account is not entitled to real-time OPRA. An unfunded
        account gets `level: demo` on a `/delayed` endpoint, which is no better
        than the yfinance fallback -- callers must not label that live.
        """
        lvl = (getattr(self, "dx_level", "") or "").lower()
        url = (self.dx_url or "").lower()
        return "demo" in lvl or "delayed" in url or "demo" in url

    async def setup(self) -> bool:
        return await self.login() and await self.get_quote_tokens()

# ── Option chain fetcher ──────────────────────────────────────────────────────
async def fetch_chain(auth: TTAuth, ticker: str, max_dte: int = 14) -> List[Dict]:
    """
    Fetch option chain contracts for ticker (≤ max_dte days to expiry).
    Returns list of dicts with: ticker, exp, dte, strike, type, streamer_symbol.
    """
    # DTE is measured against the exchange's date. date.today() is the host's,
    # and Railway runs UTC — after 17:00 ET every contract would be labelled one
    # day closer to expiry than it is.
    today = _dt.datetime.now(_ET).date()
    contracts = []

    async with httpx.AsyncClient(base_url=TT_API, timeout=20) as c:
        r = await c.get(
            f"/option-chains/{ticker}/nested",
            headers=auth._headers(),
        )
        if r.status_code != 200:
            return []

        items = r.json().get("data", {}).get("items", [])
        expirations = items[0].get("expirations", []) if items else []
        for exp_data in expirations:
            exp_str  = exp_data.get("expiration-date", "")
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0 or dte > max_dte:
                continue

            for strike_data in exp_data.get("strikes", []):
                try:
                    strike = float(strike_data.get("strike-price", 0))
                except (ValueError, TypeError):
                    continue

                for otype, sym_key in [("call", "call-streamer-symbol"),
                                        ("put",  "put-streamer-symbol")]:
                    sym = strike_data.get(sym_key, "")
                    if sym:
                        contracts.append({
                            "ticker":          ticker,
                            "exp":             exp_str,
                            "dte":             dte,
                            "strike":          strike,
                            "type":            otype,
                            "streamer_symbol": sym,
                        })

    return contracts


# ── dxFeed WebSocket collector ────────────────────────────────────────────────
class DXPrint:
    """One individual TimeAndSale trade print."""
    __slots__ = (
        "symbol", "price", "size", "bid", "ask",
        "aggressor", "exchange", "conditions",
        "spread_leg", "ts",
    )

    def __init__(self, symbol, price, size, bid, ask,
                 aggressor, exchange, conditions, spread_leg, ts):
        self.symbol     = symbol
        self.price      = float(price or 0)
        self.size       = int(size or 0)
        self.bid        = float(bid or 0)
        self.ask        = float(ask or 0)
        self.aggressor  = str(aggressor or "").lower()   # "buy", "sell", "none"
        self.exchange   = str(exchange or "")
        self.conditions = str(conditions or "")
        self.spread_leg = bool(spread_leg)
        self.ts         = int(ts or 0)   # unix ms

    @property
    def premium(self) -> float:
        return self.price * self.size * 100


async def collect_prints(
    auth: TTAuth,
    symbols: List[str],
    window_secs: int = 90,
    show_progress: bool = True,
) -> Dict[str, List[DXPrint]]:
    """
    Open a dxFeed DXLink WebSocket, subscribe to TimeAndSale for all symbols,
    collect prints for window_secs seconds, return prints by streamer_symbol.
    """
    prints: Dict[str, List[DXPrint]] = defaultdict(list)
    seen_prints: set = set()   # dedup: (symbol, ts, price, size)
    field_index: Optional[Dict[str, int]] = None
    channel_ready = False
    collecting    = False
    collect_start = 0.0

    # Track field positions delivered by FEED_CONFIG
    def parse_feed_config(data: dict, event: str = "TimeAndSale") -> Dict[str, int]:
        """
        Field positions for `event`, from whichever FEED_CONFIG shape arrives.

        dxLink sends a mapping -- {"TimeAndSale": ["eventSymbol", ...]} -- which
        is what the live feed was observed to send on 2026-09-07. The list form
        [{"eventType": ..., "eventFieldsList": [...]}] appears in dxFeed's own
        docs and older servers. Iterating the mapping as a list yields strings
        and raises AttributeError on `.get`, which would kill the scan mid-way,
        so both are handled rather than guessed at.
        """
        fields = data.get("eventFields")
        names = []
        if isinstance(fields, dict):
            names = fields.get(event) or []
        elif isinstance(fields, list):
            for item in fields:
                if isinstance(item, dict) and item.get("eventType") == event:
                    names = item.get("eventFieldsList", []) or []
                    break
        return {f: i for i, f in enumerate(names)}

    def parse_compact_tas(values: list, fmap: Dict[str, int]) -> Optional[DXPrint]:
        try:
            def g(name, default=None):
                idx = fmap.get(name)
                return values[idx] if idx is not None and idx < len(values) else default

            size = int(g("size", 0) or 0)
            if size <= 0:
                return None
            price = float(g("price", 0) or 0)
            if price <= 0:
                return None
            return DXPrint(
                symbol     = g("eventSymbol", ""),
                price      = price,
                size       = size,
                bid        = g("bidPrice", 0),
                ask        = g("askPrice", 0),
                aggressor  = g("aggressorSide", ""),
                exchange   = g("exchangeCode", ""),
                conditions = g("exchangeSaleConditions", ""),
                spread_leg = bool(g("spreadLeg", False)),
                ts         = int(g("time", 0) or 0),
            )
        except Exception:
            return None

    SETUP_DONE, AUTH_DONE = False, False

    async with httpx.AsyncClient(timeout=None) as http_client:
        async with aconnect_ws(auth.dx_url, client=http_client) as ws:

            # DXLink is client-initiated: the server says nothing until it
            # receives a SETUP. Waiting for one instead of sending it meant the
            # handshake never started, the 30s deadline expired, and the scan
            # returned {} -- indistinguishable from a quiet tape.
            await ws.send_json({
                "type": "SETUP", "channel": 0,
                "version": DXLINK_VERSION,
                "keepaliveTimeout": 60,
                "acceptKeepaliveTimeout": 60,
            })

            setup_deadline = time.time() + 30  # 30s to complete handshake
            collect_deadline = 0.0             # set once collection starts
            # Both sides must speak within keepaliveTimeout. Answering the
            # server's KEEPALIVE is not enough on a long window: send our own
            # while collecting, well inside the 60s the SETUP negotiated.
            next_keepalive = time.time() + 25

            while True:
                now = time.time()
                # Abort if setup takes too long
                if not collecting and now > setup_deadline:
                    break
                # Stop after collection window
                if collecting and now >= collect_deadline:
                    break
                if now >= next_keepalive:
                    next_keepalive = now + 25
                    try:
                        await ws.send_json({"type": "KEEPALIVE", "channel": 0})
                    except Exception:
                        break

                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue   # deadline checks at top of while loop handle exits
                except Exception:
                    break

                mtype = msg.get("type")

                if mtype == "SETUP" and not SETUP_DONE:
                    SETUP_DONE = True
                    await ws.send_json({
                        "type": "AUTH", "channel": 0,
                        "token": auth.dx_token,
                    })

                elif mtype == "AUTH_STATE":
                    if msg.get("state") == "AUTHORIZED" and not AUTH_DONE:
                        AUTH_DONE = True
                        await ws.send_json({
                            "type": "CHANNEL_REQUEST", "channel": 1,
                            "service": "FEED",
                            "parameters": {"contract": "AUTO"},
                        })

                elif mtype == "CHANNEL_OPENED" and msg.get("channel") == 1:
                    await ws.send_json({
                        "type": "FEED_SETUP", "channel": 1,
                        "acceptAggregationPeriod": 0,
                        "acceptDataFormat": "COMPACT",
                        "acceptEventFields": {"TimeAndSale": TAS_FIELDS},
                    })
                    # Subscribe in batches of 200
                    batch_size = 200
                    for i in range(0, len(symbols), batch_size):
                        batch = symbols[i : i + batch_size]
                        await ws.send_json({
                            "type": "FEED_SUBSCRIPTION", "channel": 1,
                            "add": [{"type": "TimeAndSale", "symbol": s} for s in batch],
                        })
                    channel_ready = True
                    if show_progress:
                        print(f"  [TT] Subscribed to {len(symbols)} contracts — collecting {window_secs}s...",
                              flush=True)

                elif mtype == "FEED_CONFIG" and channel_ready:
                    # The server sends one FEED_CONFIG acknowledging the setup and
                    # a second carrying eventFields. Starting the window on the
                    # first would burn the collection time with no field layout,
                    # so an empty parse is ignored rather than accepted.
                    parsed = parse_feed_config(msg)
                    if not parsed:
                        continue
                    field_index = parsed
                    # Start collection timer once we know the field layout
                    if not collecting:
                        collecting       = True
                        collect_start    = time.time()
                        collect_deadline = collect_start + window_secs

                elif mtype == "FEED_DATA" and collecting:
                    raw_data = msg.get("data", [])
                    if not raw_data or field_index is None:
                        continue

                    # DXLink COMPACT: interleaved pairs of TypeName + values list
                    # ["TimeAndSale", [v,v,...], "TimeAndSale", [v,v,...], ...]
                    # Row width is whatever FEED_CONFIG actually delivered. The
                    # server may not honour acceptEventFields exactly, and slicing
                    # by the *requested* width silently misaligns every field of
                    # every event after the first — wrong prices, wrong sizes, no
                    # error anywhere.
                    n_fields = len(field_index)
                    if n_fields == 0:
                        continue
                    i = 0
                    while i < len(raw_data) - 1:
                        type_name   = raw_data[i]
                        values_flat = raw_data[i + 1]
                        i += 2
                        if not isinstance(type_name, str) or type_name != "TimeAndSale":
                            continue
                        if not isinstance(values_flat, list):
                            continue
                        n_events = len(values_flat) // n_fields
                        for j in range(n_events):
                            row = values_flat[j * n_fields : (j + 1) * n_fields]
                            p = parse_compact_tas(row, field_index)
                            if p and p.size > 0:
                                dedup_key = (p.symbol, p.ts, p.price, p.size)
                                if dedup_key not in seen_prints:
                                    seen_prints.add(dedup_key)
                                    prints[p.symbol].append(p)

                elif mtype == "KEEPALIVE":
                    await ws.send_json({"type": "KEEPALIVE", "channel": 0})

    return dict(prints)


# ── Flow aggregation ──────────────────────────────────────────────────────────
def _classify_side(prints: List[DXPrint]) -> str:
    """
    Classify dominant trade side from real aggressor_side values.
    'buy' = buyer hit ask = bullish. 'sell' = seller hit bid = bearish.
    """
    buy_vol  = sum(p.size for p in prints if p.aggressor == "buy")
    sell_vol = sum(p.size for p in prints if p.aggressor == "sell")
    total    = buy_vol + sell_vol
    if total == 0:
        return "mid"
    if buy_vol / total >= 0.65:
        return "ask"
    if sell_vol / total >= 0.65:
        return "bid"
    return "mid"


def _is_sweep(prints: List[DXPrint]) -> bool:
    """
    True when ISO prints cluster in time: one order worked across exchanges.

    Both halves of the previous rule were broken. It returned True for any
    single print carrying OPRA's 'I' (ISO) condition, which fires on 73% of
    prints, so 72% of contracts were flagged. And its time check compared the
    first and last print across the whole collection window, so requiring ten
    prints inside 500ms could only pass if the contract traded exactly ten
    times in the entire scan -- measured at 0% on live data. It also tested for
    condition 'F', which never appears.

    A sliding window is what the docstring always claimed to do.
    """
    times = sorted(p.ts for p in prints
                   if p.ts > 0 and "I" in (p.conditions or ""))
    n = SWEEP_MIN_ISO_PRINTS
    if len(times) < n:
        return False
    return any(times[i + n - 1] - times[i] < SWEEP_WINDOW_MS
               for i in range(len(times) - n + 1))


def aggregate_flow(
    raw_prints:         Dict[str, List[DXPrint]],
    symbol_to_contract: Dict[str, Dict],
    tickers:            List[str],
) -> List[Dict]:
    """
    Aggregate individual TimeAndSale prints into per-ticker flow signal dicts.
    Returns same format as scan_options_flow() in scanner.py.
    """
    ticker_buckets: Dict[str, Dict] = {
        t: {
            "call_flow": 0.0, "put_flow": 0.0,
            "call_contracts": [], "put_contracts": [],
            "dte0_flow": 0.0, "dte1_7_flow": 0.0, "dte8p_flow": 0.0,
            "all_strikes": [],
        }
        for t in tickers
    }

    for symbol, symbol_prints in raw_prints.items():
        contract = symbol_to_contract.get(symbol)
        if not contract:
            continue
        ticker = contract["ticker"]
        if ticker not in ticker_buckets:
            continue

        # Filter out spread legs (multi-leg orders obscure true directional flow)
        all_spread = all(p.spread_leg for p in symbol_prints if p.size > 0)
        clean = [p for p in symbol_prints if not p.spread_leg and p.size > 0]
        if not clean:
            if not all_spread:
                continue   # no valid prints at all
            clean = symbol_prints   # all spread legs — keep but mark as spread position

        total_vol     = sum(p.size for p in clean)
        total_premium = sum(p.premium for p in clean)
        if total_vol == 0 or total_premium < 5_000:
            continue

        otype  = contract["type"]
        strike = contract["strike"]
        exp    = contract["exp"]
        dte    = contract["dte"]

        trade_side = _classify_side(clean)
        is_sweep   = _is_sweep(clean)

        # Golden sweep: single print > $100K + buyer aggressive + not a spread roll
        max_single_premium = max(p.premium for p in clean)
        is_golden = (max_single_premium >= 100_000 and trade_side == "ask"
                     and not all_spread)

        # Tier from total premium
        if total_premium >= 1_000_000:   tier = "whale"
        elif total_premium >= 500_000:   tier = "block"
        elif total_premium >= 100_000:   tier = "institutional"
        else:                            tier = "retail"

        # Best mid price (weighted average of prints)
        avg_price = total_premium / (total_vol * 100)

        entry = {
            "ticker":       ticker,
            "exp":          exp,
            "dte":          dte,
            "strike":       strike,
            "type":         otype,
            "vol":          total_vol,
            "oi":           0,           # OI not in T&S; fetch separately if needed
            "vol_oi":       0.0,
            "mid":          round(avg_price, 2),
            "flow":         round(total_premium, 0),
            "sweep":        is_sweep,
            "golden_sweep": is_golden,
            "trade_side":   trade_side,
            "premium_tier": tier,
            # Extra fields only available from TT (not in yfinance flow)
            "n_prints":     len(clean),
            "max_print":    round(max_single_premium, 0),
        }

        tb = ticker_buckets[ticker]
        tb["all_strikes"].append(strike)

        if otype == "call":
            tb["call_flow"] += total_premium
            tb["call_contracts"].append(entry)
        else:
            tb["put_flow"] += total_premium
            tb["put_contracts"].append(entry)

        if dte == 0:       tb["dte0_flow"]   += total_premium
        elif dte <= 7:     tb["dte1_7_flow"] += total_premium
        else:              tb["dte8p_flow"]  += total_premium

    # Build per-ticker signals
    flow_signals = []
    for ticker, tb in ticker_buckets.items():
        total_flow = tb["call_flow"] + tb["put_flow"]
        if total_flow < 10_000:
            continue

        bias     = "call" if tb["call_flow"] >= tb["put_flow"] else "put"
        top_call = max(tb["call_contracts"], key=lambda x: x["flow"], default=None)
        top_put  = max(tb["put_contracts"],  key=lambda x: x["flow"], default=None)
        top_contract   = top_call if bias == "call" else top_put

        unique_strikes = len(set(round(s, 0) for s in tb["all_strikes"]))
        stacked_flow   = unique_strikes >= 3
        golden_sweep   = any(
            c.get("golden_sweep")
            for c in tb["call_contracts"] + tb["put_contracts"]
        )
        dom_side = top_contract.get("trade_side", "mid") if top_contract else "mid"
        top_tier = top_contract.get("premium_tier", "retail") if top_contract else "retail"
        pc_ratio = tb["put_flow"] / tb["call_flow"] if tb["call_flow"] > 0 else 999.0

        signal = {
            "ticker":         ticker,
            "call_flow":      tb["call_flow"],
            "put_flow":       tb["put_flow"],
            "total_flow":     total_flow,
            "flow_bias":      bias,
            "pc_ratio":       pc_ratio,
            "top_call":       top_call,
            "top_put":        top_put,
            "top_contract":   top_contract,
            "call_contracts": tb["call_contracts"],
            "put_contracts":  tb["put_contracts"],
            "trade_side":     dom_side,
            "iv_skew":        0.0,   # available if we also stream Quote events
            "stacked_flow":   stacked_flow,
            "unique_strikes": unique_strikes,
            "golden_sweep":   golden_sweep,
            "premium_tier":   top_tier,
            "dte0_flow":      tb["dte0_flow"],
            "dte1_7_flow":    tb["dte1_7_flow"],
            "dte8p_flow":     tb["dte8p_flow"],
            "whale_score":    0,
        }
        signal["whale_score"] = _calc_whale_score(signal)
        flow_signals.append(signal)

    flow_signals.sort(key=lambda x: (x["whale_score"], x["total_flow"]), reverse=True)
    return flow_signals


def _calc_whale_score(signal: Dict) -> int:
    """Inlined to avoid circular import with scanner.py."""
    score = 0
    if signal.get("trade_side") == "ask":
        score += 25
    flow = signal.get("total_flow", 0)
    if flow >= 1_000_000:    score += 30
    elif flow >= 500_000:    score += 20
    elif flow >= 100_000:    score += 10
    if signal.get("golden_sweep"):   score += 20
    if signal.get("stacked_flow"):   score += 15
    tc = signal.get("top_contract")
    if tc:
        score += min(10, int(tc.get("vol_oi", 0)))
    return min(100, max(0, score))


# ── Session cache ─────────────────────────────────────────────────────────────
# `--live` re-scans every 45s. Authenticating per scan is ~80 POST /sessions an
# hour, which is how an account earns a rate-limit and a fresh device challenge.
# Sessions are good for hours, so one is held for the process and reused.
_AUTH_CACHE:    Optional[TTAuth] = None
_AUTH_EXPIRES:  float = 0.0
_AUTH_TTL_SECS: int   = 20 * 60


def invalidate_auth() -> None:
    """Force the next scan to re-authenticate."""
    global _AUTH_CACHE, _AUTH_EXPIRES
    _AUTH_CACHE, _AUTH_EXPIRES = None, 0.0


async def _get_auth(username: str, password: str) -> Optional[TTAuth]:
    global _AUTH_CACHE, _AUTH_EXPIRES
    now = time.monotonic()
    if (_AUTH_CACHE is not None and now < _AUTH_EXPIRES
            and _AUTH_CACHE.username == username):
        return _AUTH_CACHE

    auth = TTAuth(username, password)
    if not await auth.setup():
        invalidate_auth()
        return None
    _AUTH_CACHE, _AUTH_EXPIRES = auth, now + _AUTH_TTL_SECS
    return auth


# ── Main async scanner ────────────────────────────────────────────────────────
async def _async_scan(
    tickers:      List[str],
    username:     str,
    password:     str,
    window_secs:  int  = 90,
    max_dte:      int  = 14,
    moneyness:    float = 0.15,   # ±15% of spot price
    show_progress: bool = True,
) -> List[Dict]:
    _set_error("")

    if show_progress:
        print("  [TT] Authenticating... ", end="", flush=True)

    auth = await _get_auth(username, password)
    if auth is None:
        if not _LAST_ERROR:
            _set_error("authentication failed — check TT_USERNAME / TT_PASSWORD")
        if show_progress:
            print(f"FAILED — {_LAST_ERROR}")
        return []

    if show_progress:
        print(f"OK | Fetching chains for {len(tickers)} tickers...", flush=True)

    # Fetch chains in parallel
    async def chain_task(ticker):
        try:
            return await fetch_chain(auth, ticker, max_dte=max_dte)
        except Exception:
            return []

    results = await asyncio.gather(*[chain_task(t) for t in tickers])

    # Filter near-money strikes using current yfinance price
    symbol_to_contract: Dict[str, Dict] = {}
    all_symbols: List[str] = []

    for ticker, contracts in zip(tickers, results):
        try:
            spot = float(yf.Ticker(ticker).fast_info.last_price or 0)
        except Exception:
            spot = 0.0

        for c in contracts:
            if spot > 0:
                lo = spot * (1 - moneyness)
                hi = spot * (1 + moneyness)
                if not (lo <= c["strike"] <= hi):
                    continue
            sym = c["streamer_symbol"]
            if sym:
                all_symbols.append(sym)
                symbol_to_contract[sym] = c

    if not all_symbols:
        _set_error("no near-money contracts returned by the chain endpoint")
        if show_progress:
            print("  [TT] No symbols to subscribe to — check chain fetch.")
        return []

    if show_progress:
        print(f"  [TT] {len(all_symbols)} contracts across {len(tickers)} tickers", flush=True)

    raw = await collect_prints(auth, all_symbols, window_secs=window_secs,
                               show_progress=show_progress)

    n_prints = sum(len(v) for v in raw.values())
    if show_progress:
        print(f"  [TT] Collected {n_prints} prints from {len(raw)} contracts", flush=True)

    return aggregate_flow(raw, symbol_to_contract, tickers)


def scan_options_flow_tt(
    tickers:      List[str],
    username:     str = "",
    password:     str = "",
    window_secs:  int = 90,
    max_dte:      int = 14,
    show_progress: bool = True,
) -> List[Dict]:
    """
    Sync entry point. Drop-in replacement for scanner.scan_options_flow().
    Falls back to [] on auth failure so the rest of the scanner still works.
    """
    u = username or os.environ.get("TT_USERNAME", "")
    p = password or os.environ.get("TT_PASSWORD", "")
    if not u or not p:
        u, p = load_credentials()
    if not u or not p:
        _set_error("no credentials — set TT_USERNAME / TT_PASSWORD")
        if show_progress:
            print("  [TT] No credentials found — set TT_USERNAME / TT_PASSWORD")
        return []

    try:
        return asyncio.run(
            _async_scan(tickers, u, p,
                        window_secs=window_secs,
                        max_dte=max_dte,
                        show_progress=show_progress)
        )
    except Exception as e:
        _set_error(f"{type(e).__name__}: {e}")
        if show_progress:
            print(f"  [TT] Error: {e}")
        return []


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]

    # `--check` answers the only question that matters when the FLOW tab says
    # DELAYED: did the session authenticate, and did dxFeed hand back a feed?
    if "--check" in argv:
        u, p = load_credentials()
        print(f"username         : {u or '(unset)'}")
        print(f"password         : {'set' if p else '(unset)'}")
        _tok, _exp = _load_session()
        print(f"cached session   : {'valid until ' + _exp if _tok else '(none)'}")
        if not u:
            print("\nresult           : FAIL — no username; set TT_USERNAME")
            sys.exit(1)

        auth = TTAuth(u, p)
        ok = asyncio.run(auth.setup())
        print(f"session-token    : {'ok' if auth.session_tok else 'FAILED'}")
        print(f"dxlink feed      : {auth.dx_url or 'FAILED'}")
        print(f"data level       : {getattr(auth, 'dx_level', '') or '(unknown)'}")
        if ok and auth.is_delayed():
            print("\nresult           : DELAYED — account is not entitled to "
                  "real-time OPRA.\n                   Fund the tastytrade "
                  "account (any amount) to restore it;\n                   "
                  "unfunded accounts get 14 days, then drop to demo.")
            sys.exit(1)
        if ok:
            print("\nresult           : LIVE — flow will stream from OPRA")
            sys.exit(0)
        print(f"\nresult           : FAIL — {last_error() or 'unknown'}")
        sys.exit(1)

    tickers = argv or ["SPY", "QQQ", "NVDA", "TSLA"]
    u, p = load_credentials()
    if not u:
        import getpass
        u = input("Tastytrade username: ").strip()
        p = getpass.getpass("Tastytrade password: ").strip()
        save = input("Save credentials? [y/N]: ").strip().lower()
        if save == "y":
            save_credentials(u, p)

    print(f"\nScanning: {tickers}")
    signals = scan_options_flow_tt(tickers, u, p, window_secs=60)

    if not signals:
        print("No flow signals detected.")
    else:
        for s in signals:
            tc = s.get("top_contract")
            tc_str = ""
            if tc:
                tc_str = (f"  TOP: {tc['exp'][5:]} ${tc['strike']:.0f}"
                          f"{'C' if tc['type']=='call' else 'P'}"
                          f"  {tc['n_prints']}prints  ${tc['mid']:.2f}"
                          f"  ${tc['flow']/1e3:.0f}K  [{tc['trade_side'].upper()}]"
                          f"{'  GOLDEN' if tc.get('golden_sweep') else ''}"
                          f"{'  SWEEP' if tc.get('sweep') else ''}")
            print(
                f"{s['ticker']:<6}  "
                f"Score:{s['whale_score']:3d}  "
                f"Total:${s['total_flow']/1e3:.0f}K  "
                f"Bias:{'CALLS' if s['flow_bias']=='call' else 'PUTS '}"
                f"  Side:{s['trade_side'].upper()}"
                f"{'  GOLDEN' if s.get('golden_sweep') else ''}"
                f"{'  STACKED' if s.get('stacked_flow') else ''}"
                f"\n{tc_str}"
            )
