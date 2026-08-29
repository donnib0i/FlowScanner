# FlowScanner

Live market scanner + quantitative backtesting engine for 0DTE options setups.

**Live:** flowscanner-production.up.railway.app

---

## Structure

The scan is split by responsibility. `core/scanner.py` is a thin front door that
re-exports every module below, so `from core.scanner import X` keeps working —
but edit the module that *owns* the behavior, not the facade.

```
scanner/
├── core/
│   ├── scanner.py        ← facade: re-exports everything, CLI entry point
│   ├── runtime.py        ← sys.path / warnings / colorama setup
│   ├── constants.py      ← universe, sector map, every threshold and tunable
│   ├── market_data.py    ← yfinance session, option-chain cache, VIX, quotes
│   ├── fmt.py            ← terminal formatting: colors, bars, short numbers
│   ├── technicals.py     ← breakout labels, key levels, unfilled gaps
│   ├── options.py        ← BS delta, strike ladder, IV rank, contract scoring
│   ├── sectors.py        ← sector scans, laggards, heatmaps, breakout plays
│   ├── flow.py           ← options flow (TastyTrade live / yfinance delayed)
│   ├── pipeline.py       ← per-ticker scan → enrich → filter → sort
│   ├── report.py         ← tables, inline panels, CSV export
│   ├── cli.py            ← argparse, interactive loop, TradingView loader
│   ├── backtest.py       ← backtesting engine
│   ├── calibration.py    ← signal calibration
│   └── entry_lag.py      ← entry-lag measurement
├── data/
│   ├── tt_flow.py        ← TastyTrade live options flow
│   ├── finra_darkpool.py ← FINRA dark pool prints
│   ├── sec_insider.py    ← SEC insider activity
│   └── fred_macro.py     ← FRED macro context
├── web/
│   ├── app.py            ← FastAPI app: routes, auth, caching (Railway)
│   ├── templates/
│   │   └── index.html    ← page markup
│   └── static/
│       ├── app.css       ← all styling
│       └── app.js        ← all front-end logic
├── results/              ← backtest output (CSV + JSON)
└── requirements.txt
```

**Where to make a change**

| Want to change... | Edit |
|---|---|
| A threshold, the universe, a sector mapping | `core/constants.py` |
| How a contract is picked or scored | `core/options.py` |
| What the terminal prints | `core/report.py` |
| A CLI flag | `core/cli.py` |
| How the page looks | `web/static/app.css` |
| How the page behaves | `web/static/app.js` |
| The page's markup | `web/templates/index.html` |
| An API endpoint | `web/app.py` |

The CSS and JS are inlined into a single response at app import, so the PWA
still loads in one request. Edit the file and restart the server.

**Note on tests:** `core/scanner.py` only re-exports. To monkeypatch a name,
patch it on the module that *resolves* it (e.g. `core.sectors._quotes_for`),
not on `core.scanner`.

---

## Setup

```bash
pip install -r requirements.txt
```

---

## Run

**Web app (local):**
```bash
python3 web/app.py
# open http://localhost:8765
```

**CLI scanner:**
```bash
python3 core/scanner.py
python3 core/scanner.py --filter breakout --sort setup
python3 core/scanner.py --tickers NVDA AMD TSLA
```

**Live FlowDeck (continuous terminal dashboard):**
```bash
python3 core/scanner.py --live                 # 45s refresh, single stocks only
python3 core/scanner.py --live --interval 30
```
Single-stock volume + unusual options flow, refreshed continuously. No ETFs.
Optional real-time options data: `export TRADIER_TOKEN=…` (free, developer.tradier.com).
Without it, data is ~15 min delayed and the status bar shows `DELAYED`.

**Backtest:**
```bash
python3 core/backtest.py
python3 core/backtest.py --days 60 --tickers ALL
```

---

## Deploy

Deployed on Railway. Push to master triggers redeploy.

```bash
git push origin master
```

Entry point: `python3 web/app.py` (see `railway.toml`)
