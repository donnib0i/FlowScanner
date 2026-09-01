#!/usr/bin/env python3
"""
tt_flow.py — Real options flow scanner via Tastytrade + dxFeed TimeAndSale streaming.

Why this beats yfinance:
  - yfinance: 15-min delayed daily snapshot (vol * stale_mid * 100 = fake flow)
  - This:     individual trade prints with exact price, size, exchange, and real
              aggressor_side (Buy/Sell from the exchange — no Lee-Ready guessing)

Data source: Tastytrade API → dxFeed OPRA feed
Credentials: env vars TT_USERNAME / TT_PASSWORD, or ~/.tt_creds.json
             TT_CHALLENGE_TOKEN / TT_REMEMBER_TOKEN cover hosts with no
             writable home and no way to answer a device challenge.
Check auth:  python3 -m data.tt_flow --check
"""

from __future__ import annotations

import asyncio
import json
import os
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
    with open(path, "w") as f:
        json.dump({"username": username, "password": password}, f)
    os.chmod(path, 0o600)  # owner read/write only


# ── Auth client ───────────────────────────────────────────────────────────────
_CHALLENGE_PATH = os.path.expanduser("~/.tt_challenge.txt")
_SESSION_PATH   = os.path.expanduser("~/.tt_session.json")


def _load_challenge_token() -> str:
    """
    Device-challenge token. Env var first: a Railway container has no shell to
    `echo` a file into and a filesystem that is wiped on every redeploy, so
    ~/.tt_challenge.txt is a local-only mechanism.
    """
    tok = os.environ.get("TT_CHALLENGE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        if os.path.exists(_CHALLENGE_PATH):
            return open(_CHALLENGE_PATH).read().strip()
    except Exception:
        pass
    return ""


def _clear_challenge_token() -> None:
    # Only the file is ours to clear; the env var is the operator's to rotate.
    try:
        if os.path.exists(_CHALLENGE_PATH):
            os.remove(_CHALLENGE_PATH)
    except Exception:
        pass


# ── Remember-token store ──────────────────────────────────────────────────────
# A password login is what triggers the device challenge. A remember-token login
# does not, so once one challenge has been cleared the token carries the session
# forward indefinitely. Tastytrade rotates it on every use, so it is stored back
# after each login and the env var only seeds the first login after a redeploy.
def _load_remember_token() -> str:
    try:
        if os.path.exists(_SESSION_PATH):
            tok = json.loads(open(_SESSION_PATH).read()).get("remember-token", "")
            if tok:
                return tok
    except Exception:
        pass
    return os.environ.get("TT_REMEMBER_TOKEN", "").strip()


def _save_remember_token(token: str) -> None:
    if not token:
        return
    try:
        with open(_SESSION_PATH, "w") as f:
            json.dump({"remember-token": token}, f)
        os.chmod(_SESSION_PATH, 0o600)
    except Exception:
        pass


def _clear_remember_token() -> None:
    try:
        if os.path.exists(_SESSION_PATH):
            os.remove(_SESSION_PATH)
    except Exception:
        pass


class TTAuth:
    """
    Handles tastytrade authentication.
    Uses the /sessions endpoint (username + password → session-token).
    Then gets dxFeed streaming credentials from /api-quote-tokens.
    """

    def __init__(self, username: str, password: str):
        self.username    = username
        self.password    = password
        self.session_tok  = ""
        self.remember_tok = ""
        self.dx_token     = ""
        self.dx_url       = ""

    def _headers(self) -> dict:
        return {
            "Authorization": self.session_tok,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    async def _post_session(self, payload: dict, challenge_token: str = "") -> Any:
        headers = {"Content-Type": "application/json"}
        if challenge_token:
            headers["X-Tastyworks-Challenge-Token"] = challenge_token
        async with httpx.AsyncClient(base_url=TT_API, timeout=15) as c:
            return await c.post("/sessions", json=payload, headers=headers)

    def _consume(self, r) -> bool:
        """Record the session and the rotated remember-token from a 2xx /sessions."""
        data = r.json().get("data", {})
        self.session_tok = data.get("session-token", "")
        if not self.session_tok:
            return False
        self.remember_tok = data.get("remember-token", "")
        _save_remember_token(self.remember_tok)
        _clear_challenge_token()
        return True

    async def login(self) -> bool:
        """
        Remember-token first, password second.

        Only a password login triggers the device challenge, and a Railway
        container can never answer one interactively. So once a challenge has
        been cleared anywhere, the rotating remember-token is what keeps the
        session alive across restarts without ever sending the password again.
        """
        remember = _load_remember_token()
        if remember:
            r = await self._post_session(
                {"login": self.username, "remember-token": remember,
                 "remember-me": True}
            )
            if r.status_code in (200, 201) and self._consume(r):
                return True
            # Stale or already-consumed token: drop it and fall through to
            # password, otherwise every future login retries the same dead token.
            _clear_remember_token()

        return await self._password_login()

    async def _password_login(self, challenge_token: str = "") -> bool:
        if not self.password:
            _set_error("no remember-token and no password — set TT_PASSWORD")
            return False

        r = await self._post_session(
            {"login": self.username, "password": self.password, "remember-me": True},
            challenge_token=challenge_token,
        )

        if r.status_code == 403:
            try:
                code = r.json().get("error", {}).get("code")
            except Exception:
                code = None
            if code == "device_challenge_required":
                # Retry once. Re-reading the token on a second failure would
                # hand back the same rejected value and recurse forever.
                saved = "" if challenge_token else _load_challenge_token()
                if saved:
                    print("TTAuth: retrying with saved challenge token...")
                    return await self._password_login(challenge_token=saved)
                _set_error("device_challenge_required — TastyTrade emailed a "
                           "verification token; set TT_CHALLENGE_TOKEN (or write "
                           "~/.tt_challenge.txt) and retry")
                print(
                    "TTAuth: device challenge required.\n"
                    "  1. Check your email for a TastyTrade verification message.\n"
                    "  2. Copy the token from the link (the 'token' query param).\n"
                    "  3. Local:   echo 'TOKEN' > ~/.tt_challenge.txt\n"
                    "     Railway: railway variables --set TT_CHALLENGE_TOKEN=TOKEN\n"
                    "  4. Re-run. On success a remember-token is stored and the\n"
                    "     challenge is not asked again."
                )
            else:
                _set_error(f"login rejected (HTTP 403{f', {code}' if code else ''})")
            return False

        if r.status_code not in (200, 201):
            _set_error(f"login rejected (HTTP {r.status_code})")
            return False

        if not self._consume(r):
            _set_error("login returned no session-token")
            return False
        return True

    async def get_quote_tokens(self) -> bool:
        async with httpx.AsyncClient(base_url=TT_API, timeout=15) as c:
            r = await c.get("/api-quote-tokens", headers=self._headers())
            if r.status_code != 200:
                return False
            data = r.json().get("data", {})
            self.dx_token = data.get("token", "")
            self.dx_url   = data.get("dxlink-url", "")
            return bool(self.dx_token and self.dx_url)

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
    def parse_feed_config(data: dict) -> Dict[str, int]:
        fields_map = {}
        for item in data.get("eventFields", []):
            if item.get("eventType") == "TimeAndSale":
                for i, f in enumerate(item.get("eventFieldsList", [])):
                    fields_map[f] = i
        return fields_map

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

            setup_deadline = time.time() + 30  # 30s to complete handshake
            collect_deadline = 0.0             # set once collection starts

            while True:
                now = time.time()
                # Abort if setup takes too long
                if not collecting and now > setup_deadline:
                    break
                # Stop after collection window
                if collecting and now >= collect_deadline:
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
                    field_index = parse_feed_config(msg)
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
    True if exchange sweep condition is set OR ≥10 prints within 500ms.
    The time-based threshold is intentionally tight — 3 prints/sec is normal
    retail activity; true sweeps are rapid multi-exchange fills.
    """
    # Condition-based: exchange marks ISO intermarket sweeps with 'I' or 'F'
    for p in prints:
        if "I" in p.conditions or "F" in p.conditions:
            return True
    # Time-based: ≥10 distinct prints in 500ms = rapid multi-exchange sweep
    times = sorted(p.ts for p in prints if p.ts > 0)
    if len(times) >= 10 and (times[-1] - times[0]) < 500:
        return True
    return False


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
        print(f"remember-token   : {'set' if _load_remember_token() else '(unset)'}")
        print(f"challenge-token  : {'set' if _load_challenge_token() else '(unset)'}")
        if not u:
            print("\nresult           : FAIL — no username; set TT_USERNAME")
            sys.exit(1)

        auth = TTAuth(u, p)
        ok = asyncio.run(auth.setup())
        print(f"session-token    : {'ok' if auth.session_tok else 'FAILED'}")
        print(f"dxlink feed      : {auth.dx_url or 'FAILED'}")
        if ok:
            print("\nresult           : LIVE — flow will stream from OPRA")
            sys.exit(0)
        print(f"\nresult           : FAIL — {last_error() or 'unknown'}")
        sys.exit(1)

    tickers = argv or ["SPY", "QQQ", "NVDA", "TSLA"]
    u, p = load_credentials()
    if not u:
        u = input("Tastytrade username: ").strip()
        p = input("Tastytrade password: ").strip()
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
