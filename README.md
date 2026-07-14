# FlowScanner

Live market scanner + quantitative backtesting engine for 0DTE options setups.

**Live:** flowscanner-production.up.railway.app

---

## Structure

```
scanner/
├── core/
│   ├── scanner.py        ← main engine: signals, scoring, options contracts, CLI
│   └── backtest.py       ← backtesting engine
├── data/
│   ├── tt_flow.py        ← TastyTrade live options flow
│   ├── finra_darkpool.py ← FINRA dark pool prints
│   ├── sec_insider.py    ← SEC insider activity
│   └── fred_macro.py     ← FRED macro context
├── web/
│   └── app.py            ← deployed FastAPI web app (Railway)
├── results/              ← backtest output (CSV + JSON)
└── requirements.txt
```

---

## Setup

```bash
pip install -r requirements.txt
```

---

## Run

**Web app (local):**
```bash
python3 web_combined.py
# open http://localhost:8765
```

**CLI scanner:**
```bash
python3 scanner.py
python3 scanner.py --filter momentum --sort score
python3 scanner.py --tickers NVDA AMD TSLA
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
python3 backtest.py
python3 backtest.py --days 60 --tickers ALL
```

---

## Deploy

Deployed on Railway. Push to master triggers redeploy.

```bash
git push origin master
```

Entry point: `python3 web_combined.py` (see `railway.toml`)
