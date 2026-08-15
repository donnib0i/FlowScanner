# Sector Breakouts → Constituent Contract Plays + API Hardening

**Date:** 2026-06-26
**Status:** Approved design, pending spec review

## Goal

Close the loop between the SECTORS tab and contract scanning: detect when a
**sector is leading the market** (relative-strength breakout), and turn that into
**tradeable contract ideas on its constituent stocks** — laggards first (the
documented "sector ripping, this name hasn't caught up yet" play), then leaders
as a momentum alternative. Plus: harden all API endpoints.

Reuses existing machinery (`scan_sectors`, `_quotes_for`, `constituents_for`,
`get_best_contract`, the `_HEATMAP_CACHE` pattern). No new data sources.

## Non-goals

- Multi-day range/level breakouts (we chose RS leadership, not technical range break).
- Contracts on the sector ETF itself (XLK calls) — constituents only.
- A new tab. Everything lives on the existing SECTORS tab.

---

## Component 1: Sector RS + breakout detection (`core/scanner.py`)

Extend `scan_sectors()`:

1. Include `SPY` in the batch fetch (one extra symbol; reuse the existing
   `_spy_day_change`-style extraction so it works behind cloud IP blocks). Compute
   `spy_chg` once.
2. Add to each sector dict:
   - `rs_vs_spy = round(change_pct - spy_chg, 2)`
   - `breakout`:
     - `"up"`   when `rs_vs_spy >= RS_THRESH` **and** `change_pct > 0` **and** `rel_vol >= RS_VOL`
     - `"down"` when `rs_vs_spy <= -RS_THRESH` **and** `change_pct < 0` **and** `rel_vol >= RS_VOL`
     - `"none"` otherwise
3. New module-level tunables near the other constants:
   - `RS_THRESH = 0.5`  (sector must out/under-perform SPY by ≥0.5 pts)
   - `RS_VOL = 1.1`     (elevated relative volume gate)

`spy_chg` defaults to `0.0` on fetch failure; when `spy_chg == 0.0`, `rs_vs_spy`
falls back to `change_pct` (same convention as the existing per-ticker RS at
scanner.py:1941–1942), and `breakout` still evaluates against it.

These fields are **additive** — existing `scan_sectors` consumers
(`find_sector_laggards`, `apply_forward_directions`, SECTORS UI) are unaffected.

## Component 2: Breakout → constituent contracts (`core/scanner.py`)

New function:

```python
def sector_breakout_plays(sector: str, sector_data: Dict[str, Dict],
                          vix: float = -1.0, dte_mode: str = "all",
                          n_laggards: int = 3, n_leaders: int = 2) -> Dict
```

Logic:
1. Look up `sd = sector_data.get(sector)`. If missing or `sd["breakout"] == "none"`,
   return `{"sector": sector, "breakout": "none", "plays": []}`.
2. `direction = "up" if sd["breakout"] == "up" else "down"` — constituents trade
   **with** the sector (laggard catches up in the sector's direction).
3. `quotes = _quotes_for(constituents_for(sector, fallback_map=TICKER_SECTOR))`.
4. For each constituent compute `lag = sector_chg - ticker_chg`.
   - **Laggards** (catch-up): up-breakout → largest positive `lag`; down-breakout →
     largest-magnitude negative `lag`. Take top `n_laggards`.
   - **Leaders** (momentum): strongest movers in the sector's direction
     (highest `change` for up, lowest for down), excluding names already picked as
     laggards. Take top `n_leaders`.
5. For each selected ticker call
   `get_best_contract(ticker, direction, 0, vix, top_n=1, dte_mode=dte_mode)`.
   Skip names with no contract.
6. Return:
   ```json
   {"sector": "...", "breakout": "up",
    "plays": [{"ticker": "...", "role": "laggard|leader",
               "change": 0.0, "lag": 0.0, "contract": { ... }}]}
   ```
   Laggards listed first, then leaders.

**Caching:** module-level `_PLAYS_CACHE: Dict[str, tuple]` keyed by
`f"{sector}:{dte_mode}"`, 60s TTL, mirroring `_HEATMAP_CACHE`, so repeated taps
don't refetch and per-tap network cost stays bounded.

**Isolation:** ranking (lag computation + laggard/leader selection) is split into a
pure helper `rank_breakout_constituents(sector_chg, breakout, quotes, n_laggards,
n_leaders) -> List[(ticker, role, change, lag)]` that takes plain dicts and returns
ordered picks — no network, fully unit-testable. `sector_breakout_plays` does the
I/O (quotes + contracts) around it.

## Component 3: API (`web/app.py`)

1. `/api/sectors`: add `"rs"` and `"breakout"` to each item in the `clean` list.
   Backward-compatible additive fields.
2. New endpoint:
   ```
   GET /api/sector/{name}/plays?dte_mode=all
   ```
   - `_check_pin(req)` + `_check_rate(req, "plays", limit=15, window=60)`.
   - Reject unknown sector: `if name not in SECTOR_ETFS: raise HTTPException(404)`
     (same guard the heatmap endpoint uses).
   - `_validate_enum(dte_mode, _VALID_DTE_MODE, "dte_mode")`.
   - Fetch `sector_data` (reuse cached `scan_sectors` via the existing cache path),
     `vix` via `fetch_vix`, then `sector_breakout_plays(name, sector_data, vix,
     dte_mode)` in the executor with a `wait_for` timeout (45s) like heatmap.
   - Return `{sector, breakout, plays, last_updated}`.

## Component 4: SECTORS tab UI (`web/app.py`)

1. **Breakout badge:** in the sectors list rendering, when `s.breakout === "up"`
   show a 🚀 badge; `"down"` show a breakdown badge (🔻 / red). Plain text/emoji
   badge, no new dependencies.
2. **Plays section:** the existing tap on a sector opens its heatmap. Add a
   **Plays** block (rendered below the heatmap, or a small toggle) that calls
   `/api/sector/{name}/plays` and lists cards: ticker, role (Laggard/Leader),
   % change, and the contract (label / strike / type / mid / delta). If
   `breakout === "none"`, show "No RS breakout right now."
3. **JS-in-Python-string gotcha (mandatory):** all embedded JS uses `data-*`
   attributes + event delegation. **No escaped single quotes (`\'`) anywhere** —
   they collapse when the non-raw triple-quoted Python string is built and break
   every handler on the page (documented incident 2026-06-25). Verify against the
   *served* `web.app.HTML` output (regex the `<script>`, `node --check`), not the
   raw `.py` source.

## Component 5: Full API security hardening

Current state (audited 2026-06-26): every `/api/*` endpoint calls `_check_pin`; all
but `/api/status` are rate-limited; ticker/enum inputs are validated; PIN compare is
timing-safe (`hmac.compare_digest`); rate limiter caps keys at 10k. Good base. A full
hardening pass adds the following, grouped by area. Each item is independently
implementable and testable.

### 5a. Authentication

1. **Fail-closed on missing PIN (the core hole):** `_check_pin` is a no-op when
   `SCANNER_PIN` is unset (app.py:155-156), so a deploy with no PIN env var leaves
   the whole API open.
   - Boot warning when `_PIN` is empty: `WARNING: SCANNER_PIN not set — API is UNAUTHENTICATED`.
   - `SCANNER_REQUIRE_PIN=1` flag → when set and `_PIN` empty, `_check_pin` raises
     `HTTPException(503, "Server auth not configured")`. Set this in Railway so prod
     fails closed; local dev stays open by default.
   - **Action item:** confirm `SCANNER_PIN` is set in Railway env.
2. **PIN strength guard:** at boot, if `_PIN` is non-empty but `< 6` chars, log a
   weak-PIN warning. (No hard fail — don't lock the user out.)
3. **Header-only PIN, deprecate query param:** `_check_pin` currently accepts
   `?pin=` in the query string, which leaks into access logs, browser history, and
   `Referer` headers. Keep `X-Pin` header as the canonical path; keep query-param
   acceptance behind a `SCANNER_ALLOW_PIN_QUERY=1` flag (default off) so the PWA can
   migrate to header-only without an outage. Ensure the PIN is never logged.
4. **Brute-force lockout:** already present (`auth_fail` limiter, 10/300s). Keep, and
   apply it on the *missing-PIN* path too (currently only wrong-PIN), so unauth
   probing is throttled.

### 5b. Transport & host

5. **HTTPS enforcement:** Railway terminates TLS at the edge. Add
   `HTTPSRedirectMiddleware` gated by `SCANNER_FORCE_HTTPS=1` (off locally) so http
   hits redirect to https in prod.
6. **HSTS header:** `Strict-Transport-Security: max-age=63072000; includeSubDomains`
   (only when serving over https / behind the proxy).
7. **Trusted hosts:** `TrustedHostMiddleware` with an allowlist from
   `SCANNER_ALLOWED_HOSTS` (default: the Railway domain + `localhost`,`127.0.0.1`)
   to block Host-header injection.

### 5c. Security response headers (middleware)

8. Add one middleware that sets on every response:
   - `X-Content-Type-Options: nosniff`
   - `X-Frame-Options: DENY` (app is not meant to be framed)
   - `Referrer-Policy: no-referrer`
   - `Permissions-Policy: geolocation=(), microphone=(), camera=()`
   - **Strict `Content-Security-Policy` with per-request nonces:**
     `default-src 'self'; img-src 'self' data:; style-src 'self' 'nonce-<N>';
     script-src 'self' 'nonce-<N>'; connect-src 'self'; frame-ancestors 'none';
     base-uri 'self'; object-src 'none'`.
   - **Nonce threading:** the page is built from `_HTML_PARTS` triple-quoted strings.
     Generate a fresh `secrets.token_urlsafe(16)` nonce per request, inject
     `nonce="<N>"` into every inline `<script>` and `<style>` tag, and emit the same
     nonce in the CSP header. Implement by making the `/` (and any HTML) handler
     render via a small helper that takes the nonce and `.format`/replaces a
     `{nonce}` placeholder on each inline tag — keep it compatible with the
     non-raw-string / escaped-quote gotcha (no `\'`; the placeholder is plain text).
     Add a test asserting the served HTML's script nonces match the CSP header nonce.

### 5d. Surface reduction

9. **Disable FastAPI interactive docs in prod:** `/docs`, `/redoc`, and
   `/openapi.json` are exposed by default and enumerate every endpoint + schema.
   Construct `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` when
   `SCANNER_ENV=prod` (or always — the user doesn't need them in prod). Keep enabled
   locally for development.
10. **Generic error handling:** add an exception handler so unhandled exceptions
    return a plain `500 {"error":"internal error"}` with no traceback/detail.
    Validation `HTTPException`s keep their explicit messages. Prevents stack-trace
    and internal-path leakage.
11. **Request body size limit:** for POST endpoints (`/api/flow`, `/api/darkpool`,
    `/api/insider`) reject bodies over a small cap (e.g. 16 KB) before parsing, to
    bound abuse of the ticker-list parameters.

### 5e. Proxy / rate-limit integrity

12. **X-Forwarded-For spoofing:** `_client_ip` blindly trusts the first XFF entry, so
    a client can forge it to dodge per-IP rate limits and the brute-force lockout.
    Trust XFF only when behind the known proxy: take the **right-most** XFF hop (the
    one Railway appends) rather than the left-most, or make the trusted-hop count
    configurable via `SCANNER_PROXY_HOPS` (default 1). This makes rate limiting and
    lockout actually enforceable.

### 5f. New & existing endpoints

13. **Rate-limit `/api/status`:** `_check_rate(req, "status", limit=30, window=60)`
    for parity with `/api/vix`.
14. **New `/plays` endpoint:** full PIN + rate + sector-name (`in SECTOR_ETFS`) +
    `dte_mode` enum validation per Component 3 — no unvalidated path/query params
    reach the scanner.

Static routes (`/manifest.json`, `/apple-touch-icon.png`, `/`) remain unauthenticated
by design — the PWA shell loads them before the user enters a PIN. The security
headers middleware still applies to them.

**All security behavior is env-flag gated** so local development is unaffected and
prod is locked down by setting flags in Railway. New env vars introduced:
`SCANNER_REQUIRE_PIN`, `SCANNER_ALLOW_PIN_QUERY`, `SCANNER_FORCE_HTTPS`,
`SCANNER_ALLOWED_HOSTS`, `SCANNER_ENV`, `SCANNER_PROXY_HOPS`.

---

## Testing (`tests/test_sector_breakouts.py`)

Pure-function tests, no network:

- `rs_vs_spy` / `breakout` flag: feed synthetic `change_pct`, `spy_chg`, `rel_vol`
  → assert `up` / `down` / `none` across the threshold and volume boundaries
  (including `spy_chg == 0.0` fallback).
- `rank_breakout_constituents`: synthetic `quotes` dict + sector change → assert
  laggards ranked by catch-up gap, leaders by momentum, correct counts, no overlap,
  correct direction for up vs down breakouts.
- `sector_breakout_plays` with `breakout == "none"` returns empty plays without
  touching the network (monkeypatch `_quotes_for`/`get_best_contract` to assert
  they're not called).

Security tests (`tests/test_security.py`, FastAPI `TestClient`, no network):

- `_check_pin` fail-closed: `SCANNER_REQUIRE_PIN=1` + empty PIN → 503; valid header
  PIN → pass; wrong PIN → 401/403; query-param PIN rejected unless
  `SCANNER_ALLOW_PIN_QUERY=1`.
- `_client_ip` honors `SCANNER_PROXY_HOPS` (right-most trusted hop), so a forged
  left-most XFF entry can't change the rate-limit key.
- Security headers present on an API response and on `/` (CSP, X-Frame-Options,
  nosniff, Referrer-Policy).
- `/docs`, `/redoc`, `/openapi.json` return 404 when docs are disabled.
- Unhandled exception path returns generic 500 with no traceback (monkeypatch an
  endpoint dep to raise).

Keep `tests/test_engine_exclusions.py` out of commits (known broken WIP stub).

## Deploy

Push to `master` → Railway auto-deploys (NIXPACKS, ~1–4 min). Ignore Render
failures. After deploy, curl the new endpoint against prod with the PIN header and
load the SECTORS tab in a browser to confirm no console errors (the only reliable
check for the embedded-JS gotcha).
