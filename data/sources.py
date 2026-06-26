"""
sources.py — Multi-source options chain fetcher.

Tries sources in reliability order and returns a standardized contract list.
Add credentials via environment variables — if a source isn't configured it
is silently skipped.

Priority:
  1. Tradier          — real-time, clean REST, TRADIER_TOKEN env var
  2. Schwab API        — real-time, ThinkorSwim-grade data, OAuth2
                         SCHWAB_APP_KEY + SCHWAB_APP_SECRET + SCHWAB_REFRESH_TOKEN
  3. Polygon.io        — real-time paid / delayed free, POLYGON_API_KEY
  4. Yahoo Finance     — direct HTTP (no yfinance library), 15-min delayed
  5. yfinance          — last resort

Set env vars in Railway: Settings → Variables

Standardized contract output:
  {
    ticker, expiry, dte, strike, type (call/put),
    bid, ask, last, mid,
    volume, open_interest, iv,
    delta, gamma, theta, vega,   ← if available
    source                        ← which source returned this
  }
"""
from __future__ import annotations

import os
import time
import warnings
from datetime import datetime, date
from typing import Dict, List, Optional

import requests
import yfinance as yf
import pandas as pd

warnings.filterwarnings("ignore")

# ── Credentials from env ───────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

MAX_DTE = 60


def _dte(expiry_str: str) -> int:
    try:
        return max(0, (datetime.strptime(expiry_str, "%Y-%m-%d").date() - date.today()).days)
    except Exception:
        return 99


def _mid(bid: float, ask: float, last: float) -> float:
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    return round(last, 2)


# ── Source 1: Tradier ─────────────────────────────────────────────────────────
# Sign up: https://developer.tradier.com
# Free sandbox (simulated) or production (requires brokerage account or API subscription)
# Set env: TRADIER_TOKEN
# Docs: https://documentation.tradier.com/brokerage-api/markets/get-options-chains

def _fetch_tradier(ticker: str) -> List[Dict]:
    token = _env("TRADIER_TOKEN")
    if not token:
        return []
    base = "https://api.tradier.com/v1"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    # Get expirations first
    try:
        r = requests.get(f"{base}/markets/options/expirations",
                         params={"symbol": ticker, "includeAllRoots": "true"},
                         headers=headers, timeout=8)
        r.raise_for_status()
        expirations = r.json().get("expirations", {}).get("date", []) or []
    except Exception:
        return []

    contracts = []
    for exp in expirations[:6]:
        if _dte(exp) > MAX_DTE:
            continue
        try:
            r = requests.get(f"{base}/markets/options/chains",
                             params={"symbol": ticker, "expiration": exp, "greeks": "true"},
                             headers=headers, timeout=10)
            r.raise_for_status()
            opts = r.json().get("options", {}).get("option", []) or []
            for o in opts:
                if not isinstance(o, dict):
                    continue
                vol = int(o.get("volume") or 0)
                oi  = int(o.get("open_interest") or 0)
                bid = float(o.get("bid") or 0)
                ask = float(o.get("ask") or 0)
                last = float(o.get("last") or 0)
                strike = float(o.get("strike") or 0)
                greeks = o.get("greeks") or {}
                contracts.append({
                    "ticker": ticker,
                    "expiry": exp,
                    "dte": _dte(exp),
                    "strike": strike,
                    "type": o.get("option_type", "").lower(),
                    "bid": bid, "ask": ask, "last": last,
                    "mid": _mid(bid, ask, last),
                    "volume": vol,
                    "open_interest": oi,
                    "iv": float(greeks.get("mid_iv") or o.get("implied_volatility") or 0),
                    "delta": float(greeks.get("delta") or 0),
                    "gamma": float(greeks.get("gamma") or 0),
                    "theta": float(greeks.get("theta") or 0),
                    "vega":  float(greeks.get("vega") or 0),
                    "source": "tradier",
                })
        except Exception:
            continue
    return contracts


# ── Source 2: Schwab API (formerly TD Ameritrade / ThinkorSwim) ───────────────
# Sign up:   https://developer.schwab.com
# Register app → get app_key + app_secret
# Auth flow: OAuth2 with PKCE. After initial login, save refresh_token.
# Set env:   SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_REFRESH_TOKEN
# Docs:      https://developer.schwab.com/products/trader-api--individual-

_SCHWAB_TOKEN_CACHE: dict = {"access_token": "", "ts": 0.0}

def _schwab_access_token() -> str:
    """Exchange refresh token for access token (cached 25 min)."""
    now = time.time()
    if now - _SCHWAB_TOKEN_CACHE["ts"] < 1500 and _SCHWAB_TOKEN_CACHE["access_token"]:
        return _SCHWAB_TOKEN_CACHE["access_token"]
    app_key     = _env("SCHWAB_APP_KEY")
    app_secret  = _env("SCHWAB_APP_SECRET")
    refresh_tok = _env("SCHWAB_REFRESH_TOKEN")
    if not (app_key and app_secret and refresh_tok):
        return ""
    try:
        r = requests.post(
            "https://api.schwabapi.com/v1/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok},
            auth=(app_key, app_secret),
            timeout=10,
        )
        r.raise_for_status()
        tok = r.json().get("access_token", "")
        _SCHWAB_TOKEN_CACHE["access_token"] = tok
        _SCHWAB_TOKEN_CACHE["ts"] = now
        return tok
    except Exception:
        return ""


def _fetch_schwab(ticker: str) -> List[Dict]:
    tok = _schwab_access_token()
    if not tok:
        return []
    try:
        r = requests.get(
            "https://api.schwabapi.com/marketdata/v1/chains",
            params={
                "symbol": ticker,
                "contractType": "ALL",
                "includeUnderlyingQuote": "true",
                "strategy": "SINGLE",
                "optionType": "ALL",
            },
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    contracts = []
    for side, opt_type in [("callExpDateMap", "call"), ("putExpDateMap", "put")]:
        exp_map = data.get(side, {})
        for exp_key, strike_map in exp_map.items():
            # exp_key format: "2024-01-19:30" (date:DTE)
            exp_str = exp_key.split(":")[0]
            dte = _dte(exp_str)
            if dte > MAX_DTE:
                continue
            for strike_str, opt_list in strike_map.items():
                for o in opt_list:
                    bid    = float(o.get("bid") or 0)
                    ask    = float(o.get("ask") or 0)
                    last   = float(o.get("last") or 0)
                    strike = float(o.get("strikePrice") or strike_str or 0)
                    contracts.append({
                        "ticker": ticker,
                        "expiry": exp_str,
                        "dte": dte,
                        "strike": strike,
                        "type": opt_type,
                        "bid": bid, "ask": ask, "last": last,
                        "mid": _mid(bid, ask, last),
                        "volume": int(o.get("totalVolume") or 0),
                        "open_interest": int(o.get("openInterest") or 0),
                        "iv": float(o.get("volatility") or 0) / 100,
                        "delta": float(o.get("delta") or 0),
                        "gamma": float(o.get("gamma") or 0),
                        "theta": float(o.get("theta") or 0),
                        "vega":  float(o.get("vega") or 0),
                        "source": "schwab",
                    })
    return contracts


# ── Source 3: Polygon.io ──────────────────────────────────────────────────────
# Sign up:  https://polygon.io  (free tier: delayed + limited snapshots)
# Set env:  POLYGON_API_KEY
# Docs:     https://polygon.io/docs/options/get_v3_snapshot_options__underlyingasset

def _fetch_polygon(ticker: str) -> List[Dict]:
    key = _env("POLYGON_API_KEY")
    if not key:
        return []
    contracts = []
    url = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
    params = {"apiKey": key, "limit": 250, "expired": "false"}
    try:
        while url:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            body = r.json()
            for res in body.get("results", []):
                details = res.get("details", {})
                day     = res.get("day", {})
                greeks  = res.get("greeks", {})
                exp_str = details.get("expiration_date", "")
                dte     = _dte(exp_str)
                if dte > MAX_DTE:
                    continue
                bid  = float(res.get("last_quote", {}).get("bid") or day.get("close") or 0)
                ask  = float(res.get("last_quote", {}).get("ask") or day.get("close") or 0)
                last = float(day.get("close") or 0)
                contracts.append({
                    "ticker": ticker,
                    "expiry": exp_str,
                    "dte": dte,
                    "strike": float(details.get("strike_price") or 0),
                    "type": details.get("contract_type", "").lower(),
                    "bid": bid, "ask": ask, "last": last,
                    "mid": _mid(bid, ask, last),
                    "volume": int(day.get("volume") or 0),
                    "open_interest": int(res.get("open_interest") or 0),
                    "iv": float(res.get("implied_volatility") or 0),
                    "delta": float(greeks.get("delta") or 0),
                    "gamma": float(greeks.get("gamma") or 0),
                    "theta": float(greeks.get("theta") or 0),
                    "vega":  float(greeks.get("vega") or 0),
                    "source": "polygon",
                })
            # Pagination
            next_url = body.get("next_url")
            url = (next_url + f"&apiKey={key}") if next_url and len(contracts) < 2000 else None
            params = {}
    except Exception:
        pass
    return contracts


# ── Source 4: Yahoo Finance direct HTTP ──────────────────────────────────────
# No API key. 15-min delayed. More reliable than the yfinance library
# because we control headers, retries, and JSON parsing directly.

def _fetch_yahoo_direct(ticker: str) -> List[Dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    contracts = []
    base = f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}"
    try:
        # Get expirations
        r = requests.get(base, headers=headers, timeout=8)
        r.raise_for_status()
        body = r.json()
        result = body.get("optionChain", {}).get("result", [{}])[0]
        expirations = result.get("expirationDates", [])  # unix timestamps
        price = result.get("quote", {}).get("regularMarketPrice", 0)
    except Exception:
        return []

    for ts in expirations[:6]:
        exp_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        dte = _dte(exp_str)
        if dte > MAX_DTE:
            continue
        try:
            r = requests.get(base, params={"date": ts}, headers=headers, timeout=8)
            r.raise_for_status()
            chain_result = r.json().get("optionChain", {}).get("result", [{}])[0]
            chain = chain_result.get("options", [{}])[0]
        except Exception:
            continue
        for opt_type, key in [("call", "calls"), ("put", "puts")]:
            for o in chain.get(key, []):
                bid    = float(o.get("bid") or 0)
                ask    = float(o.get("ask") or 0)
                last   = float(o.get("lastPrice") or 0)
                strike = float(o.get("strike") or 0)
                contracts.append({
                    "ticker": ticker,
                    "expiry": exp_str,
                    "dte": dte,
                    "strike": strike,
                    "type": opt_type,
                    "bid": bid, "ask": ask, "last": last,
                    "mid": _mid(bid, ask, last),
                    "volume": int(o.get("volume") or 0),
                    "open_interest": int(o.get("openInterest") or 0),
                    "iv": float(o.get("impliedVolatility") or 0),
                    "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
                    "source": "yahoo",
                })
    return contracts


# ── Source 5: yfinance library ────────────────────────────────────────────────

def _fetch_yfinance(ticker: str) -> List[Dict]:
    contracts = []
    try:
        t = yf.Ticker(ticker)
        for exp in (t.options or [])[:6]:
            dte = _dte(exp)
            if dte > MAX_DTE:
                continue
            try:
                chain = t.option_chain(exp)
            except Exception:
                continue
            for opt_type, df in [("call", chain.calls), ("put", chain.puts)]:
                if df is None or df.empty:
                    continue
                df = df.copy()
                for col in ["volume","openInterest","lastPrice","bid","ask","impliedVolatility","strike"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
                for _, row in df.iterrows():
                    bid    = float(row.get("bid") or 0)
                    ask    = float(row.get("ask") or 0)
                    last   = float(row.get("lastPrice") or 0)
                    strike = float(row.get("strike") or 0)
                    contracts.append({
                        "ticker": ticker,
                        "expiry": exp,
                        "dte": dte,
                        "strike": strike,
                        "type": opt_type,
                        "bid": bid, "ask": ask, "last": last,
                        "mid": _mid(bid, ask, last),
                        "volume": int(row.get("volume") or 0),
                        "open_interest": int(row.get("openInterest") or 0),
                        "iv": float(row.get("impliedVolatility") or 0),
                        "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
                        "source": "yfinance",
                    })
    except Exception:
        pass
    return contracts


# ── Main entry point ──────────────────────────────────────────────────────────

def get_option_chain(ticker: str) -> List[Dict]:
    """
    Fetch options chain for a ticker using the best available source.
    Tries sources in priority order, returns first successful result.
    """
    for fn in [_fetch_tradier, _fetch_schwab, _fetch_polygon,
               _fetch_yahoo_direct, _fetch_yfinance]:
        try:
            result = fn(ticker)
            if result:
                return result
        except Exception:
            continue
    return []


def available_sources() -> List[str]:
    """Return which sources are currently configured."""
    sources = []
    if _env("TRADIER_TOKEN"):
        sources.append("tradier")
    if _env("SCHWAB_APP_KEY") and _env("SCHWAB_REFRESH_TOKEN"):
        sources.append("schwab")
    if _env("POLYGON_API_KEY"):
        sources.append("polygon")
    sources.append("yahoo_direct")
    sources.append("yfinance")
    return sources
