# Sectors Rework: Individuals-Only + Tap-to-Heatmap

**Date:** 2026-06-25
**Status:** Approved (pending spec review)
**Surface:** Scanner Pro web app (`flowscanner-production.up.railway.app`), SECTORS tab

## Problem

The SECTORS tab today shows the 10 SPDR sector **ETFs** and labels each card with its
cryptic ETF ticker (`XLK`, `XLF`, …). The "TOP LAGGARD" banner is computed as the
worst-performing *sector ETF* vs the ETF average — so it surfaces an ETF
(e.g. `XLC (CommSvcs)`), not an individual stock.

The user wants:

1. **No ETFs surfaced** — sectors and laggards should be about individual stocks.
2. **Top laggard = the diverging individual** — the stock that stands out against its
   sector (e.g. the only red name when the whole sector is green).
3. **Tap a sector → visual heatmap** of the individual stocks in that sector, so the
   move is readable at a glance.
4. **Real sector names** displayed everywhere — "Technology", not "XLK". ETF tickers
   are never shown to the user.

## Non-Goals

- No change to the FLOW / SCAN / FIND / INTEL tabs.
- No true market-cap weighting (would require an extra network call per stock). Treemap
  tiles are sized by today's dollar volume, which is already fetched.
- No new data provider — reuse the existing yfinance batch fetch + the live
  S&P 500 / GICS sector map.

## Design

### 1. Sector model

The 11 GICS sectors, each shown by its **full readable name**. An ETF proxy is kept
internally only to compute the sector's headline % move; the ticker is never rendered.

| Display name            | Internal ETF proxy | GICS sector source name   |
|-------------------------|--------------------|---------------------------|
| Technology              | XLK                | Information Technology    |
| Communication Services  | XLC                | Communication Services    |
| Consumer Discretionary  | XLY                | Consumer Discretionary    |
| Consumer Staples        | XLP                | Consumer Staples          |
| Financials              | XLF                | Financials                |
| Health Care             | XLV                | Health Care               |
| Energy                  | XLE                | Energy                    |
| Industrials             | XLI                | Industrials               |
| Materials               | XLB                | Materials                 |
| Utilities               | XLU                | Utilities                 |
| Real Estate             | XLRE               | Real Estate               |

This adds **Consumer Discretionary (XLY)**, which the current 10-card grid omits, so
every individual stock maps to exactly one card.

`SECTOR_ETFS` in `core/scanner.py` becomes keyed by full display name and gains XLY.
The old short keys (`Tech`, `CommSvcs`, …) are replaced; any internal references
(`TICKER_SECTOR`, `find_sector_laggards`) are updated to the new keys.

### 2. Constituents = individuals only

A new module `data/sector_constituents.py`:

- `GICS_TO_SECTOR: Dict[str, str]` — maps the GICS sector strings returned by
  `data.unusual_flow.get_ticker_sector_map()` to the 11 display names above.
- `constituents_for(sector: str) -> List[str]` — inverts the live cached ticker→sector
  map into `sector → [tickers]`, filtered to individual stocks only
  (ETF backstop via `data.etf_filter`). Falls back to the static `TICKER_SECTOR`
  entries for that sector if the live map is empty (market closed / fetch failed).

All constituents come from the S&P 500 + GICS map, which contains no ETFs by
construction. No ETF can appear in a heatmap or as a laggard.

### 3. Per-sector heatmap (lazy, on tap)

New fetch/compute function in `core/scanner.py` (next to the existing batch helpers):

```
def sector_heatmap(sector: str, limit: int = 30) -> dict
```

- Looks up `constituents_for(sector)`.
- One batched OHLCV fetch (`_fetch_batch_history(tickers, period="5d")`) — same path
  `scan_sectors` already uses, with the individual-fetch fallback for cloud IP blocks.
- For each stock computes `change_pct` (last close vs prior close) and
  `dollar_vol = price * today_volume`.
- Sorts by `dollar_vol` desc, keeps the top `limit` (default 30) for readability.
- Returns:

```json
{
  "sector": "Energy",
  "change": 0.97,
  "updated": "18:42:05",
  "stocks": [
    {"ticker": "XOM", "change": 1.8, "weight": 4200000000},
    {"ticker": "MP",  "change": -1.4, "weight": 310000000}
  ]
}
```

`weight` is dollar volume; the frontend uses it only for relative tile area.

Results cached in-process per sector for 60s so repeated taps don't refetch.

### 4. Top laggard = diverging individual

New function in `core/scanner.py`:

```
def top_individual_laggard(sector_data: dict) -> dict | None
```

- From `sector_data` (the cheap 11-ETF scan already done by `scan_sectors`), select
  sectors with a real move: `abs(change_pct) >= 0.5`.
- Take the **strongest** such sectors (top 2 by `abs(change_pct)`).
- For each, pull its constituents via the same batched fetch and find the individual
  stock with the largest divergence against the sector direction:
  - green sector (`change_pct > 0`): maximize `sector_chg - stock_chg` (the laggy/red name)
  - red sector (`change_pct < 0`): maximize `stock_chg - sector_chg` (the green outlier)
- Returns the single biggest-divergence stock across those sectors:

```json
{
  "ticker": "MP",
  "sector": "Energy",
  "sector_change": 0.97,
  "stock_change": -1.4,
  "divergence": 2.37
}
```

Reuses the divergence math already in `find_sector_laggards`; that function is updated
to the new sector keys but otherwise unchanged (still used by the FLOW scan).

To bound latency on `/api/sectors`, the laggard scan touches at most 2 sectors'
constituents (~50 tickers, one or two batched calls).

### 5. API

- `GET /api/sectors` (existing) — unchanged sector cards, but `laggard` is now the
  individual-stock object from `top_individual_laggard`. Cards no longer expose the ETF
  to the client (the `etf` field is dropped from the response).
- `GET /api/sector/{name}/heatmap` (new) — returns the section-3 payload. Rate-limited
  like the other endpoints; PIN-checked; 45s timeout with the same graceful errors.

### 6. Frontend (SECTORS tab in `web/app.py`)

- Sector cards: render the full display name as the title; **remove** the `sc-etf`
  ETF-ticker subtitle line. Everything else (change %, bar, vol meta) stays.
- Tap a card → toggle an inline expansion panel directly under the grid (tap again or
  tap another card to close). Panel:
  - Header: sector name + sector % + close affordance.
  - Treemap: tiles sized by `weight` (dollar volume), colored on a green→red gradient by
    `change`. Each tile shows ticker + change%. Tapping a tile shows a toast with the
    name (no navigation).
  - Loading skeleton while the heatmap fetch is in flight; graceful empty/error state
    (e.g. market closed).
- Treemap layout: a lightweight squarified treemap implemented in vanilla JS (no new
  dependencies), matching the existing hand-rolled DOM style in the file. Falls back to
  an equal-tile grid if all weights are zero (e.g. pre-market with no volume).
- Laggard banner: render `ticker`, `sector`, and the two percentages
  (`Energy +0.97% · MP −1.40%`) plus `diverges −2.37%`. Real names only.

## Data Flow

```
SECTORS tab opens
  └─ GET /api/sectors
        ├─ scan_sectors()            → 11 ETF moves (cheap, 1 batch)
        └─ top_individual_laggard()  → ≤2 strongest sectors' constituents → diverging stock
  → render 11 named cards + individual laggard banner

User taps "Energy"
  └─ GET /api/sector/Energy/heatmap
        └─ constituents_for("Energy") → batch OHLCV → change% + dollar_vol, top 30
  → render squarified treemap under the grid
```

## Error Handling

- Heatmap fetch failure / empty: panel shows "No data — market may be closed",
  matching existing empty-state styling.
- Live GICS map empty: `constituents_for` falls back to static `TICKER_SECTOR` names so
  the feature still works offline/closed.
- All weights zero: treemap renders as an equal-tile grid (still color-coded).
- Endpoints keep the existing PIN check, rate limit, 45s timeout, and 504/503 handling.

## Testing

- `tests/test_sector_constituents.py`:
  - `constituents_for` returns only individual stocks (no ETF tickers), correct bucket.
  - GICS→sector mapping covers all 11 GICS sector strings.
  - Fallback to static map when the live map is empty.
- `tests/test_sector_laggard.py`:
  - Green sector with one red constituent → that stock is the laggard.
  - Red sector → the green outlier is surfaced.
  - No sector ≥ 0.5% move → returns `None`.
  - Laggard is never an ETF ticker.
- `sector_heatmap` sizing/sorting verified with a stubbed batch fetch (top-N by dollar
  volume, weights populated).
- Existing `test_etf_filter`, `test_scoring`, `test_engine_exclusions` stay green.

## Out of Scope / Future

- True market-cap tile weighting.
- Intraday (per-minute) sector refresh / streaming.
- Drill-down from a treemap tile into that stock's options flow.
