# FlowDeck — Live Single-Stock Volume & Unusual Options Scanner

**Date:** 2026-06-11
**Status:** Design approved (UI vibe: dense-grid cockpit) — pending spec review
**Surface:** Live terminal TUI (Textual), single process

---

## 1. Goal

A continuously-refreshing terminal dashboard that shows **where volume is going right now** — in single stocks (no ETFs) and in their option contracts — and flags activity that is **unusual relative to that name's own baseline**, not just big in absolute terms.

The core insight driving the design: *a 10K-volume call on NVDA is noise; the same on a name that never trades options is a flare.* Flagging must be relative to each name's own history.

---

## 2. Non-negotiables (from Dante)

1. **Single tickers only — ETFs excluded everywhere.** Applies to this scanner *and* the existing web app / universe builder.
2. **Volume and Open Interest shown separately**, never collapsed into only a ratio.
3. **Baseline-relative flagging** — compare today's OI/volume to what that contract/name *normally* gets. Quiet name lighting up = strong signal; always-active mega-cap = dampened.
4. **Continuous / automatic** — refreshes on its own on a loop; he watches, doesn't trigger.

---

## 3. What already exists (reused, not rebuilt)

- `data/unusual_flow.py` — options-flow engine: live universe build, relative-volume screen, parallel option-chain scan, 0–100 anomaly score (notional, vol/OI, DTE, OTM, ask-side), sector rollup.
- `core/universe.py` — live universe builder (most-active + gainers/losers + S&P 500 + Nasdaq-100 + high-relvol).
- `data/sources.py` — option-chain provider abstraction: Tradier → Schwab → Polygon → Yahoo → yfinance, with `available_sources()`.
- `core/scanner.py` — has an `interactive_loop()` (`while True`) scaffold and argparse.

This feature **evolves** these; it does not duplicate them.

---

## 4. Architecture

```
core/scanner.py  --live  ─────────────►  core/live_flow.py   (Textual app + loop)
                                              │
                 ┌────────────────────────────┼──────────────────────────┐
                 ▼                            ▼                           ▼
        data/unusual_flow.py         data/etf_filter.py          data/baseline.py
        (scan, now ETF-free,         (is_etf / filter_etfs,      (record + query OI/vol
         + baseline scoring)          quoteType cache)            history → OIvAVG, RVOL)
                 │
                 ▼
        data/sources.py  (existing; Tradier real-time if TRADIER_TOKEN set, else delayed)
```

**Why a new module:** `core/scanner.py` is already ~2,870 lines. The live TUI goes in a focused new file `core/live_flow.py`; `scanner.py` gets only a thin `--live` flag that imports and runs it. Keeps the heavy file from growing.

**Concurrency:** the scan (network-bound, several seconds) runs in a **Textual worker thread**, never on the UI thread. The dashboard stays responsive and updates reactively when fresh results land. If a scan overruns the interval, the next tick is skipped rather than queued.

---

## 5. Components

### 5.1 `core/live_flow.py` — the TUI (new)
Textual `App` rendering the dense-grid cockpit (see §6). Responsibilities:
- Own the refresh loop (default 45s, `--interval` configurable).
- Dispatch scans to a worker; receive results; update two `DataTable` panels + status bar.
- Heat-color cells, render sparklines, flash on a fresh 🔴 EXTREME contract.
- Handle hotkeys (sort, pause, filter, interval, quit).

### 5.2 `data/etf_filter.py` — ETF exclusion (new, shared)
- `is_etf(ticker) -> bool` and `filter_etfs(tickers) -> list`.
- Two-tier: (1) a known-ETF set seeded from the symbols currently hardcoded as anchors in `unusual_flow.py` (`_ETF_ANCHORS`) and `universe.py` (`ANCHOR`) — these are exactly the ETFs that pollute results today; (2) for unknown screener tickers, a `yfinance` `quoteType == 'ETF'` check, cached to `data/baselines/etf_cache.json` (`ticker → EQUITY|ETF`) so it's queried at most once per name.
- Used by the universe builder, the flow scanner pool, and as a final assertion on both panels (no ETF may render).

### 5.3 `data/baseline.py` — baseline store (new)
- **Storage:** SQLite at `data/baselines/baseline.db`.
- **Tables:**
  - `contract_obs(obs_date, ticker, opt_type, strike, expiry, oi, volume, PRIMARY KEY(obs_date,ticker,opt_type,strike,expiry))`
  - `ticker_obs(obs_date, ticker, total_opt_vol, total_oi, equity_vol, PRIMARY KEY(obs_date,ticker))`
- **Write:** each scan upserts the current day's row (last-write-wins ≈ latest snapshot of the day; OI is EOD-stable, volume is cumulative).
- **Read / derived signals:**
  - `contract_oi_vs_avg(...)` → `today_oi / mean(prior-day oi for same contract)`; returns `None` (→ renders `NEW`) when no prior history.
  - `ticker_optvol_rvol(...)` → today's total option volume ÷ N-day (default 20) average for that ticker.
- The longer it runs, the sharper these get. No fabricated numbers before history exists.

### 5.4 Changes to `data/unusual_flow.py`
- **Remove** the force-included `_ETF_ANCHORS` injection (the always-prepend-anchors behavior). Replace with `filter_etfs()` on the pool. Add `exclude_etfs: bool = True` param.
- **Add baseline scoring**: a multiplier in `[0.5, 1.5]` applied to the existing 0–100 score:
  - boost when `contract_oi_vs_avg ≥ 3×` and/or `ticker_optvol_rvol` is high (spiking vs own norm);
  - **dampen** when the name's baseline option volume is very high (perennial mega-cap) so it stops hogging the top;
  - neutral `1.0` when no baseline yet.
- **Add intra-chain outlier** (zero-history fallback so day-one is still useful): score each contract's volume against the *same ticker's chain today* (vs chain median), so a single lit-up strike stands out even before the baseline store has data.
- Emit new per-signal fields: `oi_vs_avg` (float or `None`), `intra_chain_z`.

### 5.5 Changes to `core/universe.py`
- Stop hardcoding ETF `ANCHOR`s into the universe. Run the assembled list through `filter_etfs()`. Mega-cap *stocks* (AAPL/MSFT/NVDA…) stay; ETFs (SPY/QQQ/TQQQ/XLK…) go.
- **Ripple effect (intended):** the deployed web app reads this universe, so it too loses ETFs — consistent with the "all scanners, single tickers only" rule.

### 5.6 Changes to `core/scanner.py`
- Add `--live` (and pass-through `--interval`, `--min-score`, `--top`). When set, import `core.live_flow` and run the app. No other behavior change.

### 5.7 Data source / `data/sources.py`
- v1 runs on the **existing** `get_option_chain` chain. With no broker keys, that's yfinance — **~15 min delayed** (stated honestly in the UI).
- **Tradier real-time hook (optional, no code change):** `sources.py` already supports `TRADIER_TOKEN`. Setup note only: free token at developer.tradier.com → `export TRADIER_TOKEN=…` → Panel B goes real-time. The status bar reflects `available_sources()` as **LIVE** vs **DELAYED**.
- **Out of scope (future):** wiring `data/tt_flow.py` TastyTrade tick-streaming into the loop.

---

## 6. UI — dense-grid cockpit

Single screen, two stacked panels + a status bar. Neon-on-black (`#00ff88` bull / `#ff3355` bear), JetBrains Mono.

```
 FLOWDECK ▸ LIVE     single-stock vol + options              14:32:07
 ──────────────────────────────────────────────────────────────────
 VOLUME LEADERS                                       (single stocks)
 TICKER SECTOR  PRICE   %CHG  VOL      RVOL  OPTVOL  OPT OI  OPTvAVG  FLOW
 NVDA   Tech   142.30 ▲2.1% ▁▂▄▇█  4.8x  214.0K  1.82M   3.1x    🟢 +$3.2M
 PLTR   Tech    28.44 ▲5.6% ▁▁▃▅█  3.9x   88.1K  640.2K  NEW     🟢 +$1.1M
 SMCI   Tech    41.02 ▲8.9% ▂▅█▇▆  7.1x   61.0K  120.4K  9.4x    🔴 -$0.6M
 ──────────────────────────────────────────────────────────────────
 UNUSUAL CONTRACTS                                    (driving it)
 SIGNAL     TICKER STRIKE  EXP    DTE SIDE  VOL    OI    V/OI OIvAVG NOTIONAL SCR
 🔴 EXTREME NVDA   C145   06-13  2d  ask  18.2K  3.0K  6.1x  6.1x   $3.2M    88
 🟠 UNUSUAL PLTR   C30    06-13  2d  ask   9.4K  2.1K  4.5x  NEW    $1.1M    71
 🟡 NOTABLE SMCI   P38    06-20  9d  bid   5.1K  1.2K  4.2x  3.3x   $0.6M    63
 ──────────────────────────────────────────────────────────────────
 NET ▮▮▮▮▮▮▮▮░░ +$6.8M calls · TRADIER LIVE · 42 names · scan 1.3s
```

**Panel A — Volume Leaders (single stocks):** `TICKER · SECTOR · PRICE · %CHG · VOL sparkline · RVOL (equity) · OPT VOL · OPT OI · OPTvAVG (ticker option-vol RVOL from baseline) · FLOW (net call$−put$, colored)`. Volume, OI, and equity-vs-option volume are all visible and distinct.

**Panel B — Unusual Contracts:** `SIGNAL · TICKER · TYPE/STRIKE · EXP · DTE · SIDE · VOL · OI · V/OI · OIvAVG (contract OI vs own history; NEW until built) · NOTIONAL · SCR`.

**Status bar:** clock · `LIVE`/`DELAYED` + provider · universe size · scan latency · NET FLOW gauge (call$ vs put$).

**Heat / motion:** cell background scales green→red by RVOL and SCR; a fresh 🔴 EXTREME row pulses once on arrival; OIvAVG glows when a quiet name spikes.

**Hotkeys:** `q` quit · `p` pause/resume · `s` cycle sort (SCR / RVOL / NOTIONAL / OIvAVG) · `c` calls-only · `u` puts-only · `[` / `]` interval down/up · `r` force refresh.

---

## 7. Data flow per refresh

1. Get cached universe (15 min TTL) and sector map (6 h TTL); apply `filter_etfs()`.
2. Equity relative-volume screen → top N single stocks (existing `_screen_relvol`).
3. Parallel option-chain scan on those names (existing engine, 10 workers) → contracts.
4. Enrich each contract with `oi_vs_avg` + `intra_chain_z`; compute baseline-adjusted score.
5. Roll up per-ticker: OPT VOL, OPT OI, net call/put $, `OPTvAVG`.
6. Record snapshots to baseline store.
7. Post results to UI; tables + status bar update; flash new extremes.

---

## 8. Error handling & degradation

- **Failed fetch:** keep last good frame, mark it stale in the status bar; never crash the loop.
- **No baseline yet:** OIvAVG / OPTvAVG render `NEW`; baseline multiplier = neutral 1.0.
- **Market closed:** show last session + a `CLOSED` indicator; loop slows.
- **No Tradier token:** `DELAYED` indicator, yfinance fallback — explicit, not hidden.
- **Scan overruns interval:** skip next tick; show real scan latency so cadence is honest.

---

## 9. Testing

- **ETF filter:** `[SPY, AAPL, QQQ, NVDA] → [AAPL, NVDA]`; quoteType path mocked; cache hit path.
- **Baseline store:** record → query average; OIvAVG math; `NEW` when no history; N-day window.
- **Scoring:** baseline multiplier *boosts* a low-baseline spike and *dampens* a perennial mega-cap (synthetic fixtures); intra-chain outlier flags a single lit strike.
- **Flow rollup:** net call$−put$ and call/put gauge math.
- **Render smoke test:** Textual `Pilot` boots the app with mock data, asserts both panels populate and hotkeys switch sort.

---

## 10. Setup / run

- `pip install textual` (added to `requirements.txt`).
- Optional real-time: `export TRADIER_TOKEN=…` (free, developer.tradier.com).
- Run: `python3 core/scanner.py --live` (`--interval 45 --top 30 --min-score 35`).

---

## 11. Out of scope (future layers)

- TastyTrade tick-streaming (`tt_flow.py`) as the Panel B source.
- Push alerts / notifications (deliberately omitted — constant pings can feed overtrading).
- `textual serve` to browser/phone (free once the Textual app exists; not built now).
- Time-of-day-normalized intraday volume baseline (v1 compares to prior full-day averages).


> **Correction (2026-08-31):** the Tradier notes above are wrong. Tradier serves real-time data only to Tradier *brokerage* account holders; a free developer.tradier.com token is delayed. Live flow comes from the TastyTrade OPRA path (`data/tt_flow.py`), not Tradier.
