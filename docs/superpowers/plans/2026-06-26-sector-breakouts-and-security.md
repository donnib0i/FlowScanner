# Sector Breakouts + Full API Security — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect RS-leading sector breakouts and surface constituent contract plays on the SECTORS tab, and harden every API endpoint.

**Architecture:** Phase 1 adds sector relative-strength + breakout flags to `scan_sectors()`, a pure constituent-ranking helper, an I/O wrapper that builds contract plays, a new lazy endpoint, and SECTORS-tab UI. Phase 2 layers env-flag-gated security: fail-closed auth, security-header + strict-CSP-nonce middleware, HTTPS/host hardening, surface reduction, and rate-limit integrity. Each task is independently testable.

**Tech Stack:** Python 3.11, FastAPI, Starlette middleware, yfinance, pandas, pytest + FastAPI TestClient.

## Global Constraints

- Files: core logic in `core/scanner.py`; web/API/UI in `web/app.py`; tests in `tests/`.
- Backward-compat: all new fields on existing dicts/JSON are **additive**; never rename/remove existing keys.
- **Embedded-JS gotcha:** JS/CSS lives in non-raw triple-quoted Python strings in `web/app.py` (`_HTML_PARTS`). NEVER write `\'` in embedded JS. Use `data-*` attributes + event delegation, not `onclick="fn('\''+x+'\'')"`. Verify against the SERVED `web.app.HTML`, not the raw `.py`.
- All new security behavior is **env-flag gated** so local dev is unaffected; prod locks down by setting flags in Railway.
- New tunables: `RS_THRESH = 0.5`, `RS_VOL = 1.1`.
- New env vars: `SCANNER_REQUIRE_PIN`, `SCANNER_ALLOW_PIN_QUERY`, `SCANNER_FORCE_HTTPS`, `SCANNER_ALLOWED_HOSTS`, `SCANNER_ENV`, `SCANNER_PROXY_HOPS`.
- Keep `tests/test_engine_exclusions.py` OUT of commits (broken WIP stub).
- TDD: failing test → minimal impl → green → commit. Frequent commits.
- Run tests with `python3 -m pytest`.

---

## PHASE 1 — Sector Breakouts

### Task 1: Sector RS + breakout classification

**Files:**
- Modify: `core/scanner.py` (add `RS_THRESH`/`RS_VOL` near `SECTOR_ETFS`; add `classify_breakout()`; wire into `scan_sectors()` ~line 1032-1096)
- Test: `tests/test_sector_breakouts.py`

**Interfaces:**
- Produces: `classify_breakout(change_pct: float, spy_chg: float, rel_vol: float) -> tuple[float, str]` returning `(rs_vs_spy, breakout)` where breakout ∈ {"up","down","none"}. `scan_sectors()` sector dicts gain keys `"rs_vs_spy": float` and `"breakout": str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sector_breakouts.py
from core.scanner import classify_breakout

def test_classify_breakout_up():
    rs, b = classify_breakout(change_pct=1.2, spy_chg=0.3, rel_vol=1.5)
    assert rs == 0.9 and b == "up"

def test_classify_breakout_down():
    rs, b = classify_breakout(change_pct=-1.0, spy_chg=0.2, rel_vol=1.3)
    assert rs == -1.2 and b == "down"

def test_classify_breakout_below_rs_threshold():
    _, b = classify_breakout(change_pct=0.4, spy_chg=0.1, rel_vol=2.0)
    assert b == "none"  # rs 0.3 < 0.5

def test_classify_breakout_low_volume():
    _, b = classify_breakout(change_pct=1.2, spy_chg=0.0, rel_vol=1.0)
    assert b == "none"  # rel_vol 1.0 < 1.1

def test_classify_breakout_spy_zero_fallback():
    rs, b = classify_breakout(change_pct=0.8, spy_chg=0.0, rel_vol=1.2)
    assert rs == 0.8 and b == "up"  # rs falls back to change_pct
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_sector_breakouts.py -v`
Expected: FAIL — `ImportError: cannot import name 'classify_breakout'`

- [ ] **Step 3: Add tunables + helper**

In `core/scanner.py`, right after the `SECTOR_ETFS` dict (ends ~line 65):

```python
# Sector relative-strength breakout tunables
RS_THRESH = 0.5   # sector must out/under-perform SPY by >= this many points
RS_VOL    = 1.1   # elevated relative-volume gate

def classify_breakout(change_pct: float, spy_chg: float, rel_vol: float) -> tuple:
    """RS vs SPY + breakout label. When spy_chg == 0.0, rs falls back to change_pct
    (same convention as the per-ticker RS calc). Returns (rs_vs_spy, breakout)."""
    rs = round(change_pct - spy_chg, 2) if spy_chg != 0.0 else round(change_pct, 2)
    if rs >= RS_THRESH and change_pct > 0 and rel_vol >= RS_VOL:
        return rs, "up"
    if rs <= -RS_THRESH and change_pct < 0 and rel_vol >= RS_VOL:
        return rs, "down"
    return rs, "none"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_sector_breakouts.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Wire SPY + fields into `scan_sectors()`**

In `scan_sectors()`: include SPY in the batch and compute `spy_chg` once before the loop. Change the batch line (~1035-1038):

```python
    etfs = list(SECTOR_ETFS.values())
    fetch_list = etfs + ["SPY"]
    print(f"  {Fore.CYAN}Scanning sectors (batch)...{Style.RESET_ALL}", end="", flush=True)
    batch = _fetch_batch_history(fetch_list, period="30d")
```

After `use_batch = not batch.empty` is determined, compute SPY change:

```python
    spy_chg = _get_spy_change(batch) if use_batch else 0.0
```

Inside the per-sector loop, replace the `sector_data[name] = {...}` block's tail by adding the two fields (compute right before the dict, after `strength` is set):

```python
            rs_vs_spy, breakout = classify_breakout(change_pct, spy_chg, rel_vol)

            sector_data[name] = {
                "etf":        etf,
                "price":      price,
                "change_pct": change_pct,
                "rel_vol":    rel_vol,
                "price_loc":  price_loc,
                "mom_3d":     mom_3d,
                "strength":   strength,
                "bias":       "up" if strength >= 0 else "down",
                "rs_vs_spy":  rs_vs_spy,
                "breakout":   breakout,
            }
```

- [ ] **Step 6: Commit**

```bash
git add core/scanner.py tests/test_sector_breakouts.py
git commit -m "feat(scanner): sector RS-vs-SPY + breakout classification"
```

---

### Task 2: Pure constituent ranking

**Files:**
- Modify: `core/scanner.py` (add `rank_breakout_constituents()` after `find_sector_laggards`, ~line 1141)
- Test: `tests/test_sector_breakouts.py`

**Interfaces:**
- Consumes: nothing external.
- Produces: `rank_breakout_constituents(sector_chg: float, breakout: str, quotes: Dict[str, Dict], n_laggards: int = 3, n_leaders: int = 2) -> List[tuple]` → ordered `[(ticker, role, change, lag)]`, laggards first then leaders, no overlap. `quotes` is `{ticker: {"change_pct","dollar_vol","price"}}` (the `_quotes_for` shape). `role` ∈ {"laggard","leader"}.

- [ ] **Step 1: Write the failing test**

```python
from core.scanner import rank_breakout_constituents

def _q(ch):
    return {"change_pct": ch, "dollar_vol": 1e6, "price": 10.0}

def test_rank_up_breakout_laggards_then_leaders():
    quotes = {"AAA": _q(2.0), "BBB": _q(0.1), "CCC": _q(1.8), "DDD": _q(-0.5)}
    out = rank_breakout_constituents(sector_chg=1.5, breakout="up",
                                     quotes=quotes, n_laggards=2, n_leaders=1)
    roles = [(t, r) for t, r, _, _ in out]
    # laggards = biggest positive lag (sector_chg - ticker_chg): DDD(2.0), BBB(1.4)
    assert roles[0] == ("DDD", "laggard")
    assert roles[1] == ("BBB", "laggard")
    # leader = strongest mover not already chosen: AAA(2.0)
    assert roles[2] == ("AAA", "leader")
    assert len({t for t, _ in roles}) == 3  # no overlap

def test_rank_down_breakout():
    quotes = {"AAA": _q(-2.0), "BBB": _q(0.2), "CCC": _q(-1.9)}
    out = rank_breakout_constituents(sector_chg=-1.5, breakout="down",
                                     quotes=quotes, n_laggards=1, n_leaders=1)
    roles = [(t, r) for t, r, _, _ in out]
    # laggard = most-positive (least-down) lag magnitude: BBB lags a falling sector
    assert roles[0][1] == "laggard" and roles[0][0] == "BBB"
    # leader = strongest downside mover not chosen: AAA(-2.0)
    assert roles[1] == ("AAA", "leader")

def test_rank_empty_when_no_breakout():
    assert rank_breakout_constituents(1.0, "none", {"AAA": _q(0.0)}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_sector_breakouts.py -k rank -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement**

```python
def rank_breakout_constituents(sector_chg: float, breakout: str,
                               quotes: Dict[str, Dict],
                               n_laggards: int = 3, n_leaders: int = 2) -> List[tuple]:
    """Order a breakout sector's constituents: catch-up laggards first, then momentum
    leaders. lag = sector_chg - ticker_chg. Returns [(ticker, role, change, lag)]."""
    if breakout == "none" or not quotes:
        return []

    rows = [(tk, q["change_pct"], sector_chg - q["change_pct"]) for tk, q in quotes.items()]

    if breakout == "up":
        laggards = sorted(rows, key=lambda r: r[2], reverse=True)   # biggest positive lag
        leaders  = sorted(rows, key=lambda r: r[1], reverse=True)   # strongest up move
    else:  # down
        laggards = sorted(rows, key=lambda r: r[2], reverse=True)   # least-down vs sector
        leaders  = sorted(rows, key=lambda r: r[1])                 # strongest down move

    picked: List[tuple] = []
    used = set()
    for tk, ch, lag in laggards[:n_laggards]:
        picked.append((tk, "laggard", round(ch, 2), round(lag, 2)))
        used.add(tk)
    for tk, ch, lag in leaders:
        if len([p for p in picked if p[1] == "leader"]) >= n_leaders:
            break
        if tk in used:
            continue
        picked.append((tk, "leader", round(ch, 2), round(lag, 2)))
        used.add(tk)
    return picked
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_sector_breakouts.py -k rank -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/scanner.py tests/test_sector_breakouts.py
git commit -m "feat(scanner): pure breakout constituent ranking"
```

---

### Task 3: `sector_breakout_plays()` with cache

**Files:**
- Modify: `core/scanner.py` (add after `sector_heatmap`, reuse `_quotes_for`, `constituents_for`, `get_best_contract`; add `_PLAYS_CACHE`)
- Test: `tests/test_sector_breakouts.py`

**Interfaces:**
- Consumes: `classify_breakout` output on `sector_data`; `rank_breakout_constituents`; `_quotes_for`; `constituents_for`; `get_best_contract`.
- Produces: `sector_breakout_plays(sector: str, sector_data: Dict[str, Dict], vix: float = -1.0, dte_mode: str = "all", n_laggards: int = 3, n_leaders: int = 2) -> Dict` → `{"sector","breakout","plays":[{"ticker","role","change","lag","contract"}]}`.

- [ ] **Step 1: Write the failing test (no network — monkeypatched)**

```python
import core.scanner as sc

def test_breakout_plays_none_skips_network(monkeypatch):
    called = {"q": 0, "c": 0}
    monkeypatch.setattr(sc, "_quotes_for", lambda *a, **k: called.__setitem__("q", called["q"]+1) or {})
    monkeypatch.setattr(sc, "get_best_contract", lambda *a, **k: called.__setitem__("c", called["c"]+1))
    out = sc.sector_breakout_plays("Technology", {"Technology": {"change_pct": 0.1, "breakout": "none"}})
    assert out == {"sector": "Technology", "breakout": "none", "plays": []}
    assert called == {"q": 0, "c": 0}

def test_breakout_plays_builds_contracts(monkeypatch):
    sc._PLAYS_CACHE.clear()
    sd = {"Technology": {"change_pct": 1.5, "breakout": "up"}}
    monkeypatch.setattr(sc, "constituents_for", lambda *a, **k: ["AAA", "BBB"])
    monkeypatch.setattr(sc, "_quotes_for", lambda *a, **k: {
        "AAA": {"change_pct": 0.1, "dollar_vol": 1e6, "price": 10.0},
        "BBB": {"change_pct": 1.8, "dollar_vol": 2e6, "price": 20.0}})
    monkeypatch.setattr(sc, "get_best_contract",
                        lambda tk, d, *a, **k: {"label": f"{tk} {d}", "strike": 1})
    out = sc.sector_breakout_plays("Technology", sd, dte_mode="0dte", n_laggards=1, n_leaders=1)
    assert out["breakout"] == "up"
    assert out["plays"][0]["ticker"] == "AAA" and out["plays"][0]["role"] == "laggard"
    assert out["plays"][0]["contract"]["label"] == "AAA up"
    assert any(p["role"] == "leader" for p in out["plays"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_sector_breakouts.py -k plays -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'sector_breakout_plays'`

- [ ] **Step 3: Implement**

```python
_PLAYS_CACHE: Dict[str, tuple] = {}   # "sector:dte_mode" -> (timestamp, payload)
_PLAYS_TTL = 60.0

def sector_breakout_plays(sector: str, sector_data: Dict[str, Dict],
                          vix: float = -1.0, dte_mode: str = "all",
                          n_laggards: int = 3, n_leaders: int = 2) -> Dict:
    """For an RS-breakout sector, build constituent contract plays (laggards first,
    then leaders). Returns {sector, breakout, plays:[{ticker,role,change,lag,contract}]}.
    Network — call off the event loop."""
    sd = sector_data.get(sector)
    breakout = sd.get("breakout", "none") if sd else "none"
    if not sd or breakout == "none":
        return {"sector": sector, "breakout": "none", "plays": []}

    now    = time.time()
    key    = f"{sector}:{dte_mode}"
    cached = _PLAYS_CACHE.get(key)
    if cached and now - cached[0] < _PLAYS_TTL:
        return cached[1]

    direction = "up" if breakout == "up" else "down"
    quotes    = _quotes_for(constituents_for(sector, fallback_map=TICKER_SECTOR))
    ranked    = rank_breakout_constituents(sd["change_pct"], breakout, quotes,
                                           n_laggards, n_leaders)

    plays = []
    for ticker, role, change, lag in ranked:
        contract = get_best_contract(ticker, direction, 0, vix, top_n=1, dte_mode=dte_mode)
        if not contract:
            continue
        if isinstance(contract, list):
            contract = contract[0] if contract else None
        if not contract:
            continue
        plays.append({"ticker": ticker, "role": role, "change": change,
                      "lag": lag, "contract": contract})

    payload = {"sector": sector, "breakout": breakout, "plays": plays}
    _PLAYS_CACHE[key] = (now, payload)
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_sector_breakouts.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add core/scanner.py tests/test_sector_breakouts.py
git commit -m "feat(scanner): sector_breakout_plays builds constituent contracts"
```

---

### Task 4: API — sectors fields + `/plays` endpoint

**Files:**
- Modify: `web/app.py` (import `sector_breakout_plays`; `/api/sectors` `clean` dict ~line 580-587; add endpoint after `/api/sector/{name}/heatmap` ~line 618)
- Test: `tests/test_api_sectors.py`

**Interfaces:**
- Consumes: `sector_breakout_plays`, `scan_sectors`, `fetch_vix`, `SECTOR_ETFS`, `_check_pin`, `_check_rate`, `_validate_enum`, `_VALID_DTE_MODE`.
- Produces: `/api/sectors` items gain `"rs"` and `"breakout"`; new `GET /api/sector/{name}/plays?dte_mode=all` → `{sector,breakout,plays,last_updated}`.

- [ ] **Step 1: Add import**

In `web/app.py` `from core.scanner import (...)` block, add `sector_breakout_plays,` next to `sector_heatmap,`.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_api_sectors.py
import os
os.environ.pop("SCANNER_PIN", None)  # disable auth for these tests
from fastapi.testclient import TestClient
import web.app as webapp

client = TestClient(webapp.app)

def test_plays_unknown_sector_404():
    r = client.get("/api/sector/NotASector/plays")
    assert r.status_code == 404

def test_plays_bad_dte_mode_400():
    r = client.get("/api/sector/Technology/plays?dte_mode=banana")
    assert r.status_code == 400

def test_plays_ok(monkeypatch):
    monkeypatch.setattr(webapp, "scan_sectors", lambda: {"Technology": {"change_pct": 1.5, "breakout": "up"}})
    monkeypatch.setattr(webapp, "fetch_vix", lambda: 15.0)
    monkeypatch.setattr(webapp, "sector_breakout_plays",
        lambda name, sd, vix, dte_mode: {"sector": name, "breakout": "up",
            "plays": [{"ticker": "AAA", "role": "laggard", "change": 0.1, "lag": 1.4,
                       "contract": {"label": "AAA up"}}]})
    r = client.get("/api/sector/Technology/plays?dte_mode=0dte")
    assert r.status_code == 200
    j = r.json()
    assert j["breakout"] == "up" and j["plays"][0]["ticker"] == "AAA"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_sectors.py -v`
Expected: FAIL — 404 route not found on `/plays`

- [ ] **Step 4: Add fields to `/api/sectors`**

In the `clean` list comprehension (~580-587) add two keys:

```python
    clean = [{
        "name":     k,
        "change":   round(v.get("change_pct", 0), 2),
        "strength": round(v.get("strength", 0), 2),
        "price":    round(v.get("price", 0), 2),
        "rel_vol":  round(v.get("rel_vol", 1), 2),
        "bias":     v.get("bias", "neutral"),
        "rs":       round(v.get("rs_vs_spy", 0), 2),
        "breakout": v.get("breakout", "none"),
    } for k, v in data.items()]
```

- [ ] **Step 5: Add the `/plays` endpoint**

After the heatmap endpoint (~618):

```python
@app.get("/api/sector/{name}/plays")
async def api_sector_plays(req: Request, name: str, dte_mode: str = Query("all")):
    _check_pin(req)
    _check_rate(req, "plays", limit=15, window=60)
    if name not in SECTOR_ETFS:
        raise HTTPException(404, "Unknown sector")
    _validate_enum(dte_mode, _VALID_DTE_MODE, "dte_mode")

    loop = asyncio.get_event_loop()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            sector_data = await asyncio.wait_for(
                loop.run_in_executor(None, scan_sectors), timeout=45.0)
        except asyncio.TimeoutError:
            raise HTTPException(504, "Sector scan timed out")
    try:
        vix = await loop.run_in_executor(None, fetch_vix)
    except Exception:
        vix = -1.0
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: sector_breakout_plays(
                    name, sector_data, vix, dte_mode)), timeout=45.0)
        except asyncio.TimeoutError:
            raise HTTPException(504, "Plays scan timed out")

    return {"sector": data["sector"], "breakout": data["breakout"],
            "plays": data["plays"], "last_updated": datetime.now().strftime("%H:%M:%S")}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api_sectors.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add web/app.py tests/test_api_sectors.py
git commit -m "feat(api): sector breakout fields + /api/sector/{name}/plays"
```

---

### Task 5: SECTORS tab UI — badges + plays

**Files:**
- Modify: `web/app.py` (`_HTML_PARTS` — the SECTORS tab list rendering + the sector-tap handler/heatmap section)
- Verify: served `web.app.HTML`

**Interfaces:**
- Consumes: `/api/sectors` (`rs`,`breakout`), `/api/sector/{name}/plays`.

- [ ] **Step 1: Locate the SECTORS rendering JS**

Run: `python3 -c "import web.app; import re; print([m.start() for m in re.finditer(r'sectors|breakout|/api/sector', web.app.HTML)][:10])"`
Then read the relevant `_HTML_PARTS.append(...)` block that renders sector rows and the tap-to-heatmap handler. Identify the function that builds a sector row and the one that opens the heatmap (uses `dataset` keys per the gotcha).

- [ ] **Step 2: Add breakout badge to each sector row**

In the sector-row builder, append a badge span based on `s.breakout` (data already in the `/api/sectors` payload). Example (adapt to the existing row template; NO escaped quotes):

```javascript
var bk = s.breakout === "up" ? " 🚀" : s.breakout === "down" ? " 🔻" : "";
// append `bk` to the sector name cell's textContent
nameCell.textContent = s.name + bk;
```

- [ ] **Step 3: Add a Plays section to the sector detail view**

In the handler that opens a sector (currently fetches the heatmap), after rendering the heatmap, fetch plays and render cards. Use event delegation + `dataset`, never inline `onclick` with quotes:

```javascript
function loadPlays(sectorName){
  fetch("/api/sector/" + encodeURIComponent(sectorName) + "/plays" + (PIN ? "?pin=" + PIN : ""), pinHeaders())
    .then(function(r){ return r.json(); })
    .then(function(j){
      var el = document.getElementById("sectorPlays");
      if(!j.plays || !j.plays.length){ el.textContent = "No RS breakout right now."; return; }
      el.innerHTML = "";
      j.plays.forEach(function(p){
        var c = p.contract || {};
        var card = document.createElement("div");
        card.className = "play-card " + p.role;
        card.textContent = p.ticker + " · " + p.role + " · " + p.change + "% · " +
          (c.label || "") + " " + (c.strike || "") + (c.type ? " " + c.type : "") +
          (c.mid != null ? " @" + c.mid : "") + (c.delta != null ? " Δ" + c.delta : "");
        el.appendChild(card);
      });
    });
}
```

Add a `<div id="sectorPlays"></div>` container in the sector detail markup and call `loadPlays(name)` from the existing sector-tap handler (reuse the same `name` it passes to the heatmap). Match the existing `pinHeaders()`/`PIN` helpers already used by other fetches — grep them first and reuse verbatim.

- [ ] **Step 4: Verify served JS is not broken by the gotcha**

```bash
python3 - <<'PY'
import re, web.app, subprocess, tempfile, os
m = re.findall(r"<script>(.*?)</script>", web.app.HTML, re.S)
js = "\n".join(m)
assert "\\'" not in js, "escaped-quote gotcha present!"
f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False); f.write(js); f.close()
print(subprocess.run(["node","--check",f.name]).returncode)
os.unlink(f.name)
PY
```
Expected: prints `0`, no assertion error.

- [ ] **Step 5: Commit**

```bash
git add web/app.py
git commit -m "feat(ui): SECTORS breakout badges + constituent plays"
```

---

## PHASE 2 — Full API Security

### Task 6: Auth hardening (fail-closed, weak-PIN warn, header-only, lockout)

**Files:**
- Modify: `web/app.py` (`_PIN` block ~74; `_check_pin` ~154-163; module init logging)
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: env-gated `_check_pin` behavior; `_REQUIRE_PIN`, `_ALLOW_PIN_QUERY` module flags.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_security.py
import importlib, os
from fastapi.testclient import TestClient

def _reload(env):
    for k in ["SCANNER_PIN","SCANNER_REQUIRE_PIN","SCANNER_ALLOW_PIN_QUERY"]:
        os.environ.pop(k, None)
    os.environ.update(env)
    import web.app as webapp
    return importlib.reload(webapp)

def test_require_pin_fails_closed_when_unset():
    webapp = _reload({"SCANNER_REQUIRE_PIN": "1"})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    assert c.get("/api/status").status_code == 503

def test_header_pin_accepted():
    webapp = _reload({"SCANNER_PIN": "secret1"})
    c = TestClient(webapp.app)
    assert c.get("/api/status", headers={"X-Pin": "secret1"}).status_code == 200

def test_query_pin_rejected_by_default():
    webapp = _reload({"SCANNER_PIN": "secret1"})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    assert c.get("/api/status?pin=secret1").status_code in (401, 403)

def test_query_pin_allowed_with_flag():
    webapp = _reload({"SCANNER_PIN": "secret1", "SCANNER_ALLOW_PIN_QUERY": "1"})
    c = TestClient(webapp.app)
    assert c.get("/api/status?pin=secret1").status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_security.py -k pin -v`
Expected: FAIL (query pin currently accepted; no 503 path)

- [ ] **Step 3: Implement**

Replace the `_PIN = ...` line (~74) with:

```python
_PIN             = os.environ.get("SCANNER_PIN", "").strip()
_REQUIRE_PIN     = os.environ.get("SCANNER_REQUIRE_PIN", "").strip() in ("1", "true", "yes")
_ALLOW_PIN_QUERY = os.environ.get("SCANNER_ALLOW_PIN_QUERY", "").strip() in ("1", "true", "yes")

if not _PIN:
    logging.warning("SCANNER_PIN not set — API is UNAUTHENTICATED")
elif len(_PIN) < 6:
    logging.warning("SCANNER_PIN is weak (<6 chars) — consider a longer PIN")
```

Replace `_check_pin` body. Add the missing-PIN cases and header-only logic; raise 403 on wrong PIN (the existing function continues past the snippet we saw — keep its final raise, change the `supplied` source):

```python
def _check_pin(req: Request):
    if not _PIN:
        if _REQUIRE_PIN:
            raise HTTPException(503, "Server auth not configured")
        return
    ip = _client_ip(req)
    supplied = req.headers.get("x-pin", "").strip()
    if not supplied and _ALLOW_PIN_QUERY:
        supplied = req.query_params.get("pin", "").strip()
    if not hmac.compare_digest(supplied.encode("utf-8", errors="replace"),
                               _PIN.encode("utf-8")):
        if not _rl.allow(f"{ip}:auth_fail", limit=10, window=300):
            raise HTTPException(429, detail="Too many failed attempts -- try later",
                                headers={"Retry-After": "300"})
        raise HTTPException(403, "Invalid or missing PIN")
```

(Confirm the existing function's trailing lines beyond line 163 are replaced by the explicit `raise HTTPException(403, ...)` above — read 154-170 first and replace the whole function.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_security.py -k pin -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_security.py
git commit -m "feat(sec): fail-closed auth, header-only PIN, weak-PIN warning"
```

---

### Task 7: XFF spoofing fix in `_client_ip`

**Files:**
- Modify: `web/app.py` (`_client_ip` ~142-146; add `_PROXY_HOPS`)
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: `_client_ip` honors `SCANNER_PROXY_HOPS` (default 1), taking the Nth-from-right XFF entry.

- [ ] **Step 1: Write the failing test**

```python
def test_client_ip_uses_trusted_hop(monkeypatch):
    webapp = _reload({"SCANNER_PROXY_HOPS": "1"})
    class Req:
        def __init__(self, xff): self.headers = {"x-forwarded-for": xff}; self.client = None
    # forged left-most entry must be ignored; right-most (proxy-appended) used
    assert webapp._client_ip(Req("1.1.1.1, 2.2.2.2, 9.9.9.9")) == "9.9.9.9"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_security.py -k client_ip -v`
Expected: FAIL — returns `1.1.1.1`

- [ ] **Step 3: Implement**

Add near `_PIN` flags: `_PROXY_HOPS = max(1, int(os.environ.get("SCANNER_PROXY_HOPS", "1") or 1))`.
Replace `_client_ip`:

```python
def _client_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            idx = min(_PROXY_HOPS, len(parts))
            return parts[-idx]
    return req.client.host if (getattr(req, "client", None)) else "unknown"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_security.py -k client_ip -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_security.py
git commit -m "fix(sec): trust only proxy-appended XFF hop for client IP"
```

---

### Task 8: Security headers + strict CSP nonce middleware

**Files:**
- Modify: `web/app.py` (add `secrets` import; middleware after `app = FastAPI(...)` ~177; `/` handler ~951 to inject nonce)
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: every response carries security headers + per-request CSP nonce; served `/` HTML inline `<script>`/`<style>` tags carry the matching `nonce`.

- [ ] **Step 1: Write the failing test**

```python
import re
def test_security_headers_present():
    webapp = _reload({})
    c = TestClient(webapp.app)
    r = c.get("/")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert "no-referrer" in r.headers.get("referrer-policy", "")
    csp = r.headers.get("content-security-policy", "")
    m = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
    assert m, csp
    nonce = m.group(1)
    # every inline <script> in the body carries the same nonce
    scripts = re.findall(r"<script(?![^>]*src=)([^>]*)>", r.text)
    assert scripts and all(('nonce="%s"' % nonce) in s for s in scripts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_security.py -k headers -v`
Expected: FAIL — no CSP header / no nonce in scripts

- [ ] **Step 3: Implement**

Add `secrets` to the stdlib import line (~12). After `app = FastAPI(...)`, also set `openapi_url=None` (see Task 10) — but headers here:

```python
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        f"style-src 'self' 'nonce-{nonce}'; script-src 'self' 'nonce-{nonce}'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    )
    if os.environ.get("SCANNER_FORCE_HTTPS", "").strip() in ("1", "true", "yes"):
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response
```

Update the `/` handler to inject the nonce into inline tags of the precomputed `HTML`:

```python
_NONCE_TAG_RE = re.compile(r"<(script|style)(?![^>]*\bsrc=)(\s*)>")

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    nonce = getattr(request.state, "csp_nonce", "")
    html = _NONCE_TAG_RE.sub(lambda m: f"<{m.group(1)} nonce=\"{nonce}\">", HTML)
    return HTMLResponse(html)
```

(Note: this edit is in the regular Python source for the *handler*, not inside an embedded-JS string, so normal Python escaping rules apply — the `\"` is fine here.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_security.py -k headers -v`
Expected: PASS

- [ ] **Step 5: Manual browser check (post-deploy)**

Load prod, open console: no CSP violations, all clicks work. CSP nonce blocks any stray inline handler — confirm none remain (the data-* delegation pattern means inline `onclick=` should not exist).

- [ ] **Step 6: Commit**

```bash
git add web/app.py tests/test_security.py
git commit -m "feat(sec): security headers + strict CSP with per-request nonce"
```

---

### Task 9: HTTPS redirect + trusted hosts

**Files:**
- Modify: `web/app.py` (middleware registration after `app = FastAPI(...)`)
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: `TrustedHostMiddleware` (from `SCANNER_ALLOWED_HOSTS`) + `HTTPSRedirectMiddleware` (gated by `SCANNER_FORCE_HTTPS`).

- [ ] **Step 1: Write the failing test**

```python
def test_trusted_host_rejects_bad_host():
    webapp = _reload({"SCANNER_ALLOWED_HOSTS": "good.example.com"})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    r = c.get("/api/status", headers={"host": "evil.com"})
    assert r.status_code == 400  # Invalid host

def test_trusted_host_allows_listed():
    webapp = _reload({"SCANNER_ALLOWED_HOSTS": "good.example.com"})
    c = TestClient(webapp.app)
    r = c.get("/api/status", headers={"host": "good.example.com"})
    assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_security.py -k host -v`
Expected: FAIL — no host validation

- [ ] **Step 3: Implement**

Add imports near top: `from starlette.middleware.trustedhost import TrustedHostMiddleware` and `from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware`. After `app = FastAPI(...)`:

```python
_allowed_hosts = [h.strip() for h in os.environ.get(
    "SCANNER_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)
if os.environ.get("SCANNER_FORCE_HTTPS", "").strip() in ("1", "true", "yes"):
    app.add_middleware(HTTPSRedirectMiddleware)
```

(Default includes `testserver` so the TestClient and existing tests keep working; in Railway set `SCANNER_ALLOWED_HOSTS=flowscanner-production.up.railway.app`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_security.py -k host -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_security.py
git commit -m "feat(sec): trusted-host + opt-in HTTPS redirect"
```

---

### Task 10: Surface reduction — openapi off, generic 500, body cap

**Files:**
- Modify: `web/app.py` (`FastAPI(...)` call ~177; add exception handler; add body-size middleware)
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: `/openapi.json` 404; unhandled errors → generic 500; POST bodies > 16 KB → 413.

- [ ] **Step 1: Write the failing test**

```python
def test_openapi_disabled():
    webapp = _reload({})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    assert c.get("/openapi.json").status_code == 404

def test_generic_500(monkeypatch):
    webapp = _reload({})
    @webapp.app.get("/_boom")
    async def boom():
        raise RuntimeError("leaky internal detail")
    c = TestClient(webapp.app, raise_server_exceptions=False)
    r = c.get("/_boom")
    assert r.status_code == 500
    assert "leaky internal detail" not in r.text
    assert r.json() == {"error": "internal error"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_security.py -k "openapi or 500" -v`
Expected: FAIL — openapi served; traceback/detail leaks

- [ ] **Step 3: Implement**

Change `app = FastAPI(title="Scanner Pro", docs_url=None, redoc_url=None)` to add `openapi_url=None`:

```python
app = FastAPI(title="Scanner Pro", docs_url=None, redoc_url=None, openapi_url=None)
```

Add a generic handler (after middleware registration). Let `HTTPException` pass through (FastAPI handles it); only catch the rest:

```python
from fastapi.responses import JSONResponse as _JSONResponse

@app.exception_handler(Exception)
async def _generic_error(request: Request, exc: Exception):
    logging.exception("Unhandled error on %s", request.url.path)
    return _JSONResponse({"error": "internal error"}, status_code=500)
```

Add a body-size middleware (16 KB cap) — only relevant to POSTs:

```python
_MAX_BODY = 16 * 1024

@app.middleware("http")
async def _limit_body(request: Request, call_next):
    if request.method == "POST":
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > _MAX_BODY:
            return _JSONResponse({"error": "payload too large"}, status_code=413)
    return await call_next(request)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_security.py -k "openapi or 500" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_security.py
git commit -m "feat(sec): disable openapi, generic 500s, request body cap"
```

---

### Task 11: Rate-limit `/api/status`

**Files:**
- Modify: `web/app.py` (`/api/status` handler ~339-341)
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: `/api/status` rate-limited at 30/60s.

- [ ] **Step 1: Write the failing test**

```python
def test_status_rate_limited():
    webapp = _reload({})
    c = TestClient(webapp.app, raise_server_exceptions=False)
    codes = [c.get("/api/status").status_code for _ in range(35)]
    assert 429 in codes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_security.py -k status_rate -v`
Expected: FAIL — never 429

- [ ] **Step 3: Implement**

In the `/api/status` handler, after `_check_pin(req)`:

```python
    _check_rate(req, "status", limit=30, window=60)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_security.py -k status_rate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_security.py
git commit -m "feat(sec): rate-limit /api/status"
```

---

### Task 12: Full suite + Railway env action items

**Files:** none (verification + ops)

- [ ] **Step 1: Run the full suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_engine_exclusions.py`
Expected: all PASS.

- [ ] **Step 2: Served-JS sanity (re-run Task 5 Step 4 snippet)**
Expected: prints `0`, no escaped-quote assertion.

- [ ] **Step 3: Document Railway env vars to set (prod lockdown)**

Record in commit message / PR body — set in Railway dashboard:
```
SCANNER_PIN=<strong pin already set?>   # confirm present
SCANNER_REQUIRE_PIN=1
SCANNER_FORCE_HTTPS=1
SCANNER_ALLOWED_HOSTS=flowscanner-production.up.railway.app
SCANNER_ENV=prod
# leave SCANNER_ALLOW_PIN_QUERY unset (header-only)
# SCANNER_PROXY_HOPS=1 (Railway default; adjust only if multiple proxies)
```

- [ ] **Step 4: Final commit (if any docs/notes changed)**

```bash
git add -A
git commit -m "chore: sector-breakouts + security verification notes"
```

---

## Self-Review

- **Spec coverage:** RS/breakout detection (T1), constituent ranking (T2), plays builder (T3), API fields + endpoint (T4), UI badges + plays (T5); security 5a auth (T6), 5e XFF (T7), 5c headers + strict-CSP nonce (T8), 5b transport/host (T9), 5d surface reduction (T10), 5f status rate-limit (T11) + new-endpoint validation (T4). All spec sections mapped.
- **Placeholders:** none — every code/test step has concrete content.
- **Type consistency:** `classify_breakout → (rs, breakout)`, `rank_breakout_constituents → [(ticker,role,change,lag)]`, `sector_breakout_plays → {sector,breakout,plays}` used consistently across T1–T5.
- **Note for implementer:** before editing `_check_pin` (T6) and `_client_ip` (T7), read the current full function bodies (app.py ~142-170) and replace them wholesale — the snippets above are complete replacements.
