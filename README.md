# FlowScanner

Live market scanner + quantitative backtesting engine for 0DTE options setups.

**Live:** flowscanner.onrender.com

---

## What It Does

Scans 230+ tickers for high-probability trading setups. Surfaces options contracts, scores institutional flow, and ranks setups by backtest-validated edge. The backtest engine runs against real historical data so you know which signals actually work and which are noise.

---

## Setup

```bash
pip install yfinance colorama tabulate gradio pandas numpy curl_cffi
```

Clone or pull the repo, then run from the `scanner/` directory.

---

## Scanner CLI

```bash
python3 scanner.py
```

Runs a full scan on the default universe, prints results to terminal.

**Flags:**

| Flag | What it does |
|------|-------------|
| `--tickers NVDA AMD TSLA` | Scan specific tickers only |
| `--filter gap` | Show only gap setups |
| `--filter inside` | Inside day setups |
| `--filter highvol` | High relative volume |
| `--filter breakout` | Breakout setups (BK↑ / BK↓) |
| `--filter laggard` | Sector laggards only |
| `--filter any` | Any active setup (default) |
| `--filter a_grade` | Grade A setups only |
| `--sort relvol` | Sort by relative volume |
| `--sort options` | Sort by options score |
| `--sort lag` | Sort by lag score |
| `--dynamic` | Add today's top movers to scan (sleeper discovery) |
| `--no-options` | Skip options chain enrichment (faster) |

---

## Scanner Web UI

```bash
python3 scanner_web.py
```

Opens at `http://localhost:7860`. Same scanner, browser interface.

**Controls:**
- **Tickers** — comma-separated list, or leave blank for full universe
- **Filter** — dropdown: all / gap / inside / highvol / breakout / laggard / any / a_grade
- **Sort** — setup quality, options score, rel vol, gap %, change %, lag
- **Load Contracts** — fetches options chains for top setups (slower, use selectively)
- **Dynamic Mode** — appends today's movers to the scan (finds stocks not in default universe)

Live at flowscanner.onrender.com — no local install needed.

---

## Reading the Output

### Setup Flags

| Badge | Meaning | Backtest Edge |
|-------|---------|---------------|
| `HV★` | Pure high volume, volatile stock (HV20 ≥ 35%) | **Best setup. Sharpe 2.48, WR 55%** |
| `HV+G↓` | High vol + gap down | Sharpe 3.1, WR 55% |
| `HV+ID` | High vol + inside day | Sharpe 2.7, WR 54% |
| `HV+G↑FADE` | High vol + gap up — **direction is DOWN (puts)** | Sharpe 1.76, fade wins 55% |
| `HV+BK` | High vol + breakout | Sharpe 0.89, moderate edge |
| `BK↑ / BK↓` | Breakout above/below prior day high/low | Sharpe 1.43 (volatile only) |
| `ID` | Inside day — compression coiling | Best in **calm** stocks (Sharpe 5.44) |
| `G+` | Gap up | Slight continuation, fade if HV present |
| `G-` | Gap down | Fill bias (bullish) |
| `HV` yellow | High volume, calm/normal stock | **No edge** — don't trade |
| `[calm]` | HV fired but stock is calm (HV20 < 20%) | Skip this one |
| `RS+3.5` | Outperforming SPY by 3.5% today | Strength confirmation |
| `RS-3.5` | Lagging SPY by 3.5% | Laggard catch-up candidate |
| `LAG+8%` | Sector laggard — sector up 8%, this stock hasn't moved | Catch-up play |
| `**` | At a major key level (strength ≥ 5) | High-confluence |
| `*` | Near a key level (strength ≥ 3) | |

### Grade (GRD column)

| Grade | What it means |
|-------|--------------|
| **A** | High setup quality + good options score + contract found |
| **B** | Solid setup, decent contract |
| **C** | Some signal but lower conviction |
| **D** | Weak — noise or no edge |

### Direction

- `up` → scanner recommends **calls**
- `down` → scanner recommends **puts**

Direction is derived from: sector bias → gap fill direction → price location in range → nearest key level → volume confirmation.

---

## HV20 Regime — Critical Context

The most important thing the backtest proved: **signals only have edge in volatile stocks.**

| Regime | HV20 | highvol signal | breakout signal |
|--------|------|---------------|-----------------|
| **Volatile** | ≥ 35% | WR 55%, Sharpe **2.48** | WR 60%, Sharpe 1.65 |
| Normal | 20–35% | WR 41%, Sharpe **-3.58** | WR 36%, Sharpe -2.72 |
| Calm | < 20% | WR 42%, Sharpe **-1.65** | WR 36%, Sharpe -3.2 |

If a ticker's HV20 is below 35%, skip the highvol and breakout signals entirely. The scanner will show `[calm]` as a warning.

**Exception:** Inside day in calm stocks is the reverse — that's where it works best (Sharpe 5.44). Calm + inside = coiling setup.

---

## Backtest Engine

Tests scanner signals against real historical OHLCV data. Black-Scholes options P&L simulation included.

```bash
python3 backtest.py
```

### Commands

```bash
# Basic runs
python3 backtest.py                         # 60-day backtest, top 80 tickers
python3 backtest.py --days 90               # 90-day window
python3 backtest.py --all                   # full universe (~230 tickers, slow)
python3 backtest.py --tickers NVDA AMD TSLA # specific tickers

# Signal filters
python3 backtest.py --signal highvol        # one signal type only
python3 backtest.py --signal breakout
python3 backtest.py --signal gap_down
python3 backtest.py --signal inside

# Analysis modes
python3 backtest.py --compare               # all signals side by side
python3 backtest.py --combos                # signal combo breakdown (HV+GD, HV+ID, etc.)
python3 backtest.py --regime                # per-signal breakdown by HV20 regime
python3 backtest.py --ticker-rank           # rank tickers by signal performance
python3 backtest.py --multi-hold            # compare intraday vs 1D vs 2D vs 3D hold
python3 backtest.py --laggard               # sector laggard backtest only

# Output
python3 backtest.py --report                # reprint last saved run
python3 backtest.py --no-save               # run without saving to disk

# Tune parameters
python3 backtest.py --hold 2                # hold 2 days instead of 1
python3 backtest.py --delta 0.35            # target delta for contracts
python3 backtest.py --stop 0.40             # 40% stop loss on contracts
python3 backtest.py --target 1.50           # 150% take profit
```

### Metrics Explained

| Metric | What it means | What's good |
|--------|--------------|-------------|
| **Win%** | % of trades that moved in the right direction | > 52% |
| **PF** (Profit Factor) | Gross wins ÷ gross losses | > 1.2 |
| **Sharpe** | Risk-adjusted return, annualized | > 1.0 |
| **EV%** | Expected value per trade (avg signed return) | Positive |
| **Opt Win%** | % of options contracts that hit take profit | > 20% |
| **$/contract** | Avg simulated P&L per contract | Positive |

### Backtest Limitations

- Uses **end-of-day prices** (close → next close). Real 0DTE entries happen intraday.
- IV estimated from HV20 × 1.2 vol-risk-premium proxy — not real market IV.
- No real bid/ask spread history — simulates 5% spread.
- Past signals don't guarantee future results.

The stock direction signals (win rate, Sharpe) are more reliable than the options P&L numbers. The options sim is a floor estimate — intraday timing is what actually determines your P&L on 0DTE contracts.

---

## Key Backtest Findings (60-day, 80 tickers, 2,800+ signals)

### Signal Leaderboard

| Signal | Win% | Sharpe | Verdict |
|--------|------|--------|---------|
| **HV + GAP_DOWN** (volatile) | 55% | 3.10 | Trade it |
| **HV + INSIDE** (volatile) | 54% | 2.70 | Trade it |
| **HV_PURE** (volatile) | 55%+ | 2.48 | Trade it |
| HV + GAP_UP (fade/puts) | 55% | 1.76 | Fade only |
| Breakout (volatile) | 60% | 1.65 | Trade it |
| Inside (calm) | 50% | 5.44 | Trade it |
| Inside (volatile) | 54% | 0.58 | Weak |
| Gap_down only | 50% | 0.15 | Marginal |
| **Gap_up only** | 49% | -0.32 | **Skip / fade only** |
| **Trend** | 51% | -0.64 | **Skip** |
| HV (normal regime) | 41% | -3.58 | **Never** |

### Best Tickers for HV + Breakout Signals (60 days)

Top performers by Sharpe when highvol or breakout fired:

`BULL (Sharpe 12)` · `SNAP (10)` · `FNGU (12)` · `ARM (9)` · `SOXS (8)` · `PANW (8)` · `PLTR (7)` · `HOOD (6)` · `AMC (9)` · `INTC (15)`

### Hold Period (volatile regime only)

| Hold | highvol WR | highvol Sharpe | breakout WR | breakout Sharpe |
|------|-----------|----------------|-------------|-----------------|
| Intraday (open→close) | 56% | 1.98 | 56% | 1.30 |
| **Overnight (close→next)** | **55%** | **2.48** | **60%** | **1.65** |
| 2D | 49% | 1.85 | 53% | 0.63 |
| 3D | 49% | -0.20 | 53% | 0.32 |

**Hold overnight, not 3 days.** Edge decays fast after day 1.

---

## Files

```
scanner/
├── scanner.py          # Core scanner engine + CLI
├── scanner_web.py      # Gradio web UI
├── backtest.py         # Backtesting engine
├── backtest_viz.html   # Visual quant dashboard (open in browser)
├── backtest_results/   # Saved JSON + CSV from past runs
└── README.md           # This file
```

---

## Deployment

Scanner web UI is deployed on Render via GitHub auto-deploy.

Push to `master` → Render picks it up → live at flowscanner.onrender.com in ~2 min.

```bash
git add . && git commit -m "update" && git push origin master
```

---

*Built by Donny — vjrod.com*
