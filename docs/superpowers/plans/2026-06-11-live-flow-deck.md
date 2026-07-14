# FlowDeck — Live Flow Deck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A continuously-refreshing Textual terminal dashboard that shows where volume is going in single stocks (no ETFs) and their option contracts, flagging activity that is unusual relative to each name's own baseline.

**Architecture:** A new `core/live_flow.py` Textual app owns a refresh loop that runs the existing flow engine in a worker thread. Two new pure modules — `data/etf_filter.py` and `data/baseline.py` — add ETF exclusion and a self-collected SQLite OI/volume baseline. `data/unusual_flow.py` and `core/universe.py` are modified to strip ETFs and add baseline-relative scoring. All network/visual behavior sits behind injectable seams so pure logic is unit-tested.

**Tech Stack:** Python 3, Textual (TUI), SQLite (stdlib `sqlite3`), yfinance, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-06-11-live-flow-deck-design.md`

**Working dir:** `/Users/ll/AiAgent/scanner` (its own git repo, currently on `master`). All paths below are relative to this dir. Run tests from this dir.

---

## File Structure

| File | New/Mod | Responsibility |
|------|---------|----------------|
| `data/etf_filter.py` | New | `is_etf` / `filter_etfs`; known-ETF set + cached yfinance quoteType lookup |
| `data/baseline.py` | New | SQLite store: record OI/volume snapshots, derive `contract_oi_vs_avg`, `ticker_optvol_rvol` |
| `data/unusual_flow.py` | Mod | Remove ETF force-include; add pure scoring helpers + baseline multiplier; emit `oi_vs_avg`, `intra_chain_z` |
| `core/universe.py` | Mod | Strip ETFs from the built universe (keep mega-cap *stocks*) |
| `core/live_flow.py` | New | Textual dense-grid app, refresh worker, two panels + status bar, hotkeys; pure row/sparkline helpers |
| `core/scanner.py` | Mod | Add `--live` / `--interval` flags that launch the app |
| `requirements.txt` | Mod | Add `textual` |
| `requirements-dev.txt` | New | `pytest`, `pytest-asyncio` |
| `pytest.ini` | New | `asyncio_mode = auto` |
| `tests/conftest.py` | New | Put scanner root on `sys.path` |
| `tests/test_*.py` | New | Unit tests per module |

---

## Task 1: Branch, dependencies, test scaffold

**Files:**
- Create: `requirements-dev.txt`, `pytest.ini`, `tests/__init__.py`, `tests/conftest.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Create feature branch**

```bash
cd /Users/ll/AiAgent/scanner
git checkout -b feat/live-flow-deck
```

- [ ] **Step 2: Install Textual + test deps, pin resolved version**

```bash
python3 -m pip install textual pytest-asyncio
python3 -c "import textual; print(textual.__version__)"
```

Append the printed version to `requirements.txt` (replace `X.Y.Z` with the actual output), and create `requirements-dev.txt`:

`requirements.txt` — add this line:
```
textual==X.Y.Z
```

`requirements-dev.txt` (new):
```
pytest>=8.0
pytest-asyncio>=0.23
```

- [ ] **Step 3: Create pytest config**

`pytest.ini` (new):
```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 4: Create test scaffold**

`tests/__init__.py` (new, empty file).

`tests/conftest.py` (new):
```python
import os
import sys

# Put the scanner repo root on sys.path so `import data...` / `import core...` work,
# matching the absolute-import style already used in core/universe.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

- [ ] **Step 5: Verify pytest collects nothing yet (no error)**

Run: `python3 -m pytest -q`
Expected: `no tests ran` (exit 5) with no import/config errors.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt requirements-dev.txt pytest.ini tests/__init__.py tests/conftest.py
git commit -m "chore: add textual + pytest scaffold for live flow deck

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: ETF filter (`data/etf_filter.py`)

**Files:**
- Create: `data/etf_filter.py`
- Test: `tests/test_etf_filter.py`

- [ ] **Step 1: Write the failing test**

`tests/test_etf_filter.py` (new):
```python
from data import etf_filter


def test_known_etfs_are_etfs():
    assert etf_filter.is_etf("SPY") is True
    assert etf_filter.is_etf("QQQ") is True
    assert etf_filter.is_etf("TQQQ") is True


def test_mega_cap_stocks_are_not_etfs():
    # These appear in the old ANCHOR list but are stocks, not ETFs.
    for t in ("AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "TSLA"):
        assert etf_filter.is_etf(t) is False


def test_filter_etfs_drops_only_etfs():
    assert etf_filter.filter_etfs(["SPY", "AAPL", "QQQ", "NVDA"]) == ["AAPL", "NVDA"]


def test_unknown_ticker_uses_quote_type_lookup_and_caches(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_lookup(ticker):
        calls["n"] += 1
        return "ETF" if ticker == "ZZZX" else "EQUITY"

    monkeypatch.setattr(etf_filter, "_lookup_quote_type", fake_lookup)
    etf_filter._set_cache_path(str(tmp_path / "etf_cache.json"))
    etf_filter._reset_cache()

    assert etf_filter.is_etf("ZZZX") is True     # first call hits lookup
    assert etf_filter.is_etf("ZZZX") is True     # second call hits cache
    assert calls["n"] == 1                        # lookup called only once
    assert etf_filter.is_etf("ABCD") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etf_filter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'data.etf_filter'`.

- [ ] **Step 3: Write minimal implementation**

`data/etf_filter.py` (new):
```python
"""
etf_filter.py — single source of truth for "is this an ETF?".

Two tiers:
  1. KNOWN_ETFS — the symbols previously hardcoded as anchors across the scanner.
     These are exactly the ETFs that used to pollute results. Offline, instant.
  2. yfinance quoteType lookup for unknown screener tickers, cached to disk so
     each name is queried at most once.
"""
from __future__ import annotations

import json
import os
from typing import Iterable, List

# Seeded from the old _ETF_ANCHORS (unusual_flow.py) + ANCHOR ETFs (universe.py).
# Mega-cap stocks that lived in ANCHOR (AAPL/MSFT/NVDA/META/AMZN/GOOGL/TSLA) are
# deliberately NOT here — they are stocks.
KNOWN_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "MDY",
    "VXX", "UVXY", "SVXY",
    "TQQQ", "SOXL", "UPRO", "SPXL", "TNA", "LABU", "TECL", "FNGU",
    "SQQQ", "SOXS", "SPXS", "TZA", "LABD", "TECS", "FNGD",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLC", "XLRE",
    "GLD", "SLV", "USO", "CPER", "KWEB", "FXI", "XBI", "ARKK",
}

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "baselines", "etf_cache.json")
_cache: dict | None = None


def _set_cache_path(path: str) -> None:
    global _CACHE_PATH
    _CACHE_PATH = path


def _reset_cache() -> None:
    global _cache
    _cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_CACHE_PATH) as f:
            _cache = json.load(f)
    except Exception:
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(_cache, f)
    except Exception:
        pass


def _lookup_quote_type(ticker: str) -> str:
    """Query yfinance for the security type. Returns 'ETF', 'EQUITY', or '' on failure."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).get_info()
        return str(info.get("quoteType", "")).upper()
    except Exception:
        return ""


def is_etf(ticker: str) -> bool:
    t = (ticker or "").upper().strip()
    if not t:
        return False
    if t in KNOWN_ETFS:
        return True
    cache = _load_cache()
    if t in cache:
        return cache[t] == "ETF"
    qt = _lookup_quote_type(t)
    if qt:                      # only cache confident answers
        cache[t] = qt
        _save_cache()
    return qt == "ETF"


def filter_etfs(tickers: Iterable[str]) -> List[str]:
    """Drop ETFs, preserve order, dedupe."""
    out, seen = [], set()
    for t in tickers:
        u = (t or "").upper().strip()
        if not u or u in seen or is_etf(u):
            continue
        seen.add(u)
        out.append(u)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_etf_filter.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add data/etf_filter.py tests/test_etf_filter.py
git commit -m "feat: add shared ETF filter (known set + cached quoteType)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Baseline store (`data/baseline.py`)

**Files:**
- Create: `data/baseline.py`
- Test: `tests/test_baseline.py`

- [ ] **Step 1: Write the failing test**

`tests/test_baseline.py` (new):
```python
from data.baseline import BaselineStore


def _store(tmp_path):
    return BaselineStore(db_path=str(tmp_path / "baseline.db"))


def test_contract_oi_vs_avg_uses_prior_days_only(tmp_path):
    s = _store(tmp_path)
    s.record_contract("2026-06-09", "ABCD", "call", 30.0, "2026-06-20", oi=1000, volume=500)
    s.record_contract("2026-06-10", "ABCD", "call", 30.0, "2026-06-20", oi=2000, volume=900)
    # today's OI 6000 vs avg of prior (1000, 2000) = 1500 -> 4.0x
    ratio = s.contract_oi_vs_avg("ABCD", "call", 30.0, "2026-06-20", today_oi=6000)
    assert ratio == 4.0


def test_contract_oi_vs_avg_none_without_history(tmp_path):
    s = _store(tmp_path)
    assert s.contract_oi_vs_avg("ABCD", "call", 30.0, "2026-06-20", today_oi=6000) is None


def test_record_contract_is_upsert_last_write_wins(tmp_path):
    s = _store(tmp_path)
    s.record_contract("2026-06-09", "ABCD", "call", 30.0, "2026-06-20", oi=1000, volume=500)
    s.record_contract("2026-06-09", "ABCD", "call", 30.0, "2026-06-20", oi=1500, volume=700)
    # only one prior row; today 3000 vs avg 1500 -> 2.0x
    assert s.contract_oi_vs_avg("ABCD", "call", 30.0, "2026-06-20", today_oi=3000) == 2.0


def test_ticker_optvol_rvol(tmp_path):
    s = _store(tmp_path)
    s.record_ticker("2026-06-09", "ABCD", total_opt_vol=100, total_oi=10, equity_vol=1)
    s.record_ticker("2026-06-10", "ABCD", total_opt_vol=300, total_oi=20, equity_vol=2)
    # today 800 vs avg(100,300)=200 -> 4.0x
    assert s.ticker_optvol_rvol("ABCD", today_opt_vol=800) == 4.0
    assert s.ticker_optvol_rvol("ZZZZ", today_opt_vol=800) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_baseline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'data.baseline'`.

- [ ] **Step 3: Write minimal implementation**

`data/baseline.py` (new):
```python
"""
baseline.py — self-collected OI / option-volume history.

The scanner records a snapshot each run. Over days this lets us answer
"is today's OI/volume unusual for THIS name?" — the core signal. No history
is fabricated: derived ratios return None until prior observations exist.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "baselines", "baseline.db")


class BaselineStore:
    def __init__(self, db_path: str = _DEFAULT_DB):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contract_obs (
                obs_date TEXT, ticker TEXT, opt_type TEXT,
                strike REAL, expiry TEXT, oi INTEGER, volume INTEGER,
                PRIMARY KEY (obs_date, ticker, opt_type, strike, expiry)
            );
            CREATE TABLE IF NOT EXISTS ticker_obs (
                obs_date TEXT, ticker TEXT,
                total_opt_vol INTEGER, total_oi INTEGER, equity_vol INTEGER,
                PRIMARY KEY (obs_date, ticker)
            );
            """
        )
        self.conn.commit()

    # ── writes (upsert, last-write-wins per day) ──
    def record_contract(self, obs_date, ticker, opt_type, strike, expiry, oi, volume) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO contract_obs "
            "(obs_date,ticker,opt_type,strike,expiry,oi,volume) VALUES (?,?,?,?,?,?,?)",
            (obs_date, ticker.upper(), opt_type, float(strike), expiry, int(oi), int(volume)),
        )
        self.conn.commit()

    def record_ticker(self, obs_date, ticker, total_opt_vol, total_oi, equity_vol) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ticker_obs "
            "(obs_date,ticker,total_opt_vol,total_oi,equity_vol) VALUES (?,?,?,?,?)",
            (obs_date, ticker.upper(), int(total_opt_vol), int(total_oi), int(equity_vol)),
        )
        self.conn.commit()

    # ── derived signals ──
    def contract_oi_vs_avg(self, ticker, opt_type, strike, expiry, today_oi) -> Optional[float]:
        row = self.conn.execute(
            "SELECT AVG(oi) FROM contract_obs "
            "WHERE ticker=? AND opt_type=? AND strike=? AND expiry=?",
            (ticker.upper(), opt_type, float(strike), expiry),
        ).fetchone()
        avg = row[0] if row else None
        if not avg or avg <= 0:
            return None
        return round(today_oi / avg, 2)

    def ticker_optvol_rvol(self, ticker, today_opt_vol, window: int = 20) -> Optional[float]:
        rows = self.conn.execute(
            "SELECT total_opt_vol FROM ticker_obs WHERE ticker=? "
            "ORDER BY obs_date DESC LIMIT ?",
            (ticker.upper(), window),
        ).fetchall()
        vols = [r[0] for r in rows if r[0] and r[0] > 0]
        if not vols:
            return None
        avg = sum(vols) / len(vols)
        if avg <= 0:
            return None
        return round(today_opt_vol / avg, 2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_baseline.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add data/baseline.py tests/test_baseline.py
git commit -m "feat: add SQLite baseline store for OI/option-volume history

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Scoring helpers in `data/unusual_flow.py`

Add pure, testable scoring helpers and a per-contract scoring function. The existing `_scan_ticker_options` will call `score_contract` in Task 5.

**Files:**
- Modify: `data/unusual_flow.py` (add functions near the existing `_anomaly_score`, ~line 236)
- Test: `tests/test_scoring.py`

- [ ] **Step 1: Write the failing test**

`tests/test_scoring.py` (new):
```python
from data.unusual_flow import (
    intra_chain_z,
    baseline_multiplier,
    adjusted_score,
)


def test_intra_chain_z_flags_outlier():
    assert intra_chain_z(100, [1, 1, 1, 100]) > 1.0


def test_intra_chain_z_flat_chain_is_zero():
    assert intra_chain_z(5, [5, 5, 5, 5]) == 0.0
    assert intra_chain_z(1, []) == 0.0


def test_baseline_multiplier_boosts_spike_and_is_neutral_without_history():
    assert baseline_multiplier(None, None, dampen=False) == 1.0
    assert baseline_multiplier(6.1, 3.5, dampen=False) == 1.5   # clamped high
    assert baseline_multiplier(2.0, None, dampen=False) == 1.15


def test_baseline_multiplier_dampens_perennial_megacap():
    assert baseline_multiplier(None, None, dampen=True) == 0.6


def test_adjusted_score_clamps():
    assert adjusted_score(80, 1.5) == 100
    assert adjusted_score(50, 0.6) == 30
    assert adjusted_score(40, 1.0) == 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_scoring.py -v`
Expected: FAIL — `ImportError: cannot import name 'intra_chain_z'`.

- [ ] **Step 3: Write minimal implementation**

In `data/unusual_flow.py`, add after `_anomaly_score` (after line ~254):
```python
def intra_chain_z(value: float, chain_values: list) -> float:
    """How much of an outlier is `value` vs the rest of this ticker's chain today."""
    vals = [float(v) for v in chain_values if v and v > 0]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
    if std == 0:
        return 0.0
    return round((value - mean) / std, 2)


def baseline_multiplier(oi_vs_avg, ticker_optvol_rvol, dampen: bool = False) -> float:
    """
    Score multiplier in [0.5, 1.5]. Boost names spiking vs their own norm;
    dampen perennial mega-caps. Neutral 1.0 when no baseline yet.
    """
    m = 1.0
    if oi_vs_avg is not None:
        if oi_vs_avg >= 5:
            m += 0.5
        elif oi_vs_avg >= 3:
            m += 0.3
        elif oi_vs_avg >= 2:
            m += 0.15
    if ticker_optvol_rvol is not None and ticker_optvol_rvol >= 3:
        m += 0.15
    if dampen:
        m -= 0.4
    return round(max(0.5, min(1.5, m)), 2)


def adjusted_score(base_score: int, multiplier: float) -> int:
    return max(0, min(100, int(round(base_score * multiplier))))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_scoring.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add data/unusual_flow.py tests/test_scoring.py
git commit -m "feat: add intra-chain outlier + baseline score multiplier helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Wire ETF exclusion + baseline scoring into the engine

Remove the hardcoded ETF anchors and apply the filter + baseline scoring. Factor the per-contract scoring into a pure `score_contract` so it is testable.

**Files:**
- Modify: `data/unusual_flow.py` — remove `_ETF_ANCHORS` force-include (lines ~186, ~292-294); add `score_contract`; thread `exclude_etfs` through `scan_unusual_flow` / `get_scan_pool`
- Modify: `core/universe.py` — strip ETFs via `filter_etfs`, drop the ETF `ANCHOR` prepend (keep mega-cap stocks)
- Test: `tests/test_engine_exclusions.py`

- [ ] **Step 1: Write the failing test**

`tests/test_engine_exclusions.py` (new):
```python
from data import unusual_flow as uf
from core import universe


def test_score_contract_builds_signal_with_baseline_fields():
    contract = {
        "volume": 18000, "open_interest": 3000, "bid": 1.0, "ask": 1.2,
        "last": 1.18, "mid": 1.1, "strike": 145.0, "iv": 0.6,
        "expiry": "2026-06-13", "dte": 2, "type": "call", "source": "test",
    }
    sig = uf.score_contract(
        contract, price=142.0, sector="Technology",
        chain_volumes=[100, 120, 18000],
        oi_vs_avg=6.1, ticker_optvol_rvol=3.5, dampen=False,
    )
    assert sig is not None
    assert sig["ticker_oi_vs_avg"] == 6.1
    assert sig["volume"] == 18000 and sig["open_interest"] == 3000  # shown separately
    assert sig["score"] >= 50


def test_score_contract_filters_tiny_volume():
    contract = {"volume": 10, "open_interest": 5, "mid": 1.0, "strike": 145.0,
                "expiry": "2026-06-13", "dte": 2, "type": "call"}
    assert uf.score_contract(contract, 142.0, "Tech", [10], None, None, False) is None


def test_apply_pool_exclusions_drops_etfs():
    pool = ["SPY", "AAPL", "QQQ", "NVDA", "TQQQ"]
    assert uf.apply_pool_exclusions(pool, exclude_etfs=True) == ["AAPL", "NVDA"]


def test_universe_finalize_strips_etfs_keeps_megacap_stocks():
    raw = ["SPY", "AAPL", "QQQ", "NVDA", "spy", "MSFT"]
    out = universe.finalize_universe(raw, exclude_etfs=True)
    assert "SPY" not in out and "QQQ" not in out
    assert "AAPL" in out and "NVDA" in out and "MSFT" in out
```

Note: `score_contract` takes `ticker` from the contract; add `"ticker": "TEST"` to the contracts above if your implementation reads it — adjust assertion accordingly. Implementation below reads ticker from a parameter, so contracts need no ticker key.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_engine_exclusions.py -v`
Expected: FAIL — `AttributeError: module 'data.unusual_flow' has no attribute 'score_contract'`.

- [ ] **Step 3: Write minimal implementation**

In `data/unusual_flow.py`:

(a) Add the pure functions (after the helpers from Task 4):
```python
def apply_pool_exclusions(tickers: list, exclude_etfs: bool = True) -> list:
    if not exclude_etfs:
        return list(tickers)
    from data.etf_filter import filter_etfs
    return filter_etfs(tickers)


def score_contract(contract: dict, price: float, sector: str,
                   chain_volumes: list,
                   oi_vs_avg, ticker_optvol_rvol, dampen: bool,
                   ticker: str = "") -> dict | None:
    """Pure scoring of a single option contract dict -> signal dict (or None)."""
    vol    = int(contract.get("volume") or 0)
    oi     = int(contract.get("open_interest") or 0)
    bid    = float(contract.get("bid") or 0)
    ask    = float(contract.get("ask") or 0)
    last   = float(contract.get("last") or 0)
    mid    = float(contract.get("mid") or 0)
    strike = float(contract.get("strike") or 0)
    iv     = float(contract.get("iv") or 0)
    expiry = contract.get("expiry", "")
    dte    = int(contract.get("dte") or _days_to_expiry(expiry))
    opt_type = contract.get("type", "")

    if vol < MIN_VOLUME or strike <= 0 or price <= 0:
        return None
    if not mid:
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    if not mid:
        return None

    notional = vol * mid * 100
    vol_oi   = vol / max(oi, 1)
    otm      = _otm_pct(strike, price, opt_type)
    side     = _classify_side(bid, ask, last)
    base     = _anomaly_score(notional, vol_oi, dte, otm, side)
    z        = intra_chain_z(vol, chain_volumes)
    if z >= 2:
        base = min(100, base + 5)
    mult     = baseline_multiplier(oi_vs_avg, ticker_optvol_rvol, dampen)
    score    = adjusted_score(base, mult)

    if score < 25 or notional < MIN_NOTIONAL:
        return None

    return {
        "ticker": ticker or contract.get("ticker", ""),
        "sector": sector, "price": round(price, 2),
        "type": opt_type, "strike": strike, "expiry": expiry, "dte": dte,
        "volume": vol, "open_interest": oi, "vol_oi": round(vol_oi, 2),
        "mid_price": round(mid, 2), "notional": round(notional, 0),
        "iv": round(iv * 100, 1) if iv < 10 else round(iv, 1),
        "otm_pct": round(otm, 1), "trade_side": side,
        "intra_chain_z": z,
        "ticker_oi_vs_avg": oi_vs_avg,
        "ticker_optvol_rvol": ticker_optvol_rvol,
        "score": score, "label": _label(score),
        "data_source": contract.get("source", "unknown"),
        "ts": datetime.now().strftime("%H:%M:%S"),
    }
```

(b) Refactor `_scan_ticker_options` (lines ~301-353) to build `chain_volumes` once, query the baseline, and delegate to `score_contract`. Replace its body with:
```python
def _scan_ticker_options(ticker: str, price: float, sector: str,
                         store=None, dampen: bool = False) -> List[Dict]:
    signals = []
    try:
        contracts = get_option_chain(ticker)
        if not contracts:
            return []
        chain_volumes = [int(c.get("volume") or 0) for c in contracts]
        today = date.today().isoformat()
        ticker_vol = sum(chain_volumes)
        rvol = store.ticker_optvol_rvol(ticker, ticker_vol) if store else None
        for c in contracts:
            oi_vs_avg = None
            if store:
                oi_vs_avg = store.contract_oi_vs_avg(
                    ticker, c.get("type", ""), float(c.get("strike") or 0),
                    c.get("expiry", ""), int(c.get("open_interest") or 0),
                )
            sig = score_contract(c, price, sector, chain_volumes,
                                  oi_vs_avg, rvol, dampen, ticker=ticker)
            if sig:
                signals.append(sig)
    except Exception:
        pass
    return signals
```

(c) Remove the ETF force-include:
- In `build_ticker_sector_map` delete the line `merged.update(_ETF_ANCHORS)` (~line 186).
- In `_screen_relvol` replace the anchor block (lines ~292-294) — change the return to just `return [t for t, _ in scored[:top_n]]` (drop the `anchors + top` prepend).

(d) Thread `exclude_etfs` into `scan_unusual_flow` (line ~358): change signature to add `exclude_etfs: bool = True`, and right after `pool = tickers or list(sector_map.keys())` insert:
```python
    pool = apply_pool_exclusions(pool, exclude_etfs)
```

In `core/universe.py`:

(e) Add a pure finalizer and use it in `build_universe`. Add near the bottom (before `get_universe`):
```python
def finalize_universe(raw: List[str], exclude_etfs: bool = True) -> List[str]:
    cleaned = _clean(raw)
    if exclude_etfs:
        from data.etf_filter import filter_etfs
        return filter_etfs(cleaned)
    return cleaned
```

(f) In `build_universe` (line ~235) replace `final = _clean(list(ANCHOR) + ordered)` with:
```python
    final = finalize_universe(ordered, exclude_etfs=True)
    return final
```
(Drop the `ANCHOR` prepend — mega-cap stocks still arrive via S&P 500 / screeners; ETFs in `ANCHOR` are intentionally excluded.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_engine_exclusions.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python3 -m pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add data/unusual_flow.py core/universe.py tests/test_engine_exclusions.py
git commit -m "feat: exclude ETFs everywhere + baseline-relative contract scoring

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Pure presentation helpers (`core/live_flow.py`)

Build the testable transforms first: sparkline, per-ticker rollup, net flow. The Textual app (Task 7) consumes these.

**Files:**
- Create: `core/live_flow.py` (helpers only in this task)
- Test: `tests/test_live_flow_helpers.py`

- [ ] **Step 1: Write the failing test**

`tests/test_live_flow_helpers.py` (new):
```python
from core.live_flow import sparkline, aggregate_by_ticker, net_flow, fmt_compact

SIGNALS = [
    {"ticker": "NVDA", "sector": "Tech", "type": "call",
     "volume": 18000, "open_interest": 3000, "notional": 3_200_000},
    {"ticker": "NVDA", "sector": "Tech", "type": "put",
     "volume": 2000, "open_interest": 1000, "notional": 400_000},
    {"ticker": "PLTR", "sector": "Tech", "type": "call",
     "volume": 9000, "open_interest": 2000, "notional": 1_100_000},
]


def test_sparkline_shape():
    sp = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(sp) == 8
    assert sp[0] == "▁" and sp[-1] == "█"
    assert sparkline([5, 5, 5]) == "▁▁▁"
    assert sparkline([]) == ""


def test_aggregate_by_ticker_rolls_up_vol_oi_and_flow():
    agg = aggregate_by_ticker(SIGNALS)
    assert agg["NVDA"]["opt_vol"] == 20000
    assert agg["NVDA"]["opt_oi"] == 4000
    assert agg["NVDA"]["call_notional"] == 3_200_000
    assert agg["NVDA"]["put_notional"] == 400_000
    assert agg["PLTR"]["opt_vol"] == 9000


def test_net_flow_sums_calls_minus_puts():
    call, put = net_flow(SIGNALS)
    assert call == 4_300_000
    assert put == 400_000


def test_fmt_compact():
    assert fmt_compact(3_200_000) == "3.2M"
    assert fmt_compact(214_000) == "214.0K"
    assert fmt_compact(950) == "950"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_live_flow_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.live_flow'`.

- [ ] **Step 3: Write minimal implementation**

`core/live_flow.py` (new — helpers section):
```python
"""
live_flow.py — FlowDeck: live single-stock volume + unusual options TUI.

This module has two halves:
  * pure helpers (sparkline, rollups, formatting) — unit tested
  * the Textual App (added in Task 7) — smoke tested via Pilot
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list) -> str:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return _BARS[0] * len(vals)
    return "".join(_BARS[int((v - lo) / (hi - lo) * (len(_BARS) - 1))] for v in vals)


def aggregate_by_ticker(signals: List[Dict]) -> Dict[str, Dict]:
    agg: Dict[str, Dict] = {}
    for s in signals:
        t = s["ticker"]
        a = agg.setdefault(t, {
            "sector": s.get("sector", "?"),
            "opt_vol": 0, "opt_oi": 0,
            "call_notional": 0.0, "put_notional": 0.0,
        })
        a["opt_vol"] += int(s.get("volume", 0))
        a["opt_oi"] += int(s.get("open_interest", 0))
        if s.get("type") == "call":
            a["call_notional"] += s.get("notional", 0)
        else:
            a["put_notional"] += s.get("notional", 0)
    return agg


def net_flow(signals: List[Dict]) -> Tuple[float, float]:
    call = sum(s.get("notional", 0) for s in signals if s.get("type") == "call")
    put = sum(s.get("notional", 0) for s in signals if s.get("type") == "put")
    return call, put


def fmt_compact(n: float) -> str:
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_live_flow_helpers.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add core/live_flow.py tests/test_live_flow_helpers.py
git commit -m "feat: add FlowDeck presentation helpers (sparkline, rollup, fmt)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Textual app + data frame (`core/live_flow.py`)

Add the dense-grid TUI and the `gather_frame` data assembler behind an injectable seam, so the render is smoke-testable offline.

**Files:**
- Modify: `core/live_flow.py` (append the App + `gather_frame` + `run`)
- Test: `tests/test_live_flow_app.py`

- [ ] **Step 1: Write the failing test**

`tests/test_live_flow_app.py` (new):
```python
import pytest
from textual.widgets import DataTable
from core.live_flow import FlowDeckApp

FAKE_FRAME = {
    "leaders": [
        {"ticker": "NVDA", "sector": "Tech", "price": 142.3, "pct": 2.1,
         "spark": "▁▂▄▇█", "rvol": 4.8, "opt_vol": 214000, "opt_oi": 1820000,
         "optvol_rvol": 3.1, "call_notional": 3_200_000, "put_notional": 0},
    ],
    "contracts": [
        {"label": "🔴 EXTREME", "ticker": "NVDA", "type": "call", "strike": 145,
         "expiry": "2026-06-13", "dte": 2, "trade_side": "ask",
         "volume": 18000, "open_interest": 3000, "vol_oi": 6.1,
         "ticker_oi_vs_avg": 6.1, "notional": 3_200_000, "score": 88},
    ],
    "net_call": 3_200_000, "net_put": 0,
    "source": "TEST", "live": False, "universe_size": 1, "scan_secs": 0.0,
}


async def test_app_populates_both_tables():
    app = FlowDeckApp(data_fn=lambda: FAKE_FRAME, interval=0)
    async with app.run_test() as pilot:
        await pilot.pause()
        leaders = app.query_one("#leaders", DataTable)
        contracts = app.query_one("#contracts", DataTable)
        assert leaders.row_count == 1
        assert contracts.row_count == 1


async def test_sort_hotkey_changes_sort_key():
    app = FlowDeckApp(data_fn=lambda: FAKE_FRAME, interval=0)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = app.sort_key
        await pilot.press("s")
        assert app.sort_key != before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_live_flow_app.py -v`
Expected: FAIL — `ImportError: cannot import name 'FlowDeckApp'`.

- [ ] **Step 3: Write minimal implementation**

Append to `core/live_flow.py`:
```python
from datetime import date, datetime

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

_SORTS = ["score", "rvol", "notional", "oi_vs_avg"]


def gather_frame(top: int = 30, min_score: int = 35,
                 exclude_etfs: bool = True) -> dict:
    """Real data assembler: universe -> scan -> rollups. Network-bound."""
    import time as _time
    from data.unusual_flow import scan_unusual_flow
    from data.sources import available_sources
    from data.baseline import BaselineStore

    store = BaselineStore()
    t0 = _time.time()
    signals = scan_unusual_flow(min_score=min_score, max_results=top * 5,
                                exclude_etfs=exclude_etfs)
    secs = round(_time.time() - t0, 1)

    agg = aggregate_by_ticker(signals)
    today = date.today().isoformat()
    leaders = []
    for tkr, a in sorted(agg.items(),
                         key=lambda kv: kv[1]["opt_vol"], reverse=True)[:top]:
        store.record_ticker(today, tkr, a["opt_vol"], a["opt_oi"], 0)
        leaders.append({
            "ticker": tkr, "sector": a["sector"], "price": 0.0, "pct": 0.0,
            "spark": "", "rvol": 0.0, "opt_vol": a["opt_vol"], "opt_oi": a["opt_oi"],
            "optvol_rvol": store.ticker_optvol_rvol(tkr, a["opt_vol"]) or 0.0,
            "call_notional": a["call_notional"], "put_notional": a["put_notional"],
        })
    for s in signals:
        store.record_contract(today, s["ticker"], s["type"], s["strike"],
                              s["expiry"], s["open_interest"], s["volume"])
    call, put = net_flow(signals)
    srcs = available_sources()
    return {
        "leaders": leaders,
        "contracts": [dict(s) for s in signals[:top]],
        "net_call": call, "net_put": put,
        "source": (srcs[0] if srcs else "yfinance").upper(),
        "live": bool(srcs and srcs[0] not in ("yfinance", "yahoo")),
        "universe_size": len(agg), "scan_secs": secs,
    }


class FlowDeckApp(App):
    CSS = """
    Screen { background: #0a0a0f; }
    #status { height: 1; color: #00ff88; background: #11131a; }
    DataTable { background: #0a0a0f; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("p", "toggle_pause", "Pause"),
        ("s", "cycle_sort", "Sort"),
        ("r", "refresh_now", "Refresh"),
    ]

    def __init__(self, data_fn=None, interval: int = 45,
                 top: int = 30, min_score: int = 35):
        super().__init__()
        self.data_fn = data_fn or (lambda: gather_frame(top, min_score))
        self.interval = interval
        self.sort_key = "score"
        self.paused = False
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("FLOWDECK ▸ starting…", id="status")
        with Vertical():
            yield Static("VOLUME LEADERS (single stocks)", classes="hdr")
            yield DataTable(id="leaders")
            yield Static("UNUSUAL CONTRACTS (driving it)", classes="hdr")
            yield DataTable(id="contracts")

    def on_mount(self) -> None:
        lt = self.query_one("#leaders", DataTable)
        lt.add_columns("TICKER", "SECTOR", "PRICE", "%CHG", "VOL",
                       "RVOL", "OPT VOL", "OPT OI", "OPTvAVG", "FLOW")
        ct = self.query_one("#contracts", DataTable)
        ct.add_columns("SIGNAL", "TICKER", "STRIKE", "EXP", "DTE", "SIDE",
                       "VOL", "OI", "V/OI", "OIvAVG", "NOTIONAL", "SCR")
        self.refresh_now_worker()
        if self.interval and self.interval > 0:
            self._timer = self.set_interval(self.interval, self.refresh_now_worker)

    # ── data refresh runs off the UI thread ──
    def refresh_now_worker(self) -> None:
        if self.paused:
            return
        self.run_worker(self._do_refresh, thread=True, exclusive=True)

    def _do_refresh(self) -> None:
        try:
            frame = self.data_fn()
        except Exception as e:  # never let the loop die
            self.call_from_thread(self._set_status, f"error: {e}")
            return
        self.call_from_thread(self._render, frame)

    def _ticker_oi_str(self, v) -> str:
        return f"{v:.1f}x" if isinstance(v, (int, float)) and v else "NEW"

    def _render(self, frame: dict) -> None:
        lt = self.query_one("#leaders", DataTable)
        lt.clear()
        leaders = frame["leaders"]
        if self.sort_key == "rvol":
            leaders = sorted(leaders, key=lambda r: r["optvol_rvol"], reverse=True)
        for r in leaders:
            flow = r["call_notional"] - r["put_notional"]
            arrow = "🟢" if flow >= 0 else "🔴"
            lt.add_row(
                r["ticker"], r["sector"], f'{r["price"]:.2f}', f'{r["pct"]:+.1f}%',
                r["spark"], f'{r["rvol"]:.1f}x', fmt_compact(r["opt_vol"]),
                fmt_compact(r["opt_oi"]), self._ticker_oi_str(r["optvol_rvol"]),
                f'{arrow} {fmt_compact(flow)}',
            )
        ct = self.query_one("#contracts", DataTable)
        ct.clear()
        contracts = frame["contracts"]
        keymap = {"score": "score", "notional": "notional",
                  "oi_vs_avg": "ticker_oi_vs_avg", "rvol": "score"}
        k = keymap.get(self.sort_key, "score")
        contracts = sorted(contracts, key=lambda c: (c.get(k) or 0), reverse=True)
        for c in contracts:
            t = "C" if c["type"] == "call" else "P"
            ct.add_row(
                c["label"], c["ticker"], f'{t}{int(c["strike"])}', c["expiry"],
                f'{c["dte"]}d', c.get("trade_side", ""), fmt_compact(c["volume"]),
                fmt_compact(c["open_interest"]), f'{c["vol_oi"]:.1f}x',
                self._ticker_oi_str(c.get("ticker_oi_vs_avg")),
                f'${fmt_compact(c["notional"])}', str(c["score"]),
            )
        net = frame["net_call"] - frame["net_put"]
        tag = "LIVE" if frame["live"] else "DELAYED"
        self._set_status(
            f'FLOWDECK ▸ {datetime.now():%H:%M:%S}  NET {fmt_compact(net)}  '
            f'{frame["source"]} {tag} · {frame["universe_size"]} names · '
            f'scan {frame["scan_secs"]}s · sort:{self.sort_key}'
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    # ── actions ──
    def action_cycle_sort(self) -> None:
        i = (_SORTS.index(self.sort_key) + 1) % len(_SORTS)
        self.sort_key = _SORTS[i]
        self.refresh_now_worker()

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused

    def action_refresh_now(self) -> None:
        self.refresh_now_worker()


def run(interval: int = 45, top: int = 30, min_score: int = 35) -> None:
    FlowDeckApp(interval=interval, top=top, min_score=min_score).run()
```

Note: `action_cycle_sort` is bound to `s`; the test asserts `sort_key` changes after pressing `s`. With `interval=0` no timer runs, so the render uses the injected `data_fn` once on mount.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_live_flow_app.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run full suite**

Run: `python3 -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add core/live_flow.py tests/test_live_flow_app.py
git commit -m "feat: FlowDeck Textual app + data frame assembler

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `--live` flag + README

**Files:**
- Modify: `core/scanner.py` (argparse + dispatch)
- Modify: `README.md` (run section)
- Test: `tests/test_cli_live_flag.py`

- [ ] **Step 1: Write the failing test**

`tests/test_cli_live_flag.py` (new):
```python
from core.scanner import build_parser


def test_parser_has_live_and_interval():
    p = build_parser()
    ns = p.parse_args(["--live", "--interval", "30"])
    assert ns.live is True
    assert ns.interval == 30


def test_live_defaults_off():
    ns = build_parser().parse_args([])
    assert ns.live is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cli_live_flag.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_parser'` (or `live` attribute missing).

- [ ] **Step 3: Write minimal implementation**

In `core/scanner.py`, refactor `main()` so the parser is built by a named function. Find the `def main() -> None:` block (line ~2786) where `parser = argparse.ArgumentParser(...)` is created. Extract everything from `parser = argparse.ArgumentParser(` through the last `add_argument` into:
```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FlowScanner — 0DTE setups + unusual options flow",
    )
    # ... (keep all existing add_argument calls) ...
    parser.add_argument("--live", action="store_true",
                        help="launch the live FlowDeck terminal dashboard")
    parser.add_argument("--interval", type=int, default=45,
                        help="live refresh interval in seconds (default 45)")
    return parser
```
Then in `main()` replace the inline parser construction with:
```python
def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, "live", False):
        from core.live_flow import run as run_live
        run_live(interval=args.interval,
                 top=getattr(args, "enrich_top", 30) or 30,
                 min_score=35)
        return
    # ... existing main() body continues unchanged ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_cli_live_flag.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Update README run section**

In `README.md`, under **Run**, add:
```markdown
**Live FlowDeck (continuous terminal dashboard):**
```bash
python3 core/scanner.py --live                 # 45s refresh, single stocks only
python3 core/scanner.py --live --interval 30
```
Optional real-time options data: `export TRADIER_TOKEN=…` (free, developer.tradier.com).
Without it, data is ~15 min delayed and the status bar shows `DELAYED`.
```

- [ ] **Step 6: Full suite + manual smoke**

Run: `python3 -m pytest -q`
Expected: all pass.

Manual (market hours, optional): `python3 core/scanner.py --live` — confirm two panels populate, no ETFs appear, `q` quits, `s` cycles sort, status bar shows source + scan latency.

- [ ] **Step 7: Commit**

```bash
git add core/scanner.py README.md tests/test_cli_live_flag.py
git commit -m "feat: add --live flag to launch FlowDeck dashboard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Continuous refresh loop → Task 7 (`set_interval`, worker). ✓
- Live terminal / Textual dense-grid cockpit → Tasks 6–7. ✓
- Two panels, VOL + OI + option-vol separate → Task 7 columns + Task 6 rollup. ✓
- ETF exclusion everywhere (scanner + universe + web app via universe) → Tasks 2, 5. ✓
- Baseline-relative OI/volume (`OIvAVG`, `NEW` until built) → Tasks 3, 5, 7. ✓
- Intra-chain outlier (day-one signal) → Tasks 4, 5. ✓
- Baseline multiplier (boost spikes, dampen mega-caps) → Tasks 4, 5. ✓
- Tradier LIVE/DELAYED indicator, yfinance fallback → Task 7 (`available_sources`, status). ✓
- New module so `scanner.py` doesn't grow → Tasks 6–7. ✓
- Error degradation (no baseline, failed fetch) → Task 7 (`_do_refresh` try/except; `NEW`). ✓
- Testing (ETF filter, baseline, scoring, rollup, render smoke) → Tasks 2,3,4,5,6,7. ✓
- Setup notes / requirements → Tasks 1, 8. ✓

**Out of scope (per spec, intentionally not tasked):** TastyTrade tick streaming, push alerts, `textual serve`, time-of-day-normalized intraday baseline.

**Placeholder scan:** No TBD/TODO; every code step shows full code. Version pin `X.Y.Z` in Task 1 is resolved from the install command's printed output (explicit instruction, not a placeholder).

**Type consistency:** `score_contract` emits `ticker_oi_vs_avg`, `ticker_optvol_rvol`, `intra_chain_z` (Task 5); the app reads `ticker_oi_vs_avg` and the leader rollup reads `optvol_rvol` from `gather_frame` (Task 7) — consistent. `aggregate_by_ticker` keys (`opt_vol`, `opt_oi`, `call_notional`, `put_notional`) match between Tasks 6 and 7. `available_sources`/`scan_unusual_flow(exclude_etfs=)` signatures match Task 5 changes.

**Note for executor:** Task 5 also touches the deployed web app's data (universe loses ETFs) — intended per spec §5.5. Do not push to `master` (Railway auto-deploys); work stays on `feat/live-flow-deck` until Dante approves a merge.
